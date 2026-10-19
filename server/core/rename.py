# -*- coding: utf-8 -*-
"""core/rename.py — 发票智能重命名模块。纯标准库。"""
import os
import re

from .config import _log_runtime
# STRONG_KW（星号结构 `*分类*明细` 的判定词清单）与 parse.py 的详情弹窗 item_name
# 共用同一份真源，从下层 parse 导入，本模块不再自带副本。
# ⚠️ 方向不能反：rename 依赖 parse（本行 import），若让 parse 反过来 import rename
# 就会形成循环导入。所以常量只能定义在 parse.py。
from .parse import (_normalize_cjk, extract_invoice_fields, extract_pdf_texts,
                    find_star_item, STRONG_KW, is_trip_sheet)


def _sanitize_filename_part(s, max_len=20):
    """清理文件名中的非法字符并截断"""
    if not s: return "未知"
    s = re.sub(r'[\\/:*?"<>|]', '', str(s))
    s = re.sub(r'\s+', '', s)
    if len(s) > max_len:
        s = s[:max_len]
    return s or "未知"


def is_train_ticket(text):
    """判断是否为火车票。必须同时满足站名+车次+站名格式或明确关键词。"""
    if '铁路电子客票' in text or '火车票' in text:
        return True
    if re.search(r'[GDCKTZ]\d{2,5}', text):
        if re.search(r'[一-龥]{2,5}\s+[GDCKTZ]\d{2,5}\s+[一-龥]{2,5}', text):
            return True
    return False


# is_trip_sheet（网约车行程单判定）的真源在**下层 parse.py**，这里只转发。
# 理由同上：parse.extract_invoice_fields 也要用它做「行程单不抽备注」的闸门，
# 若在本模块再写一份，两份实现迟早走偏（STRONG_KW 三份副本不同步的旧事故就是这么来的）。
# 本模块直接 re-export，调用方 `from .rename import is_trip_sheet` 仍然可用。


# 「伞形大类」：**没有业务含义**的税收分类大类，只是编码体系的外层壳。
# 实测形态（网约车平台开的票）：`*生产生活服务*代订车服` —— 只取壳（xxx）会把
# 一张打车费显示成「生产生活服务」，用户按「交通费」聚合时压根看不见它。
# 遇到这些壳时，明细名（yyy）才是真正说明「买了什么」的那一层。
#
# ⚠️ 只放**实测确认过**的壳，不要凭想象加（'现代服务' 下面挂着咨询/设计/广告等
#    一堆真实业务，把它当壳会让那些票退化成销售方公司名）。
#
# 📌 位置说明：按架构铁律这类常量本该放在下层 parse.py（与 STRONG_KW 一起），
#    但 parse.py 此刻正被 scout 改 OFD 路径（并发改同一文件今天出过事故），
#    因此暂放本模块 —— **全项目只有这一份**，等 parse.py 空出来再下移。
_UMBRELLA_LABELS = ('生产生活服务',)


# ── 交通类特征词（真源，全项目只有这一份）──────────────────────────
# 两条链共用这份词表，但**判定输入各自独立**：
#   · 标签链 _normalize_type_label：在星号明细名那个窄字符串上匹配
#   · kind 链 facets.extract_invoice_meta：在票面证据（项目名/销售方/备注/明细名）上匹配
# 共用词表 = 单一真源（两边各自加词迟早走偏）；各判各的输入 = 两条链不互相绑架。
#
# ⚠️ 收窄词铁律：**绝不能写裸「出租」**。下面租赁费规则里就有裸「出租」，
#    「房屋出租 / 汽车出租」必须留在租赁费；任何一个交通词写成裸「出租」都会
#    把它们整批翻成打车票（tests/test_type_label_regression.py 已固化该回归）。
#    只认明确指向出租车的写法：出租车 / 出租汽车 / 巡游出租 / 网约车 / 代驾 / 顺风车。
#
# 「代订车」是网约车平台代开票的税收分类明细名（实测形态 `*生产生活服务*代订车服`），
# 平台自己不开客运票，票面上既没有「网约车」也没有「客运」，只能靠这个细分类别名认。
_TAXI_KW = ['网约车', '打车', '滴滴', '代驾', '顺风车', '出租汽车', '巡游出租', '出租车',
            '出行', '首汽', '曹操', 'T3', '代订车']
