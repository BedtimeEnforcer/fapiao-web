# -*- coding: utf-8 -*-
"""core/trip_html.py — 行程汇总页（交通 + 住宿合并）的 HTML 生成器。

分工
----
* `core/trip_summary.py` 负责**聚合**（四条并列数组 + 统计），
* 本模块负责**渲染**（8 个分区 + 地图 + 图表），
* `core/report_html.py` 提供共享骨架 `render_page` / `SHARED_CSS` / `base_app_js`。

为什么不直接套 `trainmap_html.render_html`
-----------------------------------------
行程统计使用两列卡片，交通和住宿明细各占整行，避免交通项目较多时留下空白侧栏。
`trainmap_html` 只有「图表面板 / 明细面板」两列，没有住宿内容，
且它的 `build_payload` 会把住宿票污染进 `unmatched`（见 6.1）。
因此复用**骨架与零件**（`R.render_page` / `R.money_strip` /
`TH.vehicle_icon_svg` / `TH._MODULE_CSS` / 地图与图表的既有做法），
这里自行拼装分区，并让 `include_map` 与 `include_charts` 真正互相独立。

⚠️ `trainmap_html._APP_JS` 是「地图 + 4 图」一整块字符串，**没法只取其中一半**。
直接复用会让 `include_map=False, include_charts=True` 这种组合退化不掉地图，
而要求里这两个开关必须独立。所以本模块自带一份地图脚本与图表脚本
（做法与实测结论照搬：`animationDurationUpdate: 0` 消除漫游错位、
漫游期间摘掉 `shadowBlur` 保帧率、`transparent` 而非 `opacity:0` 让站名标签可见）。
"""
from __future__ import annotations

import json
import os
import time

from . import report_html as R
from . import trainmap as T
from . import trainmap_html as TH

__all__ = [
    "build_trip_payload_html", "render_trip_html", "build_trip_html",
    "TRIP_FILENAME",
]

#: 文件名固定 —— **不带 (1)(2) 后缀**（要求逐字：文件名固定为「行程汇总单.html」）。
#: `trainmap_html` / `hotel_html` 用的是 `_unique_path`（同名自动加序号），
#: 这里刻意不用：预览目录每次都是新的 `trip_<时间戳>` 子目录，本来也不会撞名。
TRIP_FILENAME = "行程汇总单.html"

_esc = R.esc
_DASH = R.DASH


# ─────────────────────────── 小工具 ───────────────────────────

def _fmt_money(v):
    try:
        return "￥{:,.2f}".format(float(v or 0.0))
    except (TypeError, ValueError):
        return "￥0.00"


def _fmt_km(v):
    if v is None:
        return _DASH                      # km None ≠ 0
    try:
        return f"{float(v):,.1f}"
    except (TypeError, ValueError):
        return _DASH


#: 打车 / 大巴在 `trainmap_html.MODE_W` 里**没有条目**（那边只有 train/air/mixed），
#: 直接用 `TH.w('taxi', ...)` 会退回火车措辞，明细里就会出现
#: 「车次 / 出发站 / 开车时间」这种和打车票对不上的字眼。
#: 所以本页自带一小张补充措辞表，键名与 `MODE_W` 保持同一套。
_MODE_W_EXTRA = {
    "taxi": {"vehicle": "出租车", "no": "订单号", "no_unknown": "订单号未知",
             "time": "用车时间", "depart": "用车", "from": "上车点", "to": "下车点",
             "seat": "座位", "cruise": "行程巡航", "title": "行程汇总单",
             "ticket": "行程单", "day": "用车日", "pax": "乘车人"},
    "bus": {"vehicle": "大巴", "no": "班次", "no_unknown": "班次未知",
            "time": "发车时间", "depart": "发车", "from": "始发站", "to": "到达站",
            "seat": "座位", "cruise": "行程巡航", "title": "行程汇总单",
            "ticket": "车票", "day": "乘车日", "pax": "乘车人"},
}


def _word(mode, key):
    """措辞：先查本页补充表（打车 / 大巴），再退回 `trainmap_html.w`。"""
    extra = _MODE_W_EXTRA.get(mode) or {}
    return extra.get(key) or TH.w(mode, key)


def _taxi_icon_svg(size=30, palette=None):
    """出租车（侧视，车头朝右）。与 `train_icon_svg` 同约定：128×64、平涂不用渐变。"""
    pal = TH._palette(palette)
    body, glass, wheel = "#F3F7FF", "#123A5E", "#1B2740"
    line = pal["accent"]
    inner = (
        '<path d="M6 40C6 35.6 9.6 32 14 32H30L44 20C47 17.6 50.6 16 54.4 16H84'
        'C88 16 91.4 17.6 94.4 20L108 32H114C118.4 32 122 35.6 122 40V42'
        'C122 44.2 120.2 46 118 46H10C7.8 46 6 44.2 6 42Z" '
        'fill="%s" stroke="%s" stroke-width="3.4"/>'
        '<path d="M48 30L58 21H80L92 30Z" fill="%s"/>'
        '<circle cx="34" cy="46" r="10" fill="%s" stroke="%s" stroke-width="3.4"/>'
        '<circle cx="92" cy="46" r="10" fill="%s" stroke="%s" stroke-width="3.4"/>'
        % (body, line, glass, wheel, line, wheel, line))
    return ('<svg class="train-ic" viewBox="0 0 128 64" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>'
            % (size * 2, size, inner))


def _bus_icon_svg(size=30, palette=None):
    """大巴 / 客运（侧视）。"""
    pal = TH._palette(palette)
    body, glass, wheel = "#F3F7FF", "#123A5E", "#1B2740"
    line = pal["accent"]
    win = '<rect x="%d" y="19" width="18" height="12" rx="2" fill="%s"/>'
    inner = (
        '<rect x="8" y="12" width="112" height="36" rx="6" '
        'fill="%s" stroke="%s" stroke-width="3.4"/>'
        % (body, line)
        + (win % (16, glass)) + (win % (40, glass))
        + (win % (64, glass)) + (win % (88, glass))
        + '<circle cx="34" cy="50" r="8" fill="%s" stroke="%s" stroke-width="3"/>' % (wheel, line)
        + '<circle cx="94" cy="50" r="8" fill="%s" stroke="%s" stroke-width="3"/>' % (wheel, line))
    return ('<svg class="train-ic" viewBox="0 0 128 64" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>'
            % (size * 2, size, inner))


def _icon(mode, size, pal):
    """逐条 mode 的图标：air→飞机，taxi→出租车，bus→大巴，其余→高铁车头。"""
    if mode == "taxi":
        return _taxi_icon_svg(size, pal)
    if mode == "bus":
        return _bus_icon_svg(size, pal)
    return TH.vehicle_icon_svg(mode, size, pal)


# ─────────────────────────── 分区 1：汇总条 ───────────────────────────

def _money_html(payload):
    t = payload["totals"]
    cells = [
        ("交通费合计", _fmt_money(t["transitAmount"]), "ok"),
        ("住宿费合计", _fmt_money(t["stayAmount"]), ""),
        ("交通票据", f'{t["linkedCount"] + t["unlinkedCount"]} 张', "dim"),
        ("住宿票据", f'{t["stayCount"]} 张', "dim"),
        ("住宿晚数", (f'{t["nights"]} 晚' if t["nights"] else _DASH), ""),
    ]
    if t["unlinkedCount"]:
        cells.append(("地点信息待补充", f'{t["unlinkedCount"]} 段', "warn"))
    if t["excludedCount"]:
        cells.append(("非发票（不计金额）", f'{t["excludedCount"]} 张', "dim"))
    return R.money_strip("行程报销汇总", "交通 + 住宿", cells,
                         "票面合计（大写）", t["upper"])


# ─────────────────────────── 分区 2：交通明细 ───────────────────────────

def _trip_day(value):
    """行程日期优先；无法解析的票集中放在时间轴末尾。"""
    parts = R.date_parts(value)
    return R.short_date(value) if parts[0] else "日期未知"


def _stay_day(stay):
    for value in (stay.get("checkin"), stay.get("checkout"), stay.get("invoiceDate")):
        if R.date_parts(value)[0]:
            return _trip_day(value)
    return "日期未知"


