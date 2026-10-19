# -*- coding: utf-8 -*-
"""core/trainmap_html.py — **通用行程轨迹图**单文件自包含 HTML 生成器。

⚠️ 2026-10-10 起本图不再是火车专属：用户口径「航班跟火车一样，肯定要连线」，
支持 `mode='train'` / `mode='air'`，图标与措辞按 mode 切换（措辞表见 `MODE_W`，
新增面向用户的文案必须三张表都填）。

⚠️ mode 是**记录级**的，不是整页级：用户一趟出差既有火车又有飞机是常态，
整页一个 mode 会让另一半显示成错误的措辞与图标。**逐条判定**在
`trainmap.resolve_mode`，`_enrich()` / `_detail_records()` 把 mode 带进每一行，
时间轴 / 明细表 / 地图巡航各自按行切换；整页 mode（含 `'mixed'`）只用于
标题、副标题、页脚、巡航按钮这类一页确实只有一个的位置。

设计要点
--------
* **单文件自包含**：把 ECharts 运行库、中国省界 GeoJSON、行程数据全部内联进
  一个 .html。双击即开，**不需要联网、不需要任何外部资源**。
* **零隐私外泄**：行程含乘客与站点位置信息，所有数据只在本机渲染，
  HTML 内不含任何远程请求（有单测断言"不含 http(s) 外链脚本"）。
* **颜色注入**：`palette=None` 时回落 `DEFAULT_HTML_PALETTE`；GUI 侧从
  `ui/theme.py` 读令牌后注入（core 不得 import ui）。
* **口径**：不是"年度总结" —— 只描述"本次上传的这一批车票"。
  里程 = 站点间 haversine 球面直线距离（非铁路实际里程）；退票不计次、改签计 1 次；
  线路无向聚合。未匹配站点、港澳台/国际段不静默丢弃。

数据资产
    assets/trainmap/china_provinces.json   省级边界 + 南海九段线
    assets/trainmap/stations.json          站名 -> [lat, lon, city]
    assets/trainmap/cities.json            城市兜底坐标
    assets/trainmap/vendor/echarts.min.js  Apache ECharts 5.5.1（Apache-2.0）
"""
from __future__ import annotations

import base64
import datetime
import json
import os

from . import trainmap
from . import report_html as R
from .config import _log_error, asset_path
from .parse import _unique_path, amount_upper
#: 版面骨架与通用组件集中在 report_html（住宿票 HTML 共用同一套）
from .report_html import (esc as _esc, date_parts as _date_parts,
                          short_date as _short_date, time_part as _time_part,
                          fmt_km as _fmt_km)

__all__ = [
    "DEFAULT_HTML_PALETTE",
    "HTML_DASH_NAME",
    "build_trainmap_html",
    "build_payload",
    "build_geojson",
    "train_icon_svg",
    "train_marker_datauri",
    "air_icon_svg",
    "vehicle_icon_svg",
    "vehicle_marker_datauri",
    "MODE_W",
    "w",
    "page_mode",
]

# ─────────────────────────── 常量 ───────────────────────────

#: 南海诸岛/九段线在 GeoJSON 里的要素名 —— geo.regions 按名字单独上色
HTML_DASH_NAME = "南海诸岛及九段线示意"

#: 默认暗色影院风色板 —— 键集合必须与 ui/theme.py 的 TRAINMAP_HTML 一致（有单测断言）
#:
#: 2026-10-11：把整套**品牌金**换成前端 v0.4 定稿的**天蓝**。
#: 起因：用户早先否决了霓虹金（原话「霓虹金不要，太扎眼了太丑」），前端已全量换成
#: `#38BDF8 / #7DD3FC / #22D3EE`，但行程单 / 火车行程图这些**独立生成的 HTML 页**
#: 还攥着旧的 `#38BDF8 / #7DD3FC` —— 同一个软件里两套主色，点「生成行程单」就会撞见
#: 那片被否掉的金。这里只动色值，**不动键名**（`ui/theme.py` 的键集合断言不受影响）。
#: 语义色（`.k-warn` / `.k-change` 那几处告警琥珀）故意保留，见 report_html.py 的注释。
DEFAULT_HTML_PALETTE = {
    "bg_from":      "#060A14",
    "bg_to":        "#0D1526",
    "panel":        "#0F1A2E",
    "panel_border": "#22314F",
    "accent":       "#38BDF8",
    "accent_soft":  "#7DD3FC",
    "accent2":      "#22D3EE",
    "text":         "#E9EFFA",
    "text_dim":     "#93A4C4",
    "text_faint":   "#5C6E8F",
    "map_fill":     "#12203A",
    "map_border":   "#2E4470",
    "map_dash":     "#93A9CE",
    "map_glow":     "#22D3EE",
    #: 线路热度斜坡：过去是 金→暗金，现在改成 亮天蓝→天蓝（越亮＝越常走）
    "route_hi":     "#7DD3FC",
    "route_mid":    "#38BDF8",
    "route_lo":     "#3E5C8F",
    "route_flow":   "#BAE6FD",
    "station":      "#7DD3FC",
    #: 站点描边 —— 故意用近底色，重叠的圆点才会被「切开」不成一坨
    "station_ring": "#080D1A",
}

_COMPLIANCE = ("数据来源：Supeset/China-GeoData（MIT）· 底图为简化示意，非标准地图 · "
               "国界、台湾及南海诸岛（含九段线）按国家统一标准表达")
_PRIVACY = "本页仅在本机生成与查看，行程数据不上传任何第三方服务"

_DATE_RE = None      # 兼容旧引用：日期/时间解析已统一到 report_html
_TIME_RE = None

_VENDOR_REL = ("assets", "trainmap", "vendor", "echarts.min.js")

#: 逐条行程最多的明细条数（防止极端情况下页面过大）
_MAX_TIMELINE = 400

#: 报销明细表最多的行数 —— 表格替代原 PDF 明细表，口径要求「列全」，故上限放宽
_MAX_DETAILS = 800


# ─────────────────────── 小工具 ───────────────────────
# `_esc / _date_parts / _short_date / _time_part / _fmt_km` 由 report_html 提供
# （住宿票 HTML 共用同一套实现），这里只保留模块专属的色板处理。

def _palette(palette):
    pal = dict(DEFAULT_HTML_PALETTE)
    if palette:
        pal.update({k: v for k, v in palette.items() if v})
    return pal


def _read_vendor():
    p = asset_path(*_VENDOR_REL)
    if not os.path.exists(p):
        raise FileNotFoundError(
            "缺少 ECharts 运行库：%s（应为 Apache ECharts 5.5.1 发行版 dist/echarts.min.js）" % p)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# ─────────────────── 图标与措辞：按 mode 切换（通用行程轨迹图）───────────────────
# 2026-10-10 用户口径：「航班跟火车一样，肯定要连线」—— 本图**不再是火车专属**，
# 而是**通用行程轨迹图**，支持 train / air 两种 mode，图标与措辞一律按 mode 切换。
#
# ⛔ 旧的「⚠️ 这是「火车轨迹」…**禁止**出现飞机、航线等航空图标或措辞」约束**已作废**，
#    依据是用户明确推翻，不是你我擅自放宽。
#    注释与实际口径矛盾比没注释更糟（后人会以为还能信），故此处同步改写。

def train_icon_svg(size=30, palette=None, *, front=False):
    """返回一个「高铁车头」内联 SVG（side=侧视，front=正视）。

    全程平涂色、不使用渐变，避免同一页面多处内联时 <defs> id 互相冲突。
    """
    pal = _palette(palette)
    body, glass, wheel = "#F3F7FF", "#123A5E", "#8496B8"
    line = pal["accent"]
    accent = pal["accent"]
    if front:
        inner = (
            '<path d="M32 4C45 4 55 15 56 31L56 49C56 55 52 59 46 59L18 59C12 59 8 55 8 49L8 31'
            'C9 15 19 4 32 4Z" fill="%s" stroke="%s" stroke-width="3.4"/>'
            '<path d="M32 12C41 12 47 19 48 28L48 34L16 34L16 28C17 19 23 12 32 12Z" fill="%s"/>'
            '<rect x="8" y="41" width="48" height="4.6" fill="%s"/>'
            % (body, line, glass, accent))
        vb, w, h = "0 0 64 64", size, size
    else:
        inner = (
            '<path d="M6 24C6 15 11 12 20 12L74 12C93 12 108 20 117 32C108 44 93 52 74 52L20 52'
            'C11 52 6 49 6 40Z" fill="%s" stroke="%s" stroke-width="3.4"/>'
            '<path d="M16 20L68 20C80 20 90 24 97 30L97 34L16 34Z" fill="%s"/>'
            '<rect x="6" y="38" width="110" height="4.6" fill="%s"/>'
            '<circle cx="27" cy="53" r="5" fill="%s"/><circle cx="86" cy="53" r="5" fill="%s"/>'
            % (body, line, glass, accent, wheel, wheel))
        vb, w, h = "0 0 128 64", size * 2, size
    return ('<svg class="train-ic" viewBox="%s" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>' % (vb, w, h, inner))