# 公路客运：与网约车分开成一条，因为标签口径不同（客运 → 客运票）
_TRANSIT_KW = ['客运', '旅客运输']
_AIR_KW = ['机票', '航空', '航班']
_TRAIN_KW = ['铁路旅客运输', '铁路运输', '铁路电子客票']


def _pick_star_item(xxx, yyy):
    """星号结构 `*分类*明细` → 取哪一层作为展示名（标签 / 文件名首段共用）。

    单一真源：extract_item_name_from_text（表格标签）与 generate_invoice_filename
    （文件名）都走这里，避免历史上两份判断逻辑不同步的老事故。
    """
    # 明细名带关键词、分类名不带 → 明细名更具体（清单见 parse.STRONG_KW）
    if yyy and any(kw in yyy for kw in STRONG_KW) and not any(kw in xxx for kw in STRONG_KW):
        return yyy
    # 分类名是伞形大类 → 它只是外壳，明细名才有业务含义。
    # ⛔ `yyy not in xxx` 守门：实测有票的明细名只是壳的前缀（*生产生活服务*生产生活），
    #    这种 yyy 不携带任何新信息，取它反而把「生产生活服务」削成「生产生活」。
    if xxx in _UMBRELLA_LABELS and yyy and yyy not in xxx:
        return yyy
    return xxx or yyy


def extract_item_name_from_text(text):
    """从原始文本提取项目名称（*xxx*yyy 格式）。
    返回 (item_name, item_label) — 取 xxx（分类名），若 xxx 无关键词则取 yyy。
    """
    # 与 parse.find_star_item 同源：跳过密码区防伪码里的假星号。
    # ⛔ 不要再在这里写一份 `\*([^*]+)\*([^*\s]{1,15})` 字面量 —— 历史上三份副本不同步，
    # 导致「表格显示打车票、详情弹窗显示运输服务」。
    pair = find_star_item(text)
    if pair:
        xxx, yyy = pair
        pick = _pick_star_item(xxx, yyy)
        return pick, (pick[:8] if pick else '')

    # 兜底：单星无尾形态 `*铁路旅客运输*`（尾星号后直接结束）主正则匹配不到，
    # 因为主正则要求星号后必须有非空明细名。
    # ⛔ 绝不要把主正则的 {1,15} 放宽成 {0,15}：实测 158 张真实样本里 2 张住宿票
    # 会被改成「生产生活服务」—— 原文先出现一对无明细的星号、之后才是
    # `*住宿服务*房费`，放宽后 re.search 会停在更早的那一对。
    # 因此只在兜底处加「全文恰好只有一对星号」的守门判断，主正则一动不动。
    if len(re.findall(r'\*[^*]+\*', text)) == 1:   # 仅当全文只有一对星号
        label = re.sub(r'\s+', '', re.search(r'\*[^*]+\*', text).group(0).strip('*'))[:8]
        # 同一原则：宁可返回空，也不要把密码区乱码当标签（至少含 2 个 CJK 才认）
        if len(re.findall(r'[一-鿿]', label)) >= 2:
            return '', label
    return '', ''


