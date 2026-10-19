# -*- coding: utf-8 -*-
"""core/trip_summary.py — 行程汇总页的**聚合层**（第 3 步 · 交通 + 住宿合并视图）。

为什么不能直接用 `trainmap_html.build_payload`
--------------------------------------------
设计方案 6.1 的结论：`build_payload` 的前提是「输入全是可连线的行程」。
理由链（实测）：`core/trainmap.py:286 to_itinerary_records` 只读
`train / date / from_ / to_ / passenger / amount / special_type` 七个键，
紧接着 `trainmap.py:332 aggregate_routes` 就做站名匹配 ——
一张住宿票走到这里立刻退化成「日期未知 + 未匹配站名」，
把 `unmatched` 清单污染成酒店名单。所以本模块另起一个聚合层，
**自行**把发票分流成四条并列数组，再复用 trainmap 的匹配/几何零件。

数据结构（设计方案 6.2）
------------------------
    transit.linked[]    可连线交通（火车 / 解析完整的机票）—— 只有它有资格进地图
    transit.unlinked[]  不可连线交通（打车 / 大巴 / 客运）—— `linkable` 恒 False
    stay[]              住宿                        —— `linkable` 恒 False，永不进地图
    excluded[]          非发票（`kind == 'noninvoice'`）—— 金额恒 0，**不进** totals

红线（6.8 六条断言，本模块自己保证，测试在临时脚本里逐条验）：
  1. `all(not x["linkable"] for x in stay)`
  2. `all(not x["linkable"] for x in transit["unlinked"])`
  3. taxi 的 `from.raw` / `to.raw` 原始值为空
  4. `abs(grandAmount - (transitAmount + stayAmount)) < 0.01`
  5. 只有住宿输入时 `map is None` 且 `transit["linked"] == []`
  6. unmatched 段 `km is None`
"""
from __future__ import annotations

import hashlib
import os

from . import trip as _trip
from . import trainmap as T
from . import trainmap_html as TH
from . import report_html as R
from . import parse as _parse

__all__ = ["build_trip_payload", "leg_id", "collect_invoices", "list_invoice_files"]

#: 明细/时间轴最多渲染多少条（与火车页一致）
_MAX_DETAILS = 800
_MAX_TIMELINE = 400


def leg_id(filepath, fallback=""):
    """一条行程的稳定 id：`sha1(绝对路径)[:12]`。

    设计方案 6.5：**不能按文件名** —— PRD L1068 记过递归同名的坑
    （不同目录下同名发票会被误当成同一条）。
    """
    src = str(filepath or fallback or "")
    if not src:
        return ""
    return hashlib.sha1(os.path.normcase(os.path.abspath(src)).encode("utf-8")).hexdigest()[:12]


# ─────────────────────────── 小工具 ───────────────────────────

def _s(v):
    return str(v if v is not None else "").strip()