def _transit_card(leg, palette):
    mode = leg["mode"]
    tags = []
    if leg["kind"] == "改签":
        tags.append('<span class="tag tag-mod">改签</span>')
    if leg.get("abroad"):
        tags.append('<span class="tag tag-out">港澳台/国际</span>')
    if not leg["linkable"] and not leg.get("abroad"):
        tags.append('<span class="tag tag-warn">地点待补充</span>')
    if leg.get("review"):
        tags.append('<span class="tag tag-warn">待复核</span>')
    label = {"train": "火车", "air": "飞机", "taxi": "打车", "bus": "大巴"}.get(mode, "交通")
    detail = leg.get("unlinkedReason") or leg.get("item") or ""
    edit_air = (f'<button class="trip-air-edit" type="button" data-air-edit '
                f'data-from="{_esc(leg["from"]["raw"])}" '
                f'data-to="{_esc(leg["to"]["raw"])}" '
                f'data-passenger="{_esc(leg["passenger"])}" '
                f'data-travel-date="{_esc(leg["date"])}">补充行程信息</button>'
                if mode == "air" and leg.get("vaultId") else "")
    meta_label = leg["passenger"] or detail or leg["fareLabel"] or ""
    return (
        '<div class="tl-item trip-transit-item">'
        '<span class="tl-node"></span><div class="tl-body">'
        '<div class="tl-head">'
        f'{_icon(mode, 22, palette)}<span class="tl-seq">{label}</span>'
        f'<span class="tl-train">{_esc(leg["train"] or _word(mode, "no_unknown"))}</span>'
        + (f'<span class="tl-seat">{_esc(leg["seat"])}</span>' if leg["seat"] else "")
        + "".join(tags)
        + f'<span class="tl-time">{_esc(leg["time"] or "时间未知")}</span></div>'
        '<div class="tl-route">'
        f'<span>{_esc(leg["from"]["raw"] or "未知")}</span>'
        '<span class="arw">→</span>'
        f'<span>{_esc(leg["to"]["raw"] or "未知")}</span>'
        f'<span class="tl-km">{_fmt_km(leg["km"])}{" km" if leg["km"] is not None else ""}</span>'
        '</div>'
        + f'<div class="tl-meta"><span class="tl-meta-label" title="{_esc(detail)}">{_esc(meta_label)}</span>'
        + edit_air
        + f'<span class="amt">{_fmt_money(leg["amount"])}</span>'
        '</div></div></div>'
    )


def _stay_card(stay):
    checkin = stay.get("checkin") if R.date_parts(stay.get("checkin"))[0] else ""
    checkout = stay.get("checkout") if R.date_parts(stay.get("checkout"))[0] else ""
    nights = f'<span class="stay-nights">{stay["nights"]} 晚</span>' if stay.get("nights") else ''
    disabled = '' if stay.get("vaultId") else ' disabled title="请先从工作台导入这张发票"'
    return (
        '<div class="tl-item trip-stay-item">'
        '<span class="tl-node"></span><div class="tl-body">'
        '<div class="tl-head">'
        '<svg class="stay-ic" viewBox="0 0 24 24" width="22" height="22" fill="none" '
        'stroke="currentColor" stroke-width="1.7" aria-hidden="true">'
        '<path d="M4 21V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v16M2 21h20M9 7h2m2 0h2M9 11h2m2 0h2M9 15h2m2 0h2M10 21v-3h4v3"/></svg>'
        '<span class="tl-seq">住宿</span>'
        f'<span class="tl-train">{_esc(stay["hotelName"])}</span>'
        f'{nights}</div>'
        '<div class="stay-date-edit">'
        f'<label>入住 <input type="text" data-stay-display="checkin" value="{_esc(checkin)}" '
        f'placeholder="YYYY-MM-DD" readonly{disabled}>'
        f'<input class="stay-picker" type="date" data-stay-date="checkin" '
        f'value="{_esc(checkin)}" tabindex="-1" aria-hidden="true"{disabled}></label>'
        '<span class="arw">→</span>'
        f'<label>退房 <input type="text" data-stay-display="checkout" value="{_esc(checkout)}" '
        f'placeholder="YYYY-MM-DD" readonly{disabled}>'
        f'<input class="stay-picker" type="date" data-stay-date="checkout" '
        f'value="{_esc(checkout)}" tabindex="-1" aria-hidden="true"{disabled}></label>'
        '</div>'
        '<div class="tl-meta">'
        '<span class="stay-save-state" aria-live="polite"></span>'
        f'<span class="amt">{_fmt_money(stay["amount"])}</span>'
        '</div></div></div>'
    )


def _timeline_html(payload):
    """交通在左、住宿在右，按行程顺序交错排列，不为缺席的一侧造空卡。"""
    transit = payload.get("timeline") or []
    stays = payload.get("stay") or []
    if not transit and not stays:
        return '<div class="empty">本次没有可展示的行程</div>'
    events = []
    for index, leg in enumerate(transit):
        day = _trip_day(leg.get("date"))
        # 同日退房先于交通；有明确入住日的住宿排在同日交通之后。
        events.append((day, 1, leg.get("time") or "99:99", index, "move", leg))
    for index, stay in enumerate(stays):
        day = _stay_day(stay)
        phase = 2 if R.date_parts(stay.get("checkin"))[0] else 0
        events.append((day, phase, "", index, "stay", stay))
    events.sort(key=lambda event: (
        not R.date_parts(event[0])[0], R.date_parts(event[0]),
        event[1], event[2], event[3]))
    palette = payload["palette"]
    blocks = []
    for day, phase, _time, _index, lane, item in events:
        card = _transit_card(item, palette) if lane == "move" else _stay_card(item)
        blocks.append(
            f'<div class="trip-flow-row {lane}" data-date="{_esc(day)}" '
            f'data-phase="{phase}" data-vault-id="{_esc(item.get("vaultId") or "")}" '
            f'data-invoice-date="{_esc(_trip_day(item.get("invoiceDate")) if lane == "stay" else "")}">'
            f'<div class="trip-flow-slot {lane}">'
            f'<div class="trip-flow-date">{_esc(day)}</div>{card}'
            '</div></div>'
        )
    if payload.get("timelineTruncated"):
        blocks.append(f'<div class="det-note">交通行程仅显示前 {len(transit)} 段，'
                      f'共 {payload.get("timelineTotal", len(transit))} 段</div>')
    classes = 'trip-flow' + (' has-transit' if transit else '') + (' has-stay' if stays else '')
    return f'<div class="{classes}">' + "".join(blocks) + '</div>'

def _linked_table_html(payload):
    """连线交通明细表（火车 / 解析完整的机票）。"""
    rows = payload["transit"]["linked"]
    pal = payload["palette"]
    if not rows:
        return '<div class="empty">本次没有可在地图上展示的交通行程</div>'
    body = []
    for leg in rows:
        mode = leg["mode"]
        badge = ""
        if leg["kind"] == "退票":
            badge = '<span class="kbd k-refund">退票费</span>'
        elif leg["kind"] == "改签":
            badge = '<span class="kbd k-change">改签</span>'
        elif leg.get("review"):
            badge = '<span class="kbd k-warn">待复核</span>'
        body.append(
            f'<tr class="{"refund" if leg["kind"] == "退票" else ""}'
            f'{" change" if leg["kind"] == "改签" else ""}">'
            f'<td class="seq">{leg["seq"]}</td>'
            f'<td>{_esc(leg["date"] or "—")}</td>'
            f'<td>{_esc(leg["time"] or "—")}</td>'
            f'<td class="tr">{_icon(mode, 15, pal)}{_esc(leg["train"] or "—")}{badge}</td>'
            f'<td>{_esc(leg["from"]["raw"] or "未知")}</td>'
            f'<td>{_esc(leg["to"]["raw"] or "未知")}</td>'
            f'<td class="num">{_fmt_km(leg["km"])}</td>'
            f'<td class="num">{_fmt_money(leg["amount"])}</td>'
            f'<td class="inv">{_esc(leg["invoiceNo"] or "—")}</td>'
            '</tr>')
    total = sum(x["amount"] for x in rows)
    return (
        '<table class="dt"><thead><tr>'
        '<th>序号</th><th>出发日期</th><th>时间</th><th>车次 / 航班</th>'
        '<th>出发地</th><th>到达地</th><th>里程(km)</th><th>金额</th><th>发票号码</th>'
        '</tr></thead><tbody>'
        + "".join(body)
        + f'</tbody><tfoot><tr><td colspan="7">交通行程合计 {len(rows)} 段</td>'
          f'<td class="num">{_fmt_money(total)}</td><td></td></tr></tfoot></table>')


def _unlinked_table_html(payload):
    """不连线交通明细表 —— **必须有「为什么不连线」列**（用户口径）。"""
    rows = payload["transit"]["unlinked"]
    pal = payload["palette"]
    if not rows:
        return ""
    body = []
    for leg in rows:
        when = _esc(leg["date"] or "—")
        if leg.get("time"):
            when += f' <span class="tl-km">{_esc(leg["time"])}</span>'
        body.append(
            '<tr>'
            f'<td class="seq">{leg["seq"]}</td>'
            f'<td>{when}</td>'
            f'<td class="tr">{_icon(leg["mode"], 15, pal)}{_esc(leg["train"] or "—")}</td>'
            f'<td>{_esc(leg["from"]["raw"] or "—")}</td>'
            f'<td>{_esc(leg["to"]["raw"] or "—")}</td>'
            f'<td class="num">{_fmt_money(leg["amount"])}</td>'
            f'<td class="inv">{_esc(leg["invoiceNo"] or "—")}</td>'
            f'<td class="why">{_esc(leg.get("unlinkedReason") or "行程信息不完整")}</td>'
            '</tr>')
    total = sum(x["amount"] for x in rows)
    return (
        '<table class="dt"><thead><tr>'
        '<th>序号</th><th>日期 / 时间</th><th>车次 / 航班 / 订单号</th>'
        '<th>出发地 / 上车点</th><th>到达地 / 下车点</th>'
        '<th>金额</th><th>发票号码</th><th>行程说明</th>'
        '</tr></thead><tbody>'
        + "".join(body)
        + f'</tbody><tfoot><tr><td colspan="5">其他交通合计 {len(rows)} 段</td>'
          f'<td class="num">{_fmt_money(total)}</td><td colspan="2"></td></tr></tfoot></table>')


