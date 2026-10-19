# -*- coding: utf-8 -*-
"""core/attach.py — 「非电子发票」的挂接与废弃标记（v0.4）。

背景（用户 2026-10-10 拍板）
----------------------------
行程单 / 报销单据 / 报销辅助材料**不是电子发票**：没有发票号码、没有购销方，
金额是「多笔行程合计」。所以它们：
  · **导入即分类**为 kind='noninvoice'（在 core/facets.py 里就判好）
  · 不进任何金额合计（amount 恒为 0）
  · 不进地图轨迹（顶层 from_/to_/train 恒为空）
只有当它**严格**对上某张真发票时，才作为那张发票的「附属材料」把行程信息带过去，
在详情里显示一行备注（此时它自己不占列表一行）。

对不上的：**标注废弃**（`discarded=True`），仍然留在列表里给用户看见，
不是偷偷删掉。废弃条目照旧不进金额、不进轨迹。

匹配规则（用户：严一点，宁可标废弃也不硬塞）
--------------------------------------------
  · **金额完全一致 = 硬门槛，不可降级**
  · **并且**命中确认键：**文件名主干相同优先**；主干一个都没对上，才轮到
    **日期对得上**（±3 天 + 交通类）。两者是**优先关系**，不是并集 —— 并集会把同一张
    发票的其它原件也拉进候选，凑成"多张不同发票"被整体拒绝（实测挂上从 53 掉到 30）。
  · 两条都满足才挂；只中一条 → 不挂
  · 确认后仍对上不止一张不同的发票 → **不挂**（没有"取第一个"这种兜底）
  · 都挂不上 → 标废弃

为什么独立成模块
----------------
匹配要拿一份记录去跟**全批**其它记录比（金额 / 文件名 / 日期），单文件解析
阶段（facets.extract_invoice_meta）根本看不到全局 —— 所以挂接不能写在那儿，
也不该塞进 routes 层。本模块只做「一批池子行 → 挂接 / 标废弃」，不解析 PDF、
不碰磁盘。
"""
import os
import re
from datetime import date

#: 文件名里区分「行程单 / 发票」的冗余词。去掉这些词后两边的主干才对得上。
#: ⚠️ 长词必须排在短词前面（'电子行程单' 先于 '行程单'），否则会留下 '电子' 尾巴。
_RE_STEM_NOISE = re.compile(r'电子行程单|行程报销单|行程单|电子发票|发票')
#: 主干两端要削掉的粘连符号（'Xxx_行程单.pdf' 削完会剩一个 '_'）
_STEM_TRIM = '_-·—（）()【】[] \t'

#: 日期这条**确认键**允许「交通类」票种，与「客运票」等现行标签一致
#: （旧名「客运费」已全项目统一为「客运票」）。
#: 行程单只可能是 打车票 / 客运票 / 火车票 / 机票 这几类的附件，
#: 所以「日期对得上」时再要求对方是交通类，避免把同日同额的住宿票也认进来。
_TRANSPORT_HINTS = ('打车', '网约车', '出租汽车', '客运', '交通', '运输',
                    '火车', '铁路', '机票', '航空', '汽车票')

#: 日期确认键的容差（天）。行程起始日与开票日常差几天（月底跨月开票），
#: 只认完全相等太苛刻。
#: ⚠️ 它是**确认键**，不是匹配键 —— 单独命中日期（哪怕 ±0 天）也不能挂，
#:    必须已经有「金额完全一致」这个硬门槛才轮到它出场。别把它当成可以
#:    单独用来匹配的规则。
_DATE_NEAR_DAYS = 3

#: 渠道代码 → 中文（取不到就是空串，前端自行省略）
_CHANNEL_CN = {'amap': '高德', 'didi': '滴滴'}

_AMOUNT_EPS = 0.005


# ─────────────────────────── 判据 ───────────────────────────

def _dir_of(row):
    """候选所属目录。同目录优先：行程单和它的发票是一起导出的，
    目录外的发票光靠金额撞上的概率不低（全库实测就有同额但毫不相干的发票）。"""
    try:
        return os.path.dirname(os.path.abspath(row.get('path', '') or ''))
    except Exception:
        return ''


def stem(name):
    """文件名主干：去扩展名 + 去掉「行程单 / 发票」等冗余词 + 削掉粘连符号。

    例（脱敏）：'【某平台-X元-N个行程】高德打车电子行程单.pdf'
          与   '【某平台-X元-N个行程】高德打车电子发票.pdf'  → 同一个主干。
    """
    base = os.path.splitext(os.path.basename(name or ''))[0]
    return _RE_STEM_NOISE.sub('', base).strip(_STEM_TRIM)


def _date_tuple(s):
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', str(s or ''))
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _days_apart(a, b):
    try:
        return abs((date(*a) - date(*b)).days)
    except Exception:
        return 10 ** 6


