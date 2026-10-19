# -*- coding: utf-8 -*-
"""core/trainmap.py — 火车票「行程流转地图」（类航旅纵横年度航线图）。

设计要点
--------
* **纯函数 + 少量无状态类**，不引用任何 Tk / GUI 概念，可被 unittest 直接覆盖。
* **零新增依赖**：只用标准库 + reportlab.graphics（与 core/charts.py 同源）。
* **纯离线**：边界与车站坐标都是随包内置的 JSON，坐标不出本机。
* **颜色注入**：`palette=None` 时回落内置默认色板；GUI 侧从 ui/theme.py 读出令牌传入
  （core 不得 import ui，故用依赖注入而非直接引用）。

数据资产（assets/trainmap/，由 dev/plan/trainmap/build_assets.py 生成）
    china_provinces.json  省级边界 + 南海九段线（含台湾/港澳）
    stations.json         站名 -> [lat, lon, city]
    cities.json           城市 -> [lat, lon]（站名匹配失败的兜底）

口径约定
    * 里程 = 站点间 haversine 球面直线距离（**非铁路实际里程**）。
    * 退票不计入出行次数；改签按 1 次计。
    * 线路按「无向」聚合（A→B 与 B→A 合并计次），线宽随次数增大。
    * 站名匹配不上 / 港澳台与境外行程 **不静默丢弃**，分别进入未匹配清单与分段清单。
"""
from __future__ import annotations

import json
import math
import os
import re

from reportlab.lib import colors
from reportlab.graphics.shapes import Drawing, String, Rect, Line, Circle, Polygon, PolyLine

from .config import FONT_NAME, asset_path

__all__ = [
    "DEFAULT_TRAINMAP_PALETTE",
    "load_station_index", "load_provinces",
    "norm_station_name", "match_station", "haversine_km",
    "to_itinerary_records", "aggregate_routes",
    "TRAIN_MODES", "resolve_mode",
    "Projection",
    "build_trainmap_drawing", "build_annual_card", "build_trainmap_section",
]

# ─────────────────────────── 常量 ───────────────────────────

#: 默认调色板 —— 键集合必须与 ui/theme.py 的 TRAINMAP 完全一致（有单测断言）
DEFAULT_TRAINMAP_PALETTE = {
    "land_bg":         "#FFFFFF",
    "province_fill":   "#F4F7FC",
    "province_stroke": "#C7D2E5",
    "border_stroke":   "#9FB0CC",
    "route_primary":   "#0284C7",
    "route_mid":       "#38BDF8",
    "route_secondary": "#94A3B8",
    "station":         "#0284C7",
    "station_stroke":  "#FFFFFF",
    "station_label":   "#334155",
    "card_bg":         "#FFFFFF",
    "card_border":     "#E4E7EF",
    "card_title":      "#1F2A44",
    "card_label":      "#64748B",
    "note":            "#94A3B8",
}

#: 地图取景框：固定覆盖全国（保证台湾、海南、南海诸岛与九段线始终在图内）
FRAME = {"lon_min": 73.0, "lon_max": 136.5, "lat_min": 3.0, "lat_max": 54.5}

#: 港澳台 / 境外站名 —— **精确匹配**（归一化后的站名）。
#: 注意：绝不可用子串匹配 —— 「邢台东」含「台东」、「台北」等会把内地站误判为境外。
_HK_TW_ABROAD_EXACT = frozenset((
    "香港", "香港西九龙", "九龙", "红磡", "澳门", "台北", "高雄", "台中", "台南",
    "新竹", "花莲", "台东", "基隆", "嘉义", "桃园", "苗栗", "彰化", "云林", "屏东",
    "宜兰", "南投", "澎湖", "金门", "马祖", "莫斯科", "平壤", "河内", "乌兰巴托",
))

#: 可以安全做子串匹配的关键词（不会撞上内地站名）
_HK_TW_ABROAD_SUB = ("香港", "澳门", "台湾")

