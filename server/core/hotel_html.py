# -*- coding: utf-8 -*-
"""core/hotel_html.py — 住宿票「住宿行程单」单文件自包含 HTML 生成器。

与火车票的 `trainmap_html.py` 共用同一套版面骨架（`report_html.py`），
**不再生成 PDF**：风格统一、可交互、可「打印 / 存 PDF」当行程单交差。

与火车票的差异
--------------
* 住宿发票没有站点坐标，故本页**不含地图**；重心是明细：住宿时间轴 + 报销明细表。
* 主色取住宿模块识别色（翡翠绿 `#10B981`，与 App 内模块色一致）。
* 强调「住宿晚数」：按入住-退房自然日计算，是住宿报销的核心口径。

数据资产：本页不依赖任何外部资产（无 ECharts、无 GeoJSON）。
"""
from __future__ import annotations

import datetime
import json
import os

from . import report_html as R
from .config import _log_error
from .parse import _unique_path, amount_upper
from .report_html import (esc as _esc, date_parts as _date_parts,
                          short_date as _short_date)

__all__ = [
    "DEFAULT_HOTEL_PALETTE",
    "build_hotel_payload",
    "build_hotel_html",
    "hotel_icon_svg",
]

#: 默认色板 —— 键集合与 `DEFAULT_HTML_PALETTE`（火车票）保持一致（有单测断言），
#: 只把主色换成住宿模块的翡翠绿。
DEFAULT_HOTEL_PALETTE = {
    "bg_from":      "#060A14",
    "bg_to":        "#0D1526",
    "panel":        "#0F1A2E",
    "panel_border": "#22314F",
    "accent":       "#10B981",
    "accent_soft":  "#6EE7B7",
    "accent2":      "#22D3EE",
    "text":         "#E9EFFA",
    "text_dim":     "#93A4C4",
    "text_faint":   "#5C6E8F",
    "map_fill":     "#12203A",
    "map_border":   "#2E4470",
    "map_dash":     "#93A9CE",
    "map_glow":     "#22D3EE",
    "route_hi":     "#34D399",
    "route_mid":    "#10B981",
    "route_lo":     "#3E5C8F",
    "route_flow":   "#A7F3D0",
    "station":      "#34D399",
    "station_ring": "#080D1A",
}

_PRIVACY = "本页仅在本机生成与查看，住宿数据不上传任何第三方服务"
_MAX_DETAILS = 800


def hotel_icon_svg(size=30, palette=None):
    """住宿标识：一栋带窗的酒店楼（扁平描边，不引用外部资源）。"""
    pal = _palette(palette)
    body, glass, accent = "#F3F7FF", "#123A5E", pal["accent"]
    line = pal["accent"]
    inner = (
        '<rect x="13" y="8" width="38" height="48" rx="5" fill="%s" stroke="%s" stroke-width="3.4"/>'
        '<rect x="21" y="16" width="8" height="8" rx="1.5" fill="%s"/>'
        '<rect x="35" y="16" width="8" height="8" rx="1.5" fill="%s"/>'
        '<rect x="21" y="29" width="8" height="8" rx="1.5" fill="%s"/>'
        '<rect x="35" y="29" width="8" height="8" rx="1.5" fill="%s"/>'
        '<rect x="26" y="43" width="12" height="13" rx="2" fill="%s"/>'
        % (body, line, glass, glass, glass, glass, accent))
    return ('<svg class="ico" viewBox="0 0 64 64" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>'
            % (size, size, inner))


def _palette(palette):
    pal = dict(DEFAULT_HOTEL_PALETTE)
    if palette:
        pal.update({k: v for k, v in palette.items() if v})
    return pal


def _nights(checkin, checkout):
    """入住->退房自然日数；任一端不可解析或不为正数时返回 None。"""
    ya, ma, da = _date_parts(checkin)
    yb, mb, db = _date_parts(checkout)
    if not (ya and yb):
        return None
    try:
        n = (datetime.date(yb, mb, db) - datetime.date(ya, ma, da)).days
    except ValueError:
        return None
    return n if n > 0 else None