def train_marker_datauri(palette=None):
    """地图上「列车行进」光点用的高铁车头贴图（data URI）。"""
    svg = train_icon_svg(64, palette, front=True).replace(' class="train-ic"', "", 1)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def air_icon_svg(size=30, palette=None):
    """返回一个「飞机」内联 SVG（侧视，机头朝右）。

    与 train_icon_svg 同样的两条约束：全程平涂、不使用渐变
    （避免同一页面多处内联时 <defs> id 互相冲突）；viewBox 用 128×64，
    与高铁侧视同比例，替换时不会把版面撑变形。
    """
    pal = _palette(palette)
    body, glass = "#F3F7FF", "#123A5E"
    line = pal["accent"]
    fin = pal["accent"]
    inner = (
        # 机身（机头朝右）
        '<path d="M8 30C8 26.5 12 24.5 20 24.5L92 24.5C104 24.5 116 28 122 31'
        'C116 34 104 37.5 92 37.5L20 37.5C12 37.5 8 35.5 8 32Z" '
        'fill="%s" stroke="%s" stroke-width="3.4"/>'
        # 驾驶舱玻璃
        '<path d="M96 27.2L111 29.6L111 32.4L96 34.8Z" fill="%s"/>'
        # 主翼（上掠）
        '<path d="M50 26.5L72 7L84 7L72 26.5Z" fill="%s"/>'
        # 尾翼（上掠）
        '<path d="M14 26.5L3 11L11 11L24 26.5Z" fill="%s"/>'
        % (body, line, glass, fin, fin))
    return ('<svg class="train-ic" viewBox="0 0 128 64" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>'
            % (size * 2, size, inner))


def vehicle_icon_svg(mode, size=30, palette=None, *, front=False):
    """按 mode 返回交通工具图标：air → 飞机，其余（含未知 mode）→ 高铁车头。"""
    if mode == "air":
        return air_icon_svg(size, palette)
    return train_icon_svg(size, palette, front=front)


def vehicle_marker_datauri(mode, palette=None):
    """地图上「行进光点」用的交通工具贴图（data URI），按 mode 切换。"""
    if mode == "air":
        svg = air_icon_svg(64, palette).replace(' class="train-ic"', "", 1)
    else:
        svg = train_icon_svg(64, palette, front=True).replace(' class="train-ic"', "", 1)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# ── 措辞表：所有面向用户的火车/飞机专属表达一律走这里，禁止在函数里写死 ──
# 新增措辞时先在三张表里都填上 —— 只填 train 会导致 air 模式回退成火车话术。
#
# ⚠️ mode 是**记录级**的：一趟出差既有火车又有飞机是常态，同一页里第 3 行是航班、
# 第 4 行是火车完全正常。所以 `w()` 的实参一律是「当前这一行的 mode」，
# 只有整页同质（全是火车 / 全是航班）时才退化成整页一个 mode（'mixed' 表示混排）。
MODE_W = {
    "train": {"vehicle": "高铁车头", "no": "车次", "no_unknown": "车次未知",
              "time": "开车时间", "depart": "开车", "from": "出发站", "to": "到达站",
              "seat": "席别", "cruise": "列车巡航", "title": "火车票行程单", "ticket": "车票",
              "day": "乘车日", "pax": "乘车人"},
    "air":   {"vehicle": "飞机", "no": "航班号", "no_unknown": "航班未知",
              "time": "起飞时间", "depart": "起飞", "from": "出发机场", "to": "到达机场",
              "seat": "舱位", "cruise": "航班巡航", "title": "机票行程单", "ticket": "机票",
              "day": "乘机日", "pax": "乘机人"},
    # 混排页的「整页级」措辞（时间轴/明细表/图例仍逐条出各自的，只有标题、页脚、
    # 副标题、巡航按钮这类确实只有一个的东西才用它）
    "mixed": {"vehicle": "高铁车头 / 飞机", "no": "车次 / 航班号", "no_unknown": "车次未知",
              "time": "出发时间", "depart": "出发", "from": "出发站 / 机场", "to": "到达站 / 机场",
              "seat": "席别 / 舱位", "cruise": "行程巡航", "title": "行程单",
              "ticket": "车票 / 机票", "day": "出行日", "pax": "乘车人 / 乘机人"},
}


def w(mode, key):
    """按 mode 取措辞。未知 mode 一律退回 train —— 宁可显示火车话术，也不要 KeyError。"""
    return (MODE_W.get(mode) or MODE_W["train"])[key]


def page_mode(modes, default="train"):
    """把一批记录的 mode 收敛成整页 mode：全同质 → 该 mode；混排 → 'mixed'。

    只用于标题 / 副标题 / 页脚 / 巡航按钮这类一页只有一个的位置；
    时间轴、明细表、地图巡航一律用**记录级** mode，不看这个。
    """
    seen = []
    for m in modes or []:
        m = m or default
        if m not in seen:
            seen.append(m)
    if not seen:
        return default if default in MODE_W else "train"
    if len(seen) == 1:
        return seen[0]
    return "mixed"


# ─────────────────────── GeoJSON 组装 ───────────────────────

def build_geojson(geo=None, precision=4):
    """把 assets/trainmap/china_provinces.json 转成 ECharts 可注册的 GeoJSON。

    省界 + 南海九段线合成一个 FeatureCollection；九段线作为**独立要素**，
    由 `geo.regions` 按 name 单独上色。
    """
    geo = geo if geo is not None else trainmap.load_provinces()

    def _ring(r):
        out = []
        for pt in r or []:
            try:
                out.append([round(float(pt[0]), precision), round(float(pt[1]), precision)])
            except (TypeError, ValueError, IndexError):
                continue
        if len(out) >= 3 and out[0] != out[-1]:
            out.append(out[0])
        return out

    feats = []
    for prov in geo.get("provinces") or []:
        polys = [r for r in (_ring(x) for x in (prov.get("rings") or [])) if len(r) >= 4]
        if not polys:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"name": prov.get("name") or ""},
            "geometry": {"type": "MultiPolygon", "coordinates": [[p] for p in polys]},
        })

    dash = [r for r in (_ring(x) for x in (geo.get("nine_dash") or [])) if len(r) >= 4]
    if dash:
        feats.append({
            "type": "Feature",
            "properties": {"name": HTML_DASH_NAME},
            "geometry": {"type": "MultiPolygon", "coordinates": [[p] for p in dash]},
        })

    return {"type": "FeatureCollection", "features": feats}


# ─────────────────────── 数据组装 ───────────────────────

def _enrich(records, index, default_mode="train"):
    """逐条行程补上坐标/里程/匹配状态/是否境外 —— 供时间轴与时间分布使用。

    `mode` 是**逐条**的（由 `trainmap.resolve_mode` 在 `to_itinerary_records` 判定），
    这里原样带出；`default_mode` 只在记录自身没带时兜底。
    """
    out = []
    for r in records:
        a_raw, b_raw = r["from_name"], r["to_name"]
        abroad = trainmap._is_abroad(a_raw) or trainmap._is_abroad(b_raw)
        a = trainmap.match_station(a_raw, index)
        b = trainmap.match_station(b_raw, index)
        km = None
        if a["ok"] and b["ok"]:
            km = trainmap.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        out.append({
            "mode": r.get("mode") or default_mode,
            "date": _short_date(r["date"]),
            "dk": _date_parts(r["date"]),
            "train": r["train"],
            "from": a_raw or "未知",
            "to": b_raw or "未知",
            "from_r": a["resolved"] if a["ok"] else "",
            "to_r": b["resolved"] if b["ok"] else "",
            "from_ok": a["ok"], "to_ok": b["ok"],
            "abroad": abroad,
            "km": round(km, 1) if km is not None else None,
            "amount": round(float(r["amount"] or 0.0), 2),
            "passenger": r["passenger"],
            "tag": "改签" if r.get("special_type") == "改签" else "",
            "matched": bool(a["ok"] and b["ok"]) and not abroad,
        })
    return out


