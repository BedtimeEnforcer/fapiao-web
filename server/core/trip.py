# -*- coding: utf-8 -*-
"""core/trip.py — 行程票「准入判定」单一真源 + 酒店城市抽取 + 行程补全套用。

为什么要有它
------------
设计方案第 6.2 节：`transit.linked` / `transit.unlinked` / `stay` / `excluded`
四条并列数组**互不污染**，靠的第一件事就是「准入判定器单一真源」。
该判定器原先住在 `routes/pool.py::_trip()`（routes 层），而聚合层
`core/trip_summary.py` 在 core 层 —— **core 不能 import routes**，
所以本模块把那段逻辑原样下移，`routes/pool.py::_trip` 改成薄委托。

本文件只做三件纯函数的事，不碰 IO、不碰 HTTP：
  1. `classify_trip(m)` —— 票面元数据 → `{available, mode, from_, to_, date, no,
     passenger, checkin, checkout, note}`（**逻辑与旧 `_trip` 逐字一致**）；
  2. `nights_between(checkin, checkout)` —— 晚数，**可为 None**（缺日期时不得求和成 0）；
  3. `city_from_hotel(name)` —— 从酒店名前缀抽城市，抽不到就 `""`。
  4. `apply_trip_overrides(invoices, overrides)` —— 前端手动补全行程（机票解析不全时的入口）。
"""
from __future__ import annotations

import datetime
import os
import re

__all__ = [
    "classify_trip",
    "trip_mode",
    "nights_between",
    "city_from_hotel",
    "apply_trip_overrides",
    "norm_key",
]


# ─────────────────────────── 1. 准入判定（单一真源）───────────────────────────

def classify_trip(m: dict) -> dict:
    r"""行程候选：把票面能支撑「出行」的字段挑出来供后续行程单消费。

    有明确出发地+到达地 → `mode='transit'`（可连线）；
    只有入住区间 → `mode='stay'`；两者都没有 → `available=False`。

    ⚠️ 本函数是 `routes/pool.py::_trip()` 的**原样搬迁**（逻辑一字未改，只换了住处），
    `tests/` 里对 `pool` 行的行程候选有依赖。历史地雷留档见 pool.py 原文注释：
    `core.train.parse_train_ticket_text` 对非火车票文本也会返回一堆非空但全错的值，
    拆除方式是 core 侧 `core/train.py::_valid_trip` 的出口校验 ——
    原则是**宁可空，也不返回垃圾**，因为垃圾行程会被画成一条不存在的路线。
    """
    # 网约车行程单：明细里有上下车地点，但用户明确「打车不连线」，只有航班和火车连线。
    # core 侧（facets.extract_invoice_meta 的 noninvoice 分支）因此**故意不填**
    # 顶层 from_/to_，行程明细走 meta['trips'] 列表；这里也不会把它们当成可连线的行程。
    if m.get("kind") == "noninvoice":
        return {
            "available": False, "mode": "", "from_": "", "to_": "", "date": "",
            "no": "", "passenger": "", "checkin": "", "checkout": "",
            "note": "非电子发票",
        }

    trip = {
        "available": False,
        "mode": "",
        "from_": m.get("from_", "") or "",
        "to_": m.get("to_", "") or "",
        "date": m.get("train_date", "") or m.get("date", "") or "",
        "no": m.get("train", "") or "",          # 车次（机票后续再补航班号）
        "passenger": m.get("passenger", "") or "",
        "checkin": m.get("checkin", "") or "",
        "checkout": m.get("checkout", "") or "",
        "note": "",
    }
    if trip["from_"] and trip["to_"]:
        trip["available"] = True
        trip["mode"] = "transit"
    elif trip["checkin"] or trip["checkout"]:
        trip["available"] = True
        trip["mode"] = "stay"
        trip["note"] = f"{trip['checkin'] or '?'} → {trip['checkout'] or '?'}"
    return trip


def trip_mode(m: dict) -> str:
    """只要 mode：`'transit'` / `'stay'` / `''`。"""
    return classify_trip(m).get("mode", "") or ""


# ─────────────────────────── 2. 住宿：晚数与城市 ───────────────────────────

#: 住宿日期里的多种写法（票面备注 / 明细行实测）：
#  `2026年03月05日` / `2026-03-05` / `2026/3/5`
_DATE_PATTERNS = (
    re.compile(r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})"),
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
)

#: 直辖市与计划单列市：酒店名常以「北京王府井万豪」这类「城市+商圈」开头。
_CITY_SUFFIX = ("市",)