# ─────────────────────── 数据组装 ───────────────────────

def build_hotel_payload(records, palette=None, *, title="住宿行程单",
                        subtitle=None, source_note=None):
    """把住宿发票记录整理成 HTML 需要的全部结构化数据。"""
    pal = _palette(palette)
    details = []
    for i, r in enumerate(records or [], 1):
        if not isinstance(r, dict):
            continue
        checkin = str(r.get("checkin_date") or "")
        checkout = str(r.get("checkout_date") or "")
        try:
            seq = int(r.get("seq") or i)
        except (TypeError, ValueError):
            seq = i
        details.append({
            "seq": seq,
            "hotel": str(r.get("hotel_name") or "未知酒店"),
            # 住宿发票票面**没有「入住人」这个字段**，只有购买方；
            # core/hotel.py 取的本来就是 buyer_name，这里如实叫「购买方」。
            "buyer": str(r.get("buyer_name") or r.get("guest_name") or ""),
            "checkin": _short_date(checkin) or "日期未知",
            "checkout": _short_date(checkout) if _date_parts(checkout)[0] else "",
            "nights": _nights(checkin, checkout),
            "amount": round(float(r.get("amount") or 0.0), 2),
            "item": str(r.get("item_name") or ""),
            "invoice_no": str(r.get("invoice_no") or ""),
            "invoice_date": str(r.get("invoice_date") or ""),
            "tax_id": str(r.get("tax_id") or ""),
            "seller_tax_id": str(r.get("seller_tax_id") or ""),
            "drawer": str(r.get("drawer") or ""),
            "filename": str(r.get("filename") or ""),
            "review": (str(r.get("review_reason") or "")
                       if r.get("needs_review") else ""),
        })

    total = round(sum(d["amount"] for d in details), 2)
    night_list = [d["nights"] for d in details if d["nights"]]
    nights_sum = sum(night_list)
    buyers = sorted({d["buyer"] for d in details if d["buyer"]})

    # 开票单位（按 酒店名 + 销售方税号 去重）
    vendors, vseen = [], {}
    for d in details:
        key = (d["hotel"], d["seller_tax_id"])
        if key not in vseen:
            vseen[key] = {"name": d["hotel"], "tax_id": d["seller_tax_id"],
                          "count": 0, "amount": 0.0}
            vendors.append(vseen[key])
        vseen[key]["count"] += 1
        vseen[key]["amount"] = round(vseen[key]["amount"] + d["amount"], 2)
    vendors.sort(key=lambda v: (-v["amount"], v["name"]))

    chips = []
    dts = sorted({d["checkin"] for d in details
                  if d["checkin"] and d["checkin"] != "日期未知"})
    if dts:
        chips.append(["住宿期间", f'{dts[0]} 至 {dts[-1]}'])
    if buyers:
        chips.append(["购买方", "、".join(buyers)])
    chips.append(["住宿发票", f'{len(details)} 张'])
    if details:
        chips.append(["住宿总金额", R.fmt_money(total)])
    review_n = len([d for d in details if d["review"]])
    if review_n:
        chips.append(["待复核", f'{review_n} 张'])

    max_night = max([d for d in details if d["nights"]],
                    key=lambda d: d["nights"], default=None)
    # 住宿发票的「备注」栏常为空，此时无入住/退房区间 -> 晚数不可得，
    # 展示为「—」并说明，避免出现恒 0 的误导指标。
    night_card = ({"lab": "住宿晚数", "count": nights_sum, "unit": "晚",
                   "sub": "按入住-退房自然日", "color": "#34D399"}
                  if nights_sum > 0 else
                  {"lab": "住宿晚数", "text": R.DASH, "cls": "txt",
                   "sub": "票面未含住宿区间", "color": "#34D399"})
    kpi_cards = [
        {"lab": "住宿张数", "count": len(details), "unit": "张",
         "sub": "去重后的有效发票", "color": pal["accent"]},
        {"lab": "住宿总金额", "count": total, "dec": 2, "unit": "元",
         "sub": "票面金额合计", "color": pal["accent2"]},
        {"lab": "覆盖酒店", "count": len(vendors), "unit": "家",
         "sub": "按销售方去重", "color": "#8B7BF0"},
        night_card,
        {"lab": "最长单次", "text": (f'{max_night["nights"]} 晚' if max_night else R.DASH),
         "cls": "txt", "sub": (max_night["hotel"] if max_night else "无住宿日期区间"),
         "color": "#F472B6"},
    ]

    money = {
        "count": len(details),
        "total": total,
        "avg": round(total / len(details), 2) if details else 0.0,
        "nights": nights_sum,
        "nightCount": len(night_list),
        "vendorCount": len(vendors),
        "upper": amount_upper(total),
        "dateFrom": dts[0] if dts else "",
        "dateTo": dts[-1] if dts else "",
        "reviewCount": review_n,
    }

    return {
        "title": title,
        "subtitle": subtitle or "本次上传住宿发票的行程汇总",
        "palette": pal,
        "chips": chips,
        "kpiCards": kpi_cards,
        "money": money,
        "details": details[:_MAX_DETAILS],
        "detailsTotal": len(details),
        "detailsTruncated": len(details) > _MAX_DETAILS,
        "vendors": vendors,
        "buyers": buyers,
        "sourceNote": source_note or "",
    }