def _detail_records(invoices, index, default_mode="train"):
    """整理逐条报销明细，**含退票**，字段尽量列全（供明细表 / 打印 / 替代行程单）。

    与 `_enrich` 的区别在口径：
    * `_enrich`（→ timeline / journey）是「行程」视角 —— 退票不是一次旅程，剔除；
    * `_detail_records`（→ details）是「报销」视角 —— 票面金额与退票费都要逐条可查，
      故退票保留并用 `kind` 标注。

    每条自带 `mode`（逐条判定），明细表按行出图标与措辞。
    """
    out = []
    for i, inv in enumerate(invoices or [], 1):
        if not isinstance(inv, dict):
            continue
        raw_from = str(inv.get("from_") or "").strip()
        raw_to = str(inv.get("to_") or "").strip()
        a = trainmap.match_station(raw_from, index)
        b = trainmap.match_station(raw_to, index)
        abroad = trainmap._is_abroad(raw_from) or trainmap._is_abroad(raw_to)
        km = None
        if a["ok"] and b["ok"]:
            km = trainmap.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        date_raw = str(inv.get("train_date") or "")
        kind = str(inv.get("special_type") or "")
        try:
            seq = int(inv.get("seq") or i)
        except (TypeError, ValueError):
            seq = i
        out.append({
            "seq": seq,
            "mode": trainmap.resolve_mode(inv, default_mode),
            "date": _short_date(date_raw) or "日期未知",
            "time": _time_part(date_raw),
            "train": str(inv.get("train") or ""),
            "from": raw_from or "未知",
            "to": raw_to or "未知",
            "from_r": a["resolved"] if a["ok"] else "",
            "to_r": b["resolved"] if b["ok"] else "",
            "km": round(float(km), 1) if km is not None else None,
            "seat": str(inv.get("seat") or ""),
            "seat_class": str(inv.get("seat_class") or ""),
            "passenger": str(inv.get("passenger") or ""),
            "id_no": str(inv.get("id_no") or ""),
            "ticket_no": str(inv.get("ticket_no") or ""),
            "invoice_no": str(inv.get("invoice_no") or ""),
            "invoice_date": str(inv.get("invoice_date") or ""),
            "amount": round(float(inv.get("amount") or 0.0), 2),
            "fare_label": str(inv.get("fare_label") or "票价"),
            "kind": kind,                       # "" / "退票" / "改签"
            "matched": bool(a["ok"] and b["ok"]) and not abroad,
            "abroad": abroad,
            "review": (str(inv.get("review_reason") or "")
                       if inv.get("needs_review") else ""),
            "filename": str(inv.get("filename") or ""),
        })
    return out


