# -*- coding: utf-8 -*-
"""core/facets.py — 统一发票元数据提取。

为详情弹窗 / 统计卡片 / 筛选器 提供单一数据源，消除各模块各自解析、
字段键名不一致的旧问题。

- 火车票：复用 train.parse_train_ticket_text
- 住宿票：复用 parse.extract_invoice_fields + hotel._extract_stay_dates
- 增值税：复用 parse.extract_invoice_fields
"""
import os
import re

from .parse import (extract_pdf_texts, extract_invoice_fields, extract_invoice_no,
                    extract_invoice_date, parse_trip_sheet, is_noninvoice,
                    find_star_item, extract_old_guide_fields, _normalize_cjk)
from .rename import (is_train_ticket, is_trip_sheet, get_invoice_type_label,
                     _TAXI_KW, _TRANSIT_KW, _AIR_KW)
from .train import parse_train_ticket_text
from .hotel import _extract_stay_dates

# 增值税补充字段（详情弹窗需要，extract_invoice_fields 未直接返回）
_RE_INVOICE_CODE = re.compile(r'发票代码[:：]\s*(\d{10,12})')
_RE_CHECK_CODE = re.compile(r'校验码[:：]\s*([0-9A-Za-z]{10,20})')
_RE_MACHINE_CODE = re.compile(r'(?:机器编号|机打代码|机器编码)[:：]\s*([0-9A-Za-z]{10,20})')
# 兜底抽取：部分发票（住宿票/汽油票）经 extract_invoice_fields 未稳定返回这些字段，
# 这里直接在原文上做正则兜底，确保详情弹窗能展示发票号码/开票日期。
_RE_INVOICE_NO = re.compile(r'发票号码?\s*[:：]?\s*([0-9A-Za-z]{8,20})')
_RE_INVOICE_DATE = re.compile(r'开票日期\s*[:：]?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
# ── 铁路电子客票的真实购买方（2026-10-11 用户第①条）──────────────────────
# 票面有「购买方名称:示例购买方有限公司 统一社会信用代码:91XXXXXXXXXXXXXXX」一行。
# `train.parse_train_ticket_text` 不返回 buyer（它只管行程/乘客/票号），所以在这里单独取。
# 票面文字层里「购买方名称」周边有印章乱码，且公司名可能带全角括号但不含空白，
# 所以用「到下一个标签为止」定位，绝不吃掉后面的统一社会信用代码。
_RE_TRAIN_BUYER = re.compile(
    r'购买方名称\s*[:：]?\s*([^\s:：]{2,40}?)\s*(?=统一社会信用代码|纳税人识别号|$)')
_RE_TRAIN_BUYER_LOOSE = re.compile(r'购买方名称\s*[:：]?\s*([^\s:：]{2,40})')
# 购买方税号：票面同一行紧跟公司名（`购买方名称:<名> 统一社会信用代码:<税号>`）。
# ⛔ 必须锚在「购买方名称」之后 —— 代开机票那种「购买方是个人、票面只有一个销售方
#    税号」的形态绝不能把销售方税号算成购买方的（用户第③条红线）。
_RE_TRAIN_BUYER_TAX = re.compile(
    r'购买方名称\s*[:：]?\s*[^\s:：]{2,40}?\s*'
    r'(?:统一社会信用代码|纳税人识别号)\s*[:：]?\s*([0-9A-Za-z]{15,20})')
_RE_TRAIN_BUYER_TAX_LOOSE = re.compile(
    r'购买方名称[^\n]{0,40}?(?:统一社会信用代码|纳税人识别号)\s*[:：]?\s*'
    r'([0-9A-Za-z]{15,20})')
# ── 机票行程（备注栏里的一行航班信息）─────────────────────────
# 票面形态（代开机票的备注栏，实测两种）：
#   '2024-11-27 08:15 天津-乌鲁木齐 CZ6894 <旅客姓名>'
#   '<旅客姓名>(1) 2025/09/22 长春-郑州 QW6242'          ← 斜杠日期、无时刻、姓名在前
# 片段之间**可能紧贴也可能多个空格**（PDF 排版不定），所以分隔符一律用 \s*。
# 航班号固定是「2 位大写字母 + 3~4 位数字」（CZ6894 / GS7577 / GJ8980 / QW6242）。
_RE_FLIGHT = re.compile(
    r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})\s*日?'      # 日期（横杠/斜杠/年月日 三种）
    r'(?:\s*(\d{1,2}:\d{2}))?'                          # 时刻（可能没有）
    r'\s*([一-鿿]{2,8})\s*[-－—~至]\s*([一-鿿]{2,8})'    # 出发城市-到达城市
    r'\s*([A-Z]{2}\s?\d{3,4})')                         # 航班号

