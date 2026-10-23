# -*- coding: utf-8 -*-
"""core/parse.py — 发票文本提取与字段解析（PDF + OFD）。

提文本只用 pdfplumber，其余第三方库（reportlab/PIL/pypdf）由调用方引入；
本模块只做纯逻辑，方便单测和 CLI 复用。

extract_pdf_texts 带 (路径, mtime) 内存缓存，正则都预编译到模块级常量。
"""
import os
import re
import hashlib
import threading
import xml.etree.ElementTree as ET
import zipfile

import pdfplumber

from .config import _log_runtime

# 预编译正则（模块级，避免重复编译）
# 发票号码：普通/电子发票常为 8 位，专票/电子发票号可达 20 位；“发票号”亦可
_RE_INVOICE_NO = re.compile(r'发票号码?\s*[:：]?\s*([0-9A-Za-z]{8,20})')
# 宽松：允许「发票号码：」与号码之间夹印章/换行等噪声（如成品油发票被“成品油 全 章”隔断）
_RE_INVOICE_NO_LOOSE = re.compile(r'发票号码?\s*[:：]?[^\dA-Za-z]{0,40}?([0-9A-Za-z]{8,20})')
# OFD 无标签兜底：20 位纯数字发票号（税号为 18 位带字母，不冲突）
_RE_INVOICE_NO_OFD = re.compile(r'(?<!\d)(\d{20})(?!\d)')
_RE_INVOICE_DATE = re.compile(r'开票日期[:：]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
# 宽松：允许「开票日期：」与日期之间夹印章/换行
_RE_INVOICE_DATE_LOOSE = re.compile(r'开票日期\s*[:：]?[^\d]{0,40}?(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
# OFD 无标签兜底：裸日期
_RE_INVOICE_DATE_BARE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
# 税号锚点：兼容「统一社会信用代码/纳税人识别号」「统一社会信用代码」「纳税人识别号」三种写法
_RE_TAX_ID = re.compile(
    r'(?:统一社会信用代码/纳税人识别号|统一社会信用代码|纳税人识别号)\s*[:：]?\s*([\dA-Z]{17,20})')
# 公司名回退（仅在标签提取失败时使用）
_RE_COMPANY = re.compile(
    r'[一-鿿（）()·]{2,20}(?:有限(?:责任)?公司|股份有限公司|有限公司|酒店|宾馆|旅馆|饭店|商行|加油站|经营部|服务部)'
)
# 合计行：金额(税前) + 税额
_RE_TOTAL_LINE = re.compile(r'合\s*计\s*[¥￥]?\s*([\d,.]+\.?\d*)\s*[¥￥]?\s*([\d,.]+\.?\d*)')
_RE_TOTAL_LINE2 = re.compile(r'合\s*计.*?金额.*?[¥￥]\s*([\d,.]+).*?税额.*?[¥￥]\s*([\d,.]+)')
# 价税合计（小写）
_RE_TOTAL_XJ = re.compile(r'[（(]\s*小写\s*[）)]\s*[¥￥]?\s*([\d,]+\.\d{2})')
#: 价税合计（小写）的兜底正则（当「（小写）」锚点失配时用）。
#: ⚠️ 2026-10-11 修：原先写 `价税合计.*?[¥￥]`，`.` 默认不跨行、但在 OFD 线性化文本里
#:    整段没有换行 ⇒ 正则会**跨过整张明细表**去抓数。实测「曹操出行」的 OFD 文字层里
#:    「（小写）」后面的金额整段缺失，正则于是越过「备注 / 开票人 / 行程明细表」，
#:    抓到表里的**不含税合计 ¥79.55**（同名 PDF 的价税合计是 ¥81.94）—— 静默少算。
#:    现在中途一旦出现**独立的**「合计 / 小计」（即表格小计行，`价税合计`自身已越过）
#:    就判定不是价税合计小写、放弃匹配，交由后面「大写金额 → 末位 ¥」的兜底链处理。
#: `(?:...)` 必须是非捕获组，否则 `group(1)` 会从金额错位到中间那段文本。
_RE_TOTAL_XJ2 = re.compile(
    r'价税合计(?:(?!合\s*计|小\s*计)[\s\S]){0,120}?[¥￥]\s*([\d,]+\.\d{2})')
# 备注区间（截止到首个结构化字段或文末；允许为空，修复过度捕获）
#
# 终止锚点里的 页码 / 行程人 / 申请时间 / 申请日期 / 行程起止日期 是**行程单专用**词：
# 滴滴版行程单的表头是「…金额[元] 备注」，其后紧跟整张行程表直到文末，
# 原正则的终止词（开票人/复核人/…）在行程单里根本不存在，于是整张表被灌进 remarks。
# 加这些词后，行程单的 remarks 在「页码：1/1」处就截断，不会再吞掉表格。
# ⛔ 别再加「序号」这种通用词：实测会让 2 张购物票（京东订单清单，备注区后面跟着
#    货物明细表）的 remarks 被截断，属于**连坐**—— 该表本来就该被截断，但那是另一个问题，
#    不该混在行程单这一改里顺手改掉。
# ⚠️ 这是全局正则：改它会影响所有票的 remarks，而住宿票的入住/离店日期正是从 remarks 抽的
#    （core/facets.py 的 _extract_stay_dates），is_hotel 判定也依赖 '住宿' in remarks。
#    所以每次改动都必须复跑全库对比：住宿票的 remarks / checkin / checkout 必须零变化。
# 备注区里的「空占位符」：整串只有符号（'-'、'—'、'*'、'·'…），没有任何汉字/数字/字母
_RE_BLANK_TAIL = re.compile(r'^[\s\-—－*·.。、,，;；:：/|]+$')
_RE_REMARKS = re.compile(
    r'备\s*注\s*[:：]?\s*(.*?)(?=\s*(?:开票人|复核人|收款人|销售方地址|购方地址'
    r'|页码|行程人|申请时间|申请日期|行程起止日期)|$)', re.S)
# 结构化字段（名称后用边界限定，防止吞掉后续标签文字）
_NAME_BOUND = r'(?=\s*(?:购买方|销售方|信息|名称|开票人|复核人|收款人|注|;|；|$))'
_RE_DRAWER = re.compile(r'开票人[:：]\s*([一-鿿·]{2,4})' + _NAME_BOUND)
_RE_CHECKER = re.compile(r'复核人[:：]\s*([一-鿿·]{2,4})' + _NAME_BOUND)
_RE_PAYEE = re.compile(r'收款人[:：]\s*([一-鿿·]{2,4})' + _NAME_BOUND)
_RE_SELLER_ADDR = re.compile(r'销售方地址[:：]\s*([^;；]+)')
_RE_TAX_RATE = re.compile(r'(\d{1,2}(?:\.\d+)?)\s*%')
# 项目名称 *分类*明细
_RE_ITEM = re.compile(r'\*([^*]+)\*([^*\s]{1,15})')


# 密码区字符集：老版增值税电子普通发票的防伪码由这些 ASCII 符号+数字组成。
# 真实的项目名（税收分类名/明细名）是纯中文，不会夹带它们，
# 所以「只要出现其中任一字符就是密码区碎片」，可直接排除。
_RE_STAR_NOISE = re.compile(r'[0-9A-Za-z/<>+\-=%￥¥$#@.,;:!?*"\'\\|\[\]{}]')


# 明细表表头碎片：星号与表格线交错时，xxx 会变成「税率 税额」「货物或应税劳务、服务名称」
# 这类表头而不是项目名，必须一并排除。
_RE_STAR_HEADER = re.compile(
    r'(税率|税额|货物或应税劳务|服务名称|规格型号|单位|数量|单价|金额|项目名称|校验|密码)')


def _is_clean_cjk(s):
    """是否「纯中文项目名」：≥2 个 CJK 字符，且不含任何密码区符号/数字/字母。"""
    return len(re.findall(_CJK, s)) >= 2 and not _RE_STAR_NOISE.search(s)


def find_star_item(text):
    """在 `*分类*明细` 结构里找真实项目名，返回 (xxx, yyy)；找不到返回 None。

    ⚠️ 为什么不能用 re.search 直接取第一个匹配：老版增值税电子普通发票的 PDF 文本层
    带「密码区」防伪码（形如 `3/8<>>8768894-96*>-0518>823`），里面也有 `*`，且位置在
    真实项目名之前。re.search 会抢先命中密码区，把项目名抽成 `5`、`278/825+`、`7</`
    这类乱码（实测 26 张：购物类 22 张 + 滴滴老票 3 张 + 中石化 1 张）。

    密码区在 PDF 里跨行、且与明细表表头交错，因此这里做**两轮筛选**：

    第一轮（正常形态 `*分类*明细`）：xxx 必须是纯中文且不是表头碎片 ——
      · 真实分类名（运输服务 / 计算机配套产品 / 金属制品）是纯中文，自然通过；
      · 密码区残片（`9/+19<74货物或应税劳务…`）必含数字或符号，被滤掉；
      · 表头碎片（`税率税额` / `货物或应税劳务、服务名称`）被 _RE_STAR_HEADER 滤掉。
      yyy 只要求 ≥2 个 CJK，**允许**夹带拉丁字母与数字：明细名本来就是商品名
      （「联想（Lenovo）KN210」「得力（deli）发黑防锈高硬度」），对 yyy 要求纯中文
      会把「计算机配套产品」「金属制品」这类本来正确的票判成空（实测回归）。

    第二轮（星号错位形态）：密码区的「闭合星号」往往正好是真实分类名的「起始星号」
    ——例如 `…813*税率税额*计算机外部设备*金士顿…`，第一对星号吃掉后，
    真实分类名 `计算机外部设备` 就掉到了 yyy 上，本轮把它捡回来。

    ⛔ 不要靠放宽 `{1,15}`（如改成 `{0,15}`）来解决：实测会让 2 张住宿票被错改成
    「生产生活服务」——原文先出现一对无明细的星号，放宽后 re.search 会停在更早的那一对。

    宁可返回 None（后续走销售方兜底），也不要返回乱码。
    三个消费点（详情弹窗 item_name / 表格标签 / 文件名）共用此函数，保证行为一致。
    """
    cands = []
    for m in _RE_ITEM.finditer(text or ''):
        cands.append((re.sub(r'\s+', '', m.group(1).strip()),
                      re.sub(r'\s+', '', m.group(2).strip())))

    # 第一轮：xxx 是纯中文且非表头，yyy 至少 2 个汉字
    for xxx, yyy in cands:
        if (_is_clean_cjk(xxx) and not _RE_STAR_HEADER.search(xxx)
                and len(re.findall(_CJK, yyy)) >= 2):
            return xxx, yyy

    # 第二轮：星号错位，真实分类名落在 yyy 上；xxx 则是密码区残片 / 表头
    for xxx, yyy in cands:
        if (_is_clean_cjk(yyy) and len(re.findall(_CJK, xxx)) >= 2
                and (_RE_STAR_NOISE.search(xxx) or _RE_STAR_HEADER.search(xxx))):
            return yyy, yyy
    return None

#  网约车行程单（高德 / 滴滴）
# 行程单**不是发票**：它是打车平台开的「报销凭证」，没有发票号码、没有税收分类、
# 没有购销方。以前它被当成增值税发票走主流程，导致三处错误：
#   ① 标签退化成销售方兜底 → 抽到 POI / 酒店名（「云端智能酒店」「号服务商车型上车」）
#   ② 金额用发票正则硬抠 → 张冠李戴（10.00 的行程单抽出 19.4）
#   ③ 金额被计入发票合计 → 全库虚增约 3%
# 所以这里给它一条独立解析路径，并在 extract_invoice_fields / rename / facets
# 三处入口把它挡在发票流程之外。
#
# ⚠️ 判定与解析放在**本模块（下层）**而不是 rename.py：extract_invoice_fields
#    也要用它做「行程单不抽备注」的闸门，而 rename.py 顶部 import 了 parse.py，
#    让 parse 反过来 import rename 会循环导入。
_RE_TRIP_TITLE = re.compile(r'行程单')
_RE_TRIP_AMAP = re.compile(r'AMAP\s*ITINERARY')
_RE_TRIP_DIDI = re.compile(r'DIDI\s*TRAVEL\s*-\s*TRIP\s*TABLE')
_RE_TRIP_TOTAL_AMAP = re.compile(r'共计\s*\d+\s*单行程[，,]\s*合计\s*([\d.]+)\s*元')
_RE_TRIP_TOTAL_DIDI = re.compile(r'共\s*\d+\s*笔行程[，,]\s*合计\s*([\d.]+)\s*元')
_RE_TRIP_TOTAL_ANY = re.compile(r'合\s*计\s*([\d.]+)\s*元')
_RE_TRIP_DATE_AMAP = re.compile(r'行程时间[:：]\s*(\d{4})-(\d{1,2})-(\d{1,2})')
_RE_TRIP_DATE_DIDI = re.compile(r'行程起止日期[:：]\s*(\d{4})-(\d{1,2})-(\d{1,2})')
# 票面「申请时间 / 申请日期」：高德写作「申请时间:」、滴滴写作「申请日期:」，两种都要认。
# 它是网约车行程单上**唯一与宿主发票开票日对齐**的日期（行程起止日 D1/D2 常与开票日
# 相差几十上百天），所以是挂接时「日期确认键」真正应该比的那个日期。日期分隔符既可能是
# 横杠（-）也可能是斜杠（/），年份后也可能带「年」字，故统一写成 [-/年]/[-/月]。
_RE_TRIP_APPLY_DATE = re.compile(
    r'(?:申请时间|申请日期)[:：]\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})')
_RE_TRIP_INDEX = re.compile(r'^\d{1,3}$')
_RE_TRIP_DATE_CELL = re.compile(r'^\d{4}-\d{1,2}-\d{1,2}$')
_RE_TRIP_MONEY = re.compile(r'([\d.]+)\s*元')
# 行程表表头（用于按词坐标还原列）；'金额(元)' 是 AMAP 的另一种写法
_TRIP_COLS = ('序号', '服务商', '车型', '上车时间', '城市', '起点', '终点',
              '里程[公里]', '金额[元]', '金额(元)', '金额', '备注')
_TRIP_COL_ALIAS = {'金额(元)': '金额'}


def is_trip_sheet(text):
    """是否为网约车行程单（高德 / 滴滴）。纯文本判定，不需要 pdfplumber。

    判据：含「行程单」字样，**且**满足以下任一：
      · 出现 AMAP ITINERARY（高德版固定抬头）
      · 出现 DIDI TRAVEL - TRIP TABLE（滴滴版固定抬头）
      · 表头同时含 上车时间 + 起点 + 终点（兜底，覆盖抬头被 PDF 拆行破坏的情况）

    实测全库 53 份行程单（高德 48 + 滴滴 5）全部命中，且三条子判据重合度 100%
    —— 也就是说「抬头」和「表头」两条判据互为交叉验证，不是靠单一弱特征。
    """
    if not text or not _RE_TRIP_TITLE.search(text):
        return False
    up = text.upper()
    if _RE_TRIP_AMAP.search(up) or _RE_TRIP_DIDI.search(up):
        return True
    return ('上车时间' in text and '起点' in text and '终点' in text)


# 非电子发票（行程单 / 报销单据 / 报销辅助材料）
# 用户口径（2026-10-10）：这类文件「不是发票」，导入时就分到**非电子发票**，
# 不进金额合计、不进地图轨迹、不当发票用。
#
# 判据是**两条「与」关系**，缺一不可：
#   ① 命中行程 / 明细 / 清单 / 报销单类特征词
#   ② 取不到发票号码 —— 电子发票（含数电票）一定有发票号码，取不到才可能是附件
# 只靠 ① 会把真发票误伤（有些发票备注栏里也写「报销单」），只靠 ② 会把
# 号码抽失败的普通发票全判成附件（那样它们的金额会从合计里消失）。
#: 2026-10-12 补「运单明细」：顺丰快递电子发票的**报销凭证**版式（表头行 + 值行、
#: 行内不带冒号）正文第一行就是「顺丰电子发票 — 运单明细」，旧词表里没有这个词，
#: 于是被当成正经发票（kind='vat'、票种显示「发票」），而票号/日期/金额其实一个都抽不到。
#: 加词后走 ① + ② 两条「与」判据仍成立（该版式 extract_invoice_no 实测返回 ''）。
_RE_NONINVOICE_HINT = re.compile(
    r'行程单|行程明细|行程清单|行程表|报销单据|报销单|报销清单|报销凭证'
    r'|费用清单|辅助材料|运单明细')


def is_noninvoice(text):
    """是否为「非电子发票」（行程单 / 报销单据 / 报销辅助材料）。

    与 is_trip_sheet 的关系：is_trip_sheet 是**网约车行程单**的结构化强判据
    （抬头 or 表头），is_noninvoice 是**整类非发票材料**的判据，覆盖它以及
    行程明细、费用清单、报销凭证等。两者可以叠加：先按 is_noninvoice 把整类
    挡在发票流程之外，再看有没有行程明细可抽。

    ⚠️ 判定放在本模块（下层）而不是 rename.py：facets 要用它做「导入即分类」
    的闸门，而 rename.py 顶部 import 了 parse.py，反过来会循环导入。
    """
    if not text or not _RE_NONINVOICE_HINT.search(text):
        return False
    return not extract_invoice_no(text)


def trip_sheet_brief(text):
    """行程单摘要（纯文本，不读 PDF）：返回 (合计金额, 日期 'YYYY-MM-DD')。

    给 rename 生成文件名用 —— 那边只有文本、拿不到 pdfplumber 页面。

    日期字段**优先取票面「申请时间 / 申请日期」**，取不到才退回「行程起止日期 / 行程时间」
    的起始日（D1）。为什么这么改：本返回值经 facets（`'date': ts['start_date']`）一路流到
    routes/pool 的行 `date` 字段，而 attach 的日期确认键比的正是这个 `date`。滴滴行程单的
    「申请日期」恰好等于宿主发票的开票日，而行程起止日 D1 常与开票日差几十上百天 ——
    实测全库 53 份行程单里用 D1 时日期确认键 0 份命中，改用它才真正生效。
    """
    t = text or ''
    total = 0.0
    m = (_RE_TRIP_TOTAL_AMAP.search(t) or _RE_TRIP_TOTAL_DIDI.search(t)
         or _RE_TRIP_TOTAL_ANY.search(t))
    if m:
        try:
            total = float(m.group(1))
        except ValueError:
            total = 0.0
    d = ''
    # 优先「申请时间 / 申请日期」（= 宿主开票日），取不到才退回「行程起止日 / 行程时间」起始日
    m = (_RE_TRIP_APPLY_DATE.search(t)
         or _RE_TRIP_DATE_AMAP.search(t) or _RE_TRIP_DATE_DIDI.search(t))
    if m:
        d = '%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return round(total, 2), d


def _trip_cell(d, *names):
    for n in names:
        v = str(d.get(n, '') or '').strip()
        if v:
            return v
    return ''


def _trip_row_to_dict(raw, provider_default=''):
    """原始行 dict → 统一行程明细 dict（缺列返回空串，绝不编造）。"""
    # 金额格两种写法：高德「16.10元」带单位、滴滴「17.90」是裸数字，两种都要认。
    cell_amt = _trip_cell(raw, '金额[元]', '金额(元)', '金额')
    amt = 0.0
    m = _RE_TRIP_MONEY.search(cell_amt) or re.search(r'[\d.]+', cell_amt)
    if m:
        try:
            amt = float(m.group(0) if m.re is not _RE_TRIP_MONEY else m.group(1))
        except ValueError:
            amt = 0.0
    return {
        'provider': _trip_cell(raw, '服务商') or provider_default,
        'car_type': _trip_cell(raw, '车型'),
        'board_time': _trip_cell(raw, '上车时间'),
        'city': _trip_cell(raw, '城市'),
        'from_': _trip_cell(raw, '起点'),
        'to_': _trip_cell(raw, '终点'),
        'mileage': _trip_cell(raw, '里程[公里]', '里程'),
        'amount': round(amt, 2),
    }


def _trip_rows_from_tables(pdf):
    """从 pdfplumber 表格取行程行。

    滴滴版表格有竖线，extract_tables 切得很干净（9 列）。
    高德版**没有竖线**，整行会被塞进第 0 列 —— 这里用「除序号列外至少 2 个非空
    单元格」做判据识别这种情况并返回空，交给 _trip_rows_from_words 兜底。
    """
    rows = []
    for page in pdf.pages:
        try:
            tables = page.extract_tables()
        except Exception:
            continue
        for tb in tables or []:
            if not tb or len(tb) < 2:
                continue
            head = [str(c or '').replace('\n', '').strip() for c in tb[0]]
            if '序号' not in head or '上车时间' not in head:
                continue
            for r in tb[1:]:
                cells = [str(c or '').replace('\n', '').strip() for c in r]
                if len(cells) != len(head):
                    continue
                if not _RE_TRIP_INDEX.match(cells[0] or ''):
                    continue
                if sum(1 for c in cells[1:] if c) < 2:
                    continue
                rows.append(dict(zip(head, cells)))
    # ⚠️ 不能在第一页就 return：多笔行程单会跨页（实测某滴滴版 29 笔分 3 页），
    #    只取首页会丢掉三分之二的行程。
    return rows


def _trip_rows_from_words(pdf):
    """高德版兜底：表格无竖线，改用**词坐标**按列还原。

    原理（实测自真实样本）：
      · 表头「序号/服务商/车型/上车时间/城市/起点/终点/金额」各自有稳定 x0，
        列边界取相邻表头 x0 的中点即可把每个词归到正确的列；
      · 起点/终点常因 PDF 换行被拆成 2 行、浮在主行上下（实测偏移 ±4.4pt），
        而相邻行程行间距 ≥30pt，所以「就近吸附到上车时间列里的日期行」能可靠归行。

    ⛔ 不要改成按 extract_text() 的行序去猜起点/终点：多行程样本里上下车的文本块
       是交错的（前一行终点与后一行起点混在同一文本行），按行序必然张冠李戴。
    """
    for page in pdf.pages:
        try:
            words = page.extract_words()
        except Exception:
            continue
        if not words:
            continue
        groups = {}
        for w in words:
            groups.setdefault(round(float(w['top']), 1), []).append(w)
        hdr_top, hdr_words = None, []
        for top in sorted(groups):
            texts = {str(w.get('text', '') or '').strip() for w in groups[top]}
            if {'序号', '起点', '终点'} <= texts:
                hdr_words = sorted(
                    [w for w in groups[top]
                     if str(w.get('text', '') or '').strip() in _TRIP_COLS],
                    key=lambda w: float(w['x0']))
                if len(hdr_words) >= 6:
                    hdr_top = top
                break
        if hdr_top is None:
            continue
        names, xs = [], []
        for w in hdr_words:
            raw = str(w['text']).strip()
            names.append(_TRIP_COL_ALIAS.get(raw, raw))
            xs.append(float(w['x0']))
        bounds = []
        for i, n in enumerate(names):
            lo = -1e9 if i == 0 else (xs[i - 1] + xs[i]) / 2.0
            hi = 1e9 if i == len(xs) - 1 else (xs[i] + xs[i + 1]) / 2.0
            bounds.append((n, lo, hi))
        if '上车时间' not in names:
            continue
        ti = names.index('上车时间')
        # 行锚点：落在「上车时间」列里的 YYYY-MM-DD（每笔行程恰好一个）
        anchors = set()
        for w in words:
            t = str(w.get('text', '') or '').strip()
            if not _RE_TRIP_DATE_CELL.match(t):
                continue
            top, x = float(w['top']), float(w['x0'])
            if top > hdr_top and bounds[ti][1] <= x < bounds[ti][2]:
                anchors.add(round(top, 1))
        anchors = sorted(anchors)
        if not anchors:
            continue
        tol = 16.0
        if len(anchors) >= 2:
            gap = min(b - a for a, b in zip(anchors, anchors[1:]))
            tol = min(16.0, max(5.0, gap / 2.0))
        rows = [dict((n, '') for n in names) for _ in anchors]
        for w in words:
            top, x = float(w['top']), float(w['x0'])
            if top <= hdr_top + 0.5:
                continue
            t = str(w.get('text', '') or '').strip()
            if not t or t in _TRIP_COLS:
                continue
            idx = None
            for i, (_n, lo, hi) in enumerate(bounds):
                if lo <= x < hi:
                    idx = i
                    break
            if idx is None:
                continue
            best = None
            for j, a in enumerate(anchors):
                dist = abs(top - a)
                if dist <= tol and (best is None or dist < best[1]):
                    best = (j, dist)
            if best is None:
                continue
            key = names[idx]
            row = rows[best[0]]
            # 上车时间拆成「日期」「时刻」两个词，要空格；其余（城市/起点/终点…
            # 换行的中文）直接拼接，否则「沧州」+「市」会变成「沧州 市」
            sep = ' ' if key == '上车时间' else ''
            row[key] = (row[key] + sep + t).strip() if row[key] else t
        return [r for r in rows if _RE_TRIP_INDEX.match(str(r.get('序号', '') or '').strip())]
    return []


_trip_cache = {}
_trip_cache_lock = threading.Lock()
_TRIP_CACHE_MAX = 128


def parse_trip_sheet(filepath):
    """解析网约车行程单 → 行程明细。

    返回 dict：
      channel     'amap' | 'didi' | ''
      provider    服务商（高德版取「服务商」列；滴滴版表头无此列，用平台名兜底）
      city        城市（多笔行程取第一笔）
      trips       [ {provider, car_type, board_time, city, from_, to_,
                     mileage, amount}, ... ]（滴滴版有里程，高德版没有）
      trip_amount 本单合计金额（优先取票面「合计」，取不到才用明细求和）
      rows_total  明细金额求和（与 trip_amount 对不上时用于对账）
      sheet_total 票面「合计」金额
      trip_count  笔数
      start_date  票面日期 'YYYY-MM-DD'：优先「申请时间 / 申请日期」（= 宿主发票开票日，
                  挂接日期确认键比的正是它），取不到才退回「行程起止日 / 行程时间」起始日

    ⚠️ 这里**不返回 amount**：行程单不是发票，金额绝不进发票合计（见 facets）。
    ⚠️ 不提取行程人姓名/手机号：那是用户的个人信息，没有展示需求就不落地。
    """
    out = {'channel': '', 'provider': '', 'city': '', 'trips': [], 'trip_amount': 0.0,
           'rows_total': 0.0, 'sheet_total': 0.0, 'trip_count': 0, 'start_date': ''}
    try:
        key = _cache_key(filepath)
    except Exception:
        key = None
    if key is not None:
        with _trip_cache_lock:
            if key in _trip_cache:
                return _trip_cache[key]
    try:
        text, _err = extract_pdf_texts(filepath)
    except Exception:
        text = ''
    if not text or not is_trip_sheet(text):
        return out

    total, start = trip_sheet_brief(text)
    out['sheet_total'], out['start_date'] = total, start
    up = text.upper()
    if 'AMAP' in up:
        out['channel'] = 'amap'
    elif 'DIDI' in up:
        out['channel'] = 'didi'
    prov_default = '滴滴出行' if out['channel'] == 'didi' else ''

    rows = []
    try:
        with pdfplumber.open(filepath) as pdf:
            rows = _trip_rows_from_tables(pdf)
            if not rows:
                rows = _trip_rows_from_words(pdf)
    except Exception:
        rows = []

    trips = [_trip_row_to_dict(r, prov_default) for r in rows]
    trips = [t for t in trips if t['amount'] > 0 or t['from_'] or t['to_']]
    out['trips'] = trips
    out['trip_count'] = len(trips)
    out['rows_total'] = round(sum(t['amount'] for t in trips), 2)
    out['trip_amount'] = total if total > 0 else out['rows_total']
    out['city'] = next((t['city'] for t in trips if t['city']), '')
    out['provider'] = next((t['provider'] for t in trips if t['provider']), '')

    if key is not None:
        with _trip_cache_lock:
            if len(_trip_cache) >= _TRIP_CACHE_MAX:
                _trip_cache.clear()
            _trip_cache[key] = out
    return out


# 星号结构 `*分类*明细` 中「明细名比分类名更具体」的判定词清单（唯一真源）。
# xxx 是税收分类名（如"运输服务"），yyy 是明细名（如"网约车服务费"）；
# 若 yyy 含清单内关键词而 xxx 不含，则取 yyy，否则取 xxx。
#
# ⚠️ 历史上本清单在项目里有三份逐字重复的字面量：parse.py（详情弹窗 item_name）、
# rename.py 两处（表格标签 extract_item_name_from_text / 文件名
# generate_invoice_filename）。三份不同步导致「表格显示打车费、详情弹窗显示运输服务」。
# 现在合并为这一份：
#   - 本模块是**下层**（rename.py 顶部有 `from .parse import ...`，即 rename 依赖 parse），
#     所以真源必须放在这里；反过来让 parse 去 import rename 会形成循环导入。
#   - rename.py 通过 `from .parse import STRONG_KW` 消费，不再自带副本。
#   - 清单内容 = 三份历史副本的并集。机票/汽油/配件 是 parse.py 那份原本缺失的，已补上。
STRONG_KW = ['住宿', '快递', '餐饮', '维修', '租赁', '保洁', '机票', '汽油', '配件',
             '网约车', '出租车', '打车', '滴滴', '代驾', '顺风车', '客运']
# 脱敏证件号 + 紧邻姓名（火车票乘客）
_RE_ID_NAME = re.compile(r'([\d*]{14,20})\s*([一-鿿·]{2,4})')
# 通用金额
_RE_MONEY = re.compile(r'(?:[¥￥]|CNY)\s*(\d+\.?\d*)')
# 购销方：统一锚定（购/购买方/销售方/销 前缀 + 名称 + 统一社会信用代码）。
# 一份发票常出现「污染排版」与「干净排版」两份，取每方最短（最干净）结果即可免疫标签交错。
_RE_PARTY_ANY = re.compile(
    r'(购买方信息?|购|销售方信息?|销)\s*名称[:：]?\s*([一-鿿（）()·0-9A-Za-z]{2,40}?)\s*[:：]?\s*(?:统一社会信用代码|纳税人识别号)')
# 仅 名称…统一社会信用代码（无前缀时也兜底）
_RE_PARTY = re.compile(r'名称[:：]?\s*([一-鿿（）()·0-9A-Za-z]{2,40}?)\s*[:：]?\s*(?:统一社会信用代码|纳税人识别号)')
# OFD 扁平结构：「公司名 18位税号 公司名 18位税号」配对
_RE_PARTY_OFD = re.compile(r'([一-鿿（）()·]{4,30})\s+([0-9A-Z]{18})')

_CJK = r'[一-鿿]'

# 文本提取缓存：键=(abspath, mtime)，值=(text, err)
_text_cache = {}
_text_cache_lock = threading.Lock()
_TEXT_CACHE_MAX = 256


def _cache_key(path):
    try:
        return (os.path.abspath(path), os.path.getmtime(path))
    except OSError:
        return (os.path.abspath(path), -1)


def _cache_get(path):
    k = _cache_key(path)
    with _text_cache_lock:
        return _text_cache.get(k)


def _cache_put(path, value):
    k = _cache_key(path)
    with _text_cache_lock:
        if len(_text_cache) >= _TEXT_CACHE_MAX:
            _text_cache.clear()
        _text_cache[k] = value


def amount_upper(amt):
    """金额转中文大写。"""
    if amt == 0:
        return "零元整"
    amt = round(amt, 2)
    integer = int(amt)
    jiao = int(round((amt - integer) * 10))
    fen = int(round(round(amt - integer, 2) * 100)) - jiao * 10
    d = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
    u = ['', '拾', '佰', '仟']
    bu = ['', '万', '亿']
    s = str(integer)
    groups = [s[max(0, i - 4):i] for i in range(len(s), 0, -4)][::-1]
    parts = []
    for gi, g in enumerate(groups):
        seg, has_z = '', False
        for i, ch in enumerate(g):
            n = int(ch)
            if n == 0:
                has_z = True
            else:
                if has_z:
                    seg += '零'
                    has_z = False
                seg += d[n] + u[len(g) - 1 - i]
        if seg:
            parts.append(seg + bu[len(groups) - 1 - gi])
    result = ''.join(parts) if parts else '零'
    if jiao == 0 and fen == 0:
        result += '元整'
    else:
        result += '元'
        if jiao > 0:
            result += d[jiao] + '角'
        elif integer > 0:
            result += '零'
        if fen > 0:
            result += d[fen] + '分'
    return result


def file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for c in iter(lambda: f.read(8192), b''):
            h.update(c)
    return h.hexdigest()


def _normalize_company_name(value):
    """用于核对的名称规范化：保留业务字符，消除 PDF 排版造成的差异。"""
    return re.sub(r'[\s　()（）,，.。]', '', (value or '')).upper()


def _normalize_tax_id(value):
    return re.sub(r'\s+', '', (value or '')).upper()


def validate_output_folder(folder):
    """在生成文件前给出可读的输出路径错误，而不是让底层库报错。"""
    folder = (folder or '').strip()
    if not folder:
        return None, "请先选择输出路径"
    if not os.path.isdir(folder):
        return None, "输出路径不存在或不是文件夹"
    if not os.access(folder, os.W_OK):
        return None, "输出路径没有写入权限"
    return folder, None


def parse_date(s):
    """将 'YYYY年MM月DD日' 或 'YYYY.MM.DD' 解析为可排序元组。"""
    if not s:
        return (9999, 12, 31, 23, 59)
    m = re.search(r'(\d{4})[年.\-](\d{1,2})[月.\-](\d{1,2})日?\s*(\d{1,2})?:?(\d{2})?', s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0))
    return (9999, 12, 31, 23, 59)


def _unique_path(filepath):
    """如果文件已存在，添加 (1), (2) 等后缀防止覆盖。"""
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filepath)
    n = 1
    while os.path.exists(f"{base}({n}){ext}"):
        n += 1
    return f"{base}({n}){ext}"