def _detail_note(payload):
    t = payload["totals"]
    bits = []
    if t["excludedCount"]:
        bits.append(f'另有 <b>{t["excludedCount"]}</b> 张其他材料，未计入票面金额')
    if t["nightUnknown"]:
        bits.append(f'<b>{t["nightUnknown"]}</b> 张住宿票缺少入住或退房日期，晚数暂无法计算')
    if payload.get("reviewCount"):
        bits.append(f'<b>{payload["reviewCount"]}</b> 张票面信息待核实')
    if payload.get("detailsTruncated"):
        bits.append(f'明细超过 {len(payload["details"])} 条，已截断')
    return "；".join(bits) + "。" if bits else ""


# ─────────────────────────── 分区 3：住宿明细 ───────────────────────────

def _stay_table_html(payload):
    rows = payload["stay"]
    if not rows:
        return ""
    body = []
    for s in rows:
        nights = f'{s["nights"]} 晚' if s["nights"] else _DASH
        body.append(
            '<tr>'
            f'<td class="seq">{s["seq"]}</td>'
            f'<td>{_esc(s["checkin"])}</td>'
            f'<td>{_esc(s["checkout"])}</td>'
            f'<td class="num">{nights}</td>'
            f'<td>{_esc(s["hotelName"])}'
            + (f'<span class="city-tag">{_esc(s["city"])}</span>' if s["city"] else "")
            + '</td>'
            f'<td>{_esc(s["buyer"] or "—")}</td>'
            f'<td class="num">{_fmt_money(s["amount"])}</td>'
            f'<td class="inv">{_esc(s["invoiceNo"] or "—")}</td>'
            '</tr>')
    total = sum(x["amount"] for x in rows)
    return (
        '<table class="dt"><thead><tr>'
        '<th>序号</th><th>入住日期</th><th>退房日期</th><th>晚数</th>'
        '<th>酒店名称</th><th>购买方</th><th>金额</th><th>发票号码</th>'
        '</tr></thead><tbody>'
        + "".join(body)
        + f'</tbody><tfoot><tr><td colspan="6">住宿合计 {len(rows)} 张</td>'
          f'<td class="num">{_fmt_money(total)}</td><td></td></tr></tfoot></table>')


def _stay_vendor_html(payload):
    rows = payload.get("stayVendors") or []
    if not rows:
        return '<div class="empty">没有住宿开票单位</div>'
    top = max([r["amount"] for r in rows] or [1.0]) or 1.0
    out = []
    for i, r in enumerate(rows[:12], 1):
        pct = max(2.0, min(100.0, r["amount"] / top * 100.0))
        out.append(
            '<div class="rank-row">'
            f'<span class="rk">{i}</span>'
            f'<span class="nm">{_esc(r["name"])}</span>'
            f'<span class="bar"><span data-w="{pct:.1f}%"></span></span>'
            f'<span class="vl">{_fmt_money(r["amount"])}</span>'
            f'<span class="ct">{r["count"]} 张</span>'
            '</div>')
    return '<div class="rank">' + "".join(out) + '</div>'


# ─────────────────────────── 分区 4：未匹配 / 港澳台国际段 ───────────────────────────

def _unmatched_html(payload):
    unmatched = payload.get("unmatched") or []
    abroad = payload.get("abroad") or []
    excluded = payload.get("excluded") or []
    if not (unmatched or abroad or excluded):
        return ""
    parts = ['<section class="panel no-print" style="margin-top:18px">'
             '<div class="panel-head"><h2>需要留意的行程与票据</h2></div>']
    if unmatched:
        rows = []
        for x in unmatched:
            rows.append(
                '<div class="note warn"><b>'
                f'{_esc(x["from"])} → {_esc(x["to"])}</b>'
                f'（{_esc(TH.w(x["mode"], "vehicle"))}'
                + (f' · {_esc(x["train"])}' if x["train"] else "")
                + f' · {_esc(x["date"] or "日期未知")}'
                + f'）<br><span class="why">{_esc(x["reason"])}</span></div>')
        parts.append('<div class="notes">' + "".join(rows) + '</div>')
    if abroad:
        rows = []
        for x in abroad:
            rows.append(
                '<div class="note info"><b>'
                f'{_esc(x["from"]["raw"] or "未知")} → {_esc(x["to"]["raw"] or "未知")}</b>'
                f'（{_esc(TH.w(x["mode"], "vehicle"))}'
                + (f' · {_esc(x["date"])}' if x["date"] else "")
                + f' · {_fmt_money(x["amount"])}）'
                '<br><span class="why">当前地图未展示港澳台或境外行程</span></div>')
        parts.append('<div class="notes">' + "".join(rows) + '</div>')
    if excluded:
        rows = []
        for x in excluded:
            rows.append(
                '<div class="note"><b>'
                f'{_esc(x["filename"])}</b>（{_esc(x["type"] or "未知票种")} · 金额不计入合计）'
                f'<br><span class="why">{_esc(x["reason"])}</span></div>')
        parts.append('<div class="notes">' + "".join(rows) + '</div>')
    parts.append('</section>')
    return "".join(parts)


# ─────────────────────────── 页面主体 ───────────────────────────