def build_payload(invoices, palette=None, *, title="行程流转图",
                  subtitle=None, source_note=None, max_timeline=_MAX_TIMELINE,
                  mode="train"):
    """把发票列表整理成 HTML 需要的全部结构化数据。

    mode：单条记录没有交通信号时的**缺省** mode（'train' 默认，向后兼容纯火车场景）。
         真正的判定是**逐条**的（`trainmap.resolve_mode`）：同一页可以既有火车又有航班。
         整页 mode（'train' / 'air' / 'mixed'）由 `page_mode` 收敛，只用于标题、副标题、
         页脚这类一页只有一个的位置；时间轴、明细表、地图巡航一律逐条。
    """
    records = trainmap.to_itinerary_records(invoices, mode)
    index = trainmap.load_station_index()
    stats = trainmap.aggregate_routes(records, index, top_n=12)
    enriched = [e for e in _enrich(records, index, mode)]
    details = _detail_records(invoices, index, mode)

    # ── 逐条 mode -> 整页 mode（只服务标题 / 副标题 / 页脚 / 巡航按钮）──
    mode_count, mode_seen = {}, []
    for e in enriched:
        m = e["mode"] or mode
        mode_count[m] = mode_count.get(m, 0) + 1
        if m not in mode_seen:
            mode_seen.append(m)
    for d in details:
        if (d["mode"] or mode) not in mode_seen:
            mode_seen.append(d["mode"] or mode)
            mode_count.setdefault(d["mode"] or mode, 0)
    modes = ([m for m in trainmap.TRAIN_MODES if m in mode_seen]
             + [m for m in mode_seen if m not in trainmap.TRAIN_MODES])
    pm = page_mode(modes, mode)

    pal = _palette(palette)

    # 站点
    stations = []
    for name, s in sorted(stats["stations"].items(), key=lambda kv: (-kv[1]["count"], kv[0])):
        stations.append({"name": name, "lon": round(float(s["lon"]), 4),
                         "lat": round(float(s["lat"]), 4),
                         "city": s.get("city") or "", "count": int(s["count"])})

    coord = {s["name"]: (s["lon"], s["lat"]) for s in stations}

    # 线路（无向聚合），带上两端坐标与频率比
    max_cnt = max([rt["count"] for rt in stats["routes"]] or [1])
    routes = []
    for rt in stats["routes"]:
        ca, cb = coord.get(rt["from_key"]), coord.get(rt["to_key"])
        if not ca or not cb:
            continue
        routes.append({
            "a": rt["from_key"], "b": rt["to_key"],
            "c": [list(ca), list(cb)],
            "count": int(rt["count"]),
            "km": round(float(rt["distance_km"]), 1),
            "ratio": round(rt["count"] / max_cnt, 4),
        })

    # 按「出行顺序」排列的行程段序列 —— 地图上的单车巡航按这个顺序一段段跑。
    # 只保留两端都能定位的行程；排序键为乘车日（无日期的排到最后）。
    jseq = sorted(
        [e for e in enriched if e["matched"] and e["km"] is not None],
        key=lambda e: (e["dk"][0] or 9999, e["dk"][1] or 0, e["dk"][2] or 0))
    journey = []
    for e in jseq:
        ca, cb = coord.get(e["from_r"]), coord.get(e["to_r"])
        if not ca or not cb:
            continue
        journey.append({
            "a": e["from_r"], "b": e["to_r"],
            "c": [list(ca), list(cb)],
            "km": round(float(e["km"]), 1),
            "date": e["date"], "train": e["train"], "pax": e["passenger"] or "",
            # 逐条 mode：巡航光点跑到这里时切成对应交通工具，措辞也随之切换
            "mode": e["mode"], "cruise": w(e["mode"], "cruise"),
        })

    # 城市到访排行
    city_hits = {}
    for s in stations:
        if s["city"]:
            city_hits[s["city"]] = city_hits.get(s["city"], 0) + s["count"]
    city_rank = sorted(({"name": k, "count": v} for k, v in city_hits.items()),
                       key=lambda x: (-x["count"], x["name"]))

    # 线路 Top（给条形图）
    top_routes = [{"name": f"{r['a']} → {r['b']}", "count": r["count"], "km": r["km"]}
                  for r in routes[:10]]

    # 时间分布：跨度 <= 100 天按日、否则按月
    trips = [e for e in enriched if e["matched"]]
    dks = [e["dk"] for e in trips if e["dk"][0]]
    dist = []
    if dks:
        y_min, y_max = min(x[0] for x in dks), max(x[0] for x in dks)
        day_min = datetime.date(*min(dks)) if min(dks)[0] else None
        day_max = datetime.date(*max(dks)) if max(dks)[0] else None
        by_month = bool(day_min and day_max and (day_max - day_min).days > 100)
        buckets = {}
        for e in trips:
            if not e["dk"][0]:
                continue
            y, mo, d = e["dk"]
            key = f"{y:04d}-{mo:02d}" if by_month else f"{y:04d}-{mo:02d}-{d:02d}"
            b = buckets.setdefault(key, {"trips": 0, "km": 0.0})
            b["trips"] += 1
            b["km"] += float(e["km"] or 0.0)
        dist = [{"label": k, "trips": v["trips"], "km": round(v["km"], 1)}
                for k, v in sorted(buckets.items())]
        dist_kind = "month" if by_month else "day"
    else:
        dist_kind = "day"

    # KPI
    dates = sorted([e["date"] for e in trips if e["date"]])
    passengers = sorted({e["passenger"] for e in enriched
                         if e["passenger"] and e["passenger"] not in ("未知", "")})
    top_station = stations[0] if stations else None

    # 出发 / 到达 频次最高的站（给地图空白区的「行程洞察」用）
    from_cnt, to_cnt = {}, {}
    for e in trips:
        if e.get("from_r"):
            from_cnt[e["from_r"]] = from_cnt.get(e["from_r"], 0) + 1
        if e.get("to_r"):
            to_cnt[e["to_r"]] = to_cnt.get(e["to_r"], 0) + 1
    top_from = max(from_cnt.items(), key=lambda kv: (kv[1], kv[0])) if from_cnt else ("—", 0)
    top_to = max(to_cnt.items(), key=lambda kv: (kv[1], kv[0])) if to_cnt else ("—", 0)
    longest = max(routes, key=lambda r: r["km"]) if routes else None

    kpi = {
        "trips": int(stats["trip_count"]),
        "km": round(float(stats["total_km"]), 1),
        "cities": int(stats["city_count"]),
        "stations": int(stats["station_count"]),
        "routes": len(routes),
        "top_station": top_station["name"] if top_station else "—",
        "top_station_count": top_station["count"] if top_station else 0,
        "date_from": dates[0] if dates else "",
        "date_to": dates[-1] if dates else "",
        "days": len({e["date"] for e in trips if e["date"]}),
        "passengers": passengers,
        "max_route_km": round(max([r["km"] for r in routes] or [0.0]), 1),
        "avg_km": round(float(stats["total_km"]) / stats["trip_count"], 1) if stats["trip_count"] else 0.0,
        "top_from": top_from[0], "top_from_count": top_from[1],
        "top_to": top_to[0], "top_to_count": top_to[1],
        "longest": (f'{longest["a"]} → {longest["b"]}' if longest else "—"),
        "longest_km": (longest["km"] if longest else 0.0),
    }

    # ── 报销明细（含退票）+ 金额汇总 —— 用于替代原行程单的明细表与汇总栏 ──
    total_amt = round(sum(d["amount"] for d in details), 2)
    refund_amt = round(sum(d["amount"] for d in details if d["kind"] == "退票"), 2)
    dts = sorted({d["date"] for d in details
                  if d["date"] and d["date"] != "日期未知"})
    money = {
        "count": len(details),
        "total": total_amt,
        "normal": round(total_amt - refund_amt, 2),
        "refund": refund_amt,
        "refundCount": len([d for d in details if d["kind"] == "退票"]),
        "changeCount": len([d for d in details if d["kind"] == "改签"]),
        "upper": amount_upper(total_amt),
        "dateFrom": dts[0] if dts else "",
        "dateTo": dts[-1] if dts else "",
        # 退票不是实际行程，不计入里程 —— 与 KPI「总里程」保持同一口径
        "kmSum": round(sum(d["km"] for d in details
                           if d["km"] is not None and d["kind"] != "退票"), 1),
        "tripCount": len([d for d in details if d["kind"] != "退票"]),
        "reviewCount": len([d for d in details if d["review"]]),
    }

    # ── 头部徽章 / KPI 卡数据（前端统一渲染，住宿票 HTML 共用同一套组件）──
    chips = []
    if kpi["date_from"] or kpi["date_to"]:
        chips.append(["行程日期", f'{kpi["date_from"] or "?"} 至 {kpi["date_to"] or "?"}'])
    if kpi["passengers"]:
        chips.append([w(pm, "pax"), "、".join(kpi["passengers"])])
    chips.append(["有效行程", f'{kpi["trips"]} 段'])
    if money["count"]:
        chips.append(["票面总金额", f'￥{money["total"]:,.2f}'])
    if money["refundCount"]:
        chips.append(["含退票", f'{money["refundCount"]} 张 · 不计次'])
    if money["reviewCount"]:
        chips.append(["待复核", f'{money["reviewCount"]} 张'])
    # 混排页才出这条 —— 火车与航班各多少段，一眼能核对有没有漏判
    if pm == "mixed":
        chips.append(["交通构成", "　·　".join(
            f'{w(m, "ticket")} {mode_count.get(m, 0)} 段' for m in modes)])

    kpi_cards = [
        {"lab": "出行次数", "count": kpi["trips"], "unit": "次",
         "sub": "不含退票 · 改签计 1 次", "color": pal["accent"]},
        {"lab": "总里程", "count": kpi["km"], "dec": 0, "unit": "km",
         "sub": "站点间球面直线距离", "color": pal["accent2"]},
        {"lab": "覆盖城市", "count": kpi["cities"], "unit": "座",
         "sub": "按车站所属城市去重", "color": "#8B7BF0"},
        {"lab": "覆盖车站", "count": kpi["stations"], "unit": "个",
         "sub": "去重后的到访车站", "color": "#34D399"},
        {"lab": "最常到访", "text": kpi["top_station"], "cls": "txt",
         "sub": f'到访 {kpi["top_station_count"]} 次', "color": "#F472B6"},
    ]

    return {
        "title": title,
        "subtitle": subtitle or f'本次上传{w(pm, "ticket")}的行程流转',
        "palette": pal,
        #: 整页 mode（'train' / 'air' / 'mixed'）—— 只服务标题、副标题、页脚、巡航按钮
        "mode": pm,
        #: 本页实际出现的 mode（按 train → air 排），图例据此决定要不要两个都出
        "modes": modes,
        "modeCount": mode_count,
        #: 逐条 cruise 光点贴图：跑火车段用高铁车头、跑航班段用飞机
        "markers": {m: vehicle_marker_datauri(m, pal) for m in trainmap.TRAIN_MODES},
        "trainMarker": vehicle_marker_datauri("air" if pm == "air" else "train", pal),
        "kpi": kpi,
        "stations": stations,
        "routes": routes,
        "journey": journey,
        "topRoutes": top_routes,
        "cityRank": city_rank,
        "dateDist": dist,
        "dateDistKind": dist_kind,
        "timeline": enriched[:max_timeline],
        "timelineTotal": len(enriched),
        "timelineTruncated": len(enriched) > max_timeline,
        "details": details[:_MAX_DETAILS],
        "detailsTotal": len(details),
        "detailsTruncated": len(details) > _MAX_DETAILS,
        "money": money,
        "chips": chips,
        "kpiCards": kpi_cards,
        "unmatched": [str(x) for x in (stats["unmatched"] or [])],
        "abroad": [{"train": a.get("train", ""), "date": _short_date(a.get("date", "")),
                    "from": a.get("from_name", ""), "to": a.get("to_name", "")}
                   for a in (stats["hk_tw_abroad"] or [])],
        "hasRefund": bool([i for i in (invoices or []) if isinstance(i, dict)
                           and i.get("special_type") == "退票"]),
        "sourceNote": source_note or "",
    }


# ─────────────────────── 行程明细（服务端渲染） ───────────────────────
# 两个视角共用同一份 `payload["details"]`：
#   * 行程时间轴 —— 「行程」视角，退票不是一次旅程，不入轴；
#   * 报销明细表 —— 「报销」视角，票面与退票费逐条列全，口径对齐原行程单明细表。

def _money_strip(payload):
    """《行程报销汇总》—— 票面金额一眼可见，替代原行程单表头那一栏。"""
    m = payload.get("money") or {}
    pax = "、".join(payload["kpi"].get("passengers") or []) or R.DASH
    pm = payload.get("mode") or "train"
    return R.money_strip(
        "行程报销汇总",
        f'行程日期 {m.get("dateFrom") or R.DASH} 至 {m.get("dateTo") or R.DASH}'
        f'　·　{w(pm, "pax")} {pax}',
        [("票面总金额", f'￥{m.get("total", 0):,.2f}', ""),
         ("正常车票", f'￥{m.get("normal", 0):,.2f}', "ok"),
         ("退票费", f'￥{m.get("refund", 0):,.2f}',
          "warn" if m.get("refund") else "dim"),
         ("票据张数", f'{m.get("count", 0)} 张', ""),
         ("有效行程", f'{m.get("tripCount", 0)} 段', "")],
        "合计（大写）", m.get("upper") or "零元整")