# 老式增值税电子普通发票的「监制章带」兜底
# 2020–2024 年的老式增值税电子普通发票（京东购物票、中石化加油票…）把
# 发票代码 / 发票号码 / 开票日期 印在票面下方**监制章带**里，而
# 「发票代码:」「发票号码:」「开票日期: 年 月 日」三个标签后面**全是空的**
# —— PDF 文字层线性化把标签与值拆到了不同位置，按标签抓取必然抓空。
# 文字层实测形如（`_normalize_cjk` 之后）：
#     … 量单价金额 / 000000000000 / 湖北增值税电子普通发票统一发票监 31869827 /
#       国制全国家税务总局章 2020 09 01 / 机器编号: 000000000000 …
# 即：发票代码 = 12 位独立数字；发票号码 = 「统一发票监」后的 8 位；
#     开票日期 = 「国家税务总局…章」后的 YYYY MM DD。
# ⛔ 只在标签三级链（严格 → 宽松 → OFD 裸数字/裸日期）全部失败后才启用，
#    绝不覆盖已经抓到的结果；且要求「增值税电子普通发票」与「统一发票监」
#    两个锚点相邻出现 —— 保证不误伤任何其它版式。
_RE_GUIDE_ANCHOR = re.compile(
    r'增值税电子普通发票\s*统一发票监|统一发票监\s*[^\n]{0,20}?增值税电子普通发票')