_COMPLIANCE_NOTE = "数据来源：Supeset/China-GeoData（MIT）· 本图为简化示意，非标准地图"

_YEAR_RE = re.compile(r"(\d{4})")

#: 支持的交通工具 mode（顺序即展示顺序：火车在前、航班在后）
TRAIN_MODES = ("train", "air")

#: 判定「这条是航班」的票种标签关键词（与 rename.py::_normalize_type_label 的机票类同义）
_AIR_LABEL_KW = ("机票", "航空", "航班", "民航", "客票")

#: 站名里出现这些词 → 航班（火车站不会叫「机场」）
_AIR_PLACE_KW = ("机场", "航站楼", "空港")

#: 航班号形态：2 位航空公司代码 + 3~4 位数字（CZ6894 / GS7577 / MU5100）。
#: 车次是「1 位字母 + 数字」（G1234 / D3106 / K105），不会与它撞形。
_AIR_NO_RE = re.compile(r"^[A-Z]{2}\s?\d{3,4}$")


# ─────────────────────── 数据加载（带缓存） ───────────────────────

_index_cache = {"station": None, "province": None}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_station_index(path=None):
    """加载车站/城市坐标索引（模块级缓存，只读一次磁盘）。"""
    if _index_cache["station"] is None:
        base = os.path.dirname(path) if path else asset_path("assets", "trainmap")
        s_name = path or os.path.join(base, "stations.json")
        c_name = os.path.join(os.path.dirname(s_name), "cities.json")
        idx = {"by_station": {}, "by_city": {}, "alias": {}, "meta": {}}
        if os.path.exists(s_name):
            d = _read_json(s_name)
            idx.update({k: d.get(k) or {} for k in ("by_station", "by_city", "alias")})
            idx["meta"] = d.get("_meta") or {}
        if os.path.exists(c_name):
            idx["by_city"] = _read_json(c_name).get("by_city") or idx["by_city"]
        _index_cache["station"] = idx
    return _index_cache["station"]


def load_provinces(path=None):
    """加载省界 + 九段线（模块级缓存）。"""
    if _index_cache["province"] is None:
        p = path or asset_path("assets", "trainmap", "china_provinces.json")
        data = {"provinces": [], "nine_dash": [], "_meta": {}}
        if os.path.exists(p):
            data = _read_json(p)
        _index_cache["province"] = data
    return _index_cache["province"]


def clear_cache():
    """清空缓存（测试用）。"""
    _index_cache["station"] = None
    _index_cache["province"] = None


# ─────────────────────────── 站名匹配 ───────────────────────────

def norm_station_name(name):
    """站名归一化：去首尾空白与末尾「站」字。

    注意：**不动方位词** —— 「北京南」是站名本身，去掉「南」会退回北京站。
    """
    return (str(name) if name is not None else "").strip().rstrip("站").strip()


def match_station(name, index):
    """站名 -> 坐标。返回 dict(query/resolved/lat/lon/city/match_type/ok)。

    匹配级别：L1 精确站名 → L2 别名 → L3 去方位词/城市兜底 → L4 未匹配。
    """
    raw = (str(name) if name is not None else "").strip()
    key = norm_station_name(raw)
    out = {"query": raw, "resolved": "", "lat": None, "lon": None,
           "city": "", "match_type": "unmatched", "ok": False}
    if not key or key == "未知":
        return out

    by_station = index.get("by_station") or {}
    by_city = index.get("by_city") or {}
    alias = index.get("alias") or {}

    def _hit(rec, resolved, mtype):
        lat, lon, city = rec[0], rec[1], (rec[2] if len(rec) > 2 else "")
        out.update(resolved=resolved, lat=float(lat), lon=float(lon),
                   city=city or "", match_type=mtype, ok=True)
        return out

    # L1 精确站名
    if key in by_station:
        return _hit(by_station[key], key, "exact")

    # L2 别名表
    tgt = alias.get(key)
    if tgt and tgt in by_station:
        return _hit(by_station[tgt], tgt, "alias")

    # L3a 去掉末尾方位词（西安北 -> 西安）后按站名/城市兜底
    base = key.rstrip("东南西北")
    if base and base != key:
        if base in by_station:
            return _hit(by_station[base], base, "alias")
        if base in by_city:
            return _hit(by_city[base], base, "city")

    # L3b 直接按城市兜底
    if key in by_city:
        return _hit(by_city[key], key, "city")

    # L3c 城市名前缀兜底（数据集偏旧时的新站，如「北京丰台」->「北京」）
    # 取最长匹配，避免「上海」把「上海虹桥」之类的意外情况提前截断。
    for city in sorted(by_city, key=len, reverse=True):
        if city and len(city) >= 2 and key.startswith(city):
            return _hit(by_city[city], city, "city")

    return out