def _timeline_html(payload):
    # ⚠️ 逐条：每一行用自己的 mode 出图标与措辞（同一页火车与航班混排是常态）
    mode = payload.get("mode") or "train"
    rows = [d for d in (payload.get("details") or []) if d.get("kind") != "退票"]
    if not rows:
        return ('<div class="empty">本次没有可用于时间轴的行程明细'
                '（可能全部为退票，或站名均未匹配）。</div>')

    pal = payload.get("palette")
    out, last_date = [], None
    for e in rows:
        md = e.get("mode") or mode
        date = e.get("date") or "日期未知"
        if date != last_date:
            out.append(f'<div class="tl-day"><span>{_esc(date)}</span></div>')
            last_date = date

        cls = "tl-item"
        if not e.get("matched"):
            cls += " muted"
        elif e.get("kind") == "改签":
            cls += " changed"

        tags = []
        if e.get("kind") == "改签":
            tags.append('<i class="tag tag-mod">改签</i>')
        if e.get("abroad"):
            tags.append('<i class="tag tag-out">港澳台/国际</i>')
        elif not e.get("matched"):
            tags.append('<i class="tag tag-warn">站名未匹配</i>')
        if e.get("review"):
            tags.append(f'<i class="tag tag-warn">待复核 {_esc(e["review"])}</i>')

        seat = e.get("seat") or e.get("seat_class") or ""
        seat_html = (f'<span class="tl-seat" title="{_esc(w(md, "seat"))}">{_esc(seat)}</span>'
                     if seat else "")
        time_html = (f'<span class="tl-time">{_esc(e["time"])} {_esc(w(md, "depart"))}</span>'
                     if e.get("time") else "")
        km_txt = f'{_fmt_km(e["km"])} km' if e.get("km") is not None else "里程未知"

        # 二级信息：有值才渲染，不留空标签
        sub = []
        if e.get("passenger"):
            sub.append(f'<span>乘客<i>{_esc(e["passenger"])}</i></span>')
        if e.get("id_no"):
            sub.append(f'<span class="id">证件<i>{_esc(e["id_no"])}</i></span>')
        if e.get("invoice_no"):
            sub.append(f'<span class="id">发票号码<i>{_esc(e["invoice_no"])}</i></span>')
        if e.get("ticket_no"):
            sub.append(f'<span class="id">电子客票号<i>{_esc(e["ticket_no"])}</i></span>')
        if e.get("invoice_date"):
            sub.append(f'<span>开票日期<i>{_esc(e["invoice_date"])}</i></span>')
        if e.get("filename"):
            sub.append(f'<span>源文件<i>{_esc(e["filename"])}</i></span>')
        sub_html = f'<div class="tl-sub">{"".join(sub)}</div>' if sub else ""

        ic = vehicle_icon_svg(md, 13, pal)
        out.append(
            f'<div class="{cls}">'
            f'<span class="tl-node"></span>'
            f'<div class="tl-body">'
            f'<div class="tl-head">'
            f'<span class="tl-seq">{e.get("seq", 0):02d}</span>'
            f'{ic}<span class="tl-train">{_esc(e.get("train") or w(md, "no_unknown"))}</span>'
            f'{seat_html}{"".join(tags)}{time_html}'
            f'</div>'
            f'<div class="tl-route">'
            f'<span class="st">{_esc(e.get("from"))}</span>'
            f'<span class="arw">→</span>'
            f'<span class="st">{_esc(e.get("to"))}</span>'
            f'<span class="tl-km">{km_txt}</span>'
            f'</div>'
            f'{sub_html}'
            f'<div class="tl-meta"><span>报销金额</span>'
            f'<span class="amt">￥{e.get("amount", 0):,.2f}'
            f'<i class="lb">{_esc(e.get("fare_label") or "票价")}</i></span>'
            f'</div>'
            f'</div></div>')
    return "".join(out)


def _detail_table_html(payload):
    """报销明细表 —— 口径对齐原行程单明细表（含退票行、改签行与合计行）。"""
    rows = payload.get("details") or []
    if not rows:
        return '<div class="empty">本次没有可列示的票据明细。</div>'
    m = payload.get("money") or {}
    mode = payload.get("mode") or "train"
    # 表头一页只有一个，混排时退到 'mixed' 的中性措辞；行内再逐条出图标与措辞
    head = ["序号", w(mode, "time"), w(mode, "from"), w(mode, "to"), w(mode, "no"),
            w(mode, "seat"), "乘客", "里程", "金额", "发票号码"]
    th = "".join(f'<th>{_esc(h)}</th>' for h in head)
    pal = payload.get("palette")

    trs = []
    for e in rows:
        md = e.get("mode") or mode
        kind = e.get("kind") or ""
        cls = (' class="refund"' if kind == "退票"
               else (' class="change"' if kind == "改签" else ""))
        tag = ""
        if kind == "退票":
            tag = '<i class="kbd k-refund">退票费</i>'
        elif kind == "改签":
            tag = '<i class="kbd k-change">改签</i>'
        elif e.get("review"):
            tag = '<i class="kbd k-warn">待复核</i>'
        km = _fmt_km(e["km"]) if e.get("km") is not None else "—"
        # 行内图标逐条切换（火车=高铁车头 / 航班=飞机），title 给出这一行的措辞
        ic = vehicle_icon_svg(md, 10, pal)
        trs.append(
            f'<tr{cls}>'
            f'<td class="seq">{e.get("seq", 0)}</td>'
            f'<td class="d">{_esc(e.get("date"))}'
            f'<i>{_esc(e.get("time") or "—")}</i></td>'
            f'<td>{_esc(e.get("from"))}</td>'
            f'<td>{_esc(e.get("to"))}</td>'
            f'<td class="tr" title="{_esc(w(md, "no"))}">{ic}'
            f'{_esc(e.get("train") or "—")}</td>'
            f'<td class="d">{_esc(e.get("seat") or e.get("seat_class") or "—")}</td>'
            f'<td>{_esc(e.get("passenger") or "—")}</td>'
            f'<td class="num">{km}</td>'
            f'<td class="num"><span class="amt">￥{e.get("amount", 0):,.2f}</span>{tag}</td>'
            f'<td class="inv">{_esc(e.get("invoice_no") or "—")}</td>'
            f'</tr>')

    foot = (f'<tfoot><tr><td colspan="8">合计 {m.get("count", 0)} 张'
            f'（正常车票 ￥{m.get("normal", 0):,.2f}'
            + (f' · 退票费 ￥{m.get("refund", 0):,.2f}' if m.get("refund") else "")
            + f'）</td><td class="num"><span class="amt">￥{m.get("total", 0):,.2f}</span></td>'
            f'<td class="inv">{_esc(m.get("upper") or "")}</td></tr></tfoot>')
    return (f'<table class="dt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody>{foot}</table>')


def _detail_note(payload):
    """明细脚注：口径、截断、待复核提醒。"""
    d = payload.get("details") or []
    m = payload.get("money") or {}
    trips = len([x for x in d if x.get("kind") != "退票"])
    bits = [f'共 {m.get("count", 0)} 张票据',
            f'有效行程 {trips} 段',
            f'退票 {m.get("refundCount", 0)} 张',
            f'改签 {m.get("changeCount", 0)} 张',
            f'里程 {_fmt_km(m.get("kmSum", 0))} km']
    txt = "　·　".join(bits)
    if m.get("reviewCount"):
        txt += f'　·　{m["reviewCount"]} 张票据有字段待复核'
    if payload.get("detailsTruncated"):
        txt += (f'　·　仅展示前 {len(d)} 条，共 {payload.get("detailsTotal", 0)} 条')
    return _esc(txt)


# ─────────────────────── 模板 ───────────────────────

def _map_inset(payload):
    """地图左下水域空白处的浮动「行程洞察」卡（顺带解决构图留白）。"""
    k = payload["kpi"]
    day_lab = w(payload.get("mode") or "train", "day")   # 乘车日 / 乘机日 / 出行日
    rows = [
        ("最常出发", k.get("top_from") or "—", f'{k.get("top_from_count", 0)} 次'),
        ("最常到达", k.get("top_to") or "—", f'{k.get("top_to_count", 0)} 次'),
        ("单程最长", k.get("longest") or "—", f'{_fmt_km(k.get("longest_km", 0))} km'),
        ("出行天数", "", f'{k.get("days", 0)} 天'),
    ]
    body = "".join(
        f'<div class="mi-row"><span>{_esc(a)}</span>'
        + (f'<b>{_esc(b)}</b>' if b else f'<b class="dim">{_esc(day_lab)}</b>')
        + f'<i>{_esc(c)}</i></div>'
        for a, b, c in rows)
    return ('<div class="map-inset"><div class="mi-t">行程洞察</div>' + body + '</div>')