_TRIP_CSS = r"""
/* ---------- 分区标题与自适应统计卡片 ---------- */
.split-head{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:600;
  letter-spacing:.4px;padding:2px 0 10px;margin:-2px 0 12px;
  border-bottom:1px solid var(--panel-border)}
.split-head .dot{width:7px;height:7px;border-radius:50%;flex:none}
.split-head .cnt{margin-left:auto;font-weight:500;color:var(--text-faint);font-size:11.5px}
.split-head.move .dot{background:var(--accent);box-shadow:0 0 9px 1px rgba(56,189,248,.65)}
.split-head.stay .dot{background:var(--station);box-shadow:0 0 9px 1px rgba(255,194,77,.55)}
.trip-chart-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.trip-chart-grid > .panel{min-width:0}
.trip-chart-grid > .panel:last-child:nth-child(odd){grid-column:1/-1}
.trip-stay-card h2::before{background:var(--station)}
.trip-chart-grid .rank-row{grid-template-columns:20px minmax(80px,1.2fr) minmax(35px,1fr) auto auto;
  gap:8px;font-size:12px}
.trip-chart-grid .rank-row .rk{color:var(--text-faint)}
.trip-chart-grid .rank-row .bar{margin-top:0}
.trip-chart-grid .rank-row .vl,.trip-chart-grid .rank-row .ct{white-space:nowrap;
  font-variant-numeric:tabular-nums}
.trip-chart-grid .rank-row .ct{color:var(--text-dim)}
.trip-journey-panel{min-width:0}
.trip-totals{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:4px 0 22px}
.trip-totals>div{display:flex;justify-content:space-between;gap:12px;padding:12px 15px;
  border:1px solid var(--panel-border);border-radius:11px;background:rgba(255,255,255,.025)}
.trip-totals span{color:var(--text-dim);font-size:12px}
.trip-totals b{font-size:13px;font-weight:650;font-variant-numeric:tabular-nums}
.trip-flow-row{display:grid;grid-template-columns:minmax(0,1fr);break-inside:avoid}
.trip-flow.has-transit.has-stay .trip-flow-row{grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.trip-flow.has-transit.has-stay .trip-flow-row.stay .trip-flow-slot{grid-column:2}
.trip-flow-row + .trip-flow-row{margin-top:10px}
.trip-flow.has-transit.has-stay .trip-flow-row.move + .trip-flow-row.stay,
.trip-flow.has-transit.has-stay .trip-flow-row.stay + .trip-flow-row.move{margin-top:-48px}
.trip-flow-slot{min-width:0}
.trip-flow-date{margin:0 0 6px 26px;color:var(--accent-soft);font-size:12px;font-weight:700;
  font-variant-numeric:tabular-nums}
.trip-flow-row.stay .trip-flow-date{color:var(--station)}
.trip-flow-slot .tl-item{padding-bottom:0}
.trip-flow-slot .tl-item::before{display:none}
.trip-flow-slot .tl-body{min-width:0}
.trip-flow-slot .tl-body{min-height:108px}
.trip-flow-slot .tl-route{flex-wrap:wrap;word-break:break-word}
.trip-flow-row.stay .tl-node{background:var(--station);box-shadow:0 0 0 3px rgba(255,194,77,.16)}
.trip-flow-row.stay .stay-ic{color:var(--station)}
.trip-flow-row.stay .tl-body{background:rgba(255,194,77,.035)}
.trip-flow-row.stay .tl-item:hover .tl-body{border-color:var(--station)}
.trip-stay-item .stay-nights{margin-left:auto;color:var(--text-dim);font-size:11px;white-space:nowrap}
.trip-stay-item .stay-date-edit{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-top:9px}
.trip-stay-item .stay-date-edit label{display:flex;align-items:center;gap:5px;
  color:var(--text-dim);font-size:11px;white-space:nowrap;position:relative}
.trip-stay-item .stay-date-edit input[data-stay-display]{width:112px;min-width:0;padding:3px 5px;
  color:var(--text);background:var(--panel);border:1px solid var(--panel-border);
  border-radius:5px;font:inherit;font-variant-numeric:tabular-nums;cursor:pointer}
.trip-stay-item .stay-date-edit input[data-stay-display]:focus{outline:1px solid var(--accent-soft)}
.trip-stay-item .stay-date-edit input:disabled{opacity:.7;cursor:not-allowed}
.trip-stay-item .stay-date-edit input.stay-picker{position:absolute;right:0;bottom:0;
  width:1px;height:1px;opacity:0;pointer-events:none}
.trip-stay-item .tl-meta{margin-top:6px}
.stay-save-state{font-size:11px;color:var(--accent-soft)}
.stay-save-state.error{color:#ef6f6f}
.trip-flow-slot .tl-meta-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trip-air-edit{flex:none;border:0;background:none;color:var(--accent-soft);font:inherit;font-size:11px;cursor:pointer;padding:0;text-decoration:underline}
.trip-air-modal{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.72);display:grid;place-items:center;padding:16px}
.trip-air-panel{width:min(520px,100%);max-height:90vh;overflow:auto;padding:20px;border:1px solid var(--panel-border);border-radius:12px;background:var(--panel);color:var(--text)}
.trip-air-panel h3{margin:0 0 12px;font-size:16px}.trip-air-panel label{display:block;margin:10px 0 4px;color:var(--text-dim);font-size:12px}
.trip-air-panel select,.trip-air-panel input{width:100%;box-sizing:border-box;padding:8px;margin-top:4px;color:var(--text);background:var(--panel);border:1px solid var(--panel-border);border-radius:6px;font:inherit}
.trip-air-panel .airport-picks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.trip-air-panel .airport-picks select{min-width:0}
.trip-air-panel .trip-date-box{position:relative}.trip-air-panel .trip-date-box [type=date]{position:absolute;bottom:0;right:0;width:1px;height:1px;opacity:0;pointer-events:none}
.trip-air-panel .air-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}.trip-air-panel button{padding:7px 14px;border:1px solid var(--panel-border);border-radius:6px;color:var(--text);background:var(--panel);cursor:pointer}
.trip-air-panel button[data-air-save]{background:var(--accent);color:#06111c}.trip-air-panel .air-error{color:#ef6f6f;font-size:12px}
.trip-table-view .sub-h{margin:18px 0 10px}
.trip-table-view .sub-h:first-child{margin-top:0}
@media (max-width:760px){
  .trip-flow.has-transit.has-stay .trip-flow-row,.trip-totals{grid-template-columns:minmax(0,1fr)}
  .trip-flow.has-transit.has-stay .trip-flow-row.stay .trip-flow-slot{grid-column:1}
  .trip-flow.has-transit.has-stay .trip-flow-row.move + .trip-flow-row.stay,
  .trip-flow.has-transit.has-stay .trip-flow-row.stay + .trip-flow-row.move{margin-top:10px}
  .trip-flow-slot .tl-route{font-size:13px}
}
@media print{
  #timeline{display:block!important}
  #detailTable{display:none!important}
  .trip-stay-item .stay-date-edit input{color-scheme:light;background:#fff}
  .stay-save-state{display:none}
}
@media (max-width:900px){
  .trip-chart-grid{grid-template-columns:minmax(0,1fr)}
  .trip-chart-grid > .panel:last-child:nth-child(odd){grid-column:auto}
}

/* 明细表里的「为什么不连线」列 */
.dt td.why{color:var(--text-faint);font-size:11.5px;max-width:280px;white-space:normal;line-height:1.5}
.city-tag{margin-left:7px;padding:1px 7px;border-radius:999px;font-size:11px;
  color:var(--text-dim);border:1px solid var(--panel-border)}

/* 住宿分区整体（只有住宿时铺满整行） */
.col > .panel + .panel{margin-top:18px}

/* 共享骨架的 KPI 栅格是按「正好 5 张卡」设计的，而本页宽屏最多 6 张、只有住宿时
   3 张 —— 沿用 5 列会让第 6 张单独掉到第二行、右侧留一大片空。宽屏改成 3 列：
   6 张即 3+3 两行铺满，3 张正好一行铺满，且 1181~1300px 段不会像 6 列那样把
   「¥21,874.13」挤到折行。窄屏仍交给共享骨架自己的 3 列 / 2 列断点。 */
@media (min-width:1181px){
  .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
}

/* 共享骨架在 1280px 就把 .grid 塌成单列，而用户要求「≤1180px 才堆叠」。
   用同优先级 + 后置声明把 1181~1280px 这段顶回双列（.grid.solo 有两个类，
   特异性更高，仍保持单列不受影响）。列宽沿用骨架的 minmax()，不写死。 */
@media (min-width:1181px) and (max-width:1280px){
  .grid{grid-template-columns:minmax(0,1.62fr) minmax(0,1fr)}
}
"""


def _mix_html(payload):
    """「交通构成」条形（纯 CSS，不占 ECharts）。

    设计 6.7 把它列为新增图表 5。这里刻意用共享骨架的 `.rank` 条形而不是
    ECharts：它只有 3~4 个分类，用 ECharts 要多付一整块图表的初始化与
    1003KB 的内联运行库（见 `_body_charts` 的体积说明），而观感与既有
    「开单单位」条形完全一致。
    """
    all_legs = payload["transit"]["linked"] + payload["transit"]["unlinked"]
    # 一行的「段数」与「金额」必须用**同一个**分桶谓词。原先段数按
    # `mode not in ('taxi','bus')`、金额按 `_mode_bucket(x)==mode` 各算一遍，
    # 于是「其他不连线」会显示成「4 段 ￥0.00」——那 4 段其实是备注栏
    # 解析不全的机票，钱被算进了「飞机」行。统一走 `_mode_bucket`。
    mix = []
    for bucket, name in (("train", "火车 / 高铁"), ("air", "飞机"),
                         ("taxi", "打车 / 网约车"), ("bus", "大巴 / 客运"),
                         ("", "其他不可归类")):
        legs = [x for x in all_legs if _mode_bucket(x) == bucket]
        if legs:
            mix.append((name, len(legs), round(sum(x["amount"] for x in legs), 2)))
    if not mix:
        return '<div class="empty">本次没有交通票据</div>'
    top = max(m[1] for m in mix)
    out = []
    for i, (name, n, amt) in enumerate(mix, 1):
        pct = max(3.0, min(100.0, n / top * 100.0))
        out.append(
            '<div class="rank-row">'
            f'<span class="rk">{i}</span>'
            f'<span class="nm">{_esc(name)}</span>'
            f'<span class="bar"><span data-w="{pct:.1f}%"></span></span>'
            f'<span class="vl">{n} 段</span>'
            f'<span class="ct">{_fmt_money(amt)}</span>'
            '</div>')
    return '<div class="rank">' + "".join(out) + '</div>'


def _mode_bucket(leg):
    """构成图的分桶键（火车 / 飞机 / 打车 / 大巴 / 其他）。"""
    m = leg.get("mode") or ""
    return m if m in ("train", "air", "taxi", "bus") else ""