_RE_GUIDE_CODE = re.compile(r'(?<!\d)(\d{12})(?!\d)')
_RE_GUIDE_NO = re.compile(r'统一发票监\s*(\d{8})(?!\d)')
_RE_GUIDE_DATE = re.compile(r'国家税务总局[^\n]{0,8}?章\s*(\d{4})\s+(\d{1,2})\s+(\d{1,2})')


def extract_old_guide_fields(text):
    """老式增值税电子发票「监制章带」兜底：返回 {invoice_code, invoice_no, invoice_date}。

    只在正文同时含「增值税电子普通发票」+「统一发票监」两个锚点时启用，
    否则返回空值字典（调用方据此保持原结果不变）。纯只读，不写盘。
    """
    empty = {'invoice_code': '', 'invoice_no': '', 'invoice_date': ''}
    if not text:
        return empty
    for t in (text, _normalize_cjk(text)):
        if not _RE_GUIDE_ANCHOR.search(t):
            continue
        out = dict(empty)
        m = _RE_GUIDE_CODE.search(t)
        if m:
            out['invoice_code'] = m.group(1)
        m = _RE_GUIDE_NO.search(t)
        if m:
            out['invoice_no'] = m.group(1)
        m = _RE_GUIDE_DATE.search(t)
        if m:
            out['invoice_date'] = (f"{int(m.group(1))}年{int(m.group(2)):02d}月"
                                   f"{int(m.group(3)):02d}日")
        return out
    return empty