def _map_legend(payload):
    """地图下方图例 —— **两种交通工具都出**，本页没有的那个压暗但保留。

    ⚠️ 混排是常态（一趟出差既有火车又有飞机），只画当前页那一个图标的话，
    另一半连线的图标含义就没有出处，等于半个图例说不清。
    """
    pal = payload["palette"]
    modes = payload.get("modes") or ["train"]
    counts = payload.get("modeCount") or {}
    caps = []
    for m in trainmap.TRAIN_MODES:
        ic = vehicle_icon_svg(m, 11, pal)
        cnt = int(counts.get(m) or 0)
        off = " off" if m not in modes else ""
        caps.append(
            f'<span class="cap-{m}{off}">{ic}{w(m, "vehicle")} = {w(m, "cruise")}'
            f'（本页 {cnt} 段）</span>')
    return (
        '<div class="map-cap">'
        + "".join(caps) +
        f'<span><i style="background:{pal["map_dash"]}"></i>南海诸岛（含九段线）</span>'
        '<span>可滚轮缩放 / 拖拽平移</span>'
        '</div>')


# ─────────────── 模块专属样式（地图 / 巡航）───────────────

_MODULE_CSS = r"""
/* ---------- 轻量版（无地图/图表）：明细单列铺开，避免右列大片留白 ---------- */
.grid.solo{grid-template-columns:minmax(0,1fr)}

/* ---------- 地图 ---------- */
.map-panel{padding-bottom:12px}
#map{width:100%;height:688px}
.map-cap{display:flex;gap:18px;flex-wrap:wrap;color:var(--text-faint);font-size:11.5px;padding:2px 4px 6px}
.map-cap i{display:inline-block;width:22px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.map-cap .cap-train,.map-cap .cap-air{display:inline-flex;align-items:center;gap:6px}
/* 本页没有的那种交通工具：压暗保留 —— 图例要能解释另一种连线 */
.map-cap .off{opacity:.42}

/* 时间轴 / 图例里的高铁车头 */
.train-ic{display:inline-block;vertical-align:-.14em;flex:none}
.tl-head .train-ic{margin-right:-2px;opacity:.95;filter:drop-shadow(0 0 6px rgba(56,189,248,.4))}
/* 明细表「车次 / 航班号」列的行内交通工具图标（逐条） */
.dt .tr .train-ic{margin-right:5px;vertical-align:-.15em}

/* 巡航指示：全图只有这一个高铁车头在跑，角标只说明「现在跑到第几段」 */
#cruise{position:absolute;right:20px;top:72px;display:flex;align-items:center;gap:10px;
  padding:7px 13px;border-radius:999px;background:rgba(9,15,29,.78);
  border:1px solid var(--panel-border);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  font-size:12px;color:var(--text-dim);box-shadow:0 14px 34px -20px rgba(0,0,0,.95);
  pointer-events:none;max-width:min(620px,calc(100% - 40px));transition:opacity .25s}
#cruise.off{opacity:.4}
#cruise b{color:var(--text);font-weight:700;font-size:12.5px;flex:none}
#cruise .c-dim{color:var(--text-faint)}
#cruise .c-run{display:inline-flex;align-items:center;gap:6px;color:var(--accent-soft);
  font-size:11.5px;flex:none;letter-spacing:.6px}
#cruise .c-run i{width:6px;height:6px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 8px 2px rgba(56,189,248,.7);animation:cpulse 1.6s ease-in-out infinite}
@keyframes cpulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.65)}}
#cruise .c-seg{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#cruise .c-km{color:var(--accent-soft);font-size:11.5px;flex:none}

/* 地图空白区（西南海域）的浮动洞察卡 */
.map-inset{position:absolute;left:22px;bottom:66px;width:238px;padding:13px 15px;border-radius:14px;
  background:rgba(9,15,29,.74);border:1px solid var(--panel-border);
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  box-shadow:0 16px 40px -22px rgba(0,0,0,.95);pointer-events:none}
.map-inset .mi-t{font-size:11.5px;color:var(--text-faint);letter-spacing:1.4px;margin-bottom:9px;
  display:flex;align-items:center;gap:7px}
.map-inset .mi-t::before{content:"";width:3px;height:11px;border-radius:2px;background:var(--accent)}
.mi-row{display:flex;align-items:baseline;gap:8px;font-size:12.5px;padding:3.5px 0}
.mi-row span{color:var(--text-faint);flex:none;width:52px;font-size:11.5px}
.mi-row b{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mi-row b.dim{color:var(--text-dim);font-weight:500}
.mi-row i{margin-left:auto;font-style:normal;color:var(--accent-soft);font-size:11.5px;flex:none}

/* 地图尺寸自适应（通用尺寸规则在 report_html 的共享媒体查询里） */
@media (max-width:1280px){ #map{height:560px} }
@media (max-width:760px){
  .map-inset{width:196px;left:14px;bottom:60px;padding:11px 12px}
  #cruise{right:12px;top:64px;gap:7px;padding:6px 10px;font-size:11px;max-width:calc(100% - 24px)}
  #cruise .c-km{display:none}
  .mi-row{font-size:11.5px}
  #map{height:430px}
}
"""


# ─────────────── 页面主体 ───────────────

# ── 图表区：地图 + 三张分析图 ──
# 仅在「内嵌地图」时拼入；关闭地图时整段省略（避免空 div 与多余的 ECharts 依赖）。
_BODY_CHARTS = r"""
  <section class="grid">
    <div class="col">
      <div class="panel map-panel no-print">
        <div class="panel-head">
          <h2>行程流转地图</h2>
          <div class="tools">
            <button class="btn on" id="btn-flow">__CRUISE__</button>
            <button class="btn on" id="btn-label">站名标签</button>
            <button class="btn" id="btn-reset">重置视图</button>
          </div>
        </div>
        <div id="map"></div>
        <div id="cruise"></div>
        __MAP_INSET__
        __MAP_LEGEND__
      </div>
    </div>

    <div class="col">
      <div class="panel no-print">
        <div class="panel-head"><h2>高频线路 Top</h2></div>
        <div id="chartRoute" class="chart"></div>
      </div>
      <div class="panel no-print">
        <div class="panel-head"><h2>城市到访排行</h2></div>
        <div id="chartCity" class="chart sm"></div>
      </div>
    </div>
  </section>

  <section class="panel no-print" style="margin-top:18px">
    <div class="panel-head"><h2>出行时间分布</h2></div>
    <div id="chartDist" class="chart md"></div>
  </section>
"""

#: 里程概览面板（ECharts）—— 关地图时一并省略
_KM_PANEL = r"""      <div class="panel no-print">
        <div class="panel-head"><h2>里程概览</h2></div>
        <div id="chartKm" class="chart sm"></div>
      </div>
"""

# ── 明细区：行程时间轴 / 报销明细表（行程单主体，永远存在）──
_BODY_DETAIL = r"""
  <section class="__GRID_CLASS__" style="margin-top:18px">
    <div class="col">
      <div class="panel">
        <div class="panel-head">
          <h2>行程明细</h2>
          <div class="tools">
            <button class="btn on" id="btn-view-tl">行程时间轴</button>
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
__KM_PANEL__    </div>
  </section>
"""


# ─────────────── 模块脚本（地图 + 图表）───────────────