def _body_charts(payload, include_map, include_charts):
    """统计图表按卡片排布；交通图较多时住宿卡片不再独占一根空长列。

    ECharts 只在「有可连线的交通段」时才内联：4 张图（高频线路 / 城市排行 /
    出行时间分布 / 里程概览）的数据全部来自 `linked[]`，没有连线段时它们
    只是 4 个空壳，却要为此多付 1003KB 的运行库。住宿侧的「酒店金额 Top」
    与「交通构成」用共享 CSS 的 `.rank` 条形表达（`hotel_html` 的住宿页
    本来就是零 ECharts 的 68KB 静态页，住宿侧不该因为合并而变重）。
    """
    linked = payload["transit"]["linked"]
    unlinked = payload["transit"]["unlinked"]
    has_move = bool(linked)
    has_stay = bool(payload.get("stay"))
    cards = []

    if include_charts and has_move:
        cards.extend([
            '<div class="panel no-print">'
              '<div class="panel-head"><h2>高频线路</h2></div>'
              '<div id="chartRoute" class="chart"></div></div>',
            '<div class="panel no-print">'
              '<div class="panel-head"><h2>城市到访</h2></div>'
              '<div id="chartCity" class="chart sm"></div></div>',
            '<div class="panel no-print">'
              '<div class="panel-head"><h2>出行时间分布</h2></div>'
              '<div id="chartDist" class="chart md"></div></div>',
            '<div class="panel no-print">'
              '<div class="panel-head"><h2>里程概览</h2></div>'
              '<div id="chartKm" class="chart sm"></div></div>',
            '<div class="panel">'
              '<div class="panel-head"><h2>交通费构成</h2></div>'
            + _mix_html(payload) + '</div>',
        ])

    if has_stay:
        cards.append(
            '<div class="panel trip-stay-card">'
              '<div class="panel-head"><h2>住宿酒店金额</h2></div>'
              + _stay_vendor_html(payload) + '</div>')

    out = ""
    if include_map and payload.get("includeMap"):
        cruise = TH.w(payload.get("mode") or "train", "cruise")
        out += (
            '  <section class="grid solo" style="margin-top:18px">\n'
            '    <div class="col">\n'
            '      <div class="panel map-panel no-print">\n'
            '        <div class="panel-head">\n'
            '          <h2>行程流转地图</h2>\n'
            '          <div class="tools">\n'
            f'            <button class="btn on" id="btn-flow">{_esc(cruise)}</button>\n'
            '            <button class="btn on" id="btn-label">站名标签</button>\n'
            '            <button class="btn" id="btn-reset">重置视图</button>\n'
            '          </div>\n'
            '        </div>\n'
            '        <div id="map"></div>\n'
            '        <div id="cruise"></div>\n'
            '        __MAP_INSET__\n'
            '        __MAP_LEGEND__\n'
            '      </div>\n'
            '    </div>\n'
            '  </section>\n')
    if cards:
        count = " · ".join(part for part in (
            f'{len(linked) + len(unlinked)} 段交通' if linked or unlinked else "",
            f'{len(payload["stay"])} 张住宿' if has_stay else "",
        ) if part)
        out += ('  <section style="margin-top:18px">\n'
                + _split_head("move", "行程统计", count)
                + '<div class="trip-chart-grid">\n' + "\n".join(cards)
                + '\n</div>\n  </section>\n')
    return out


def _split_head(kind, title, count):
    return (f'<div class="split-head {kind}"><span class="dot"></span>{_esc(title)}'
            f'<span class="cnt">{_esc(count)}</span></div>')


def _body_detail(payload):
    """按日期交错排列交通与住宿，分别放在左右两侧。"""
    linked = payload["transit"]["linked"]
    unlinked = payload["transit"]["unlinked"]
    stays = payload.get("stay") or []
    if not (linked or unlinked or stays):
        return ""
    totals = payload["totals"]
    move_n = len(linked) + len(unlinked)
    move_table = ""
    if move_n:
        move_table = ('<div class="sub-h">交通票据明细</div>'
                      + _linked_table_html(payload)
                      + ('<div class="sub-h">其他交通行程</div>' + _unlinked_table_html(payload)
                         if unlinked else ""))
    stay_table = ('<div class="sub-h">住宿票据明细</div>' + _stay_table_html(payload)
                  if stays else "")
    return (
        '<section class="trip-journeys" style="margin-top:24px">'
        + _split_head("move", "按日期排列的行程", f'{move_n} 段交通 · {len(stays)} 张住宿')
        + '<div class="panel trip-journey-panel">'
          '<div class="panel-head"><h2>交通与住宿行程</h2>'
          '<div class="tools">'
          '<button class="btn on" id="btn-view-tl">日期视图</button>'
          '<button class="btn" id="btn-view-tb">票据明细表</button>'
          '<button class="btn" id="btn-print">打印 / 存 PDF</button>'
          '</div></div>'
        + '<div class="trip-totals">'
          f'<div><span>交通</span><b>{move_n} 段 · {_fmt_money(totals["transitAmount"])}</b></div>'
          f'<div><span>住宿</span><b>{len(stays)} 张 · {_fmt_money(totals["stayAmount"])}</b></div>'
          '</div>'
        + f'<div class="tl" id="timeline">{_timeline_html(payload)}</div>'
        + f'<div class="trip-table-view dt-wrap" id="detailTable" hidden>{move_table}{stay_table}</div>'
        + (f'<div class="det-note">{_detail_note(payload)}</div>'
           if _detail_note(payload) else "")
        + '</div></section>'
    )

def _upper_of(v):
    from . import parse as _parse
    return _parse.amount_upper(v)


# ─────────────────────────── 地图脚本（照搬既有实测结论）───────────────────────────