def _normalize_type_label(label):
    """将项目名称统一归类为标准标签。"""
    # ── 标签后缀口径（用户 2026-10-10 拍板）：交通与住宿类一律加「票」，
    #    其余费用类（快递费/餐饮费/租赁费/运输费/物业费…）保持「费」不动。
    #    即：火车票 / 机票 / 打车票 / 住宿票 / 客运票 —— 消除原来「票」「费」混用。
    rules = [
        (['住宿'], '住宿票'),
        (_AIR_KW, '机票'),
        (['汽油', '燃油', '加油', '柴油', '油品'], '汽油'),
        # ↓↓↓ 交通类细分：必须排在「快递费」之前，否则会被裸"运输"抢先命中。
        # 注意：这里一律用收窄词，"出租"必须写成"出租车/出租汽车/巡游出租"，
        # 否则"房屋出租/汽车出租"会被误判成打车票（租赁费回归用例在 tests/ 下有固化）。
        (_TRAIN_KW, '火车票'),
        # 后 4 个词是「销售方/业务形态」词：老式巡游出租车票没有星号结构，
        # 只能靠销售方公司名识别（配合 get_invoice_type_label 第 ③ 条出口）。
        # 词表真源见模块顶部 _TAXI_KW（与 facets 的 kind 链共用）。
        (_TAXI_KW, '打车票'),
        (_TRANSIT_KW, '客运票'),
        # ↑↑↑ 原第 4 条已去掉裸"运输"（它是"凡含运输二字即判快递费"的误判源头）
        (['快递', '物流', '速运', '收派', '配送', '货运'], '快递费'),
        (['打印', '复印', '印刷', '文印', '图文', '晒图', '装订'], '打印费'),
        (['餐饮', '食品', '餐费', '伙食', '食材', '外卖'], '餐饮费'),
        (['维修', '修理', '维护', '保养', '检修'], '维修费'),
        (['租赁', '租金', '出租'], '租赁费'),
        (['办公', '文具', '耗材'], '办公用品'),
        (['咨询', '顾问'], '咨询服务费'),
        (['广告', '宣传', '推广', '营销'], '广告费'),
        (['物业', '物管'], '物业费'),
        (['停车', '泊车'], '停车费'),
        (['通讯', '电信', '电话', '宽带', '上网'], '通讯费'),
        (['水电', '水费', '电费'], '水电费'),
        (['培训', '教育', '学习', '课程'], '培训费'),
        (['会议', '会展', '展览'], '会议费'),
        (['保险', '保费'], '保险费'),
        (['信息技术', 'IT服务'], '软件服务费'),
        (['设计', '制作', '策划'], '设计费'),
        (['检测', '检验', '测试'], '检测费'),
        (['劳务', '人力', '外包', '派遣', '临时工', '小时工', '兼职', '用工', '招聘', '猎头', '人事代理'], '劳务费'),
        (['保洁', '清洁', '环卫'], '保洁费'),
        (['安保', '保安'], '安保费'),
        (['绿化', '园林'], '绿化费'),
        (['装修', '装饰'], '装修费'),
        (['法律', '律师'], '法律服务费'),
        (['审计', '会计', '财务'], '审计费'),
        # 兜底（必须留在最后）：裸"运输"已从快递费规则摘除，纯"运输服务"且无更
        # 明细信息时在此落地，避免规则表漏空返回原始非标准标签。
        (['运输'], '运输费'),
    ]
    for keywords, result in rules:
        for kw in keywords:
            if kw in label:
                return result
    return label


# ── 品名中类（**货物**粒度，与上面那条链的**票种/费用**粒度正交）──────────
# 起因（用户第 7 条）：242 张真实样本里，`get_invoice_type_label` 会产出 30 种标签，
# 其中 19 种是**票面星号后的真实商品名**（蓄电池 / 计算机配套产品 / 电线电缆…）。
# 识别本身没错，错在**粒度** —— 一张增值税票的「商品名」和「火车票 / 住宿票」
# 并排站在同一列，用户会以为「蓄电池」是一种票。
#
# 分层：第一层 6 个票种大类**一字不改**；这里补第二层「品名中类」，
#       表格里**只在票种=「其他」时**显示，票面原文仍然看得到（chip 的 title、
#       详情栏「票面明细」、导出列，三处都在，不丢信息）。
#
# ⛔ 铁律一：**绝不把这套映射塞回 `_normalize_type_label`**。那 27 条规则同时服务
#    **文件名生成**（`generate_invoice_filename`），混表会让「蓄电池.pdf」变成
#    「电子配件.pdf」—— 正交维度必须各自成表。本表只读，不参与任何命名。
#
# ⛔ 铁律二：**只放实测确认过的键**。下面每个键都来自 242 张样本的真实产出
#    （见 tests/ 下的语料），键取的是 `_normalize_type_label` 的**输出值**，
#    所以票面写「文具」会先被规则表归一成「办公用品」，再在这里命中「办公用品」。
#
# 📌 「汽车用品」「服务费」两个中类本轮语料**落点为 0**（汽油类已在第一层由
#    `['汽油','燃油',…] → 汽油` 拦下），故不在此表凭空编键 —— 真出现新词再加。
_MID_CLASS = {
    # 电子配件（7 种，实测 9 张）
    '计算机配套产品': '电子配件',
    '计算机外部设备': '电子配件',
    '电子计算机': '电子配件',
    '移动通信设备': '电子配件',
    '家用音视频设备': '电子配件',
    '配件': '电子配件',
    '蓄电池': '电子配件',          # ← 用户点名的那个词，落这里，不单独成类
    # 办公用品（2 种，实测 4 张）
    '办公用品': '办公用品',
    '纸制品': '办公用品',
    # 日用百货（2 种，实测 4 张）
    '美容护肤品': '日用百货',
    '日用杂品': '日用百货',
    # 食品饮料（7 种，实测 8 张）
    '熟肉制品': '食品饮料',
    '水果': '食品饮料',
    '果类加工品': '食品饮料',
    '软饮料': '食品饮料',
    '调味品': '食品饮料',
    '方便食品': '食品饮料',
    '焙烤食品': '食品饮料',
    # 医药保健（1 种，实测 1 张）
    '化学药品制剂': '医药保健',
    # 五金工具（2 种，实测 4 张）
    '电线电缆': '五金工具',
    '金属制品': '五金工具',
}