# ─────────────────────────── 几何 ───────────────────────────

def haversine_km(lat1, lon1, lat2, lon2):
    """两点球面大圆距离（km）。"""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class Projection:
    """等距圆柱投影（x 按中纬度压缩，避免中国地图被横向拉长）。

    画布坐标系遵循 reportlab 约定：原点在左下、y 轴向上。
    """

    def __init__(self, lon_min=None, lon_max=None, lat_min=None, lat_max=None,
                 width=440, height=380, padding=12):
        f = FRAME
        self.lon_min = f["lon_min"] if lon_min is None else lon_min
        self.lon_max = f["lon_max"] if lon_max is None else lon_max
        self.lat_min = f["lat_min"] if lat_min is None else lat_min
        self.lat_max = f["lat_max"] if lat_max is None else lat_max
        self.width, self.height, self.padding = width, height, padding

        mid = (self.lat_min + self.lat_max) / 2.0
        self.kx = math.cos(math.radians(mid))          # 经度压缩系数

        avail_w = max(1.0, width - 2 * padding)
        avail_h = max(1.0, height - 2 * padding)
        span_x = (self.lon_max - self.lon_min) * self.kx
        span_y = (self.lat_max - self.lat_min)
        self.scale = min(avail_w / span_x, avail_h / span_y)   # 等比缩放，不拉伸

        draw_w = span_x * self.scale
        draw_h = span_y * self.scale
        self.ox = padding + (avail_w - draw_w) / 2.0 - self.lon_min * self.kx * self.scale
        self.oy = padding + (avail_h - draw_h) / 2.0 - self.lat_min * self.scale

    def project(self, lon, lat):
        """(经度, 纬度) -> (x, y)。"""
        return (self.ox + float(lon) * self.kx * self.scale,
                self.oy + float(lat) * self.scale)


# ─────────────────────────── 行程聚合 ───────────────────────────

def resolve_mode(inv, default="train"):
    """判定**单张发票**的交通工具 mode：`'air'` / `'train'`。

    ⚠️ 逐条判定，不做「整页一种」的假设 —— 用户一趟出差既有火车又有飞机是常态，
    整页级 mode 会让另一半显示成错误的措辞与图标（航班连线的意义就没了）。

    判定优先级（从强到弱，命中即返回）：
      1. 发票上显式带 `mode`（调用方已判定过，如前端手动补全）；
      2. 票种标签（`type` / `item_name`）命中机票类关键词；
      3. 出发/到达地名含「机场」等航空场所词；
      4. 「车次」槽里的值符合航班号形态（2 位航司码 + 3~4 位数字）；
      5. 以上都不中 → `default`（默认 'train'，向后兼容纯火车场景）。
    """
    if not isinstance(inv, dict):
        return default
    m = str(inv.get("mode") or "").strip().lower()
    if m in TRAIN_MODES:
        return m

    label = "%s %s" % (inv.get("type") or "", inv.get("item_name") or "")
    if any(kw in label for kw in _AIR_LABEL_KW):
        return "air"

    place = "%s %s" % (inv.get("from_") or "", inv.get("to_") or "")
    if any(kw in place for kw in _AIR_PLACE_KW):
        return "air"

    if _AIR_NO_RE.match(str(inv.get("train") or "").strip()):
        return "air"

    return default if default in TRAIN_MODES else "train"