_MAP_JS = r"""
const GEO = __GEOJSON__;
echarts.registerMap('china_full', GEO);
const mapChart = echarts.init($('#map'), null, {renderer: 'canvas'});
const maxVisit = Math.max(1, ...P.stations.map(s => s.count));
const routeColor = r => r > .66 ? pal.route_hi : (r > .3 ? pal.route_mid : pal.route_lo);

const flowData = P.routes.map(r => ({
  coords: r.c, value: r.count, name: r.name || (r.a + ' → ' + r.b),
  extra: r.km + ' km',
  lineStyle: {color: routeColor(r.ratio), width: .9 + 2.6 * Math.pow(r.ratio, .9),
              opacity: .4 + .4 * r.ratio, curveness: .26, cap: 'round',
              shadowColor: 'rgba(56,189,248,.35)', shadowBlur: 2.5 + 3.5 * r.ratio}
}));

/* 单车巡航：全图只留一个行进光点，逐段切换贴图（火车段车头 / 航班段飞机） */
const journey = P.journey || [];
const segN = journey.length;
const totalKm = journey.reduce((s, x) => s + (x.km || 0), 0) || 1;
const cruiseSpan = Math.min(42, Math.max(14, segN * .85));
const cruiseSpeed = totalKm / cruiseSpan;
const segPeriod = km => Math.min(2.8, Math.max(.45, (km || 40) / cruiseSpeed));
const CRUISE_SERIES = ['__CRUISE_A__', '__CRUISE_B__'];
let jIdx = 0, jTimer = null, jOn = true, jFlip = 0;

function cruiseHTML(){
  const el = $('#cruise');
  if (!el) return;
  if (!segN) { el.innerHTML = '<span class="c-dim">本次行程不足以生成巡航</span>'; return; }
  const s = journey[jIdx] || {};
  el.innerHTML =
    '<span class="c-run"><i></i>' + (s.cruise || '__CRUISE__') + '</span>' +
    '<b>' + (jIdx + 1) + '<span class="c-dim"> / ' + segN + '</span></b>' +
    '<span class="c-seg">' + (s.train || '__NO_UNKNOWN__') + '　' + (s.a || '?') + ' → ' + (s.b || '?') + '</span>' +
    '<span class="c-km">' + (s.km != null ? fmt(s.km, 0) + ' km' : '') + (s.date ? '　' + s.date : '') + '</span>';
}
function cruiseStop(){
  clearTimeout(jTimer); jTimer = null;
  const off = {data: [], effect: {show: false}};
  mapChart.setOption({series: [{name: CRUISE_SERIES[0], ...off},
                               {name: CRUISE_SERIES[1], ...off}]});
  const el = $('#cruise'); if (el) el.classList.add('off');
}
function cruiseTo(i){
  if (!segN || !jOn) return;
  jIdx = ((i % segN) + segN) % segN;
  const s = journey[jIdx], T = segPeriod(s.km);
  jFlip ^= 1;
  const on = CRUISE_SERIES[jFlip], off = CRUISE_SERIES[jFlip ^ 1];
  mapChart.setOption({series: [
    {name: off, data: [], effect: {show: false}},
    {name: on, data: [{
        coords: s.c, value: s.km, name: s.a + ' → ' + s.b, kind: 'journey',
        extra: (s.train ? s.train + ' · ' : '') + (s.pax ? s.pax + ' · ' : '') +
               (s.date || '') + ' · ' + fmt(s.km, 0) + ' km',
        lineStyle: {color: pal.route_flow, width: 2.2, opacity: 1, curveness: .26,
                    cap: 'round', shadowColor: 'rgba(56,189,248,.9)', shadowBlur: 12}}],
     effect: {show: true, period: T, trailLength: .3,
              symbol: 'image://' + ((P.markers && P.markers[s.mode]) || P.trainMarker),
              symbolSize: 15, color: pal.route_flow}}
  ]});
  cruiseHTML();
  clearTimeout(jTimer);
  jTimer = setTimeout(() => cruiseTo(jIdx + 1), T * 1000 + 60);
}

const stationData = P.stations.map(s => ({
  name: s.name, value: [s.lon, s.lat, s.count], city: s.city,
  symbolSize: 3.2 + 6.6 * Math.sqrt(s.count / maxVisit)
}));
const labelData = P.stations.slice(0, 10).map(s => ({
  name: s.name, value: [s.lon, s.lat, s.count], city: s.city
}));

function mapOption(){
  return {
    backgroundColor: 'transparent',
    /* 入场动画保留 900ms，但「更新动画」必须归零：漫游与每秒一次的巡航 setOption
       都会触发位置补间，线条圆点会追不上底图（实测错位 20.7% → 1.1%）。 */
    animationDuration: 900, animationDurationUpdate: 0,
    animationEasing: 'cubicOut', animationEasingUpdate: 'linear',
    tooltip: {
      trigger: 'item', confine: true,
      backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border, borderWidth: 1,
      padding: [10, 13], textStyle: {color: pal.text, fontSize: 12.5},
      extraCssText: 'border-radius:12px;box-shadow:0 14px 40px rgba(0,0,0,.6);backdrop-filter:blur(8px)',
      formatter: p => {
        if (p.seriesType === 'effectScatter' || p.seriesType === 'scatter') {
          const v = p.value || [];
          return '<b style="color:' + pal.accent_soft + '">' + p.name + '</b>' +
                 (p.data && p.data.city ? '<br><span style="color:' + pal.text_dim + '">城市：' + p.data.city + '</span>' : '') +
                 '<br>到访 <b>' + (v[2] || 0) + '</b> 次';
        }
        if (p.seriesType === 'lines') {
          if (p.data && p.data.kind === 'journey') {
            return '<b style="color:' + pal.accent_soft + '">' + p.name + '</b>' +
                   '<br><span style="color:' + pal.text_dim + '">正在行进 · ' + (p.data.extra || '') + '</span>';
          }
          return '<b style="color:' + pal.accent_soft + '">' + (p.data.name || p.name) + '</b>' +
                 '<br><span style="color:' + pal.text_dim + '">乘坐 ' + (p.value || 0) + ' 次 · ' +
                 (p.data.extra || '') + '</span>';
        }
        return p.name || '';
      }
    },
    geo: {
      map: 'china_full', roam: true, zoom: 1.0, silent: false,
      layoutCenter: ['50%', '50%'], layoutSize: '100%',
      scaleLimit: {min: .7, max: 12},
      itemStyle: {areaColor: pal.map_fill, borderColor: pal.map_border, borderWidth: .75,
                  shadowColor: 'rgba(34,211,238,.30)', shadowBlur: 14, shadowOffsetY: 2},
      emphasis: {
        itemStyle: {areaColor: '#1B2C4C', borderColor: '#5B8FD6', borderWidth: 1.1,
                    shadowColor: 'rgba(34,211,238,.55)', shadowBlur: 20},
        label: {show: true, color: pal.text, fontSize: 12, fontWeight: 600,
                textShadowColor: '#000', textShadowBlur: 6}
      },
      select: {disabled: true},
      label: {show: false},
      regions: [{
        name: __DASH_NAME_JSON__,
        itemStyle: {areaColor: pal.map_dash, borderColor: pal.map_dash, borderWidth: 0, shadowBlur: 0},
        emphasis: {itemStyle: {areaColor: '#BCD0EC', borderColor: '#BCD0EC'}, label: {show: false}}
      }]
    },
    series: [
      {name: '线路', type: 'lines', coordinateSystem: 'geo', zlevel: 2,
       polyline: false, silent: false, data: flowData,
       lineStyle: {curveness: .26}, progressive: 0},
      {name: '__CRUISE_A__', type: 'lines', coordinateSystem: 'geo', zlevel: 7, silent: true,
       polyline: false, data: [],
       effect: {show: false, period: 1.4, trailLength: .3,
                symbol: 'image://' + P.trainMarker, symbolSize: 15, color: pal.route_flow},
       lineStyle: {curveness: .26}},
      {name: '__CRUISE_B__', type: 'lines', coordinateSystem: 'geo', zlevel: 7, silent: true,
       polyline: false, data: [],
       effect: {show: false, period: 1.4, trailLength: .3,
                symbol: 'image://' + P.trainMarker, symbolSize: 15, color: pal.route_flow},
       lineStyle: {curveness: .26}},
      {name: '车站', type: 'effectScatter', coordinateSystem: 'geo', zlevel: 3,
       rippleEffect: {brushType: 'stroke', scale: 1.45, period: 5},
       itemStyle: {color: pal.station, borderColor: pal.station_ring, borderWidth: 1.6,
                   shadowColor: 'rgba(255,194,77,.8)', shadowBlur: 7},
       labelLayout: {hideOverlap: true},
       data: stationData},
      {name: '站名', type: 'scatter', coordinateSystem: 'geo', zlevel: 6,
       symbolSize: 1, silent: true,
       /* ⚠️ 不能用 itemStyle.opacity:0 —— label 会继承它，站名一起变透明。
          用 transparent 填充让圆点不可见、但 opacity 保持 1。 */
       itemStyle: {color: 'transparent'},
       label: {show: true, position: 'right', distance: 6, align: 'left', color: pal.text,
               fontSize: 12, fontWeight: 600, textShadowColor: 'rgba(0,0,0,.95)',
               textShadowBlur: 6, formatter: p => p.name},
       labelLayout: {hideOverlap: true},
       data: labelData}
    ]
  };
}
mapChart.setOption(mapOption());
cruiseTo(0);

/* 漫游性能：青色外发光是 34 个省多边形各带一遍阴影，拖拽时逐帧重算（帧率 60→31）。
   漫游期间先摘掉，停手 260ms 再装回，肉眼几乎察觉不到。 */
let roamIdle = null;
mapChart.on('georoam', function(){
  if (!roamIdle) {
    mapChart.setOption({geo: {itemStyle: {shadowBlur: 0, shadowColor: 'rgba(0,0,0,0)'}}});
  }
  clearTimeout(roamIdle);
  roamIdle = setTimeout(function(){
    roamIdle = null;
    mapChart.setOption({geo: {itemStyle: {shadowBlur: 14, shadowColor: 'rgba(34,211,238,.30)'}}});
  }, 260);
});

let labelOn = true;
$('#btn-flow').onclick = function(){
  jOn = !jOn; this.classList.toggle('on', jOn);
  if (jOn) cruiseTo(jIdx); else cruiseStop();
};
$('#btn-label').onclick = function(){
  labelOn = !labelOn; this.classList.toggle('on', labelOn);
  mapChart.setOption({series: [{name: '站名', label: {show: labelOn}}]});
};
$('#btn-reset').onclick = function(){
  mapChart.setOption(mapOption(), true);
  jFlip = 0;
  if (jOn) cruiseTo(jIdx); else cruiseStop();
};
"""


# ─────────────────────────── 图表脚本 ───────────────────────────

_TOOLTIP = ("backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border, "
            "textStyle: {color: pal.text, fontSize: 12}")

_CHART_JS = r"""
function _hbar(id, items, color, unit){
  const el = $(id);
  if (!el) return;
  if (!items.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const d = items.slice().reverse();
  echarts.init(el).setOption({
    backgroundColor: 'transparent',
    grid: {left: 6, right: 52, top: 8, bottom: 4, containLabel: true},
    animationDuration: 1000, animationDelay: i => i * 70,
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'}, __TOOLTIP__,
      formatter: p => '<b>' + p[0].name + '</b><br>' + p[0].value + ' ' + unit},
    xAxis: {type: 'value', show: false, max: v => v.max * 1.18},
    yAxis: {type: 'category', data: d.map(x => x.name),
      axisLine: {show: false}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 12}},
    series: [{
      type: 'bar', data: d.map(x => x.value), barWidth: 13,
      itemStyle: {borderRadius: [0, 7, 7, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0,
          [{offset: 0, color: 'rgba(56,189,248,.32)'}, {offset: 1, color: color}])},
      label: {show: true, position: 'right', color: pal.text_dim, fontSize: 11.5,
              formatter: p => fmt(p.value) + ' ' + unit},
      emphasis: {itemStyle: {color: pal.accent_soft}}
    }]
  });
}

_hbar('#chartRoute',
      P.topRoutes.map(x => ({name: x.a + ' → ' + x.b, value: x.count})),
      pal.accent, '次');
_hbar('#chartCity',
      P.cityRank.slice(0, 8).map(x => ({name: x.name, value: x.count})),
      pal.accent2, '次');

(function(){
  const el = $('#chartDist');
  if (!el) return;
  const d = P.dateDist || [];
  if (!d.length) { el.innerHTML = '<div class="empty">暂无出行时间数据</div>'; return; }
  echarts.init(el).setOption({
    backgroundColor: 'transparent',
    grid: {left: 6, right: 14, top: 22, bottom: 4, containLabel: true},
    animationDuration: 900,
    tooltip: {trigger: 'axis', __TOOLTIP__},
    legend: {show: false},
    xAxis: {type: 'category', data: d.map(x => x.name),
      axisLine: {lineStyle: {color: pal.panel_border}}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 11.5}},
    yAxis: [
      {type: 'value', axisLine: {show: false}, axisTick: {show: false},
       splitLine: {lineStyle: {color: 'rgba(34,49,79,.55)'}},
       axisLabel: {color: pal.text_faint, fontSize: 11}},
      {type: 'value', axisLine: {show: false}, axisTick: {show: false},
       splitLine: {show: false}, axisLabel: {color: pal.text_faint, fontSize: 11}}
    ],
    series: [
      {name: '出行次数', type: 'bar', data: d.map(x => x.count), barMaxWidth: 22,
       itemStyle: {borderRadius: [5, 5, 0, 0], color: pal.accent}},
      {name: '里程', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 6,
       data: d.map(x => Math.round(x.km || 0)),
       lineStyle: {color: pal.accent2, width: 2}, itemStyle: {color: pal.accent2}}
    ]
  });
})();

(function(){
  const el = $('#chartKm');
  if (!el) return;
  const k = P.kpi || {};
  const d = [
    {name: '总里程', value: Math.round(k.km || 0)},
    {name: '单程最长', value: Math.round(k.max_route_km || 0)},
    {name: '平均每段', value: Math.round(k.avg_km || 0)}
  ];
  if (!d[0].value && !d[2].value) { el.innerHTML = '<div class="empty">暂无里程数据</div>'; return; }
  echarts.init(el).setOption({
    backgroundColor: 'transparent',
    grid: {left: 6, right: 56, top: 8, bottom: 4, containLabel: true},
    animationDuration: 900,
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'}, __TOOLTIP__,
      formatter: p => '<b>' + p[0].name + '</b><br>' + fmt(p[0].value) + ' km'},
    xAxis: {type: 'value', show: false, max: v => v.max * 1.18},
    yAxis: {type: 'category', data: d.map(x => x.name).reverse(),
      axisLine: {show: false}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 12}},
    series: [{
      type: 'bar', data: d.map(x => x.value).reverse(), barWidth: 13,
      itemStyle: {borderRadius: [0, 7, 7, 0], color: '#8B7BF0'},
      label: {show: true, position: 'right', color: pal.text_dim, fontSize: 11.5,
              formatter: p => fmt(p.value) + ' km'}
    }]
  });
})();
"""