def _sheet_confirm_date(sheet):
    """日期确认键（`_date_hit`）用于比较的「行程单侧」日期元组。

    优先票面「申请日期 / 申请时间」——它等于宿主发票的开票日，是这张行程单该挂给谁的
    最强日期证据。parse 已把它写进 `sheet['date']`（见 parse.trip_sheet_brief 的注释：
    facets/pool 只把 ts['start_date'] 映射成行 `date`，这是该日期送进挂接层的唯一通道），
    所以这里先看一眼显式的 `apply_date` 字段（前向兼容），取不到就退回 `sheet['date']`。
    """
    return _date_tuple(sheet.get('apply_date', '')) or _date_tuple(sheet.get('date', ''))


def _trip_amount(row):
    return float((row.get('detail') or {}).get('trip_amount') or 0.0)


def _amount_hit(sheet, inv):
    """硬门槛：行程单票面合计 == 发票金额（误差 ≤ 0.005 元）。

    最可靠也最严格的键 —— 网约车开票时行程单合计就恰好等于发票金额。
    ⚠️ 不可降级：这一条不中就没得谈，直接不挂。
    """
    ta = _trip_amount(sheet)
    return ta > 0 and abs(float(inv.get('amount') or 0.0) - ta) <= _AMOUNT_EPS


def _stem_hit(sheet, inv):
    """确认键 A：去掉「行程单 / 发票」后文件名主干相同。"""
    s = stem(sheet.get('filename', ''))
    return bool(s) and s == stem(inv.get('filename', ''))


def _date_hit(sheet, inv):
    """确认键 B：日期对得上（±3 天）**且**对方是交通类票种。

    ⚠️ 只作确认，不作匹配：必须已有金额硬门槛才轮得到它。
    行程单侧日期走 `_sheet_confirm_date`：优先票面「申请日期 / 申请时间」（= 宿主开票日），
    取不到退回 `sheet['date']`。⚠️ 改前这里直接比「行程起止日 D1」，D1 与开票日常差几十上百天，
    实测全库 53 份行程单日期确认键 0 份生效（真正在干活的只剩文件名主干键）。
    """
    if not _is_transport(inv):
        return False
    a, b = _sheet_confirm_date(sheet), _date_tuple(inv.get('date', ''))
    return bool(a and b) and _days_apart(a, b) <= _DATE_NEAR_DAYS


def _is_transport(row):
    """发票是否属交通类（确认键 B 的附加限制）。"""
    txt = '%s %s' % (row.get('type', '') or '', row.get('label', '') or '')
    return any(w in txt for w in _TRANSPORT_HINTS)


# ─────────────────────────── 匹配 ───────────────────────────

def _ext_of(row):
    return os.path.splitext(row.get('path', '') or '')[1].lower()


def _prefer_ext(sheet, cands):
    """同一张发票的不同原件（.pdf / .ofd /.xml 常常三份都在）里挑一份。

    归并键是（发票号码, 金额）（取不到号码时回退（主干, 金额）），所以进来的一定是
    「同一张发票」的多个格式副本；行程单本身是 PDF，优先挂给 PDF 那份更符合直觉。
    """
    te = _ext_of(sheet)
    for c in cands:
        if te and _ext_of(c) == te:
            return c
    return cands[0]


def find_host(sheet, invoices):
    """为非电子发票找宿主发票；找不到返回 None（宁可标废弃，不可硬塞）。

    规则（详见模块 docstring）：
      金额完全一致（硬门槛） **且** 命中确认键（主干相同**优先**，主干全不中才用
      「日期对得上 + 交通类」）。确认后若对上**不止一张不同的发票** → 不挂。
    """
    same = [r for r in invoices if _dir_of(r) == _dir_of(sheet)]
    cands = [r for r in same if _amount_hit(sheet, r)]
    if not cands:
        return None                                   # 硬门槛没过 → 不挂
    # 确认键取「主干优先」：只要主干能对上，就**只用主干候选**（一张行程单对一份发票的
    # 一个原件）；主干一个都没对上，才退回日期候选。
    # ⚠️ 为什么不能用「主干 OR 日期」并集：日期键会把同一张发票的其它原件也拉进候选，
    #    于是同一份行程单命中 ≥2 个候选 → 触发下面的「多候选不挂」被整体拒绝。实测并集
    #    会让挂上从 53 掉到 30（叠加按发票号码归并后）；主干优先则既让日期键对「无主干
    #    命中」的行程单生效（51→53），又不破坏其余行程单的一对一挂接。
    stem_c = [r for r in cands if _stem_hit(sheet, r)]
    conf = stem_c if stem_c else [r for r in cands if _date_hit(sheet, r)]
    if not conf:
        return None                                   # 只有金额、没有确认键 → 不挂

    # 归并同一张发票的多份原件（.pdf / .ofd / .xml），避免被当成"多张不同的发票"。
    # 键**优先用发票号码**：同一张发票的多个格式副本文件名/主干可以不同，但发票号码相同
    # （实测 46/53 份行程单的同目录里就有一对这样"主干不同、金额相同、号码相同"的副本）；
    # 旧键 (主干, 金额) 会把它们判成两张不同发票 →「对上多张不挂」整体拒绝，行程单被丢弃。
    # 取不到发票号码时（非数电票 / 抽取失败）回退到原 (主干, 金额) 键，别让没号码的票失去归并。
    # 键加 'NO' / 'STEM' 前缀：避免某个发票号码字符串恰好等于某个主干名而误合并。
    groups = {}
    for r in conf:
        amt = round(float(r.get('amount') or 0.0), 2)
        ino = str(r.get('invoice_no') or '').strip()
        if ino:
            key = ('NO', ino, amt)
        else:
            key = ('STEM', stem(r.get('filename', '')), amt)
        groups.setdefault(key, []).append(r)
    if len(groups) != 1:
        return None                                   # 对上多张不同的发票 → 不挂
    return _prefer_ext(sheet, next(iter(groups.values())))