# ─────────────────────── 服务端渲染 ───────────────────────

def _money_html(payload):
    m = payload["money"]
    buyers = "、".join(payload.get("buyers") or []) or R.DASH
    sub = (f'住宿期间 {m["dateFrom"] or R.DASH} 至 {m["dateTo"] or R.DASH}'
           f'　·　购买方 {buyers}')
    return R.money_strip(
        "住宿报销汇总", sub,
        [("住宿总金额", R.fmt_money(m["total"]), "ok"),
         ("单均金额", R.fmt_money(m["avg"]), ""),
         ("票据张数", f'{m["count"]} 张', ""),
         ("住宿晚数", (f'{m["nights"]} 晚' if m["nights"] else R.DASH), ""),
         ("覆盖酒店", f'{m["vendorCount"]} 家', "")],
        "合计（大写）", m["upper"] or "零元整")


def _timeline_html(payload):
    rows = payload.get("details") or []
    if not rows:
        return '<div class="empty">本次没有可列示的住宿明细。</div>'
    ic = hotel_icon_svg(13, payload.get("palette"))
    out, last = [], None
    for e in rows:
        day = e.get("checkin") or "日期未知"
        if day != last:
            out.append(f'<div class="tl-day"><span>{_esc(day)}</span></div>')
            last = day
        nights = e.get("nights")
        night_txt = f'{nights} 晚' if nights else R.DASH
        stay = (f'{_esc(e.get("checkin"))}'
                + (f' → {_esc(e.get("checkout"))}' if e.get("checkout") else ""))
        tags = []
        if e.get("review"):
            tags.append(f'<i class="tag tag-warn">待复核 {_esc(e["review"])}</i>')

        sub = []
        if e.get("buyer"):
            sub.append(f'<span>购买方<i>{_esc(e["buyer"])}</i></span>')
        if e.get("invoice_no"):
            sub.append(f'<span class="id">发票号码<i>{_esc(e["invoice_no"])}</i></span>')
        if e.get("seller_tax_id"):
            sub.append(f'<span class="id">销售方税号<i>{_esc(e["seller_tax_id"])}</i></span>')
        if e.get("drawer"):
            sub.append(f'<span>开票人<i>{_esc(e["drawer"])}</i></span>')
        if e.get("invoice_date"):
            sub.append(f'<span>开票日期<i>{_esc(e["invoice_date"])}</i></span>')
        if e.get("filename"):
            sub.append(f'<span>源文件<i>{_esc(e["filename"])}</i></span>')
        sub_html = f'<div class="tl-sub">{"".join(sub)}</div>' if sub else ""

        out.append(
            f'<div class="tl-item">'
            f'<span class="tl-node"></span>'
            f'<div class="tl-body">'
            f'<div class="tl-head">'
            f'<span class="tl-seq">{e.get("seq", 0):02d}</span>'
            f'{ic}<span class="tl-train">{_esc(e.get("hotel"))}</span>'
            f'<span class="tl-seat">{_esc(night_txt)}</span>'
            f'{"".join(tags)}'
            f'</div>'
            f'<div class="tl-route">'
            f'<span class="st">入住 {_esc(e.get("checkin"))}</span>'
            f'<span class="arw">→</span>'
            f'<span class="st">退房 {_esc(e.get("checkout") or R.DASH)}</span>'
            f'<span class="tl-km">{_esc(night_txt)}</span>'
            f'</div>'
            f'{sub_html}'
            f'<div class="tl-meta"><span>住宿日期</span>'
            f'<span class="amt">{R.fmt_money(e.get("amount", 0))}'
            f'<i class="lb">住宿票</i></span>'
            f'</div>'
            f'</div></div>')
    return "".join(out)