_DATE_EDIT_JS = r"""
/* 在行程单中选择住宿日期即保存至本机发票库，不增加确认按钮。 */
const stayFlow = document.querySelector('#timeline .trip-flow');
if (stayFlow) {
  [...stayFlow.querySelectorAll('.trip-flow-row')].forEach((row, index) => {
    row.dataset.originalOrder = String(index);
  });
  stayFlow.addEventListener('click', (event) => {
    const display = event.target.closest('input[data-stay-display]');
    if (!display || display.disabled) return;
    const picker = display.parentElement.querySelector('input[data-stay-date]');
    if (picker && picker.showPicker) picker.showPicker();
    else if (picker) picker.click();
  });
  stayFlow.addEventListener('change', async (event) => {
    const field = event.target.closest('input[data-stay-date]');
    if (!field) return;
    const row = field.closest('.trip-flow-row.stay');
    const id = Number(row && row.dataset.vaultId);
    if (!row || !Number.isInteger(id) || id <= 0) return;
    const checkin = row.querySelector('input[data-stay-date="checkin"]');
    const checkout = row.querySelector('input[data-stay-date="checkout"]');
    const checkinDisplay = row.querySelector('input[data-stay-display="checkin"]');
    const checkoutDisplay = row.querySelector('input[data-stay-display="checkout"]');
    const state = row.querySelector('.stay-save-state');
    const before = [checkin.dataset.saved ?? checkin.defaultValue,
                    checkout.dataset.saved ?? checkout.defaultValue];
    checkin.disabled = checkout.disabled = true;
    checkinDisplay.disabled = checkoutDisplay.disabled = true;
    state.classList.remove('error');
    state.textContent = '正在同步…';
    try {
      const response = await fetch('/api/vault/journey', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, updates: {checkin: checkin.value, checkout: checkout.value}}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '保存失败');
      if (typeof BroadcastChannel === 'function') {
        const channel = new BroadcastChannel('fapiao-journey-updates');
        channel.postMessage({row: data.row});
        channel.close();
      }
      const saved = data.row.journey || {};
      checkin.value = checkin.dataset.saved = saved.checkin || '';
      checkout.value = checkout.dataset.saved = saved.checkout || '';
      checkinDisplay.value = checkin.value;
      checkoutDisplay.value = checkout.value;
      const head = row.querySelector('.tl-head');
      let nights = head.querySelector('.stay-nights');
      if (saved.nights) {
        if (!nights) {
          nights = document.createElement('span');
          nights.className = 'stay-nights';
          head.appendChild(nights);
        }
        nights.textContent = saved.nights + ' 晚';
      } else if (nights) nights.remove();
      const day = saved.checkin || saved.checkout || row.dataset.invoiceDate || '日期未知';
      row.dataset.date = day;
      row.dataset.phase = saved.checkin ? '2' : '0';
      row.querySelector('.trip-flow-date').textContent = day;
      const ordered = [...stayFlow.querySelectorAll('.trip-flow-row')].sort((a, b) => {
        const da = a.dataset.date || '日期未知';
        const db = b.dataset.date || '日期未知';
        if (da === '日期未知') return db === '日期未知' ? 0 : 1;
        if (db === '日期未知') return -1;
        return da.localeCompare(db) || Number(a.dataset.phase) - Number(b.dataset.phase)
          || Number(a.dataset.originalOrder) - Number(b.dataset.originalOrder);
      });
      ordered.forEach((item) => stayFlow.appendChild(item));
      state.textContent = '已同步发票库';
    } catch (error) {
      checkin.value = before[0];
      checkout.value = before[1];
      checkinDisplay.value = before[0];
      checkoutDisplay.value = before[1];
      state.classList.add('error');
      state.textContent = '保存失败：' + error.message;
    } finally {
      checkin.disabled = checkout.disabled = false;
      checkinDisplay.disabled = checkoutDisplay.disabled = false;
    }
  });
}
"""

_AIR_EDIT_JS = r"""
/* 机票地点采用省份→城市→机场；机场可留空，地图按城市定位。 */
const airEsc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
document.addEventListener('click', async (event) => {
  const edit = event.target.closest('[data-air-edit]');
  if (!edit) return;
  const row = edit.closest('.trip-flow-row');
  const id = Number(row?.dataset.vaultId);
  if (!id) return;
  const modal = document.createElement('div');
  modal.className = 'trip-air-modal';
  modal.innerHTML = `<div class="trip-air-panel" role="dialog" aria-modal="true" aria-label="补充机票行程信息">
    <h3>补充机票行程信息</h3>
    <label>出发地</label><div class="airport-picks" data-endpoint="from_"><select data-province></select><select data-city></select><select data-airport></select></div>
    <label>目的地</label><div class="airport-picks" data-endpoint="to_"><select data-province></select><select data-city></select><select data-airport></select></div>
    <label>行程日期</label><div class="trip-date-box"><input data-date-display type="text" readonly placeholder="YYYY-MM-DD"><input data-date-picker type="date" tabindex="-1" aria-hidden="true"></div>
    <label>乘客姓名<input data-passenger type="text" autocomplete="off"></label>
    <p class="air-error" aria-live="polite"></p><div class="air-actions"><button type="button" data-air-cancel>取消</button><button type="button" data-air-save>保存</button></div>
  </div>`;
  document.body.appendChild(modal);
  modal.addEventListener('click', (click) => {
    if (click.target === modal || click.target.closest('[data-air-cancel]')) modal.remove();
  });
  const picker = modal.querySelector('[data-date-picker]');
  const display = modal.querySelector('[data-date-display]');
  picker.value = /^\d{4}-\d{2}-\d{2}$/.test(edit.dataset.travelDate || '') ? edit.dataset.travelDate : '';
  display.value = picker.value;
  display.addEventListener('click', () => picker.showPicker ? picker.showPicker() : picker.click());
  picker.addEventListener('change', () => { display.value = picker.value; });
  modal.querySelector('[data-passenger]').value = edit.dataset.passenger || '';
  let catalog;
  try {
    const response = await fetch('/api/pool/airports');
    if (!response.ok) throw new Error('机场目录读取失败');
    catalog = (await response.json()).provinces;
  } catch (error) {
    modal.querySelector('.air-error').textContent = error.message;
    modal.querySelector('[data-air-save]').disabled = true;
    return;
  }
  const optionHtml = (values, label) => `<option value="">${label}</option>`
    + values.map((value) => `<option value="${airEsc(value)}">${airEsc(value)}</option>`).join('');
  for (const box of modal.querySelectorAll('.airport-picks')) {
    const province = box.querySelector('[data-province]');
    const city = box.querySelector('[data-city]');
    const airport = box.querySelector('[data-airport]');
    const old = edit.dataset[box.dataset.endpoint === 'from_' ? 'from' : 'to'];
    province.innerHTML = optionHtml(Object.keys(catalog), '选择省份');
    province.addEventListener('change', () => {
      city.innerHTML = optionHtml(Object.keys(catalog[province.value] || {}), '选择城市');
      airport.innerHTML = optionHtml([], '机场可选');
    });
    city.addEventListener('change', () => {
      airport.innerHTML = optionHtml(catalog[province.value]?.[city.value] || [], '机场可选');
    });
    const match = Object.entries(catalog).find(([, cities]) => Object.hasOwn(cities, old));
    if (match) { province.value = match[0]; province.dispatchEvent(new Event('change'));
      city.value = old; city.dispatchEvent(new Event('change')); }
    else province.dispatchEvent(new Event('change'));
  }
  modal.querySelector('[data-air-save]').addEventListener('click', async (click) => {
    const button = click.currentTarget;
    const from = modal.querySelector('[data-endpoint="from_"]');
    const to = modal.querySelector('[data-endpoint="to_"]');
    const city = (box) => box.querySelector('[data-city]').value;
    const airport = (box) => box.querySelector('[data-airport]').value;
    const updates = {from_:city(from), to_:city(to), from_airport:airport(from),
      to_airport:airport(to), travel_date:picker.value,
      passenger:modal.querySelector('[data-passenger]').value.trim()};
    if (!updates.from_ || !updates.to_) {
      modal.querySelector('.air-error').textContent = '请选择出发城市和目的城市'; return;
    }
    button.disabled = true;
    try {
      const response = await fetch('/api/vault/journey', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify({id,updates})});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '保存失败');
      if (typeof BroadcastChannel === 'function') {
        const channel = new BroadcastChannel('fapiao-journey-updates');
        channel.postMessage({row:data.row}); channel.close();
      }
      const route = row.querySelector('.tl-route');
      if (route) {
        route.children[0].textContent = updates.from_;
        route.children[2].textContent = updates.to_;
      }
      const oldWarn = [...row.querySelectorAll('.tag-warn')].find((tag) => tag.textContent === '地点待补充');
      if (oldWarn) oldWarn.remove();
      edit.dataset.from = updates.from_; edit.dataset.to = updates.to_;
      edit.dataset.passenger = updates.passenger; edit.dataset.travelDate = updates.travel_date;
      const meta = row.querySelector('.tl-meta span:first-child');
      if (meta) meta.textContent = updates.passenger || meta.title || '票价';
      if (updates.travel_date) {
        row.dataset.date = updates.travel_date;
        row.querySelector('.trip-flow-date').textContent = updates.travel_date;
      }
      edit.textContent = data.row.completeness === 'complete' ? '修改行程信息' : '继续补充信息';
      modal.remove();
    } catch (error) {
      modal.querySelector('.air-error').textContent = '保存失败：' + error.message;
    } finally { button.disabled = false; }
  });
});
"""