def mid_class(label):
    """把「品名」映射到品名中类（9 个之一）。**只用于展示，不参与命名。**

    返回 `None` = 这个 label **不是**开放商品名（它是票种/费用名，第一层已经
    说清楚了），调用方应继续显示原 label。

    `生产生活服务` 这类**伞形壳**（`_UMBRELLA_LABELS`）落「其他」：实测有 3 张票
    的票面明细只写了这一层壳，配的销售方又是酒店，硬归任何一个中类都是编造。
    用户第 8 条已定「不开放手工改标」，故这里给一个诚实的兜底而不是猜。
    """
    if not label:
        return None
    hit = _MID_CLASS.get(label)
    if hit:
        return hit
    if label in _UMBRELLA_LABELS:
        return '其他'
    return None


def get_invoice_type_label(text, info=None):
    """获取发票类型标签（用于表格显示）。所有模块统一入口。"""
    # 行程单不是发票，必须抢在火车票/增值税分支之前拦下，
    # 否则它的 POI 名（酒店名/上车点名）会被当成销售方或项目名。
    if is_trip_sheet(text):
        return '行程单'
    if is_train_ticket(text):
        # 口径统一加「票」：facets 侧同一条链早已返回「火车票」，
        # 这里长期返回「火车」，导致同一张票在列表与详情里显示两个名字
        # （全库实测 54 条不一致，全部是 PDF，形态只有这一种）。
        return '火车票'
    _, label = extract_item_name_from_text(text)
    if label:
        return _normalize_type_label(label)
    # 兜底：用销售方简称（去掉法人后缀后先过一遍规则表，命中就用标准标签）。
    # 老式巡游出租车票 / 机票行程单往往没有 *分类*明细 结构，只能靠销售方识别；
    # 不过规则表的话标签会退化成公司名（实测：某航空类公司名本应为「机票」）。
    if info:
        seller = re.sub(r'[买购销]售?方?[方信名称息]*$', '', str(info.get('seller_name', '')))
        seller = re.sub(r'\s+', '', seller)
        if seller and seller != '未知':
            short = re.sub(r'(有限.*公司|股份.*公司|有限公司)', '', seller).strip()
            if short:
                hit = _normalize_type_label(short)
                return hit if hit != short else short[:8]
    return '发票'