def extract_invoice_no(text):
    """发票号码（多层兜底）：标签严格 → 标签宽松(跳印章) → OFD 20位裸数字 → 监制章带。"""
    m = _RE_INVOICE_NO.search(text)
    if m:
        return m.group(1)
    m = _RE_INVOICE_NO_LOOSE.search(text)
    if m:
        return m.group(1)
    m = _RE_INVOICE_NO_OFD.search(text)
    if m:
        return m.group(1)
    # ⛔ 最后一级：老式票的监制章带（只在上面全抓空时才启用）
    return extract_old_guide_fields(text)['invoice_no']


def extract_invoice_date(text):
    """开票日期（多层兜底）：标签严格 → 标签宽松(跳印章) → OFD 裸日期 → 监制章带。"""
    m = _RE_INVOICE_DATE.search(text)
    if m:
        return f"{int(m.group(1))}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    m = _RE_INVOICE_DATE_LOOSE.search(text)
    if m:
        return f"{int(m.group(1))}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    m = _RE_INVOICE_DATE_BARE.search(text)
    if m:
        return f"{int(m.group(1))}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"
    # ⛔ 最后一级：老式票的监制章带（只在上面全抓空时才启用）
    return extract_old_guide_fields(text)['invoice_date']


def _extract_parties(text, *, conservative=False):
    """返回 (buyer, seller, buyer_tax, seller_tax)，多层策略确保稳健。"""
    buyer = seller = buyer_tax = seller_tax = ''
    # 1) 统一锚定：区分 购/购买方(买方) 与 销/销售方(卖方)，取每方最短（最干净）结果
    parts = _RE_PARTY_ANY.findall(text)
    buyers = [n for p, n in parts if p.startswith('购')]
    sellers = [n for p, n in parts if p.startswith('销')]
    if buyers:
        clean_b = [b for b in buyers if _looks_like_company(b)]
        if clean_b:
            buyer = _clean_name(min(clean_b, key=len))
    if sellers:
        clean_s = [s for s in sellers if _looks_like_company(s)]
        if clean_s:
            seller = _clean_name(min(clean_s, key=len))
    # OFD 的平铺文字经常打乱左右两栏；没有「购买方/销售方」标签时，
    # 公司名的先后顺序不具备归属含义，不能据此编造购买方。
    if conservative:
        ids = list(dict.fromkeys(_RE_TAX_ID.findall(text)))
        if buyer and seller and len(ids) >= 2:
            return buyer, seller, ids[0], ids[1]
        return buyer, seller, '', ids[-1] if seller and ids else ''
    # 2) 兜底：仅 名称…统一社会信用代码（取首=购，末=销）；过滤标签残渣/污染
    if not buyer or not seller:
        names = [_clean_name(n) for n in _RE_PARTY.findall(text)
                 if _looks_like_company(_clean_name(n))]
        if not buyer and names:
            buyer = names[0]
        if not seller and names:
            seller = names[-1]
    # 3) OFD 扁平结构：公司名 税号 公司名 税号（同样过滤标签残渣）
    if not buyer or not seller:
        clean_pairs = [(p[0], p[1]) for p in _RE_PARTY_OFD.findall(text)
                       if _looks_like_company(_clean_name(p[0]))]
        if clean_pairs:
            if not buyer:
                buyer, buyer_tax = _clean_name(clean_pairs[0][0]), clean_pairs[0][1]
            if not seller and len(clean_pairs) >= 2:
                seller, seller_tax = _clean_name(clean_pairs[-1][0]), clean_pairs[-1][1]
    # 4) 公司名正则兜底
    if not buyer or not seller:
        companies = [c for c in re.findall(_RE_COMPANY, text) if len(c) >= 6]
        if not buyer and companies:
            buyer = _clean_name(companies[0])
        if not seller and len(companies) >= 2:
            seller = _clean_name(companies[-1])
    # 5) 航空行程单/退票单（OFD 或特殊 PDF）：购买方=乘客（个人），销售方=航空公司。
    #    仅当文本含航司特征时介入；若标准逻辑已给出正确销售方（如代订机票 VAT 的航司卖家）
    #    且 buyer 不是被误占的航司名，则不破坏原有结果，避免误伤正常增值税发票。
    if re.search(r'航空|客票|航班', text):
        air = [c for c in re.findall(_RE_COMPANY, text) if '航空' in c]
        if not air:
            mm = re.search(r'([一-鿿（）()]{2,10}航空[一-鿿（）()·]{0,6})', text)
            if mm:
                air = [mm.group(1)]
        if air:
            airline = _clean_name(air[0])
            m = re.search(r'\d{20}\D*?([一-鿿]{2,4}(?<![变退航旅乘客]))\s*(?:变更|退票|航班|旅客|乘客)', text)
            passenger = _clean_name(m.group(1)) if m else ''
            if not seller:
                seller = airline
            if buyer and '航空' in buyer and passenger:
                buyer = passenger          # buyer 被误设为航司名 → 纠正为乘客
            elif not buyer and passenger:
                buyer = passenger
    # 税号归属
    # 红线：**购买方税号绝不许被填成销售方税号**。
    # 票面只印一个税号、或同一税号重复出现时，那个税号是**销售方**的
    # （销售方税号是票面主体，购买方栏常常整个留空，尤其个人抬头票）。
    # 旧逻辑直接 `tax_ids[0] → buyer_tax`，于是销售方税号被当成购买方税号
    # ——只读审查实测 55 份命中（含 9 份飞猪机票），且 `_check_status` 拿它
    # 去跟预设核对，会误报「不符」。
    # 新逻辑：去重后**只有 ≥2 个不同税号**才按出现顺序分配（购买方在前、销售方在后）；
    # 只剩 1 个 → 只填销售方，购买方留空字符串（核对列因「税号未识别」走 review 待确认）。
    # 例外：`buyer_tax` 若已由上方 OFD 配对（公司名与税号同行，位置锚定可信）给出，
    # 则保留，且同一个税号不会再重复塞给销售方。
    anchored_buyer_tax = bool(buyer_tax)
    tax_ids = []
    for _t in _RE_TAX_ID.findall(text):
        _t = (_t or '').strip().upper()
        if _t and _t not in tax_ids:
            tax_ids.append(_t)
    if len(tax_ids) >= 2:
        if not buyer_tax:
            buyer_tax = tax_ids[0]
        if not seller_tax:
            seller_tax = tax_ids[1]
    elif len(tax_ids) == 1:
        _only = tax_ids[0]
        if not seller_tax and _only != buyer_tax:
            seller_tax = _only
        if not anchored_buyer_tax:
            buyer_tax = ''
    return buyer, seller, buyer_tax, seller_tax