_STAY_JS = r"""
/* 已停用：住宿侧「酒店金额 Top」改成共享 CSS 的 `.rank` 条形（`_stay_vendor_html`），
   不再载入 ECharts —— 只有住宿输入时整页必须退化成 `hotel_html` 那种
   零 ECharts 的轻量静态页，不能再为此内联 1003KB 运行库。
   这段保留在此仅作对照，**没有任何代码路径引用它**。 */
"""


# ─────────────────────────── 渲染入口 ───────────────────────────

def _js_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

#: 内联进页面的 `P` **只保留 JS 真正读到的键**（`_MAP_JS` / `_CHART_JS` /
#: `report_html.BASE_APP_JS`）。明细表与时间轴都是 Python 侧渲染好的静态 HTML，
#: `bindDetailView()` 只切 `hidden`，**不会**重新渲染；`transit.linked` 之类的
#: 判定也全在服务端做完。全量 payload 有 180 条腿的原始数据，实测多出约 500KB
#: 纯为死重（`include_map=False` 的轻量版也只有它撑着，630KB → 100KB 出头）。
_P_KEYS = ("palette", "mode", "markers", "trainMarker", "chips", "kpiCards", "kpi",
           "stations", "routes", "journey", "topRoutes", "cityRank",
           "dateDist", "dateDistKind")


def _slim_payload(payload):
    """payload → 仅含前端所需键的浅拷贝（用于 `__PAYLOAD__` 内联）。"""
    return {k: payload[k] for k in _P_KEYS if k in payload}


def render_trip_html(payload, geojson, echarts_js, *,
                     include_map=True, include_charts=True,
                     footer_left="", footer_right=""):
    """把 payload 拼成完整单文件 HTML 文档（返回字符串）。"""
    pal = payload["palette"]
    mode = payload.get("mode") or "train"
    draw_map = bool(include_map and payload.get("includeMap"))
    # ECharts 只为「交通统计 4 图」而载入：没有可连线段时那 4 个容器根本
    # 不存在（见 `_body_charts`），住宿侧图表全是 CSS 条形 ⇒ 不必载入运行库。
    draw_charts = bool(include_charts and payload["transit"]["linked"])

    body = _body_charts(payload, draw_map, draw_charts)
    if draw_map:
        body = (body
                .replace("__MAP_INSET__", "")
                .replace("__MAP_LEGEND__", ""))
    body += _body_detail(payload)
    body += _unmatched_html(payload)

    base_js = ("renderChips(P.chips);\n"
               "renderKpis(P.kpiCards);\n"
               "initReveal();\n"
               "bindDetailView();\n")
    app_js = base_js + _DATE_EDIT_JS + _AIR_EDIT_JS
    if draw_map:
        app_js += (_MAP_JS
                   .replace("__DASH_NAME_JSON__", _js_json(TH.HTML_DASH_NAME))
                   .replace("__GEOJSON__", _js_json(geojson))
                   .replace("__CRUISE__", TH.w(mode, "cruise"))
                   .replace("__CRUISE_A__", TH.w(mode, "cruise") + "A")
                   .replace("__CRUISE_B__", TH.w(mode, "cruise") + "B")
                   .replace("__NO_UNKNOWN__", TH.w(mode, "no_unknown")))
    if draw_charts:
        app_js += _CHART_JS.replace("__TOOLTIP__", _TOOLTIP)

    hero_icon = TH.vehicle_icon_svg(mode if mode in T.TRAIN_MODES else "train", 30, pal)
    if mode == "mixed":
        hero_icon = (TH.vehicle_icon_svg("train", 30, pal)
                     + '<span style="width:8px;display:inline-block"></span>'
                     + TH.vehicle_icon_svg("air", 30, pal))

    doc = R.render_page(
        head_title=payload.get("title") or "行程汇总单",
        palette=pal,
        module_css=TH._MODULE_CSS + _TRIP_CSS,
        hero_icon=hero_icon,
        title=payload.get("title") or "行程汇总单",
        subtitle=payload.get("subtitle") or "",
        body=body,
        footer_left=footer_left,
        footer_right=footer_right,
        app_js=app_js,
        echarts_js=(echarts_js if (draw_map or draw_charts) else None),
        payload_json=_js_json(_slim_payload(payload)),
    )
    return doc


def build_trip_payload_html(payload, output_folder, *,
                            include_map=True, include_charts=True,
                            filename=None, footer_left="", footer_right=""):
    """把 payload 落盘为单文件 HTML，返回**固定文件名**的完整路径。

    ⚠️ 这里**不用** `trainmap_html._unique_path` —— 要求文件名固定为
    「行程汇总单.html」，不得出现 `(1)(2)` 后缀。预览目录是
    `preview.new_run_dir("trip")` 新建的 `trip_<时间戳>` 子目录，本来就不会撞名。
    """
    draw_map = bool(include_map and payload.get("includeMap"))
    draw_charts = bool(include_charts and payload["transit"]["linked"])
    geojson = TH.build_geojson() if draw_map else None
    echarts_js = TH._read_vendor() if (draw_map or draw_charts) else None

    doc = render_trip_html(        payload, geojson, echarts_js,
        include_map=draw_map, include_charts=draw_charts,
        footer_left=footer_left or _footer_left(payload),
        footer_right=footer_right or _footer_right(payload),
    )
    os.makedirs(output_folder, exist_ok=True)
    path = os.path.join(output_folder, filename or TRIP_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def _footer_left(payload):
    return (f'生成时间：{time.strftime("%Y-%m-%d %H:%M")}　·　'
            '报销助手　·　'
            + TH._COMPLIANCE)


def _footer_right(payload):
    t = payload["totals"]
    return (f'本次汇总 交通 {t["linkedCount"] + t["unlinkedCount"]} 张'
            f' · 住宿 {t["stayCount"]} 张　|　'
            f'票面合计 ￥{t["grandAmount"]:,.2f}　·　' + TH._PRIVACY)


def build_trip_html(invoices, output_folder, *, include_map=True, include_charts=True,
                    mode_default="train", title="行程汇总单", subtitle=None,
                    palette=None, filename=None, footer_left="", footer_right=""):
    """一步到位：发票记录列表 → 落盘的 HTML 路径。"""
    from . import trip_summary as TS
    payload = TS.build_trip_payload(
        invoices, include_map=include_map, include_charts=include_charts,
        mode_default=mode_default, title=title, subtitle=subtitle, palette=palette)
    return build_trip_payload_html(payload, output_folder,
                                   include_map=include_map,
                                   include_charts=include_charts,
                                   filename=filename,
                                   footer_left=footer_left,
                                   footer_right=footer_right)