def _num(v):
    try:
        return round(float(v or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


#: 「属于交通费大类」的票种。设计 6.2：打车/大巴/客运进 `transit.unlinked[]`
#: （`linkable` 恒 False），但**金额要算进交通费** —— 用户要的
#: 「交通费那个大类」就包含打车。`noninvoice`（网约车行程单）不在其中：
#: 它不是发票，金额恒 0，只能进 `excluded`。
_TRANSPORT_KINDS = ("taxi", "bus", "road", "train", "air", "transit")


def _is_transport(inv):
    """这张票是否属于交通费大类（哪怕没能连线）。"""
    return _s(inv.get("kind")) in _TRANSPORT_KINDS


def _invoices_of(paths, dedup=True, trip_only=False):
    """真实路径列表 → 解析后的规范发票 dict 列表。

    分两条腿走（**这是与既有火车页最关键的一处不同**）：

    * **交通腿**：`core.facets.extract_invoice_meta(fp)` 逐文件拿元数据
      （票种单一真源）。`kind == 'noninvoice'`（网约车行程单等）**完全没有 amount**，
      facets 侧也刻意不填 from_/to_，所以这里补 `amount=0.0` 占位，
      并交给 `trip.classify_trip` 判成 `available=False` → 落 `excluded`。
    * **住宿腿**：必须换成 `core.hotel.parse_hotel_invoices`，**不能只靠 facets** ——
      实测（tests/samples/住宿 18 张）`facets.extract_invoice_meta` 对住宿票
      `kind='hotel'` 但 `checkin`/`checkout` **一律为空**（那段日期只从备注栏
      `core/hotel.py::_extract_stay_dates` 里来，facets 不重复实现）。
      若只读 facets，18 张住宿票会全部被判成「无行程信息」丢进 excluded，
      住宿分区直接消失 —— 这是第一轮探针实测到的真实故障。
    """
    from . import facets as F
    from . import hotel as _hotel
    from . import journey_info, vault
    from . import parse as _p

    reg = _p.DedupRegistry() if dedup else None
    out, skipped = [], 0
    hotel_files = []
    saved = vault.cached_meta_map(paths) if os.path.isfile(vault.DB) else {}

    for fp in paths or []:
        fp = _s(fp)
        if not fp:
            continue
        try:
            m = F.extract_invoice_meta(fp)
        except Exception:
            skipped += 1
            continue
        if not isinstance(m, dict):
            skipped += 1
            continue
        if trip_only and m.get("kind") != "hotel" and not _is_transport(m):
            continue
        if m.get("kind") == "hotel":
            hotel_files.append(fp)
            continue
        m = journey_info.apply_saved_values(m, saved.get(fp))
        if reg is not None:
            try:
                if reg.check_md5(fp, os.path.basename(fp)):
                    skipped += 1
                    continue
            except Exception:
                pass
        inv = dict(m)
        inv["filepath"] = fp
        inv["filename"] = inv.get("filename") or os.path.basename(fp)
        inv.setdefault("amount", 0.0)
        inv["amount"] = _num(inv.get("amount"))
        out.append(inv)

    out.extend(_stay_records(hotel_files, dedup, saved))
    return out, skipped

def _stay_records(files, dedup=True, saved=None):
    """住宿文件列表 → 与交通腿同形（同键约定）的规范 dict 列表。

    `core.hotel.parse_hotel_invoices(folder_or_files, is_folder, deduplicate)`
    返回 `(res, err)`，`res['records']` 的键是 `hotel_name / guest_name /
    checkin_date / checkout_date / invoice_date / invoice_no / tax_id /
    seller_tax_id / drawer / item_name / needs_review / review_reason`。
    这里把 `checkin/checkout/date/seller/buyer` 这些**交通腿的键名**补齐，
    让下游 `classify_trip` 与聚合函数只认一套键名。
    """
    if not files:
        return []
    from . import hotel as _hotel
    from . import journey_info
    try:
        res, _err = _hotel.parse_hotel_invoices(list(files), False, bool(dedup))
    except Exception:
        return []
    recs = (res or {}).get("records") or []
    out = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        hotel = _s(r.get("hotel_name"))
        inv = {
            "kind": "hotel",
            "filepath": _s(r.get("filepath")),
            "filename": _s(r.get("filename")),
            "seq": r.get("seq"),
            "amount": _num(r.get("amount")),
            "hotel_name": hotel,
            "seller": hotel,
            "seller_name": hotel,
            "buyer_name": _s(r.get("buyer_name")),
            "guest_name": "",
            "checkin": _s(r.get("checkin_date")),
            "checkout": _s(r.get("checkout_date")),
            "checkin_date": _s(r.get("checkin_date")),
            "checkout_date": _s(r.get("checkout_date")),
            "checkout_inferred": bool(r.get("checkout_inferred")),
            "date": _s(r.get("invoice_date")),
            "invoice_no": _s(r.get("invoice_no")),
            "invoice_date": _s(r.get("invoice_date")),
            "item_name": _s(r.get("item_name")),
            "tax_id": _s(r.get("tax_id")),
            "seller_tax_id": _s(r.get("seller_tax_id")),
            "drawer": _s(r.get("drawer")),
            "needs_review": bool(r.get("needs_review")),
            "review_reason": _s(r.get("review_reason")),
        }
        edit = (saved or {}).get(inv["filepath"])
        merged = journey_info.apply_saved_values(inv, edit)
        if merged.get("journey_edited"):
            merged["checkin_date"] = merged.get("checkin") or ""
            merged["checkout_date"] = merged.get("checkout") or ""
        out.append(merged)
    return out


def _list_files(paths):
    """把输入（文件或目录）展开成真实的发票文件路径列表。"""
    out = []
    for p in paths or []:
        p = _s(p)
        if not p:
            continue
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                fp = os.path.join(p, name)
                if os.path.isfile(fp) and _is_invoice_file(fp):
                    out.append(fp)
        elif os.path.isfile(p):
            out.append(p)
    return out


def _is_invoice_file(fp):
    """扩展名白名单 —— **与 `routes/_shared.py::SUPPORTED_EXTS` 同源**。

    既有 5 个模块的入口都走 `_shared.resolve_inputs`，它只认 `core.parse.DEFAULT_EXTS`
    （`.pdf` / `.ofd`）。实测 `tests/samples/taxi` 里有 23 个 `.xml`（OFD 的 XML 旁挂件，
    不是独立票面）：若这里放宽，同一批目录「直接调 core」与「走 HTTP」会得出两个计数
    （180 vs 157），报销人对不上账。所以核心层也按同一把尺子量。
    """
    try:
        return os.path.splitext(fp)[1].lower() in _parse.DEFAULT_EXTS
    except Exception:
        return False


def list_invoice_files(paths):
    """公开别名：输入路径列表 → 真实发票文件列表（目录会被展开）。"""
    return _list_files(paths)


def collect_invoices(paths, *, dedup=True, trip_only=False):
    """公开入口：输入路径/目录列表 → `(规范发票 dict 列表, skipped)`。

    routes 层只要这两样东西就够；解析口径（票种分类）全部由 core 决定。
    """
    files = _list_files(paths)
    return _invoices_of(files, dedup=dedup, trip_only=trip_only)


# ─────────────────────────── 主聚合函数 ───────────────────────────

def build_trip_payload(invoices, *, include_map=True, include_charts=True,
                       mode_default="train", title="行程汇总单", subtitle=None,
                       palette=None, max_timeline=_MAX_TIMELINE, skipped=0):
    """把「交通 + 住宿」混排发票聚合成行程汇总页 payload。

    入参 `invoices` 是 `_invoices_of` 出来的规范 dict 列表（每张票一个 dict）。
    返回的 payload 同时服务**两种消费方**：
      * 本页自己的 HTML（`trip_html`）—— 读 `transit` / `stay` / `excluded` / `totals`；
      * 复用来的 `trainmap_html._APP_JS` —— 读 `routes` / `stations` / `journey` /
        `topRoutes` / `cityRank` / `dateDist` / `markers` / `palette` / `kpi`。
    所以下面既要建并列数组，也要建那一份 ECharts 形状的统计。
    """
    pal = TH._palette(palette)
    invs = [x for x in (invoices or []) if isinstance(x, dict)]
    index = T.load_station_index()

    linked, unlinked, stays, excluded = [], [], [], []
    transit_amount = stay_amount = 0.0
    review_count = 0
    abroad = []

    for i, inv in enumerate(invs, 1):
        try:
            seq = int(inv.get("seq") or i)
        except (TypeError, ValueError):
            seq = i
        cls = _trip.classify_trip(inv)
        if inv.get("needs_review"):
            review_count += 1

        # 住宿票面即使只有开票日，仍要作为住宿票显示；开票日不能冒充入住日。
        is_stay = (cls.get("mode") == "stay") or _s(inv.get("kind")) == "hotel"
        is_transport = _is_transport(inv)

        if not cls.get("available") and not is_stay and not is_transport:
            # ── 非发票 / 无行程信息：如实入 excluded，金额恒 0，不进 totals ──
            note = _s(inv.get("kind") if inv.get("kind") == "noninvoice" else "")
            excluded.append({
                "legId": leg_id(inv.get("filepath"), inv.get("filename")),
                "seq": seq,
                "filename": _s(inv.get("filename") or inv.get("filepath")),
                "kind": _s(inv.get("kind")),
                "type": _s(inv.get("type")),
                "item": _s(inv.get("item_name")),
                "seller": _s(inv.get("seller")),
                "date": R.short_date(inv.get("date")) or "",
                "amount": 0.0,          # 红线：非发票金额恒 0
                "reason": _s(inv.get("trip_excluded_reason"))
                          or ("非电子发票（行程单/小票，不作为发票入账）" if note
                              else "无出发地/到达地，也无入住日期"),
            })
            continue

        if is_stay:
            # ── 住宿：永不进地图，city 从酒店名前缀抽，抽不到即 "" ──
            hotel = _s(inv.get("hotel_name") or inv.get("seller") or "未知酒店")
            checkin = _s(inv.get("checkin_date") or inv.get("checkin"))
            checkout = _s(inv.get("checkout_date") or inv.get("checkout"))
            amt = _num(inv.get("amount"))
            stay_amount += amt
            nights = _trip.nights_between(checkin, checkout)
            city = (_trip.city_from_hotel(hotel) or _s(inv.get("city"))
                    or _s(inv.get("hotel_city")) or _s(inv.get("checkin_city")))
            stays.append({
                "legId": leg_id(inv.get("filepath"), hotel),
                "sourcePath": _s(inv.get("filepath")),
                "seq": seq,
                "hotelName": hotel,
                "company": _s(inv.get("company") or inv.get("seller")),
                "city": city,
                "buyer": _s(inv.get("buyer_name") or inv.get("guest_name")),
                "checkin": R.short_date(checkin) or "—",
                "checkout": R.short_date(checkout) if R.date_parts(checkout)[0] else "—",
                "checkoutInferred": bool(inv.get("checkout_inferred")),
                "nights": nights,               # 可为 None：缺日期时页面显 —，**不得求和成 0**
                "amount": amt,
                "item": _s(inv.get("item_name")),
                "invoiceNo": _s(inv.get("invoice_no")),
                "invoiceDate": _s(inv.get("invoice_date")),
                "filename": _s(inv.get("filename")),
                "review": bool(inv.get("needs_review")),
                "linkable": False,              # 红线：住宿永不进地图
            })
            continue

        # ── 交通：先判定能不能连线 ──
        raw_from = _s(inv.get("from_"))
        raw_to = _s(inv.get("to_"))
        a = T.match_station(raw_from, index)
        b = T.match_station(raw_to, index)
        is_abroad = bool(T._is_abroad(raw_from) or T._is_abroad(raw_to))
        km = None
        if a["ok"] and b["ok"]:
            km = T.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        # `trainmap.resolve_mode` 只认 train/air（`TRAIN_MODES=("train","air")`，
        # 设计 6.3 已确认没有 bus/road 这一档）。而打车票在 facets 里就是
        # `kind='taxi'`、**from_/to_ 被刻意留空**，走到 resolve_mode 会被当成
        # 普通火车票（默认值），所以这里必须先按票种短路，否则明细里
        # 打车会挂着「车次 / 出发站」的错措辞。
        kind = _s(inv.get("kind"))
        if kind == "taxi":
            mode = "taxi"
        elif kind in ("bus", "road"):
            mode = "bus"
        else:
            mode = T.resolve_mode(inv, mode_default)
        date_raw = _s(inv.get("train_date") or inv.get("date"))
        time_raw = _s(inv.get("train_time") or "")

        def _end(mt, raw):
            """一个端点：`{raw, resolved, city, lon, lat, matchType, coordLevel}`。"""
            return {
                "raw": raw,
                "resolved": mt.get("resolved") if mt.get("ok") else "",
                "city": mt.get("city") if mt.get("ok") else "",
                "lon": (float(mt["lon"]) if mt.get("ok") and mt.get("lon") is not None else None),
                "lat": (float(mt["lat"]) if mt.get("ok") and mt.get("lat") is not None else None),
                "matchType": mt.get("match_type") or "unmatched",
                "coordLevel": "station" if (mt.get("match_type") in ("exact", "alias")) else (
                    "city" if mt.get("ok") else "none"),
            }

        leg = {
            "legId": leg_id(inv.get("filepath"), f"{raw_from}>{raw_to}"),
            "sourcePath": _s(inv.get("filepath")),
            "seq": seq,
            "mode": mode,
            "date": R.short_date(date_raw) or "",
            "time": R.time_part(date_raw) or time_raw,
            "train": _s(inv.get("train")),
            "seat": _s(inv.get("seat")),
            "seatClass": _s(inv.get("seat_class")),
            "passenger": _s(inv.get("passenger")),
            "idNo": _s(inv.get("id_no")),
            "ticketNo": _s(inv.get("ticket_no")),
            "invoiceNo": _s(inv.get("invoice_no")),
            "amount": _num(inv.get("amount")),
            "fareLabel": _s(inv.get("fare_label") or "票价"),
            "kind": _s(inv.get("special_type")),
            "from": _end(a, raw_from),
            "to": _end(b, raw_to),
            "km": (round(km, 1) if km is not None else None),
            "abroad": is_abroad,
            "review": bool(inv.get("needs_review")),
            "filename": _s(inv.get("filename")),
            "item": _s(inv.get("item_name")),
            "note": _s(inv.get("note")),
        }
        transit_amount += leg["amount"]
        if is_abroad:
            abroad.append(leg)

        # 可连线的三个条件：两端坐标都解析出来了 + 不是境外 + 没被标成「不可连线」
        ok = bool(a["ok"] and b["ok"] and not is_abroad and not inv.get("no_link"))
        if ok:
            leg["linkable"] = True
            leg["unlinkedReason"] = ""
            linked.append(leg)
        else:
            leg["linkable"] = False          # 红线：不可连线者一律 False
            # 「未匹配」只指**票面有地名但匹配不上**的段；打车/大巴本来就没有
            # 上下车地名（`facets` 侧刻意留空），不算未匹配，不能混进未匹配清单。
            leg["unmatchedEndpoint"] = bool(
                not is_abroad and (
                    (raw_from and not a["ok"])
                    or (raw_to and not b["ok"])
                    # 机票：`facets` 只在备注栏抽行程（`_RE_FLIGHT`），实测覆盖率
                    # 约 41%，解析不全时 from_/to_ 就是空的。这不是「按口径不连线」，
                    # 而是**行程信息分析不全**（用户原话），要进未匹配清单等人工补全。
                    or (mode == "air" and not (raw_from and raw_to))))
            if is_abroad:
                leg["unlinkedReason"] = "港澳台或境外行程，当前地图未展示"
            elif mode == "taxi" or kind == "taxi":
                leg["unlinkedReason"] = "票面未提供具体的上下车地点"
            elif mode == "bus":
                leg["unlinkedReason"] = "票面未提供具体的出发地和到达地"
            elif mode == "air" and not (raw_from and raw_to):
                leg["unlinkedReason"] = "未识别出完整的出发地和目的地，可在明细中补充"
            elif not a["ok"] and not b["ok"]:
                leg["unlinkedReason"] = "出发地和目的地信息不完整"
            elif not a["ok"]:
                leg["unlinkedReason"] = f"未识别出出发地「{raw_from or '未填写'}」"
            elif not b["ok"]:
                leg["unlinkedReason"] = f"未识别出目的地「{raw_to or '未填写'}」"
            elif not (leg["date"] and leg["time"]):
                leg["unlinkedReason"] = "出发日期或时间不完整"
            else:
                leg["unlinkedReason"] = "行程地点信息不完整"
            unlinked.append(leg)

    # ── 明细（含退票，报销视角）供表格渲染；linked 在前、unlinked 次之 ──
    details = linked + unlinked

    return _assemble(
        palette=pal, linked=linked, unlinked=unlinked, stays=stays,
        excluded=excluded, details=details, abroad=abroad,
        transit_amount=transit_amount, stay_amount=stay_amount,
        review_count=review_count, index=index,
        include_map=include_map, include_charts=include_charts,
        title=title, subtitle=subtitle, max_timeline=max_timeline,
        skipped=skipped,
    )


def _station_city(mt, raw):
    """端点 → 城市键（用于「城市到访排行」）：先取 match 出来的城市，再退站名前缀。"""
    if mt.get("city"):
        return mt["city"]
    name = _s(raw)
    if not name:
        return ""
    base = T.norm_station_name(name)
    for cand in (base, base.rstrip("东南西北")):
        if cand and cand in (T.load_station_index().get("by_city") or {}):
            return cand
    return ""


def _date_order(value):
    parts = R.date_parts(value)
    return (0, *parts) if parts[0] else (1, 9999, 12, 31)


def _transit_order(leg):
    return (_date_order(leg.get("date")), leg.get("time") or "99:99",
            leg.get("seq") or 0)


def _stay_order(stay):
    day = next((value for value in (stay.get("checkin"), stay.get("checkout"),
                                   stay.get("invoiceDate"))
                if R.date_parts(value)[0]), "")
    return (_date_order(day), stay.get("seq") or 0)


def _assemble(*, palette, linked, unlinked, stays, excluded, details, abroad,
              transit_amount, stay_amount, review_count, index,
              include_map, include_charts, title, subtitle, max_timeline, skipped):
    """把四条数组折算成 payload 的统计部分（ECharts 形状 + 本页自己的 totals）。"""
    # 时间轴与明细使用同一顺序：日期、当天时间、原票序；缺日期的放最后。
    linked.sort(key=_transit_order)
    unlinked.sort(key=_transit_order)
    stays.sort(key=_stay_order)
    details = sorted(linked + unlinked, key=_transit_order)
    timeline_rows = details[:max_timeline]
    drawable = bool(linked) and bool(include_map)

    # ── 车站访问次数（只有连线段才参与，住宿/不可连线者一律不进）──
    station_hits = {}
    for leg in linked:
        for key in ("from", "to"):
            e = leg[key]
            nm = e.get("resolved") or e.get("raw")
            if not nm or e.get("lat") is None:
                continue
            rec = station_hits.setdefault(nm, {
                "name": nm, "lon": e["lon"], "lat": e["lat"],
                "city": e.get("city") or "", "count": 0,
            })
            rec["count"] += 1
    stations = sorted(station_hits.values(), key=lambda x: (-x["count"], x["name"]))
    station_max = max([s["count"] for s in stations] or [1])

    # ── 无向线路聚合 ──
    route_hits = {}
    for leg in linked:
        a, b = leg["from"], leg["to"]
        key = tuple(sorted([a.get("resolved") or a.get("raw"), b.get("resolved") or b.get("raw")]))
        if not all(key):
            continue
        rec = route_hits.setdefault(key, {
            "a": key[0], "b": key[1],
            "name": key[0] + " → " + key[1],
            "c": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
            "count": 0, "km": leg["km"] or 0.0,
        })
        rec["count"] += 1
    routes = sorted(route_hits.values(), key=lambda x: (-x["count"], x["a"]))
    top_count = max([r["count"] for r in routes] or [1])
    for r in routes:
        r["ratio"] = round(r["count"] / top_count, 4)
    top_routes = routes[:10]

    # ── 城市排行 ──
    city_hits = {}
    for leg in linked:
        for key in ("from", "to"):
            e = leg[key]
            city = e.get("city") or _station_city(
                {"city": e.get("city")}, e.get("resolved") or e.get("raw"))
            if not city:
                continue
            city_hits[city] = city_hits.get(city, 0) + 1
    city_rank = [{"name": k, "count": v}
                 for k, v in sorted(city_hits.items(), key=lambda kv: (-kv[1], kv[0]))]

    # ── 出行时间分布（跨度 > 100 天按月，否则按日）──
    dk_list = [R.date_parts(leg["date"]) for leg in linked]
    dk_list = [d for d in dk_list if d[0]]
    if dk_list:
        dk_list.sort()
        import datetime as _dt
        d0 = _dt.date(*dk_list[0])
        d1 = _dt.date(*dk_list[-1])
        span = (d1 - d0).days
    else:
        span = 0
    date_kind = "month" if span > 100 else "day"
    dist = {}
    for leg in linked:
        y, mo, d = R.date_parts(leg["date"])
        if not y:
            continue
        key = f"{y}-{mo:02d}" if date_kind == "month" else f"{y}-{mo:02d}-{d:02d}"
        cell = dist.setdefault(key, {"name": key, "count": 0, "km": 0.0})
        cell["count"] += 1
        cell["km"] = round(cell["km"] + (leg["km"] or 0.0), 1)
    date_dist = [dist[k] for k in sorted(dist)]

    # ── 巡航轨迹（只有连线段；`km` 为 None 的一律不进，不能当 0）──
    journey = []
    for leg in linked:
        a, b = leg["from"], leg["to"]
        if leg["km"] is None or a.get("lat") is None or b.get("lat") is None:
            continue
        journey.append({
            "a": a.get("resolved") or a.get("raw"),
            "a_lon": a["lon"], "a_lat": a["lat"],
            "b": b.get("resolved") or b.get("raw"),
            "b_lon": b["lon"], "b_lat": b["lat"],
            "c": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
            "km": leg["km"], "date": leg["date"], "train": leg["train"],
            "pax": leg["passenger"], "mode": leg["mode"],
            "cruise": TH.w(leg["mode"], "cruise"),
        })
    journey.sort(key=lambda x: (_date_order(x["date"]), x["a"] or ""))
    km_total = round(sum(j["km"] for j in journey), 1)
    max_route_km = round(max([j["km"] for j in journey] or [0.0]), 1)

    # ── 整页 mode ──
    modes = [r["mode"] for r in linked] or [r["mode"] for r in unlinked]
    pm = TH.page_mode(modes, "train") if modes else "train"
    mode_count = {}
    for r in linked + unlinked:
        mode_count[r["mode"]] = mode_count.get(r["mode"], 0) + 1

    # ── KPI ──
    station_only = [s for s in stations]
    kpi = {
        "trips": len(linked) + len(unlinked),
        "linkedTrips": len(linked),
        "unlinkedTrips": len(unlinked),
        "km": km_total,
        "cities": len(city_rank),
        "stations": len(station_only),
        "routes": len(routes),
        "top_station": (max(station_only, key=lambda s: s["count"])["name"] if station_only else ""),
        "top_station_count": (max((s["count"] for s in station_only), default=0)),
        "date_from": (journey[0]["date"] if journey else ""),
        "date_to": (journey[-1]["date"] if journey else ""),
        "days": len({j["date"] for j in journey if j["date"]}),
        "passengers": len({r["passenger"] for r in linked + unlinked if r["passenger"]}),
        "max_route_km": max_route_km,
        "avg_km": (round(km_total / len(journey), 1) if journey else 0.0),
        "top_from": _top_endpoint(linked, "from"),
        "top_from_count": _top_endpoint(linked, "from", count=True),
        "top_to": _top_endpoint(linked, "to"),
        "top_to_count": _top_endpoint(linked, "to", count=True),
        "longest": (max(journey, key=lambda j: j["km"])["a"] if journey else ""),
        "longest_km": max_route_km,
    }

    # ── totals（红线 4：grand = transit + stay，误差 < 0.01）──
    transit_amount = round(transit_amount, 2)
    stay_amount = round(stay_amount, 2)
    grand = round(transit_amount + stay_amount, 2)
    # 住宿晚数：**全部票面都缺日期时是 None，不是 0**（设计 6.5：
    # 「nights 可空…不得求和成 0」）。0 看起来像「本次住宿 0 晚」，
    # 那是假话；None 在页面上一律显「—」。
    night_vals = [s["nights"] for s in stays if s["nights"]]
    totals = {
        "grandAmount": grand,
        "transitAmount": transit_amount,
        "stayAmount": stay_amount,
        "linkedAmount": round(sum(x["amount"] for x in linked), 2),
        "unlinkedAmount": round(sum(x["amount"] for x in unlinked), 2),
        "excludedAmount": 0.0,
        "upper": _parse.amount_upper(grand),
        "count": len(linked) + len(unlinked) + len(stays),
        "linkedCount": len(linked),
        "unlinkedCount": len(unlinked),
        "stayCount": len(stays),
        "excludedCount": len(excluded),
        "nights": (sum(night_vals) if night_vals else None),
        "nightUnknown": sum(1 for s in stays if not s["nights"]),
    }

    # ── 未匹配清单：**只有真正「需要人工补全」的段** ──
    # 打车/大巴是按口径不连线（票面本来就没有地名），不是「未匹配」，
    # 把它们塞进未匹配清单会让 75 张打车票淹掉真正需要补全的几段；
    # 机票则是「行程信息分析不全」（备注栏抽不出出发/到达地），要进清单。
    unmatched = [{
        "legId": x["legId"], "mode": x["mode"], "date": x["date"], "train": x["train"],
        "from": x["from"]["raw"] or "未知", "to": x["to"]["raw"] or "未知",
        "km": None, "amount": x["amount"], "reason": x.get("unlinkedReason") or "",
    } for x in unlinked if x.get("unmatchedEndpoint")]

    chips = _chips(linked=linked, unlinked=unlinked, stays=stays,
                   totals=totals, review_count=review_count)
    kpi_cards = _kpi_cards(kpi, totals, len(linked), len(unlinked), len(stays),
                           review_count, palette)

    return {
        "title": title or "行程汇总单",
        "subtitle": subtitle or _subtitle(totals, kpi),
        "palette": palette,
        "mode": pm,
        "modes": sorted({r["mode"] for r in linked}) or ([pm] if linked else []),
        "modeCount": mode_count,
        "markers": {m: TH.vehicle_marker_datauri(m, palette) for m in T.TRAIN_MODES},
        "trainMarker": TH.vehicle_marker_datauri(pm, palette),

        # ── 并列四数组（本页的核心）──
        "transit": {"linked": linked, "unlinked": unlinked},
        "stay": stays,
        "excluded": excluded,
        "totals": totals,
        "details": details[:_MAX_DETAILS],
        "detailsTotal": len(details),
        "detailsTruncated": len(details) > _MAX_DETAILS,
        "timeline": timeline_rows,
        "timelineTotal": len(linked) + len(unlinked),
        "timelineTruncated": (len(linked) + len(unlinked)) > max_timeline,
        "unmatched": unmatched,
        "abroad": abroad,

        # ── 复用 trainmap `_APP_JS` 需要的 ECharts 形状 ──
        "includeMap": drawable,
        "includeCharts": bool(include_charts),
        # 设计方案 6.8 的断言就是「只有住宿输入时 map is None 且 linked == []」。
        # 这里 `map` 是「地图要画什么」的摘要段：没有可连线行程时**必须是 None**
        # （不是空 dict —— 空 dict 在断言里是假话）。
        "map": ({"drawable": True, "stations": len(stations), "routes": len(routes),
                 "segments": len(journey), "modes": sorted({r["mode"] for r in linked})}
                if drawable else None),
        "stations": stations,
        "routes": routes,
        "journey": journey if drawable else [],
        "journeyTotal": len(journey),
        "topRoutes": top_routes,
        "cityRank": city_rank,
        "dateDist": date_dist,
        "dateDistKind": date_kind,
        "kpi": kpi,
        "kpiCards": kpi_cards,
        "chips": chips,
        "stayVendors": _vendors(stays),
        "hasStay": bool(stays),
        "skipped": skipped,
        "reviewCount": review_count,
        "generatedAt": "",
        "sourceNote": "数据来源：本人上传的电子发票（火车票 / 机票 / 住宿）",
    }


def _top_endpoint(legs, key, count=False):
    tally = {}
    for leg in legs:
        nm = leg[key].get("resolved") or leg[key].get("raw")
        if nm:
            tally[nm] = tally.get(nm, 0) + 1
    if not tally:
        return 0 if count else ""
    name = max(tally, key=lambda k: (tally[k], k))
    return tally[name] if count else name


def _vendors(stays):
    """住宿开单单位（按 酒店名 + 销售方税号 去重，按金额降序）—— 对齐 hotel_html 口径。"""
    tally = {}
    for s in stays:
        key = (s["hotelName"], "")
        rec = tally.setdefault(key, {"name": s["hotelName"], "taxId": "",
                                     "count": 0, "amount": 0.0})
        rec["count"] += 1
        rec["amount"] = round(rec["amount"] + s["amount"], 2)
    return sorted(tally.values(), key=lambda x: -x["amount"])


def _chips(*, linked, unlinked, stays, totals, review_count):
    chips = []
    if linked or unlinked:
        chips.append(["交通", f"{len(linked) + len(unlinked)} 张 · ￥{totals['transitAmount']:,.2f}"])
    if stays:
        chips.append(["住宿", f"{len(stays)} 张 · ￥{totals['stayAmount']:,.2f}"])
    if totals["nights"]:
        chips.append(["住宿晚数", f"{totals['nights']} 晚"])
    chips.append(["票面合计", f"￥{totals['grandAmount']:,.2f}"])
    if review_count:
        chips.append(["待复核", f"{review_count} 张"])
    return chips


def _kpi_cards(kpi, totals, linked_n, unlinked_n, stay_n, review_count, palette):
    """KPI 卡：**只住宿时交通那几张自动消失**（三态退化之一）。

    卡片数是设计 6.7 的「交通 3 张 + 住宿 2 张 + 票面合计 1 张 = 6 张」：
    只住宿时交通 3 张（出行段数 / 总里程 / 覆盖城市）整组不出现，剩 3 张。
    """
    cards = []
    if linked_n or unlinked_n:
        cards.append({"lab": "交通行程", "count": linked_n + unlinked_n, "unit": "段",
                      "dec": "火车、飞机及其他交通",
                      "color": palette["accent"]})
    if linked_n:
        cards.append({"lab": "总里程", "count": kpi["km"], "unit": "km",
                      "dec": f"单程最长 {kpi['max_route_km']:,} km", "color": palette["accent"]})
        cards.append({"lab": "覆盖城市", "count": kpi["cities"], "unit": "城",
                      "dec": f"覆盖车站 {kpi['stations']} 个", "color": palette["accent2"]})
    if stay_n:
        cards.append({"lab": "住宿", "count": stay_n, "unit": "张",
                      "dec": ("共 %d 晚" % totals["nights"]) if totals["nights"] else "晚数未知",
                      "color": palette["station"]})
        cards.append({"lab": "住宿金额", "text": f"￥{totals['stayAmount']:,.2f}",
                      "dec": "含全部住宿发票", "color": palette["station"]})
    cards.append({"lab": "票面合计", "text": f"￥{totals['grandAmount']:,.2f}",
                  "dec": totals["upper"], "color": palette["accent"], "cls": "wide"})
    if review_count:
        cards.append({"lab": "待复核", "count": review_count, "unit": "张",
                      "dec": "票面字段不完整", "color": palette["map_glow"], "cls": "warn"})
    return cards[:8]


def _subtitle(totals, kpi):
    bits = []
    n_move = totals["linkedCount"] + totals["unlinkedCount"]
    if n_move:
        bits.append(f"交通 {n_move} 段")
    if totals["stayCount"]:
        bits.append(f"住宿 {totals['stayCount']} 张")
    bits.append(f"票面合计 ￥{totals['grandAmount']:,.2f}")
    return " · ".join(bits)