_APP_JS = r"""
const GEO = __GEOJSON__;

/* ================= 地图 ================= */
echarts.registerMap('china_full', GEO);
const mapChart = echarts.init($('#map'), null, {renderer: 'canvas'});
const maxRouteCnt = Math.max(1, ...P.routes.map(r => r.count));
const maxVisit = Math.max(1, ...P.stations.map(s => s.count));
const routeColor = r => r > .66 ? pal.route_hi : (r > .3 ? pal.route_mid : pal.route_lo);

/* 静态行程网：线细一档、辉光收一档 —— 密集区（京津石）靠「细线 + 弱辉光」拉开层次 */
const flowData = P.routes.map(r => ({
  coords: r.c, value: r.count, name: r.a + ' → ' + r.b,
  extra: r.km + ' km',
  lineStyle: {color: routeColor(r.ratio), width: .9 + 2.6 * Math.pow(r.ratio, .9),
              opacity: .4 + .4 * r.ratio, curveness: .26, cap: 'round',
              shadowColor: 'rgba(56,189,248,.35)', shadowBlur: 2.5 + 3.5 * r.ratio}
}));

/* ================= 单车巡航 =================
   全图只保留「一个」行进光点：按出行顺序，一段跑完接下一段，循环往复。
   每段时长按里程等比分配 —— 长段多跑一会儿、短段快过（段内始终匀速）。
   两套 series 交替使用，是为了让 ECharts 每次都重建动画元素，否则中途换线不重启动画。
   ⚠️ 光点贴图**逐段**取：跑火车段是高铁车头、跑航班段是飞机（P.markers），
      混排行程里整页用同一个图标会把另一半画成错误的交通工具。 */
const journey = P.journey || [];
const segN = journey.length;
const totalKm = journey.reduce((s, x) => s + (x.km || 0), 0) || 1;
const cruiseSpan = Math.min(42, Math.max(14, segN * .85));   // 跑完一轮的目标秒数
const cruiseSpeed = totalKm / cruiseSpan;                     // km/秒（观感速度，非真实车速）
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
  $('#cruise').classList.add('off');
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
  name: s.name, value: [s.lon, s.lat, s.count],
  city: s.city,
  symbolSize: 3.2 + 6.6 * Math.sqrt(s.count / maxVisit)
}));
/* 站名标签：候选前 10 个高频站，重叠时由 ECharts 自动让位；其余靠 hover 看 */
const labelData = P.stations.slice(0, 10).map(s => ({
  name: s.name, value: [s.lon, s.lat, s.count], city: s.city
}));

function mapOption(){
  return {
    backgroundColor: 'transparent',
    /* 入场动画（首次渲染 / 重置视图）保留 900ms，看起来有生气；
       但「更新动画」必须归零 —— 漫游（滚轮缩放 / 拖拽平移）与每秒一次的巡航
       setOption 都会触发 series 的位置更新，只要它走补间，线条与圆点就会一路
       追不上底图，松手后还要几秒才慢慢对齐（实测：归零后错位像素 20.7% → 1.1%）。 */
    animationDuration: 900,
    animationDurationUpdate: 0,
    animationEasing: 'cubicOut',
    animationEasingUpdate: 'linear',
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
                   '<br><span style="color:' + pal.text_dim + '">正在行进 · ' +
                   (p.data.extra || '') + '</span>';
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
      itemStyle: {
        areaColor: pal.map_fill, borderColor: pal.map_border, borderWidth: .75,
        shadowColor: 'rgba(34,211,238,.30)', shadowBlur: 14, shadowOffsetY: 2
      },
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
                symbol: 'image://' + P.trainMarker,
                symbolSize: 15, color: pal.route_flow},
       lineStyle: {curveness: .26}},
      {name: '__CRUISE_B__', type: 'lines', coordinateSystem: 'geo', zlevel: 7, silent: true,
       polyline: false, data: [],
       effect: {show: false, period: 1.4, trailLength: .3,
                symbol: 'image://' + P.trainMarker,
                symbolSize: 15, color: pal.route_flow},
       lineStyle: {curveness: .26}},
      {name: '车站', type: 'effectScatter', coordinateSystem: 'geo', zlevel: 3,
       rippleEffect: {brushType: 'stroke', scale: 1.45, period: 5},
       itemStyle: {color: pal.station, borderColor: pal.station_ring, borderWidth: 1.6,
                   shadowColor: 'rgba(255,194,77,.8)', shadowBlur: 7},
       labelLayout: {hideOverlap: true},
       data: stationData},
      {name: '站名', type: 'scatter', coordinateSystem: 'geo', zlevel: 6,
       symbolSize: 1, silent: true,
       /* ⚠️ 不能用 itemStyle.opacity:0 —— ECharts 的 label 会继承它，导致站名一起变透明。
          这里用 transparent 填充让圆点不可见、但保持 opacity:1，标签才出得来。 */
       itemStyle: {color: 'transparent'},
       label: {show: true, position: 'right', distance: 6, align: 'left', color: pal.text,
               fontSize: 12, fontWeight: 600, textShadowColor: 'rgba(0,0,0,.95)',
               textShadowBlur: 6, formatter: p => p.name},
       /* hideOverlap 按数据顺序保留「到访最多」的站，密集区其余自动让位 */
       labelLayout: {hideOverlap: true},
       data: labelData}
    ]
  };
}
mapChart.setOption(mapOption());
cruiseTo(0);

/* 漫游性能：地图那层青色外发光是 34 个省多边形各带一遍 14px 阴影模糊，拖拽/缩放时
   每帧都要重算，是掉帧的主要来源之一（实测拖拽帧率 60 → 31）。漫游期间先摘掉，
   停手 260ms 再装回来，肉眼几乎察觉不到，手感却跟手得多。 */
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

/* 工具栏 */
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

/* ================= 高频线路 ================= */
(function(){
  const d = P.topRoutes.slice().reverse();
  if (!d.length) { $('#chartRoute').innerHTML = '<div class="empty">暂无可配对的行程</div>'; return; }
  echarts.init($('#chartRoute')).setOption({
    backgroundColor: 'transparent',
    grid: {left: 6, right: 46, top: 8, bottom: 4, containLabel: true},
    animationDuration: 1000, animationDelay: i => i * 70,
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'},
      backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border,
      textStyle: {color: pal.text, fontSize: 12},
      formatter: p => '<b>' + p[0].name + '</b><br>乘坐 <b style="color:' + pal.accent_soft + '">' +
                      p[0].value + '</b> 次 · ' + d[p[0].dataIndex].km + ' km'},
    xAxis: {type: 'value', show: false, max: v => v.max * 1.16},
    yAxis: {type: 'category', data: d.map(x => x.name),
      axisLine: {show: false}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 12}},
    series: [{
      type: 'bar', data: d.map(x => x.count), barWidth: 13,
      itemStyle: {borderRadius: [0, 7, 7, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0,
          [{offset: 0, color: 'rgba(56,189,248,.35)'}, {offset: 1, color: pal.accent}])},
      label: {show: true, position: 'right', color: pal.text_dim, fontSize: 11.5,
              formatter: p => p.value + ' 次'},
      emphasis: {itemStyle: {color: pal.accent_soft}}
    }]
  });
})();

/* ================= 城市排行 ================= */
(function(){
  const d = P.cityRank.slice(0, 8);
  if (!d.length) { $('#chartCity').innerHTML = '<div class="empty">暂无城市数据</div>'; return; }
  echarts.init($('#chartCity')).setOption({
    backgroundColor: 'transparent',
    grid: {left: 6, right: 40, top: 8, bottom: 4, containLabel: true},
    animationDuration: 1000, animationDelay: i => i * 70,
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'},
      backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border,
      textStyle: {color: pal.text, fontSize: 12},
      formatter: p => '<b>' + p[0].name + '</b><br>到访 <b style="color:' + pal.accent_soft + '">' + p[0].value + '</b> 次'},
    xAxis: {type: 'value', show: false, max: v => v.max * 1.16},
    yAxis: {type: 'category', data: d.map(x => x.name).reverse(),
      axisLine: {show: false}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 12}},
    series: [{
      type: 'bar', data: d.map(x => x.count).reverse(), barWidth: 12,
      itemStyle: {borderRadius: [0, 7, 7, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0,
          [{offset: 0, color: 'rgba(34,211,238,.3)'}, {offset: 1, color: pal.accent2}])},
      label: {show: true, position: 'right', color: pal.text_dim, fontSize: 11.5,
              formatter: p => p.value + ' 次'}
    }]
  });
})();

/* ================= 出行时间分布 ================= */
(function(){
  const d = P.dateDist;
  if (!d.length) { $('#chartDist').innerHTML = '<div class="empty">暂无可用日期数据</div>'; return; }
  echarts.init($('#chartDist')).setOption({
    backgroundColor: 'transparent',
    grid: {left: 8, right: 14, top: 30, bottom: 6, containLabel: true},
    legend: {right: 0, top: 0, textStyle: {color: pal.text_dim, fontSize: 11.5}, icon: 'roundRect',
             itemWidth: 12, itemHeight: 8, data: ['出行次数', '里程(km)']},
    tooltip: {trigger: 'axis',
      backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border,
      textStyle: {color: pal.text, fontSize: 12}},
    xAxis: {type: 'category', data: d.map(x => x.label),
      axisLine: {lineStyle: {color: pal.panel_border}}, axisTick: {show: false},
      axisLabel: {color: pal.text_faint, fontSize: 10.5, interval: 'auto', rotate: d.length > 14 ? 38 : 0}},
    yAxis: [
      {type: 'value', name: '次', nameTextStyle: {color: pal.text_faint, fontSize: 11},
       axisLabel: {color: pal.text_faint, fontSize: 11}, splitLine: {lineStyle: {color: 'rgba(255,255,255,.06)'}}},
      {type: 'value', name: 'km', nameTextStyle: {color: pal.text_faint, fontSize: 11},
       axisLabel: {color: pal.text_faint, fontSize: 11}, splitLine: {show: false}}
    ],
    series: [
      {name: '出行次数', type: 'bar', data: d.map(x => x.trips), barMaxWidth: 54,
       itemStyle: {borderRadius: [6, 6, 0, 0],
         color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
           [{offset: 0, color: pal.accent_soft}, {offset: 1, color: 'rgba(56,189,248,.30)'}])}},
      {name: '里程(km)', type: 'line', yAxisIndex: 1, data: d.map(x => x.km),
       smooth: 0.25, smoothMonotone: 'x', symbolSize: 7, symbol: 'circle',
       lineStyle: {width: 2.4, color: pal.accent2, shadowColor: 'rgba(34,211,238,.7)', shadowBlur: 12},
       itemStyle: {color: pal.accent2, borderColor: '#0B1220', borderWidth: 2},
       areaStyle: {color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
         [{offset: 0, color: 'rgba(34,211,238,.18)'}, {offset: 1, color: 'rgba(34,211,238,0)'}])}}
    ]
  });
})();

/* ================= 里程概览 ================= */
(function(){
  const k = P.kpi;
  const items = [
    {name: '总里程', v: k.km},
    {name: '单段最长', v: k.max_route_km},
    {name: '平均每段', v: k.avg_km}
  ];
  const maxv = Math.max(1, ...items.map(x => x.v));
  echarts.init($('#chartKm')).setOption({
    backgroundColor: 'transparent',
    grid: {left: 8, right: 56, top: 6, bottom: 4, containLabel: true},
    animationDuration: 1000, animationDelay: i => i * 80,
    tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'},
      backgroundColor: 'rgba(10,16,30,.94)', borderColor: pal.panel_border,
      textStyle: {color: pal.text, fontSize: 12},
      formatter: p => '<b>' + p[0].name + '</b><br>' + fmt(p[0].value) + ' km'},
    xAxis: {type: 'value', show: false, max: maxv * 1.28},
    yAxis: {type: 'category', data: items.map(x => x.name).reverse(),
      axisLine: {show: false}, axisTick: {show: false},
      axisLabel: {color: pal.text_dim, fontSize: 12}},
    series: [{
      type: 'bar', data: items.map(x => x.v).reverse(), barWidth: 16,
      itemStyle: {borderRadius: [0, 8, 8, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0,
          [{offset: 0, color: 'rgba(139,123,240,.32)'}, {offset: 1, color: '#8B7BF0'}])},
      label: {show: true, position: 'right', color: pal.text_dim, fontSize: 11.5,
              formatter: p => fmt(p.value) + ' km'}
    }]
  });
})();
"""