def extract_pdf_texts(filepath):
    """提取 PDF/OFD 文字与表格文本；返回 (文本, 错误信息)。

    支持格式：PDF(带文字层)、OFD(中国电子发票标准格式)。
    扫描件 PDF 会被清晰标注为待确认。带 (路径, mtime) 内存缓存。
    """
    cached = _cache_get(filepath)
    if cached is not None:
        return cached

    # 按扩展名分流：OFD → XML → PDF
    if filepath.lower().endswith('.ofd'):
        result = extract_ofd_text(filepath)
    elif filepath.lower().endswith('.xml'):
        # ⚠️ 此分支**不再被触发**：DEFAULT_EXTS（上面）已摘掉 .xml，
        #    collect_files 不会再产出 .xml 路径，扫描入口也就不会走到这里。
        #    保留它是为了让 XML 解析的实现（extract_xml_text）活着 ——
        #    将来若某个来源只给 XML，把 .xml 加回 DEFAULT_EXTS 即可复用。
        result = extract_xml_text(filepath)
    else:
        result = _extract_pdf_text_inner(filepath)

    _cache_put(filepath, result)
    return result


def extract_xml_text(filepath):
    """数电发票的 XML 原件 → 伪票面文本，**复用现有那套发票正则**。

    为什么不单独写一套 XML 字段解析：那样等于把 extract_invoice_fields 的
    二十来条规则再实现一遍，两份实现迟早不同步（STRONG_KW 三份副本的事故已经出过一次）。
    这里把 XML 拼成一段「长得像 PDF 文字层」的文本，交给既有正则消费，
    只要拼出来的字面形态对得上，抽取逻辑一行都不用改。

    ⚠️ 拼装的字段顺序是有讲究的：
      · 购买方/销售方必须写成「购买方信息 名称：X 统一社会信用代码：Y」——
        _RE_PARTY_ANY 就是按这个形态锚定的；
      · 金额必须同时给出「合计 ¥不含税 ¥税额」和「价税合计（小写）¥X」——
        extract_invoice_fields 先用 _RE_TOTAL_LINE 取前两个，再用 _RE_TOTAL_XJ 取合计；
      · 开票日期要写成「开票日期：2026年10月09日」——_RE_INVOICE_DATE 只认这个写法；
      · 开票人放最后一行——_RE_DRAWER 的边界断言 _NAME_BOUND 要求后面接结构字段或行尾。

    好处：XML 里的 ItemName 是**完整的**「*交通运输服务*客运服务费」，
    不会像 PDF 文字层那样被换行截成「客运服」「代订车服」。
    """
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
    except OSError as e:
        return '', f'XML读取失败: {e}'
    src = None
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk'):
        try:
            src = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if src is None:
        return '', 'XML编码无法识别（不是 UTF-8 / GBK）'
    try:
        root = ET.fromstring(src)
    except ET.ParseError as e:
        return '', f'XML解析失败: {e}'

    def _local(tag):
        return str(tag).split('}')[-1]

    def _all(name):
        return [(e.text or '').strip() for e in root.iter()
                if _local(e.tag) == name and (e.text or '').strip()]

    def _one(name, default=''):
        v = _all(name)
        return v[0] if v else default

    lines = ['电子发票（普通发票）']
    inv_no = _one('InvoiceNumber')
    if inv_no:
        lines.append(f'发票号码：{inv_no}')
    # IssueTime 形如 2026-10-09；_RE_INVOICE_DATE 只认「2026年10月09日」
    _t = _one('IssueTime')
    _m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', _t)
    if _m:
        lines.append('开票日期：%s年%02d月%02d日'
                     % (_m.group(1), int(_m.group(2)), int(_m.group(3))))
    for _label, _name_key, _id_key in (('购买方信息', 'BuyerName', 'BuyerIdNum'),
                                       ('销售方信息', 'SellerName', 'SellerIdNum')):
        _n, _i = _one(_name_key), _one(_id_key)
        if _n:
            lines.append(f'{_label} 名称：{_n} 统一社会信用代码：{_i}')

    for _item in _all('ItemName'):          # 可能有多个明细行
        lines.append(_item)
    # ⚠️ 空行不是排版，是**功能性的**：_normalize_cjk 会把两个汉字之间的单个换行
    #    吃掉（PDF 文字层就是这么被拼接的），不加空行的话明细名会和下一行的「合计」
    #    粘成「客运服务费合计」，项目名直接被污染。双换行不会被那条规则命中。
    lines.append('')
    _excl, _tax = _one('TotalAmWithoutTax'), _one('TotalTaxAm')
    _total = _one('TotalTax-includedAmount')
    if _excl or _tax:
        lines.append(f'合计 ¥{_excl} ¥{_tax}')
    if _total:
        lines.append(f'价税合计（大写）{_one("TotalTax-includedAmountInChinese")}'
                     f'（小写）¥{_total}')
    _drawer = _one('Drawer')
    if _drawer:
        lines.append(f'开票人：{_drawer}')
    return '\n'.join(lines), None


def _extract_pdf_text_inner(filepath):
    texts = []
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
                for table in page.extract_tables():
                    for row in table:
                        rt = ' '.join(str(c) if c else '' for c in row)
                        if rt:
                            texts.append(rt)
    except Exception as e:
        return '', f"无法读取 PDF：{e}"
    text = '\n'.join(texts).strip()
    if not text:
        return '', "未检测到可提取文字（可能是扫描件、加密文件或空白 PDF；当前版本暂不支持 OCR）"
    return text, None


#: OFD 文字抽取的「有效」下限（字符数）。
#:
#: 实测锚点（全库 35 份 OFD）：
#:   · 抽取不完整、只剩碎片的 9 份 —— 长度**全部为 39**
#:   · 抽取成功的样本里最短的一个 —— **170**
#: 空隙是 40..169，取 100 居中。
#: ⚠️ 阈值不是拍脑袋定的：改之前必须重跑实测拿到新的两个锚点。阈值过高会把
#:    正常票判成失败，过低会让碎片继续冒充成功（碎片曾长期被统计成"抽取成功"）。
_OFD_MIN_TEXT_LEN = 100


def _is_ofd_page_content(name):
    """OFD 压缩包内某条目是否为「页面内容」。

    放宽原因：原先要求同时含 'Content.xml' 和 'Page' 两个子串才算页面内容，
    遇到版式命名里不含 'Page' 的文件就一个都匹配不到 —— 而「匹配不到页面」
    与「页面里没有文字层」是**两回事**。原先两者共用同一句「图片版式」提示，
    会把结构问题误诊成「源文件没文字」，用户照提示去换同名 PDF 也解决不了。
    """
    if not name.lower().endswith('.xml'):
        return False
    return name.rsplit('/', 1)[-1].lower() == 'content.xml'


def _ofd_structured_text(zf, objects: dict[str, str]) -> str:
    """按 OFD CustomTag 的 ObjectRef 找票面字段，避免按文字出现顺序猜购销方。"""
    values = {}
    wanted = {"InvoiceNo", "IssueDate", "BuyerName", "BuyerTaxID", "SellerName",
              "SellerTaxID", "TaxInclusiveTotalAmount", "TaxExclusiveTotalAmount",
              "TaxTotalAmount"}
    for name in zf.namelist():
        if name.rsplit('/', 1)[-1].lower() != 'customtag.xml':
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except (ET.ParseError, OSError):
            continue
        for field in root.iter():
            key = field.tag.rsplit('}', 1)[-1]
            if key not in wanted:
                continue
            chunks = [objects.get((ref.text or '').strip(), '') for ref in field.iter()
                      if ref.tag.rsplit('}', 1)[-1] == 'ObjectRef']
            value = ''.join(chunks).strip()
            if value:
                values[key] = value
    if not values:
        return ''
    lines = ['【OFD结构化字段】']
    if values.get('InvoiceNo'):
        lines.append('发票号码：' + values['InvoiceNo'])
    date = values.get('IssueDate', '')
    m = re.search(r'(\d{4})\D*(\d{1,2})\D*(\d{1,2})', date)
    if m:
        lines.append('开票日期：%s年%02d月%02d日' %
                     (m.group(1), int(m.group(2)), int(m.group(3))))
    for title, name_key, tax_key in (("购买方信息", "BuyerName", "BuyerTaxID"),
                                      ("销售方信息", "SellerName", "SellerTaxID")):
        if values.get(name_key):
            lines.append('%s 名称：%s 统一社会信用代码：%s' %
                         (title, values[name_key], values.get(tax_key, '')))
    if values.get('TaxExclusiveTotalAmount') and values.get('TaxTotalAmount'):
        lines.append('合计 ¥%s ¥%s' % (values['TaxExclusiveTotalAmount'],
                                      values['TaxTotalAmount']))
    if values.get('TaxInclusiveTotalAmount'):
        lines.append('价税合计（小写）¥' + values['TaxInclusiveTotalAmount'])
    return '\n'.join(lines)


def _ofd_has_key_field(text):
    """抽取到的 OFD 文本里是否至少含一个关键字段（金额 或 发票号码）。

    与长度阈值组成**双重校验**：长度先拦下碎片；长度够但金额/发票号一个都没有的，
    说明版式受限、抽出来的不是可用票面 —— 同样不能算成功。
    """
    if _RE_INVOICE_NO.search(text) or _RE_INVOICE_NO_OFD.search(text):
        return True
    return bool(_RE_TOTAL_XJ.search(text) or _RE_TOTAL_XJ2.search(text)
                or _RE_TOTAL_LINE.search(text) or _RE_MONEY.search(text))


#: OFD 抽取薄弱、改读同名 PDF 成功时，在正文前加的来源标注。
#:
#: ⚠️ 存在的唯一目的是**留痕**：这段内容其实取自同名 PDF，不是 OFD。
#:    没有它，后来的人会对着 OFD 源码 debug 半天才发现取的是 PDF（**禁止静默替换**）。
#:    用词刻意避开所有发票正则会命中的形态：不含数字 / ¥ / 「发票号码」/ 非发票提示词
#:    （行程单、报销凭证、费用清单…），确保 is_noninvoice 与字段抽取不被污染。
_OFD_PDF_FALLBACK_MARK = '【来源：同名 PDF 补齐（OFD 抽取薄弱）】'
#: 2026-10-12 新增的第二级退路标记：**同名 XML**（数电票 XML 原件）补齐。
#: 用词同样刻意避开所有发票正则：不含数字 / ¥ / 「发票号码」/ 非发票提示词。
_OFD_XML_FALLBACK_MARK = '【来源：同名 XML 补齐（OFD 抽取薄弱）】'