_VAT_TYPE_MAP = {
    '增值税专用发票': '专用发票',
    '增值税电子普通发票': '电子发票',
    '增值税普通发票': '普通发票',
    '电子发票（铁路电子客票）': '火车票',
    '火车票电子发票': '火车票',
}


def _norm_date(s):
    """'2026年04月29日 18:47' / '2026年05月08日' -> '2026-04-29'。"""
    if not s:
        return ''
    s = s.replace('年', '-').replace('月', '-').replace('日', '').strip()
    s = s.split(' ')[0]
    s = re.sub(r'[^\d\-]', '', s)
    parts = [p for p in s.split('-') if p]
    if len(parts) >= 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except Exception:
            return ''
    return ''


def _norm_time(s):
    """'2026年05月08日 14:30开' / '14:30' -> '14:30'；无时间返回 ''。"""
    if not s:
        return ''
    m = re.search(r'(\d{1,2}):(\d{2})', s)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    return ''


def _short_vat_type(label):
    if not label:
        return '发票'
    if label in _VAT_TYPE_MAP:
        return _VAT_TYPE_MAP[label]
    # 取前 8 个字作为短标签（住宿费 / 餐饮费 / 快递费 ...）
    return label[:8]


def _train_buyer(raw):
    """从铁路电子客票票面取**真实购买方名称**（票面「购买方名称:」一行）。

    2026-10-11 用户第①条：以前这里硬写 `'buyer': ''`，于是 49 张火车票在工作台
    「购买方」列恒为空、核对列却显示「通过」。现在按票面取值。

    ⛔ 绝不用乘客名兜底：`passenger` 是「示例乘客」这类自然人旅客，与「购买方」
       （报销抬头：公司 / 个人）是两回事。票面取不到就返回 ''，不臆造。
    """
    if not raw:
        return ''
    for t in (raw, _normalize_cjk(raw)):
        m = _RE_TRAIN_BUYER.search(t) or _RE_TRAIN_BUYER_LOOSE.search(t)
        if m:
            name = (m.group(1) or '').strip()
            if name:
                return name
    return ''


def train_buyer_tax(raw):
    """从铁路电子客票票面取**购买方税号**（「购买方名称:… 统一社会信用代码:…」同一行）。

    2026-10-11 补：第①条只修了购买方**名称**，漏了税号 —— 46 张火车票的核对列
    于是从「通过」掉成「待确认（税号未识别）」，而票面其实明明白白印着
    `统一社会信用代码:91XXXXXXXXXXXXXXX`。这里按位置把它取回来。

    ⛔ 不用通用增值税解析器的那一套：那条路径按「票面只有一个税号 ⇒ 归销售方」
       处理（第③条修正，对代开机票是对的）。火车票的税号位置是**锚死的**
       —— 紧跟「购买方名称」之后，所以只在这一支里单独取。
    取不到就返回 ''，不臆造。
    """
    if not raw:
        return ''
    for t in (raw, _normalize_cjk(raw)):
        m = _RE_TRAIN_BUYER_TAX.search(t) or _RE_TRAIN_BUYER_TAX_LOOSE.search(t)
        if m:
            code = (m.group(1) or '').strip().upper()
            if code:
                return code
    return ''