def _table_html(payload):
    rows = payload.get("details") or []
    if not rows:
        return '<div class="empty">本次没有可列示的住宿明细。</div>'
    m = payload["money"]
    head = ["序号", "入住日期", "退房日期", "晚数", "酒店名称", "购买方",
            "金额", "发票号码"]
    th = "".join(f'<th>{_esc(h)}</th>' for h in head)
    trs = []
    for e in rows:
        tag = ('<i class="kbd k-warn">待复核</i>' if e.get("review") else "")
        trs.append(
            f'<tr>'
            f'<td class="seq">{e.get("seq", 0)}</td>'
            f'<td class="d">{_esc(e.get("checkin"))}</td>'
            f'<td class="d">{_esc(e.get("checkout") or R.DASH)}</td>'
            f'<td class="num">{e["nights"] if e.get("nights") else R.DASH}</td>'
            f'<td>{_esc(e.get("hotel"))}</td>'
            f'<td>{_esc(e.get("buyer") or R.DASH)}</td>'
            f'<td class="num"><span class="amt">{R.fmt_money(e.get("amount", 0))}</span>{tag}</td>'
            f'<td class="inv">{_esc(e.get("invoice_no") or R.DASH)}</td>'
            f'</tr>')
    night_note = (f'住宿 {m["nightCount"]} 张含日期区间 · 共 {m["nights"]} 晚'
                  if m["nights"] else '票面未含住宿日期区间')
    foot = (f'<tfoot><tr><td colspan="6">合计 {m["count"]} 张（{night_note}）</td>'
            f'<td class="num"><span class="amt">{R.fmt_money(m["total"])}</span></td>'
            f'<td class="inv">{_esc(m["upper"])}</td></tr></tfoot>')
    return (f'<table class="dt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody>{foot}</table>')


def _detail_note(payload):
    m = payload["money"]
    bits = [f'共 {m["count"]} 张住宿发票',
            f'{m["vendorCount"]} 家开票单位']
    if m["nights"]:
        bits.append(f'住宿 {m["nights"]} 晚')
    bits.append(f'票面合计 {R.fmt_money(m["total"])}')
    txt = "　·　".join(bits)
    if m["reviewCount"]:
        txt += f'　·　{m["reviewCount"]} 张发票有字段待复核'
    if payload.get("detailsTruncated"):
        txt += f'　·　仅展示前 {len(payload["details"])} 条，共 {payload["detailsTotal"]} 条'
    return _esc(txt)