def _sibling_pdf_path(filepath):
    """OFD 改道用的「同名 PDF」匹配规则（本函数是唯一真源）。

    规则 = **同目录** + **同主干名**（去掉扩展名后的完整文件名）+ 后缀 PDF：
      · 同目录：只在本目录找，不做跨目录搜索（否则可能改道到无关文件）。
      · 同主干名：仅把 `.ofd` 换成 `.pdf`，其余一字不差（含空格、全角括号、点号等）。
      · 后缀大小写：先试 `.pdf` 再试 `.PDF`。Windows 文件系统大小写不敏感但保留原始
        大小写，实测伴生 PDF 两种写法都出现过，所以两种都试。
    找不到返回 None。
    """
    base = os.path.splitext(filepath)[0]
    for cand in (base + '.pdf', base + '.PDF'):
        if os.path.exists(cand):
            return cand
    return None


def _sibling_xml_path(filepath):
    """OFD 改道用的「同名 XML」匹配规则 —— 与 _sibling_pdf_path 同一条规则，只换后缀。

    2026-10-12 新增。为什么要有这一级：
      高德打车那批 OFD 里有 7 份是**纯矢量版式**（Content.xml 里 TextCode=0、
      文字全是贝塞尔路径），源文件根本没有文字层，任何解析器都抽不出字；
      而它们伴生的 `…高德打车电子发票.pdf` 又被上一轮重命名搬走了（同目录只剩
      `…高德打车电子行程单.pdf`，主干名不同 ⇒ _sibling_pdf_path 找不到）。
      但**同主干名的 `.xml`（数电票 XML 原件）还在**，`extract_xml_text` 能稳定
      拿到票号 / 金额 / 开票日期 / 购销方 —— 这是那几张票唯一的救兵。

    ⚠️ 只在 PDF 那一级**失败之后**才试 XML（见 _ofd_weak_fallback）：
       有同名 PDF 时维持原有行为一字不变，纯属向下追加，不动已验收的 27 份样本。
    """
    base = os.path.splitext(filepath)[0]
    for cand in (base + '.xml', base + '.XML'):
        if os.path.exists(cand):
            return cand
    return None


def _ofd_weak_fallback(filepath, ofd_err):
    """OFD 抽取薄弱（无文字层 / 抽不完整）时的既定退路：改读同目录同名 PDF。

    返回 (text, err)：
      · 同名 PDF 抽出文字  → (带来源标注的 PDF 文本, None)；
      · 同名 PDF 不存在 / 也抽不到文字 → ('', **原始 OFD 错误** + PDF 侧的失败说明)。

    ⚠️ 只在**薄弱**分支调用（见 extract_ofd_text），正常抽成功的 OFD 不走这里 ——
       不是无条件拿 PDF 顶替 OFD。
    ⚠️ 成败都先记 _log_runtime 留痕；失败时把 OFD 原始文案**原样带回**，不丢原始失败信息。
    """
    sib = _sibling_pdf_path(filepath)
    if sib:
        try:
            pdf_text, _pdf_err = extract_pdf_texts(sib)
        except Exception as e:
            # ⚠️ 只记异常类型、不记异常消息：异常消息会把真实票件的完整路径带进来，
            #    而 runtime.log 是可能外发的产物。
            _log_runtime('OFD 改道同名 PDF 异常 (%s)' % type(e).__name__)
            return '', ofd_err + '；同名 PDF 解析异常，未能改道'
        if pdf_text:
            _log_runtime('OFD 抽取薄弱 → 已改道同名 PDF 成功（来源=同名 PDF）')
            return _OFD_PDF_FALLBACK_MARK + '\n' + pdf_text, None
        _log_no_pdf = 'OFD 抽取薄弱，且同名 PDF 也未取到文字'
        _no_pdf_msg = '；同名 PDF 也未能解析（未取到文字）'
    else:
        _log_no_pdf = 'OFD 抽取薄弱，且同目录未找到同名 PDF'
        _no_pdf_msg = '；且同目录未找到同名 PDF，无法改道'

    # 第二级退路：同主干名的 `.xml`（数电票 XML 原件）
    #    纯向下追加：只有 PDF 那一级没救回来才走到这里，有同名 PDF 时行为一字不变。
    xsib = _sibling_xml_path(filepath)
    if xsib:
        try:
            xml_text, _xml_err = extract_pdf_texts(xsib)
        except Exception as e:
            _log_runtime('OFD 改道同名 XML 异常 (%s)' % type(e).__name__)
            xml_text = ''
        if xml_text:
            _log_runtime('OFD 抽取薄弱 → 已改道同名 XML 成功（来源=同名 XML）')
            return _OFD_XML_FALLBACK_MARK + '\n' + xml_text, None
        _no_pdf_msg += '；同名 XML 也未能解析（未取到文字）'

    _log_runtime(_log_no_pdf + '，回退原错误')
    return '', ofd_err + _no_pdf_msg


def _ofd_fields_unreliable(filepath, text):
    """长度达标的 OFD 文本，其**票面关键字段**是否不可信（不可信 → 该改道同名 PDF）。

    为什么需要这一步：`_OFD_MIN_TEXT_LEN` 只看文本长度，但实测发现 OFD 原路抽取会
    **静默产出错误字段值**（不是抽不出来）——长度够长、票号也对，购销方却缺字/错字，
    这类文件永远走不到长度判据，界面上就会显示错误的公司名。

    判定口径（本函数是唯一真源），**全部以同名 PDF 为参照系**，逐条独立触发：

      1) 同名源能抽到、而 OFD 自己抽不到（rule「缺」）：
         票号 / 购买方 / 销售方，任一字段「PDF 有值 且 OFD 为空」→ 不可信。
         ⚠️ 这一条天然规避了火车票 / 行程单的误判：这两类票**自己就没有**购买方
            （火车票销售方按设计恒为「中国铁路」），所以同名 PDF 也抽不到对应字段，
            「PDF 有值 OFD 为空」不成立 → 不会误伤。
      2) 同一票号下、双方都有值却不同（rule「错」）：
         OFD 的票号 == 同名 PDF 的票号（= 同一张票，非两份不同票），且
         购买方 / 销售方 / **金额**任一为「两侧都非空 且 不同」→ 不可信。
         实测形态：OFD 销售方被**截断**（是 PDF 真名的子串）、或购买方被**拼接**出
         一个 PDF 原文里根本不存在的公司名、或金额抓成了**不含税合计**
         （曹操出行 OFD 79.55 vs 同名 PDF 81.94，2026-10-11 补的金额比对）。
         ⚠️ 只在**票号相同**时才比字段值：票号不同则无从判断是否同一张票，宁可不改道。

    返回 True = 判定 OFD 不可信，应改道；False = 维持原行为。

    ⚠️ 硬约束：**找不到同名 PDF / 同名 PDF 也取不到文字 → 一律返回 False**。
       没有参照系就无从判断，此时绝不能改变原有返回（见 extract_ofd_text 的调用点）。
    ⚠️ 选型依据（27 份有同名 PDF 的 OFD 实测）：以「票号/销售方/购买方/开票日期/金额>0」
       五项完整度比较，**同名 PDF 更好 = 12、打平 = 15、OFD 更好 = 0** —— 同名 PDF 从不
       比 OFD 差，所以「不一致时改道」没有回归风险。
    """
    # CustomTag 通过对象 ID 明确标注字段归属，优先于页面文字阅读顺序。
    # 带齐关键字段时不再因平铺文本中的残片触发改道。
    if text.startswith('【OFD结构化字段】'):
        fields = extract_invoice_fields(text, source='ofd')
        if (fields.get('invoice_no') and fields.get('invoice_date')
                and fields.get('buyer_name') and fields.get('seller_name')
                and fields.get('amount')):
            return False
    sib = _sibling_pdf_path(filepath)
    if not sib:
        return False
    try:
        pdf_text, _pdf_err = extract_pdf_texts(sib)
    except Exception:
        return False
    if not pdf_text:
        return False

    ofd = extract_invoice_fields(text, source='ofd')
    pdf = extract_invoice_fields(pdf_text)
    # 旧版平铺文本可能靠无标签兜底读出购销方。若这五项与独立的同名 PDF
    # 完全一致，就没有改道的理由；展示时仍使用上面的保守抽取，避免把猜测
    # 当作已经可靠识别的公司名称。
    legacy = extract_invoice_fields(text)
    agreement_keys = ('invoice_no', 'invoice_date', 'buyer_name', 'seller_name', 'amount')
    if (legacy.get('invoice_no') and legacy.get('invoice_date')
            and legacy.get('seller_name') and legacy.get('amount')
            and all(legacy.get(key) == pdf.get(key) for key in agreement_keys)):
        return False

    # rule「缺」：同名源有、OFD 没有 —— 票号 / 购买方 / 销售方
    for key in ('invoice_no', 'buyer_name', 'seller_name'):
        if key == 'buyer_name' and pdf.get('buyer_name') == pdf.get('seller_name'):
            # 无购买方的铁路票，PDF 通用兜底偶尔把销售方又塞给购买方。
            continue
        if pdf.get(key) and not ofd.get(key):
            return True

    # rule「错」：同一票号下，购销方 / 金额任一「两侧都非空且不同」
    same_no = bool(ofd.get('invoice_no')) and ofd.get('invoice_no') == pdf.get('invoice_no')
    if same_no:
        for key in ('buyer_name', 'seller_name'):
            o, p = ofd.get(key), pdf.get(key)
            if o and p and o != p:
                return True
        # 金额：2026-10-11 补。原先只比票号 / 购销方，**不比金额** ——
        # 「曹操出行」的 OFD 把不含税 ¥79.55 当成了价税合计（同名 PDF 是 ¥81.94），
        # 票号一样、购销方一样，两道护栏全放过 ⇒ 静默少算。
        # 金额不一致一律改道同名 PDF：选型实测 27 份里同名 PDF 从不比 OFD 差（见函数注释）。
        ao, ap = ofd.get('amount') or 0.0, pdf.get('amount') or 0.0
        if ao > 0 and ap > 0 and abs(float(ao) - float(ap)) > 0.005:
            return True
    return False