def generate_invoice_filename(text, info, orig_path=''):
    """根据发票内容生成规范文件名。
    火车票特殊处理；其余统一使用：项目名称-金额-日期-销售方名称
    保留原始文件扩展名（.ofd/.pdf）。
    """
    nt = _normalize_cjk(text)

    # ── 火车票特殊处理 ──
    if is_train_ticket(text):
        fr, to = '', ''
        nt_clean = re.sub(r'\d{1,2}日', ' ', nt)
        # 直接用原始文本（与 parse_train_tickets 相同逻辑）
        for m in re.findall(r'([一-龥]+)\s+[A-Z]\d+\s+([一-龥]+)', text):
            if '站' not in m[0] and '站' not in m[1]:
                fr, to = m
        _log_runtime(f"火车票站名(原始文本): fr=[{fr}] to=[{to}]")
        # 清理：去重"站"字，去掉括号内容
        def _clean_station(s):
            s = re.sub(r'站站$', '站', s)  # 邯郸站站 → 邯郸站
            s = re.sub(r'[（(].*[）)]', '', s)  # 去掉括号
            return s.strip()
        fr, to = _clean_station(fr), _clean_station(to)
        _log_runtime(f"火车票站名(清理后): fr=[{fr}] to=[{to}]")
        d = info.get('invoice_date', '')
        dm = re.search(r'(\d{4})[年.\-](\d{1,2})[月.\-](\d{1,2})', d or '')
        d_show = f"{int(dm.group(1))}.{int(dm.group(2))}.{int(dm.group(3))}" if dm else ''
        amt = f"{info['amount']}元" if info['amount'] > 0 else ''
        parts = ['火车票']
        if fr: parts.append(fr)
        if to: parts.append(to)
        if d_show: parts.append(d_show)
        if amt: parts.append(amt)
        parts.append('中国铁路')
        if '退票' in nt: parts.insert(1, '退票')
        if '改签' in nt: parts.insert(1, '改签')
        # 发票号码不再拼进文件名（用户：末尾那串代码太长，没必要）
        _log_runtime(f"火车票最终文件名: {'-'.join(parts)}.pdf")

    else:
        # ── 统一规则：项目名称 - 金额 - 日期 - 销售方 ──
        seller = str(info.get('seller_name', ''))
        seller = re.sub(r'[买购销]售?方?[方信名称息]*$', '', seller)
        seller = re.sub(r'\s+', '', seller)
        if not seller or seller == '未知':
            # 火车票没有销售方，统一标记
            seller = ''
        amt = info['amount']
        d = info.get('invoice_date', '')

        # 项目名称提取：*xxx*yyy，xxx优先，无关键词取yyy
        item_clean = ''
        pair2 = find_star_item(text)
        if pair2:
            # 与表格标签同源（_pick_star_item）：不再各写一份靠人工同步，
            # 否则表格标签判成"打车票"、文件名却仍是"运输服务"。截断层长不同故不复用标签。
            item_clean = _pick_star_item(*pair2)[:15]
        # 后备：用 extract_invoice_fields 的 item_name
        if not item_clean or item_clean.isdigit():
            item = str(info.get('item_name', ''))
            if item and not item.isdigit() and len(item) >= 2:
                star_parts = [p.strip() for p in item.split('*') if p.strip()]
                meaningful = [p for p in star_parts if len(p) >= 2 and not p.isdigit()]
                item_clean = meaningful[-1] if meaningful else (star_parts[-1] if star_parts else '')
        if not item_clean or item_clean.isdigit():
            item_clean = '发票'
        item_clean = _normalize_type_label(item_clean)

        # 金额
        amt_str = f"{amt}元" if amt > 0 else ''

        # 日期
        dm = re.search(r'(\d{4})[年.\-](\d{1,2})[月.\-](\d{1,2})', d or '')
        d_show = f"{int(dm.group(1))}.{int(dm.group(2))}.{int(dm.group(3))}" if dm else ''

        # 销售方（去掉公司后缀）
        seller_clean = re.sub(r'(有限.*公司|股份.*公司|有限公司|销售方|购买方)', '', seller or '')
        seller_clean = seller_clean.strip()

        parts = [item_clean]
        if amt_str: parts.append(amt_str)
        if d_show: parts.append(d_show)
        if seller_clean: parts.append(seller_clean)
        # 同上：发票号码不再拼进文件名

    # ── 清理组装 ──
    name = '-'.join(str(p) for p in parts if p)
    name = _sanitize_filename_part(name, 100)
    if not name: name = '发票'
    ext = os.path.splitext(orig_path)[1] if orig_path else '.pdf'
    # .xml 是数电发票原件，重命名后必须还是 .xml，退化成 .pdf 会打不开
    if ext.lower() not in ('.pdf', '.ofd', '.xml'):
        ext = '.pdf'
    return f"{name}{ext}"


def parse_invoice_for_rename(filepath):
    """解析单张发票用于重命名预览。"""
    result = {
        'filepath': filepath,
        'current_name': os.path.basename(filepath),
        'new_name': '',
        'type': '未知',
        'amount': 0.0,
        'date': '',
        'error': None,
    }
    if not os.path.isfile(filepath):
        result['error'] = f"文件不存在: {filepath}"
        return result

    raw_text, read_error = extract_pdf_texts(filepath)
    if read_error:
        result['error'] = read_error
        return result

    info = extract_invoice_fields(raw_text)
    result['amount'] = info['amount']
    result['date'] = info['invoice_date']
    result['new_name'] = generate_invoice_filename(raw_text, info, filepath)

    # 类型标签（统一入口）
    result['type'] = get_invoice_type_label(raw_text, info)

    return result