def _vendor_html(payload):
    vs = payload.get("vendors") or []
    if not vs:
        return '<div class="empty">没有解析到开票单位信息。</div>'
    rows = "".join(
        f'<div class="rank-row{" top1" if i == 0 else ""}">'
        f'<span class="no">{i + 1}</span>'
        f'<span class="nm">{_esc(v["name"])}'
        f'<small>{_esc(v["tax_id"]) if v["tax_id"] else "税号未解析"}</small></span>'
        f'<span class="cnt">{v["count"]} 张 · {R.fmt_money(v["amount"])}</span>'
        f'<span class="bar" style="grid-column:1/-1">'
        f'<span data-w="{round(v["amount"] / max([x["amount"] for x in vs] or [1]) * 100)}"></span>'
        f'</span>'
        f'</div>'
        for i, v in enumerate(vs))
    return f'<div class="rank">{rows}</div>'


# ─────────────────────── 模块样式 / 主体 ───────────────────────

_MODULE_CSS = r"""/* 住宿标识图标 */
.ico{display:inline-block;vertical-align:-.16em;flex:none}
.tl-head .ico{margin-right:-2px;opacity:.95;filter:drop-shadow(0 0 6px rgba(16,185,129,.45))}"""

_BODY = r"""
  <section class="grid">
    <div class="col">
      <div class="panel">
        <div class="panel-head">
          <h2>住宿明细</h2>
          <div class="tools">
            <button class="btn on" id="btn-view-tl">住宿时间轴</button>
            <button class="btn" id="btn-view-tb">报销明细表</button>
            <button class="btn" id="btn-print">打印 / 存 PDF</button>
          </div>
        </div>
        __MONEY_STRIP__
        <div class="tl" id="timeline">__TIMELINE__</div>
        <div class="dt-wrap" id="detailTable" hidden>__DETAIL_TABLE__</div>
        <div class="det-note">__DETAIL_NOTE__</div>
      </div>
    </div>

    <div class="col">
      <div class="panel">
        <div class="panel-head"><h2>开票单位信息</h2><span class="hint">按销售方去重</span></div>
        __VENDORS__
      </div>
    </div>
  </section>"""

_APP_JS = r"""/* 住宿行程单没有地图与图表，只需要公共组件把徽章/KPI 渲染出来 */
"""


def render_hotel_html(payload, *, footer_left="", footer_right=""):
    def js_json(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    body = (_BODY
            .replace("__MONEY_STRIP__", _money_html(payload))
            .replace("__TIMELINE__", _timeline_html(payload))
            .replace("__DETAIL_TABLE__", _table_html(payload))
            .replace("__DETAIL_NOTE__", _detail_note(payload))
            .replace("__VENDORS__", _vendor_html(payload)))
    return R.render_page(
        head_title=payload.get("title", "住宿行程单"),
        palette=payload["palette"],
        module_css=_MODULE_CSS,
        hero_icon=hotel_icon_svg(30, payload["palette"]),
        title=payload.get("title", "住宿行程单"),
        subtitle=payload.get("subtitle", ""),
        body=body,
        footer_left=footer_left, footer_right=footer_right,
        app_js="renderChips(P.chips);\nrenderKpis(P.kpiCards);\ninitReveal();\nbindDetailView();\n"
               + _APP_JS,
        echarts_js=None,
        payload_json=js_json(payload))


def build_hotel_html(records, output_folder, *, palette=None,
                     title="住宿行程单", subtitle=None,
                     filename="住宿行程单.html"):
    """生成单文件自包含住宿行程单 HTML，返回落盘路径；失败抛异常（调用方决定降级）。"""
    payload = build_hotel_payload(records, palette=palette, title=title, subtitle=subtitle)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    m = payload["money"]
    footer_left = (f'生成时间：{_esc(now)} &nbsp;|&nbsp; 由「报销助手」生成'
                   f'<br>住宿发票明细按票面金额列示，不含任何外部依赖')
    footer_right = (f'本次上传住宿发票 {m["count"]} 张 &nbsp;|&nbsp; '
                    f'票面合计 {R.fmt_money(m["total"])}'
                    f'<br>{_esc(_PRIVACY)}')

    doc = render_hotel_html(payload, footer_left=footer_left, footer_right=footer_right)
    path = _unique_path(os.path.join(output_folder, filename))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