def to_itinerary_records(invoices, default_mode="train"):
    """发票 dict 列表 -> 归一化行程记录（剔除退票、改签计 1 次、抽取年份）。

    每条记录额外带 `mode`（逐条判定，见 `resolve_mode`）—— 地图巡航、时间轴、
    明细表都按各自的 mode 出图标与措辞，同一页可以火车与航班混排。
    """
    recs = []
    for inv in invoices or []:
        if not isinstance(inv, dict):
            continue
        if inv.get("special_type") == "退票":
            continue
        date = str(inv.get("train_date") or "")
        m = _YEAR_RE.search(date)
        recs.append({
            "train": str(inv.get("train") or ""),
            "date": date,
            "from_name": str(inv.get("from_") or "").strip(),
            "to_name": str(inv.get("to_") or "").strip(),
            "passenger": str(inv.get("passenger") or ""),
            "amount": float(inv.get("amount") or 0.0),
            "special_type": str(inv.get("special_type") or ""),
            "year": int(m.group(1)) if m else None,
            "mode": resolve_mode(inv, default_mode),
        })
    return recs


def _is_abroad(name):
    """港澳台 / 境外站名判定（精确匹配 + 安全子串 + 无汉字兜底）。

    历史坑：「邢台东」曾被 "台东" 子串误判成境外 —— 所以非安全关键词一律精确匹配。
    """
    n = str(name or "").strip()
    if not n:
        return False
    key = norm_station_name(n)
    if key in _HK_TW_ABROAD_EXACT:
        return True
    if any(sub in n for sub in _HK_TW_ABROAD_SUB):
        return True
    # 站点名不含任何汉字（境外站名常见）也视为境外
    return not re.search(r"[\u4e00-\u9fff]", n)



def aggregate_routes(records, index, top_n=8):
    """行程记录 -> 统计结果（站点频次 / 线路聚合 / 未匹配 / 港澳台境外）。"""
    stations = {}     # resolved 名 -> {lat, lon, city, count}
    routes = {}       # (a, b) 排序后的元组 -> 线路统计
    unmatched, abroad = [], []
    years = set()
    trip_count = 0

    for r in records:
        a_raw, b_raw = r["from_name"], r["to_name"]
        if r.get("year"):
            years.add(r["year"])

        if _is_abroad(a_raw) or _is_abroad(b_raw):
            abroad.append({"train": r["train"], "date": r["date"],
                           "from_name": a_raw or "未知", "to_name": b_raw or "未知",
                           "amount": r["amount"]})
            continue

        a = match_station(a_raw, index)
        b = match_station(b_raw, index)
        if not a["ok"]:
            unmatched.append(a_raw or "未知")
        if not b["ok"]:
            unmatched.append(b_raw or "未知")
        if not (a["ok"] and b["ok"]):
            continue

        trip_count += 1
        for m in (a, b):
            s = stations.setdefault(m["resolved"],
                                    {"lat": m["lat"], "lon": m["lon"],
                                     "city": m["city"], "count": 0})
            s["count"] += 1

        key = tuple(sorted((a["resolved"], b["resolved"])))
        if key[0] == key[1]:
            continue
        rt = routes.get(key)
        if rt is None:
            rt = routes[key] = {"from_key": key[0], "to_key": key[1], "count": 0,
                                "distance_km": haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]),
                                "dates": []}
        rt["count"] += 1
        rt["dates"].append(r["date"])

    route_list = sorted(routes.values(), key=lambda x: (-x["count"], -x["distance_km"]))
    total_km = sum(rt["distance_km"] * rt["count"] for rt in route_list)
    cities = {s["city"] for s in stations.values() if s["city"]}

    return {
        "trip_count": trip_count,
        "total_km": total_km,
        "station_count": len(stations),
        "city_count": len(cities),
        "stations": stations,
        "routes": route_list,
        "top_routes": route_list[:top_n],
        "unmatched": _dedup_keep_order(unmatched),
        "hk_tw_abroad": abroad,
        "years": sorted(years),
    }