# ─────────────────────────── 挂接 / 废弃 ───────────────────────────

def build_attachment(sheet):
    """非电子发票行 → 发票上的 attachment 结构（前端照此对接）。"""
    d = sheet.get('detail') or {}
    src = _CHANNEL_CN.get(str(d.get('channel') or '').lower(), '')
    if not src:
        # 渠道没解析出来时，从文件名 / 服务商名里兜一次，取不到就留空串
        txt = '%s %s' % (sheet.get('filename', '') or '', sheet.get('seller', '') or '')
        src = '滴滴' if '滴滴' in txt else ('高德' if '高德' in txt else '')
    return {
        'type': '行程单',
        'source': src,
        'trip_count': int(d.get('trip_count') or 0),
        'trip_amount': round(_trip_amount(sheet), 2),
        'start_date': sheet.get('date', '') or '',
        'trips': d.get('trips') or [],
    }


def attachment_note(att):
    """详情里的一行备注文案（金额在这里**只用于展示**，不参与任何合计）。"""
    if not att:
        return ''
    src = att.get('source') or ''
    return '行程单%s · %d 笔 · 合计 ¥%.2f' % (
        ('（%s）' % src) if src else '',
        int(att.get('trip_count') or 0),
        float(att.get('trip_amount') or 0.0),
    )


def _clear_amount(row):
    """把一行的金额清零 —— **源头清零**。

    ⛔ 前端的合计就是 `rows.reduce((s, r) => s + (Number(r.amount) || 0), 0)`
       （web/js/app.js），**没有 discarded 过滤**。所以「废弃材料不进金额」必须在
       数据出口就做到，不能指望显示层跳过 —— 金额不该存在于数据里。
       因此这里对**所有**离开本函数的废弃行 / 非电子发票行统一清零：
       既覆盖我们自己标 discarded 的路径，也覆盖外部传进来就已经带着
       discarded=True + 非零 amount 的行。
    """
    row['amount'] = 0.0


def attach_noninvoices(rows):
    """把「非电子发票」挂到宿主发票上，挂不上的**标废弃**。

    两类走向：
      · 挂上了 → 它作为附件进宿主发票，自己**不占列表一行**
      · 挂不上 → `discarded=True`，**留在列表里**（用户要的是"标注"，不是"删除"）

    ⚠️ 顺序不能反：必须先挂接再决定剔除，否则宿主已经不在候选里了。
    这一步必须在**全批行都建好之后**做 —— 匹配要跟其它记录比金额/文件名/日期，
    单文件解析阶段（facets.extract_invoice_meta）看不到全局。

    返回：新行列表（保留的发票 + 废弃条目），id 已按 0..n-1 重排
    （前端拿 id 当行键，留空洞会错位）。
    """
    sheets = [r for r in rows if r.get('kind') == 'noninvoice']
    invoices = [r for r in rows if r.get('kind') != 'noninvoice']
    for s in sheets:
        # 标废弃的两种情形：① 找不到宿主 ② 宿主已被别的材料认领
        # （一张发票只带一份行程单附件）
        s['discarded'] = True
        host = find_host(s, invoices)
        if host is None or host.get('attachment'):
            continue
        s['discarded'] = False
        att = build_attachment(s)
        host['attachment'] = att
        host['detail']['attachment'] = att
        host['detail']['attachment_note'] = attachment_note(att)

    # 挂上的材料自己不占列表一行；标了废弃的留在列表里给用户看见
    out = [r for r in rows
           if r.get('kind') != 'noninvoice' or r.get('discarded')]
    # 出口统一收敛（不分散在各个分支里，免得漏掉"外部预设 discarded"那条路）
    for r in out:
        if r.get('discarded') or r.get('kind') == 'noninvoice':
            _clear_amount(r)
    for i, r in enumerate(out):
        r['id'] = i
    return out