def extract_ofd_text(filepath):
    """从 OFD 提取文本。保留中文、金额、税号、日期与关键标签（含火车票 站/车次/客票号）。

    仅解析 Content.xml 中的可见文字节点，跳过矢量绘图路径噪点。

    返回 (文本, 错误信息)。**四种失败各有各的文案**，不要合并成一句 ——
    它们的处置方式完全不同（见各分支注释）。
    """
    parts = []
    page_hits = 0      # 判定为「页面内容」的条目数
    bad_pages = 0      # 解析失败的页数
    objects = {}
    structured = ''
    try:
        with zipfile.ZipFile(filepath, 'r') as zf:
            for name in zf.namelist():
                if not _is_ofd_page_content(name):
                    continue
                page_hits += 1
                # ⚠️ try 必须收在这一层（per-page）：
                # 原先 try 包住整个循环，任何一页的 Content.xml 解析失败都会让
                # **整份文件**报废 —— 其它好页已经抽到的文字也一起丢掉。
                try:
                    c = zf.read(name).decode('utf-8', errors='replace')
                    root = ET.fromstring(c)
                except Exception as e:
                    bad_pages += 1
                    _log_runtime('OFD 单页解析失败，已跳过该页: %s (%s: %s)'
                                 % (name, type(e).__name__, e))
                    continue
                for e in root.iter():
                    if e.tag.rsplit('}', 1)[-1] == 'TextObject' and e.attrib.get('ID'):
                        objects[e.attrib['ID']] = ''.join(
                            (node.text or '') for node in e.iter()
                            if node.tag.rsplit('}', 1)[-1] == 'TextCode').strip()
                    if not e.text:
                        continue
                    t = e.text.strip()
                    if not t:
                        continue
                    # 跳过矢量绘图路径（M/L/B 开头的指令串）
                    if re.match(r'^[MLB]\s', t) and len(t) > 12:
                        continue
                    if (re.search(_CJK, t)
                            or re.search(r'[¥￥]', t)
                            or re.search(r'\d+\.\d{2}', t)
                            or re.search(r'[\dA-Z]{17,20}', t)
                            or '站' in t or '开' in t or '车' in t or '号' in t
                            or '发票' in t):
                        parts.append(t)
            structured = _ofd_structured_text(zf, objects)
    except Exception as e:
        # ⚠️ 只记异常类型、不记异常消息：FileNotFoundError 等会把真实票件的
        #    完整路径带进来，而 runtime.log 是可能外发的产物。
        _log_runtime('OFD读取失败 (%s)' % type(e).__name__)
        return '', f'OFD读取失败: {e}'

    text = (structured + '\n' if structured else '') + re.sub(r'\s+', ' ', ' '.join(parts)).strip()

    # 失败 ②：压缩包里根本没有页面内容 —— 结构问题，换 PDF 没用，得换文件
    if page_hits == 0:
        _log_runtime('OFD 结构异常：压缩包内未找到任何 Content.xml 页面内容')
        return '', 'OFD 结构异常：未找到页面内容（Content.xml），文件可能损坏或不是标准版式'

    # 失败 ③：有页面但全部解析失败 —— 也是结构问题，不是"没文字层"
    if bad_pages == page_hits:
        _log_runtime('OFD 全部 %d 页解析失败' % page_hits)
        return '', 'OFD 页面内容解析失败（%d 页全部无法解析），文件可能损坏' % page_hits

    # ⚠️ 取不到文字时不要把锅甩给「格式有问题」：
    # 已查明根因是**渠道**而非格式 —— 高德打车、飞猪这两个渠道开的 OFD 是矢量/图片版式
    # （Content.xml 里只有 PathObject / ImageObject，没有 TextObject），源文件根本没有文字层。
    # 提示语要点明「票本身没文字、换个文件就行」，否则用户会以为程序坏了，
    # 或者以为需要去做 OCR（源文件没文字层，OCR 也救不回来）。
    #
    # 🆕 但「换个文件」现在由程序**自动做**：实测这批 OFD 同目录都带同名 PDF 伴生，
    #    所以先改道同名 PDF；只有 PDF 也不在/也取不到文字，才把原话术交给用户。
    if not text:
        _log_runtime('OFD 无文字层（%d 页有页面内容但无可见文字）' % page_hits)
        return _ofd_weak_fallback(
            filepath,
            'OFD 为图片版式（源文件无文字层），无法提取文字，请改用同名 PDF')

    # 失败 ①：抽到了一点但明显不完整 —— 与「无文字层」区分开：
    # 这种是版式受限、换了同名 PDF 通常就能解决；报成"源文件没文字层"会让用户白忙。
    # 🆕 同「无文字层」：先自动改道同名 PDF，PDF 也不行才回退到这句原话术。
    #
    # 🆕 判据已从「只看长度」扩成「长度不足 OR 关键字段不可信」：
    #    长度这个单一判据漏掉了一类更隐蔽的坏文件 —— 长度达标、票号也对，
    #    但 OFD 原路抽出来的购销方**缺字/错字**（见 _ofd_fields_unreliable 的口径）。
    #    它们永远走不到这里，却会在界面上显示错误的公司名，必须一并改道。
    _weak_msg = None
    if len(text) < _OFD_MIN_TEXT_LEN or not _ofd_has_key_field(text):
        _log_runtime('OFD 抽取不完整：%d 字符，关键字段=%s'
                     % (len(text), '有' if _ofd_has_key_field(text) else '无'))
        _weak_msg = ('OFD 文字抽取不完整（仅抽出 %d 个字符，且未取到金额与发票号码），'
                     '版式受限无法完整解析，请改用同名 PDF' % len(text))
    elif _ofd_fields_unreliable(filepath, text):
        # ⚠️ 只记「判定为不可信」这一事实，**不记任何票面字段值**（公司名/票号不外泄到日志）。
        _log_runtime('OFD 抽取字段不可信（与同名 PDF 比对不一致），改道同名 PDF')
        _weak_msg = ('OFD 抽取到的票面字段与同名 PDF 不一致（同一票号下购销方信息有出入），'
                     '判定 OFD 字段不可信，请改用同名 PDF')

    if _weak_msg is not None:
        return _ofd_weak_fallback(filepath, _weak_msg)

    return text, None


def _normalize_cjk(text):
    """移除 CJK 字符之间的单空格（PDF 字符间距导致），并清理全角括号内外多余空格。"""
    if not text:
        return ""
    t = re.sub(f'(?<={_CJK})(?<![日年月])\\s(?={_CJK})', '', text)  # CJK 与 CJK（日期单位 日/月/年 后保留分隔）
    t = re.sub(f'(?<={_CJK})\\s(?=[（(])', '', t)            # CJK 与左括号
    t = re.sub(f'(?<=[）)])\\s(?={_CJK})', '', t)             # 右括号与 CJK
    t = re.sub(f'(?<={_CJK})\\s(?=[：:])', '', t)             # CJK 与冒号
    t = re.sub(f'(?<=[（(])\\s', '', t)                     # 左括号后空格
    t = re.sub(f'\\s(?=[）)])', '', t)                      # 右括号前空格
    return t


def _flatten(text):
    if not text:
        return ""
    t = text.replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'\s+', ' ', t).strip()


def _clean_name(raw):
    if not raw:
        return ''
    raw = raw.strip()
    raw = re.sub(r'\s+', '', raw)
    # 去掉可能残留的标签残渣（前缀/后缀）
    raw = re.sub(r'^(?:购买方信息?|销售方信息?|购\s*销\s*买\s*信息|.*?名称)\s*[:：]?', '', raw)
    raw = re.sub(r'(?:买售方方信|方方信信|方信信|信息|名称|销售方|购买方|方)$', '', raw)
    return raw


# 标签残渣 / 交错排版污染识别：如「方方信信统一社会信用代码纳税人识别号」
# 「X售名称Y: :方方信信」等，这些不是真实公司名，必须过滤。
#
# 「销备」是 2026-10-11 只读审查（242 份实测）抓到的**真实**污染，26 份命中：
# 旧版增值税发票的购销两栏被文字层线性化成
#   「名称:\n销备纳税人识别号:\n售地址、电话:\n方注开户行及账号:」
# ——「销售方」「备注」两个标签被拆散成「销」「备」「售」「方」，「销」+「备」跨行粘成
# 两个字的残片，又被 `_RE_PARTY` 的跨行 `\s*` 吃掉，于是残片当成了购买方名称。
# 后果：22 张个人抬头票的「购买方」显示成「销备」而不是「个人」，另有 4 张真票
# （3 张滴滴 + 1 张中石化）被 `_check_status` 判成「不符」。
_LABEL_RESIDUE = re.compile(
    r'(?:信用代码|纳税人识别号|统一社会|方方信|方信信|方方|名称|购买方|销售方|售名称|信息$'
    r'|销备|售备|购备)')

#: 标签字表：极短串（≤4 字）里**全是**这些字 → 必是排版残片，不可能是公司名或人名。
#: 例「销备」「售方」「购名」「备注」。正常名字（「个人」「王伟」「京东」「方正」）不含这些字。
_LABEL_CHARS = frozenset('销备售购名称方注信息码号')


def _looks_like_company(name):
    """过滤掉标签残渣 / 交错排版污染，仅保留看起来像公司名或人名的串。"""
    if not name:
        return False
    if _LABEL_RESIDUE.search(name):
        return False
    if len(name) <= 4 and all(ch in _LABEL_CHARS for ch in name):
        return False
    return 2 <= len(name) <= 30