def _parse_date(s):
    """宽松解析日期串 → `datetime.date`，失败返回 None。"""
    t = str(s or "").strip()
    if not t:
        return None
    for pat in _DATE_PATTERNS:
        mt = pat.search(t)
        if not mt:
            continue
        try:
            return datetime.date(int(mt.group(1)), int(mt.group(2)), int(mt.group(3)))
        except ValueError:
            continue
    return None


def nights_between(checkin, checkout):
    """入住区间 → 晚数。**缺任一端或端不可解析时返回 None**（不得求和成 0）。

    设计方案 6.5：「`nights` 可空（`hotel_html.py:171-177` 用 `R.DASH` 退化，
    **不得求和成 0**）」—— 缺日期时页面显示 `—`，且不参与任何合计。
    """
    a = _parse_date(checkin)
    b = _parse_date(checkout)
    if not a or not b:
        return None
    n = (b - a).days
    return n if n > 0 else None


def city_from_hotel(name):
    """从酒店名前缀抽城市，抽不到返回 `""`。

    只做「保守前缀」：能明确识别出城市才返回，否则留空 ——
    宁可空，也不要猜出一个错城市（错城市会误导人）。
    """
    n = str(name or "").strip()
    if not n:
        return ""
    # 直接前缀命中城市库（trainmap 的 321 城市表是现成资产，不另建表）
    try:
        from . import trainmap as _tm
        index = _tm.load_station_index()
        cities = set((index.get("by_city") or {}).keys())
    except Exception:
        cities = set()
    if not cities:
        return ""
    for city in sorted(cities, key=len, reverse=True):
        if len(city) >= 2 and n.startswith(city):
            return city
    # 「上海市…」这类带行政后缀的写法
    head = n[:4]
    for suffix in _CITY_SUFFIX:
        if suffix in head:
            cand = head.split(suffix, 1)[0]
            if cand in cities:
                return cand
    return ""


# ─────────────────────────── 3. 前端手动补全行程 ───────────────────────────

def norm_key(p):
    """路径归一化键（大小写 + 绝对化），用于 `filepath` 对表。"""
    return os.path.normcase(os.path.abspath(p)) if p else ""


def apply_trip_overrides(invoices, overrides):
    """把前端「手动补全行程」按 filepath 套到解析结果上（逐条、可缺省）。

    用户口径：机票解析不到行程时给用户一个入口自己填，**不填也没关系**。

    - 只认 from_/to_/date/no/mode 五个键；
    - 缺 from_ 或 to_ → **静默跳过**（不报错、不留空行、不进轨迹）；
    - mode 只在有值时写成该记录自己的**逐条显式 mode**（resolve_mode 优先级 1）——
      绝不拿它当整页 mode，否则同一页混排火车/航班时另一半会全画错；
    - 返回实际套用的条数。

    本函数是 `routes/train.py::_apply_trip_overrides` 的逐字搬迁（供 trip 页复用）。
    """
    if not isinstance(overrides, dict) or not overrides:
        return 0
    norm = {}
    for k, v in overrides.items():
        if isinstance(v, dict):
            nk = norm_key(k)
            if nk:
                norm[nk] = v
    if not norm:
        return 0

    applied = 0
    for inv in invoices or []:
        if not isinstance(inv, dict):
            continue
        ov = norm.get(norm_key(inv.get("filepath")))
        if not ov:
            continue
        from_ = str(ov.get("from_") or "").strip()
        to_ = str(ov.get("to_") or "").strip()
        if not from_ or not to_:
            continue                       # 不填不能报错：缺一不可连线，静默跳过
        inv["from_"] = from_
        inv["to_"] = to_
        d = str(ov.get("date") or "").strip()
        if d:
            inv["train_date"] = d
        no = str(ov.get("no") or "").strip()
        if no:
            inv["train"] = no
        m = str(ov.get("mode") or "").strip().lower()
        if m in ("air", "train"):
            inv["mode"] = m                # 该记录自己的显式 mode（不钉整页）
        # 补齐后重算「待复核」，别让已填的字段还挂着旧提醒
        missing = []
        if str(inv.get("train") or "") in ("", "未知"):
            missing.append("车次")
        if str(inv.get("train_date") or "") in ("", "未知"):
            missing.append("出发时间")
        if (str(inv.get("from_") or "") in ("", "未知")
                or str(inv.get("to_") or "") in ("", "未知")):
            missing.append("站点")
        if float(inv.get("amount") or 0.0) <= 0:
            missing.append("金额")
        inv["needs_review"] = bool(missing)
        inv["review_reason"] = "、".join(missing)
        applied += 1
    return applied