def _dedup_keep_order(seq):
    seen, out = set(), []
    for x in seq:
        key = str(x)
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


# ─────────────────────────── 绘制工具 ───────────────────────────

def _c(palette, key, fallback="#888888"):
    return colors.HexColor(palette.get(key) or fallback)


def _text(d, x, y, s, size=8, fill="#333333", anchor="start", bold=False):
    d.add(String(x, y, str(s), fontName=FONT_NAME, fontSize=size,
                 fillColor=colors.HexColor(fill) if isinstance(fill, str) else fill,
                 textAnchor=anchor))


def _fmt_km(km):
    return f"{km:,.0f}"


# ─────────────────────────── 地图绘制 ───────────────────────────

def build_trainmap_drawing(stats, *, width=440, height=400, palette=None):
    """把统计结果画成一张矢量中国地图（Drawing，可直接 append 进 PDF）。"""
    pal = dict(DEFAULT_TRAINMAP_PALETTE)
    if palette:
        pal.update(palette)

    d = Drawing(width, height)
    # 底板
    d.add(Rect(0, 0, width, height, fillColor=_c(pal, "land_bg", "#FFFFFF"),
               strokeColor=colors.HexColor("#e8edf3"), strokeWidth=1, rx=8, ry=8))

    proj = Projection(width=width, height=height, padding=14)
    geo = load_provinces()

    # 省界描边（同一颜色填充，形成陆地块）
    fill = _c(pal, "province_fill")
    stroke = _c(pal, "province_stroke")
    for prov in geo.get("provinces") or []:
        for ring in prov.get("rings") or []:
            if len(ring) < 4:
                continue
            pts = []
            for lon, lat in ring:
                x, y = proj.project(lon, lat)
                pts.extend((x, y))
            d.add(Polygon(pts, fillColor=fill, strokeColor=stroke, strokeWidth=0.4))

    # 南海九段线：细长实心条
    dash_col = _c(pal, "border_stroke")
    for seg in geo.get("nine_dash") or []:
        if len(seg) < 3:
            continue
        pts = []
        for lon, lat in seg:
            x, y = proj.project(lon, lat)
            pts.extend((x, y))
        d.add(Polygon(pts, fillColor=dash_col, strokeColor=dash_col, strokeWidth=0.3))

    # 线路（线宽随次数）
    max_cnt = max([rt["count"] for rt in stats.get("routes") or []] or [1])
    for rt in (stats.get("routes") or [])[:120]:
        a = stats["stations"].get(rt["from_key"])
        b = stats["stations"].get(rt["to_key"])
        if not a or not b:
            continue
        x1, y1 = proj.project(a["lon"], a["lat"])
        x2, y2 = proj.project(b["lon"], b["lat"])
        ratio = rt["count"] / max_cnt
        if ratio >= 0.6:
            col = _c(pal, "route_primary")
        elif ratio >= 0.25:
            col = _c(pal, "route_mid")
        else:
            col = _c(pal, "route_secondary")
        lw = 0.8 + 2.6 * ratio
        d.add(PolyLine([x1, y1, x2, y2], strokeColor=col, strokeWidth=lw,
                       strokeOpacity=0.55 + 0.35 * ratio))

    # 站点（半径随到访次数）
    st_col = _c(pal, "station")
    st_stroke = _c(pal, "station_stroke")
    max_st = max([s["count"] for s in stats["stations"].values()] or [1])
    for name, s in stats["stations"].items():
        x, y = proj.project(s["lon"], s["lat"])
        r = 2.2 + 3.4 * math.sqrt(s["count"] / max_st)
        d.add(Circle(x, y, r, fillColor=st_col, strokeColor=st_stroke, strokeWidth=1.0))

    # 站名标签：只标「去过 >=2 次」的高频站；每个标签依次尝试 6 个方位，
    # 找一个不与已有标签/站点圆点重叠的位置——京津沪这种密集区才不会糊成一团。
    ordered = sorted(stats["stations"].items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    cands = [(n, s) for n, s in ordered if s["count"] >= 2][:16] or ordered[:5]
    placed_boxes, labeled = [], []
    label_h = 11
    for name, s in cands:
        x, y = proj.project(s["lon"], s["lat"])
        r = 2.2 + 3.4 * math.sqrt(s["count"] / max_st)
        tw = len(name) * 7.0 + 6
        for anchor, dx, dy in (
            ("start", r + 3, -2), ("end", -(r + 3), -2),
            ("start", r + 3, label_h - 1), ("start", r + 3, -label_h - 1),
            ("middle", 0.0, r + 5), ("middle", 0.0, -(r + 7)),
        ):
            if anchor == "start":
                x0 = x + dx
            elif anchor == "end":
                x0 = x + dx - tw
            else:
                x0 = x - tw / 2.0
            by = y + dy - 3
            box = (x0 - 1, by, x0 + tw + 1, by + label_h)
            if x0 >= 1 and x0 + tw <= width - 1 and not any(_overlap(box, b) for b in placed_boxes):
                placed_boxes.append(box)
                labeled.append((name, x + dx, y + dy, anchor))
                break
        if len(labeled) >= 10:
            break

    for name, lx, ly, anchor in labeled:
        _text(d, lx, ly, name, size=7,
              fill=pal.get("station_label", "#334155"), anchor=anchor)

    # 图例
    _text(d, 12, height - 16, "行程流转地图", size=10.5, fill=pal.get("card_title", "#1F2A44"))
    _text(d, 12, height - 28,
          f"线路粗细 = 乘坐次数   ·   站点大小 = 到访次数（已标注 {len(labeled)} 个高频站）",
          size=6.5, fill=pal.get("card_label", "#64748B"))
    _text(d, 12, 9, _COMPLIANCE_NOTE, size=5.8, fill=pal.get("note", "#94A3B8"))
    return d


def _overlap(a, b):
    """两个矩形是否相交：(x0, y0, x1, y1)。"""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def build_annual_card(stats, *, width=440, palette=None):
    """年度数据卡（里程 / 次数 / 覆盖城市 / Top 高频线路 / 未匹配提示）。

    版面按「游标自上而下」排布，高度由内容反推 —— 避免固定高度导致末行与脚注重叠。
    """
    pal = dict(DEFAULT_TRAINMAP_PALETTE)
    if palette:
        pal.update(palette)

    show_routes = (stats.get("top_routes") or [])[:5]
    unmatched = stats.get("unmatched") or []
    abroad = stats.get("hk_tw_abroad") or []

    rows = max(len(show_routes), 1)
    extras = (1 if unmatched else 0) + (1 if abroad else 0)
    # 17 标题 + 20 起排 + 30 指标块 + 13 小标题 + rows*15 + extras*15 + 12 脚注
    height = 92 + rows * 15 + extras * 15

    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, fillColor=_c(pal, "card_bg", "#FFFFFF"),
               strokeColor=_c(pal, "card_border"), strokeWidth=1, rx=10, ry=10))

    title_col = pal.get("card_title", "#1F2A44")
    label_col = pal.get("card_label", "#64748B")

    y = height - 17
    _text(d, 14, y, "年度行程概览", size=10.5, fill=title_col)

    # 四项指标
    y -= 20
    metrics = [
        ("出行次数", f"{stats.get('trip_count', 0)} 次"),
        ("总里程", f"{_fmt_km(stats.get('total_km', 0))} km"),
        ("覆盖城市", f"{stats.get('city_count', 0)} 座"),
        ("覆盖车站", f"{stats.get('station_count', 0)} 个"),
    ]
    col_w = (width - 28) / 4.0
    for i, (label, val) in enumerate(metrics):
        x = 14 + i * col_w
        _text(d, x, y, val, size=11, fill=title_col)
        _text(d, x, y - 11, label, size=6.5, fill=label_col)

    y -= 30
    _text(d, 14, y, "高频线路 Top 5", size=7.5, fill=label_col)
    y -= 13

    if show_routes:
        for rt in show_routes:
            _text(d, 18, y, f"{rt['from_key']} → {rt['to_key']}", size=8, fill=title_col)
            _text(d, width - 18, y, f"{rt['count']} 次 · {_fmt_km(rt['distance_km'])} km",
                  size=8, fill=label_col, anchor="end")
            y -= 15
    else:
        _text(d, 18, y, "（暂无可配对的行程）", size=8, fill=label_col)
        y -= 15

    if unmatched:
        names = "、".join(str(x) for x in unmatched[:8])
        more = f" 等 {len(unmatched)} 个" if len(unmatched) > 8 else ""
        _text(d, 14, y, f"注意：未匹配站点 —— {names}{more}（未计入地图与里程，请在行程明细中核对）",
              size=6.5, fill="#D97706")
        y -= 15
    if abroad:
        _text(d, 14, y, f"港澳台 / 国际段：{len(abroad)} 段行程已单列，未绘入中国地图",
              size=6.5, fill=label_col)
        y -= 15

    _text(d, 14, max(7, y), "里程为站点间球面直线距离（非铁路实际里程）· 退票不计次、改签计 1 次",
          size=5.8, fill=pal.get("note", "#94A3B8"))
    return d