def _clean_person(raw):
    """清理开票人/复核人/收款人姓名：去除尾随标签残渣（注/开/购/销…），并去重重复半段。"""
    if not raw:
        return ''
    s = raw.strip()
    # 去除尾随的标签关键词
    s = re.sub(r'(?:注|开票人|购买方|销售方|信息|名称)+$', '', s)
    s = s.strip()
    # 去重「高雪高雪」→「高雪」
    if len(s) >= 2 and len(s) % 2 == 0 and s[:len(s) // 2] == s[len(s) // 2:]:
        s = s[:len(s) // 2]
    return s


def extract_invoice_fields(text, *, source=''):
    """从电子发票文本提取所有关键字段（数字发票：增值税/普通发票/全电/成品油）。

    返回字典同时包含：
      - 旧键（向后兼容）：buyer_name, tax_id, seller_name, seller_tax_id,
        amount(=价税合计), total_amount, tax_amount, invoice_date, item_name, remarks
      - 新增键：invoice_no, invoice_type, tax_rate, amount_excl_tax(税前金额),
        drawer(开票人), checker(复核人), payee(收款人), seller_addr

    设计原则：名称采用「标签锚定」提取，杜绝税务局印章文本污染（旧 bug D2）。
    """
    info = {
        'buyer_name': '', 'tax_id': '', 'seller_name': '', 'seller_tax_id': '',
        'amount': 0.0, 'invoice_date': '', 'item_name': '', 'remarks': '',
        'tax_amount': 0.0, 'total_amount': 0.0,
        # 新增
        'invoice_no': '', 'invoice_type': '', 'tax_rate': '',
        'amount_excl_tax': 0.0, 'drawer': '', 'checker': '', 'payee': '',
        'seller_addr': '',
    }
    if not text:
        return info

    nt = _normalize_cjk(text)
    flat = _flatten(text)

    # 1. 发票号码 / 开票日期 / 类型
    info['invoice_no'] = extract_invoice_no(nt)
    info['invoice_date'] = extract_invoice_date(nt)
    if '铁路电子客票' in nt or '火车票' in nt:
        info['invoice_type'] = '火车票电子发票'
    elif '专用' in nt:
        info['invoice_type'] = '增值税专用发票'
    elif '电子发票' in nt or '全电' in nt:
        info['invoice_type'] = '增值税电子普通发票'
    else:
        info['invoice_type'] = '发票'

    # 2. 购销方（多层策略）
    buyer, seller, buyer_tax, seller_tax = _extract_parties(
        nt, conservative=source == 'ofd' and not text.startswith(
            (_OFD_PDF_FALLBACK_MARK, _OFD_XML_FALLBACK_MARK)))
    info['buyer_name'] = buyer
    info['seller_name'] = seller
    # 税号由 _extract_parties 已处理；此处补充：若未取到则用原逻辑兜底
    if not info.get('tax_id'):
        info['tax_id'] = buyer_tax
    if not info.get('seller_tax_id'):
        info['seller_tax_id'] = seller_tax

    # 4. 金额 / 税额 / 价税合计
    m = _RE_TOTAL_LINE.search(flat)
    if not m:
        m = _RE_TOTAL_LINE2.search(flat)
    tax_excl = 0.0
    if m:
        try:
            tax_excl = float(re.sub(r'[,，]', '', m.group(1)))
            info['tax_amount'] = float(re.sub(r'[,，]', '', m.group(2)))
        except ValueError:
            pass

    # 价税合计（小写）
    total = 0.0
    m = _RE_TOTAL_XJ.search(nt) or _RE_TOTAL_XJ2.search(nt)
    if m:
        try:
            total = float(re.sub(r'[,，]', '', m.group(1)))
        except ValueError:
            pass
    if total <= 0 and tax_excl > 0:
        total = tax_excl + info['tax_amount']

    if total <= 0:
        # 后备：OFD / 非常规排版——优先取「大写金额」后的¥金额（即价税合计），
        # 其次取最后一个¥金额（价税合计通常列在末尾）。
        m = re.search(r'大写.*?[¥￥]\s*(\d+\.\d{2})', nt)
        if m:
            try:
                total = float(re.sub(r'[,，]', '', m.group(1)))
            except ValueError:
                pass
        if total <= 0:
            amts = [float(re.sub(r'[,，]', '', x)) for x in _RE_MONEY.findall(nt)]
            if amts:
                total = amts[-1] if len(amts) > 1 else amts[0]
                if len(amts) == 1:
                    tax_excl = 0.0

    info['total_amount'] = round(total, 2)
    info['amount'] = round(total, 2)          # 向后兼容：主金额 = 价税合计
    info['amount_excl_tax'] = round(tax_excl, 2) if tax_excl > 0 else 0.0
    if info['tax_amount'] <= 0 and tax_excl > 0 and total > tax_excl:
        info['tax_amount'] = round(total - tax_excl, 2)

    # 5. 税率
    rm = _RE_TAX_RATE.search(flat)
    if rm:
        info['tax_rate'] = f"{rm.group(1)}%"

    # 6. 项目名称（*分类*明细）
    # 用 find_star_item 而非 _RE_ITEM.search：跳过密码区防伪码里的假星号（见函数注释）。
    _pair = find_star_item(nt)
    if _pair:
        xxx, yyy = _pair
        if any(kw in yyy for kw in STRONG_KW) and not any(kw in xxx for kw in STRONG_KW):
            info['item_name'] = yyy[:15]
        else:
            info['item_name'] = xxx[:15]
    # 去除尾随计量单位（OFD 可能将「天/次/升」与名称粘连）
    # 守门：词本身以单位字结尾时（「配件」的「件」、「本子」的「本」）不能被削成单字，
    # 否则详情弹窗会显示「配」——削完不足 2 字就保留原名。
    _stripped = re.sub(
        r'(天|次|个|张|升|份|月|年|公里|件|台|本|页|项|只|条|米|平方|立方|时|分钟|小时)$', '', info['item_name'])
    if len(_stripped) >= 2:
        info['item_name'] = _stripped

    # 7. 备注（限定区间，修复 D1 过度捕获）
    # 取 findall 里**第一个非空**的，而不是 re.search 的第一个：
    # 机票类发票的 PDF 里常有两处「备注」块（发票重复排版），第一处是空壳、
    # 第二处才写着行程（实测 findall = ['', '2024-12-02 08:10 乌鲁木齐-天津 GS7577 …']）。
    # 用 search 会停在空壳上，行程信息永远抽不到。
    #
    # 同一个问题在其它票种上也存在（购物票的备注栏是订单号、机票票的是行程），
    # 所以这里统一取第一个非空 —— 拿到的是**真实备注**，不是噪声。
    # 但「非空」不等于「有意义」：有些票的第二块是空占位符（'-'、
    # 「购买方地址:-; 电话:-」），冒号后面全是空的，这种要跳过。
    for _rm in _RE_REMARKS.findall(nt):
        _rm = re.sub(r'\s+', ' ', _rm).strip()
        if not _rm:
            continue
        if _RE_BLANK_TAIL.match(_rm):          # 纯符号 / 「字段:-」式空壳
            continue
        if ':' in _rm or '：' in _rm:
            if not re.search(r'[:：]\s*[^\s\-—－;；]', _rm):
                continue                        # 冒号后全是 '-'，没有实际内容
        info['remarks'] = _rm
        break

    # 8. 结构化字段：开票人 / 复核人 / 收款人 / 销售方地址
    m = _RE_DRAWER.search(nt)
    if m:
        info['drawer'] = _clean_person(m.group(1))
    m = _RE_CHECKER.search(nt)
    if m:
        info['checker'] = _clean_person(m.group(1))
    m = _RE_PAYEE.search(nt)
    if m:
        info['payee'] = _clean_person(m.group(1))
    m = _RE_SELLER_ADDR.search(nt)
    if m:
        info['seller_addr'] = m.group(1).strip().rstrip(';；')

    return info


def ofd_display_fields(filepath: str, meta: dict | None = None) -> dict:
    """只读地复核 OFD 购销方；旧发票库缓存也使用当前解析规则显示。"""
    raw, err = extract_pdf_texts(filepath)
    if not raw:
        return {"buyer": "", "seller": "", "parse_status": "failed",
                "parse_error": err or "OFD 未取得可解析文字"}
    info = extract_invoice_fields(raw, source='ofd')
    result = {"buyer": info.get("buyer_name") or "",
              "seller": info.get("seller_name") or "",
              "invoice_no": info.get("invoice_no") or "",
              "invoice_date": info.get("invoice_date") or "",
              "amount": info.get("amount") or 0.0}
    if not (info.get("invoice_no") and info.get("invoice_date")
            and info.get("amount") and result["seller"]):
        result["parse_status"] = "failed"
        result["parse_error"] = "票号、开票日期、金额或销售方未可靠识别"
    elif not result["buyer"]:
        result["parse_status"] = "todo"
        result["parse_error"] = "购买方未可靠识别，请核对票面"
    else:
        result["parse_status"] = "done"
        result["parse_error"] = ""
    return result


# 共享工具：列目录 + MD5 去重
# .xml 是数电发票的**结构化原件**（含 IssuItemInformation/ItemName 等完整字段），
# 比 PDF 文字层可靠：明细名不会被换行截成「客运服」「代订车服」。
# 由 extract_xml_text 把它转成伪票面文本后复用既有正则。
#
# ⚠️ 2026-10-10 用户改定：**.xml 不再作为发票文件参与采集**。
#    上游下载目录里，同一张数电发票常常 pdf / ofd / xml 三份并存，XML 是并存的
#    结构化副本、不是单独的票 —— 把它当第三种「发票文件」收进来，只会让同一张票
#    在列表里出现多次（这也是同票去重要处理的噪声源）。所以这里从采集白名单里摘掉它。
#    下面的 XML 解析实现（extract_xml_text 及其调用）**保留不删**：
#      · 万一将来某个来源只给 XML，把它加回这个元组即可复用，不必重写解析；
#      · 删除会连带废掉一整套带回归的抽取逻辑，成本远大于留着。
#    注意 collect_files 只认这个白名单，故 .xml 现在既不列目录、也不进解析队列。
DEFAULT_EXTS = ('.pdf', '.ofd')

# 递归列目录时**跳过**的目录名：它们不可能装发票，但可能非常大 ——
# 用户若整个桌面 / 项目根目录拖进来会撞上，扫进去既无意义又拖慢速度。
# （以 `.` 开头的隐藏目录另有 `not d.startswith('.')` 兜住，这里只列非隐藏的那几个。）
_SKIP_DIRS = frozenset({
    'node_modules', '__pycache__', 'site-packages',
    'venv', 'dist', 'build',
})


def collect_files(folder_or_files, is_folder, exts=DEFAULT_EXTS):
    """返回 [(filepath, filename), ...]，按文件名排序，仅保留指定扩展名。

    ⛔ **唯一标识请用 ``filepath``，不要用 ``filename``**：后者只是 basename，
       递归后不同子目录下可能存在同名文件（例如按月分目录时各月都有一份同名票），
       当主键会撞车。

    **递归**（用户 2026-10-10 改定）：文件夹里还有子文件夹时，
    **子文件夹里的发票也要一并收进来**。
    此前只列一层（``os.listdir``），用户拖带子目录的结构会**一无所获**
    —— 实测拖 ``tests/samples`` 这种多子目录结构会得到 **0 个文件**。
    """
    if is_folder:
        hits = []
        for root, dirs, files in os.walk(folder_or_files):
            # ⚠️ 必须**就地**改 dirs 才能真正阻止 os.walk 继续往下走；
            #    事后过滤等于大树照样爬了一遍，省不下时间。
            dirs[:] = [d for d in dirs
                       if d not in _SKIP_DIRS and not d.startswith('.')]
            for f in files:
                if f.lower().endswith(exts):
                    hits.append((os.path.join(root, f), f))
        # 先按文件名、再按全路径 —— 保证出现同名文件时顺序也稳定可复现
        return sorted(hits, key=lambda t: (t[1], t[0]))
    paths = list(folder_or_files)
    return [(p, os.path.basename(p)) for p in paths]


class DedupRegistry:
    """MD5 去重注册表：返回 True 表示应跳过（重复）。

    同时维护业务指纹去重（由调用方提供 fingerprint 字符串）。
    """

    def __init__(self):
        self._md5 = set()
        self._md5_files = {}
        self._fp = set()
        self._fp_files = {}
        self.skipped = 0
        self.dedup_details = []

    def check_md5(self, fp, filename):
        try:
            fh = file_md5(fp)
        except Exception:
            return False
        if fh in self._md5:
            self.skipped += 1
            self.dedup_details.append(
                {"kept": self._md5_files.get(fh, "?"), "removed": filename,
                 "reason": "文件MD5完全相同"})
            return True
        self._md5.add(fh)
        self._md5_files[fh] = filename
        return False

    def check_fingerprint(self, fp_str, filename, reason):
        if fp_str in self._fp:
            self.skipped += 1
            self.dedup_details.append(
                {"kept": self._fp_files.get(fp_str, "?"), "removed": filename,
                 "reason": reason})
            return True
        self._fp.add(fp_str)
        self._fp_files[fp_str] = filename
        return False