def extract_invoice_meta(filepath):
    """提取单张发票的统一元数据。

    返回字典始终包含下列键（缺失补空），调用方按 kind 读取对应字段：
      kind: 'train' | 'hotel' | 'air' | 'taxi' | 'vat' | 'noninvoice' | 'unknown'
      type: 短类型标签（火车票 / 住宿票 / 电子发票 / 住宿费 ...）
      amount / date / seller / buyer / invoice_no
      火车票: from_ to_ train train_date car_no seat_no seat_class seat
              passenger id_no ticket_no special_type
      住宿票: hotel_name guest_name checkin checkout
      增值税: invoice_date tax_amount amount_without_tax seller_tax_id
              buyer_tax_id invoice_code invoice_number check_code
              machine_code drawer tax_rate items
    """
    res = {
        'kind': 'unknown', 'type': '未知', 'amount': 0.0, 'date': '',
        'seller': '', 'buyer': '', 'invoice_no': '',
        'from_': '', 'to_': '', 'train': '', 'train_date': '',
        'train_time': '', 'car_no': '', 'seat_no': '', 'seat_class': '', 'seat': '',
        'passenger': '', 'id_no': '', 'ticket_no': '', 'special_type': None,
        'hotel_name': '', 'guest_name': '', 'checkin': '', 'checkout': '',
        'invoice_date': '', 'tax_amount': 0.0, 'amount_without_tax': 0.0,
        'seller_tax_id': '', 'buyer_tax_id': '', 'invoice_code': '',
        'invoice_number': '', 'check_code': '', 'machine_code': '',
        'drawer': '', 'tax_rate': '', 'items': [], 'filepath': filepath,
        # 网约车行程单专用（其它票种为空）
        'trips': [], 'trip_amount': 0.0, 'trip_count': 0, 'channel': '',
    }
    try:
        raw, err = extract_pdf_texts(filepath)
    except Exception:
        raw, err = '', 'read error'
    if not raw:
        return res

    # ── 非电子发票（行程单 / 报销单据 / 报销辅助材料）—— 导入即分类 ──
    # 用户口径：这类文件**不是发票**，不进金额合计、不进地图轨迹、不当发票用；
    # 能对上某张真发票时作为那张发票的附属材料（见 core/attach.py），对不上就标废弃。
    # 必须排在火车票/增值税之前：行程单文本里含大量 POI 名与酒店名，
    # 走发票分支会把它们抽成销售方/项目名（实测抽出过「云端智能酒店」「中国铁路北京局集团」）。
    if is_noninvoice(raw):
        # ⚠️ amount 故意留 0：这类文件不是发票，不能进发票金额合计
        #    （routes/pool.py 的 total_amount 直接对 amount 求和）。
        #    票面合计放在 trip_amount，只在行程明细里展示。
        ts = parse_trip_sheet(filepath) if is_trip_sheet(raw) else {}
        if ts.get('trips') or ts.get('trip_amount'):
            res.update({
                'kind': 'noninvoice', 'type': '行程单',
                'amount': 0.0, 'trip_amount': float(ts.get('trip_amount') or 0.0),
                'trips': ts.get('trips') or [],
                'trip_count': int(ts.get('trip_count') or 0),
                # 渠道（amap / didi）：挂到发票上时用它显示「高德 / 滴滴」
                'channel': ts.get('channel') or '',
                'date': ts.get('start_date') or '',
                'seller': ts.get('provider') or '',
                'buyer': '',
            })
        else:
            # 行程明细抽不出来（行程明细表 / 费用清单 / 报销凭证等其它辅助材料）：
            # 只登记类别，不带任何金额与行程字段。
            res.update({'kind': 'noninvoice', 'type': '报销单据'})
        # ⛔ 顶层 from_/to_/train 一律留空：routes/pool.py 的 _trip() 只要 from_ 和 to_
        #    都非空就判 mode='transit' 并画进地图。用户明确「打车不连线」，
        #    行程明细只走 trips 列表，避免被当成一条可连线的行程。
        return res

    # ── 火车票（独立解析路径：销售方固定为中国铁路） ──
    if is_train_ticket(raw):
        # 火车票不套用「购买方/销售方」模型：乘客名存于 passenger，不作为 buyer；
        # 核心字段为车次/乘车时间/车站/席位，由 train.parse_train_ticket_text 单独解析。
        # ⚠️ 2026-10-11 用户第①条：`buyer` 以前硬写 `''`，于是 49 张火车票
        #    「购买方」列恒空、核对列却显示「通过」。票面**确实印着**
        #    「购买方名称:」一行（报销抬头，可能是公司也可能是「个人」），改为取真值。
        d = parse_train_ticket_text(raw, os.path.basename(filepath))
        res.update({
            'kind': 'train', 'type': '火车票',
            'amount': float(d.get('amount', 0.0) or 0.0),
            # ⚠️ 2026-10-11 修：这里原本反过来 —— `train_date or invoice_date`，
            #    于是 `row['date']`（列表「开票日期」列 + 日期区间筛选）拿到的是**乘车日期**，
            #    与列头 `web/index.html` 的「开票日期」、筛选提示「按开票日期区间筛选」矛盾。
            #    实测 46/46 全错，偏差最大跨月（`火车\250506_25.00_…pdf`：票面开票
            #    2025-05-06、乘车 2025-04-09 —— 票面文件名与样本目录命名用的都是开票日期）。
            #    改后按「开票日期优先、缺了才退回乘车日期」；乘车日期另有 `train_date` 字段，
            #    行程单/地图读的是 `train_date`（见本文件下方 `_RE_FLIGHT` 注释），不受影响。
            'date': _norm_date(d.get('invoice_date') or d.get('train_date', '')),
            'seller': '中国铁路',
            'buyer': _train_buyer(raw),
            # 购买方税号（2026-10-11 补）：票面同一行印着，锚在「购买方名称」之后取。
            'buyer_tax_id': train_buyer_tax(raw),
            'from_': d.get('from_', '') or '', 'to_': d.get('to_', '') or '',
            'train': d.get('train', '') or '',
            'train_date': d.get('train_date', '') or '',
            'train_time': _norm_time(d.get('train_date', '') or ''),
            'invoice_date': d.get('invoice_date', '') or '',
            'car_no': d.get('car_no', '') or '', 'seat_no': d.get('seat_no', '') or '',
            'seat_class': d.get('seat_class', '') or '', 'seat': d.get('seat', '') or '',
            'passenger': d.get('passenger', '') or '', 'id_no': d.get('id_no', '') or '',
            'ticket_no': d.get('ticket_no', '') or '', 'special_type': d.get('special_type'),
            'invoice_no': d.get('invoice_no', '') or '',
        })
        return res

    # ── 增值税 / 住宿 ──
    info = extract_invoice_fields(raw, source='ofd' if filepath.lower().endswith('.ofd') else '')
    item = info.get('item_name', '') or ''
    remarks = info.get('remarks', '') or ''
    is_hotel = ('住宿' in item) or ('住宿' in remarks)

    if is_hotel:
        checkin, checkout = _extract_stay_dates(remarks, raw)
        inv_no = extract_invoice_no(raw) or info.get('invoice_no') or ''
        _m_dt = _RE_INVOICE_DATE.search(raw)
        inv_date = info.get('invoice_date') or extract_invoice_date(raw) or (
            f"{_m_dt.group(1)}-{int(_m_dt.group(2)):02d}-{int(_m_dt.group(3)):02d}"
            if _m_dt else '')
        res.update({
            'kind': 'hotel', 'type': '住宿票',
            'amount': float(info.get('amount', 0.0) or 0.0),
            'date': _norm_date(inv_date or info.get('invoice_date', '')),
            'seller': info.get('seller_name', '') or '',
            'buyer': info.get('buyer_name', '') or '',
            'hotel_name': info.get('seller_name', '') or '',
            # 购买方是公司开票抬头，不能冒充实际入住人。
            'guest_name': '',
            'checkin': checkin, 'checkout': checkout,
            'invoice_no': inv_no,
            'seller_tax_id': info.get('seller_tax_id', '') or '',
            'buyer_tax_id': info.get('tax_id', '') or '',
            'invoice_date': inv_date,
            'drawer': info.get('drawer', '') or '',
            'tax_amount': float(info.get('tax_amount', 0.0) or 0.0),
            'tax_rate': info.get('tax_rate', '') or '',
            'items': info.get('items', []) or [],
            # 票面实际明细（代订机票 / 网约车服务费 / 住宿服务…）。
            # 与 type（归一化类别）是两回事：type 用于分组，item_name 只用于给用户看清楚买了什么。
            'item_name': info.get('item_name', '') or '',
        })
        return res

    # ── 普通增值税 ──
    nt = raw
    inv_no = info.get('invoice_no') or extract_invoice_no(nt) or ''
    inv_date = info.get('invoice_date') or extract_invoice_date(nt) or ''

    # ── 交通类细分（打车 / 机票）────────────────────────────────────
    # 用户口径：「网约车有没有不重要，只要这类票能解析到**交通费的大类**里面就行」
    # —— 所以这里做的是「落大类」，不是硬贴「打车票」这个名。
    #
    # ⚠️ kind 与标签是**两条独立链**：这里在**票面证据**上匹配（项目名 / 备注 /
    #    销售方 / 星号明细名），绝不拿 _normalize_type_label 的输出当判据 ——
    #    否则以后改标签口径会连带把票种改坏，反之亦然。
    #    词表与 rename 共用同一份真源（_TAXI_KW / _TRANSIT_KW / _AIR_KW），
    #    两边各自加词走偏的旧事故就是这么来的。
    #
    # ⛔ 用 '|' 分隔而不是直接拼接：字段尾 + 字段头会在边界上拼出假关键词
    #    （前端 app.js 的 wbCat 踩过：'汽车出租' 拼 '汽车出租' → 中间出现「出租汽车」）。
    # ⛔ 不拿全文 raw 当证据：票面正文里「出行」这类词太泛，会让住宿票/餐饮票
    #    被误判成打车。只认项目名 / 备注 / 销售方 / 明细名这几处强证据位。
    _star = find_star_item(raw)
    _evi = '|'.join([item, remarks, info.get('seller_name', '') or '',
                     (_star[1] if _star else '')])
    _kind = 'vat'
    if any(k in _evi for k in _AIR_KW):
        _kind = 'air'
    elif any(k in _evi for k in _TAXI_KW + _TRANSIT_KW):
        _kind = 'taxi'

    # 2026-10-12 用户第③条：交通费大类只留三项 —— **火车票 / 机票 / 客运服务**。
    # 打车票、网约车、客车票、包车…凡落到 kind='taxi' 的一律显示「客运服务」，
    # 不再分「打车票 / 客运票」两个名。type 只是**显示口径**；kind 仍是数据口径
    # （'taxi' 不变），查重 / 行程单 / 发票库 / 前端 wbCat 都还认 kind，不受影响。
    _type = _short_vat_type(get_invoice_type_label(raw, info))
    if _kind == 'taxi':
        _type = '客运服务'

    res.update({
        'kind': _kind,
        'type': _type,
        'amount': float(info.get('amount', 0.0) or 0.0),
        'date': _norm_date(inv_date or info.get('invoice_date', '')),
        'seller': info.get('seller_name', '') or '',
        'buyer': info.get('buyer_name', '') or '',
        'invoice_no': inv_no,
        'invoice_date': inv_date,
        'tax_amount': float(info.get('tax_amount', 0.0) or 0.0),
        'amount_without_tax': float(info.get('amount_excl_tax', 0.0) or 0.0),
        'seller_tax_id': info.get('seller_tax_id', '') or '',
        'buyer_tax_id': info.get('tax_id', '') or '',
        'invoice_code': ((lambda m: m.group(1) if m else '')(_RE_INVOICE_CODE.search(nt))
                         # 2026-10-11 用户第②条：老式票的「发票代码:」标签后面是空的，
                         # 代码印在下方监制章带里（12 位独立数字），这里补一级兜底。
                         or (extract_old_guide_fields(nt).get('invoice_code') or '')),
        'invoice_number': inv_no,
        'check_code': (lambda m: m.group(1) if m else '')(_RE_CHECK_CODE.search(nt)),
        'machine_code': (lambda m: m.group(1) if m else '')(_RE_MACHINE_CODE.search(nt)),
        'drawer': info.get('drawer', '') or '',
        'tax_rate': info.get('tax_rate', '') or '',
        'items': info.get('items', []) or [],
        # 同上：票面实际明细，仅供展示，不参与分组。
        'item_name': info.get('item_name', '') or '',
    })

    # ── 机票行程：从备注栏补 from_/to_/train/train_date ──
    # 用户明确「航班跟火车一样要连线」，所以航班复用火车那条地图链路：
    # trainmap.to_itinerary_records 只读 from_/to_/train/train_date/passenger/amount/
    # special_type 七个键，match_station 又有 by_city 城市兜底（321 城），
    # 给城市名就能落到坐标 —— **不需要新建机场经纬度表**。
    #
    # ⛔ 只认备注栏的证据。**绝不用文件名里的航线兜底**：文件名是用户随手起的，
    #    不是票面证据。抽不到起终点就不填、不画线，只在明细里正常显示。
    #    实测覆盖率约 41%（能抽到的才连线），这是票面本身的客观限制。
    _f = _RE_FLIGHT.search(remarks)
    if _f and _f.group(5) != _f.group(6):
        _y, _mo, _d, _hm = _f.group(1), int(_f.group(2)), int(_f.group(3)), _f.group(4)
        res.update({
            'from_': _f.group(5),
            'to_': _f.group(6),
            'train': _f.group(7).replace(' ', ''),      # 航班号放进「车次」槽
            # 与火车票保持同一种日期写法，to_itinerary_records 的 _YEAR_RE 才能取到年份
            'train_date': '%s年%02d月%02d日%s' % (_y, _mo, _d, (' ' + _hm) if _hm else ''),
            'passenger': res.get('passenger') or '',
        })
    return res


def meta_type_short(meta):
    """统计卡片用的超短类型标签。"""
    t = meta.get('type', '') or '未知'
    return t[:8]


def meta_date_tuple(meta):
    """用于按日期筛选 / 排序的可比较元组。"""
    d = meta.get('date', '') or ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', d)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


def meta_date_field(meta, key):
    """按指定原始日期字段（如 invoice_date / train_date）取可比较元组。

    invoice_date / train_date 是 'YYYY年MM月DD日' 原始串，先 _norm_date 再比较。
    """
    s = _norm_date(meta.get(key, '') or '')
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)