def build_trainmap_section(invoices, *, palette=None, width=440, height=400,
                           include_card=True, title="行程流转地图"):
    """供 core/train.py 一行接入：返回可直接 append 进 PDF 的 flowable 列表。

    无可用行程时返回一段说明文字（而不是空白图）。
    """
    from reportlab.lib import colors as _colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer

    records = to_itinerary_records(invoices)
    index = load_station_index()
    stats = aggregate_routes(records, index)

    ts = ParagraphStyle("trainmap_t", fontName=FONT_NAME, fontSize=13,
                        textColor=_colors.HexColor("#1a1a2e"), spaceAfter=6)
    ns = ParagraphStyle("trainmap_n", fontName=FONT_NAME, fontSize=9, leading=14,
                        textColor=_colors.HexColor("#64748B"))

    out = [Paragraph(title, ts)]
    if stats["trip_count"] <= 0:
        reason = "行程数据不足" if not records else "站点坐标未能匹配"
        out.append(Paragraph(f"（暂无行程流转图：{reason}）", ns))
        out.append(Spacer(1, 8))
        return out

    out.append(build_trainmap_drawing(stats, width=width, height=height, palette=palette))
    if include_card:
        out.append(Spacer(1, 8))
        out.append(build_annual_card(stats, width=width, palette=palette))
    out.append(Spacer(1, 12))
    return out


def trainmap_summary(invoices, palette=None):
    """给 GUI 结果提示用的一句话摘要（不绘制，纯统计）。"""
    records = to_itinerary_records(invoices)
    stats = aggregate_routes(records, load_station_index())
    return {
        "trip_count": stats["trip_count"],
        "city_count": stats["city_count"],
        "station_count": stats["station_count"],
        "total_km": stats["total_km"],
        "unmatched_count": len(stats["unmatched"]),
        "abroad_count": len(stats["hk_tw_abroad"]),
    }