# ─────────────────────── 组装 ───────────────────────

def render_html(payload, geojson, echarts_js, timeline_html, table_html, note_html,
                money_html, *, include_map=True, footer_left="", footer_right=""):
    """把模块内容填进公共骨架（report_html.PAGE_TEMPLATE），返回完整 HTML 文本。

    include_map=False 时整页退化为「纯明细行程单」：不含地图与任何 ECharts 图表，
    产物体积极小（约 70KB），适合只要明细/需要打印的场景。
    """
    def js_json(obj):
        # 防止字面量 </script> 提前闭合脚本块；并保持中文可读
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    mode = payload.get("mode") or "train"
    cruise = w(mode, "cruise")          # 列车巡航 / 航班巡航 / 行程巡航（按钮 + JS 系列名）
    title_default = w(mode, "title")    # 火车票行程单 / 机票行程单 / 行程单
    # 混排页的页头图标两个都摆（火车 + 飞机），单一交通工具页只摆那一个
    hero_modes = payload.get("modes") or [mode]
    if len(hero_modes) > 1:
        hero_icon = "".join(vehicle_icon_svg(m, 30, payload["palette"]) for m in hero_modes)
    else:
        hero_icon = vehicle_icon_svg(hero_modes[0], 30, payload["palette"])

    detail = (_BODY_DETAIL
              .replace("__GRID_CLASS__", "grid" if include_map else "grid solo")
              .replace("__KM_PANEL__", _KM_PANEL if include_map else "")
              .replace("__MONEY_STRIP__", money_html)
              .replace("__TIMELINE__", timeline_html)
              .replace("__DETAIL_TABLE__", table_html)
              .replace("__DETAIL_NOTE__", note_html))
    body = detail
    app_js = "renderChips(P.chips);\nrenderKpis(P.kpiCards);\ninitReveal();\nbindDetailView();\n"
    if include_map:
        body = (_BODY_CHARTS
                .replace("__MAP_INSET__", _map_inset(payload))
                .replace("__MAP_LEGEND__", _map_legend(payload))
                .replace("__CRUISE__", cruise)) + detail
        app_js += (_APP_JS
                   .replace("__DASH_NAME_JSON__", json.dumps(HTML_DASH_NAME, ensure_ascii=False))
                   .replace("__GEOJSON__", js_json(geojson))
                   .replace("__CRUISE__", cruise)
                   .replace("__CRUISE_A__", cruise + "A")
                   .replace("__CRUISE_B__", cruise + "B")
                   .replace("__NO_UNKNOWN__", w(mode, "no_unknown")))
    return R.render_page(
        head_title=payload.get("title", title_default),
        palette=payload["palette"],
        module_css=_MODULE_CSS,
        hero_icon=hero_icon,
        title=payload.get("title", title_default),
        subtitle=payload.get("subtitle", ""),
        body=body,
        footer_left=footer_left, footer_right=footer_right,
        app_js=app_js,
        echarts_js=echarts_js if include_map else None,
        payload_json=js_json(payload))


def build_trainmap_html(invoices, output_folder, *, palette=None,
                        title=None, subtitle=None, filename=None,
                        max_timeline=_MAX_TIMELINE, include_map=True,
                        mode="train"):
    """生成单文件自包含 HTML 行程单，返回落盘路径；失败抛异常（由调用方决定降级）。

    include_map=True（默认）：含地图 + 分析图 + 行程明细的完整版；
    include_map=False：仅行程明细的轻量版（无地图、无 ECharts）。

    mode：单条记录没有交通信号时的**缺省** mode。整页 mode 由 `build_payload` 收敛
         后回填到 payload，标题 / 页脚 / 文件名一律跟着它走（混排 → 「行程单」）。
    """
    payload = build_payload(invoices, palette=palette,
                            title=title or w(mode, "title"),
                            subtitle=subtitle, max_timeline=max_timeline,
                            mode=mode)
    pm = payload.get("mode") or mode
    if include_map:
        geojson = build_geojson()
        echarts_js = _read_vendor()
    else:
        geojson, echarts_js = None, None

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    k = payload["kpi"]
    m = payload["money"]
    footer_left = (f'生成时间：{_esc(now)} &nbsp;|&nbsp; 由「报销助手」生成<br>'
                   f'{_esc(_COMPLIANCE)}')
    footer_right = (f'本次上传{w(pm, "ticket")} {m["count"]} 张（有效行程 {k["trips"]} 段） &nbsp;|&nbsp; '
                    f'票面合计 ￥{m["total"]:,.2f}'
                    f'<br>{_esc(_PRIVACY)}')

    doc = render_html(payload, geojson, echarts_js,
                      _timeline_html(payload),
                      _detail_table_html(payload),
                      _detail_note(payload),
                      _money_strip(payload),
                      include_map=include_map,
                      footer_left=footer_left, footer_right=footer_right)

    path = _unique_path(os.path.join(output_folder, filename or (w(pm, "title") + ".html")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
