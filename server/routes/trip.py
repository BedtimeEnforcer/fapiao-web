# -*- coding: utf-8 -*-
r"""routes/trip.py — POST /api/trip/run：「行程汇总单」预览生成入口。

职责边界：本层**只做 HTTP ⇄ core 的翻译**（与 `train.py` / `hotel.py` 同一套规矩）：
  * 路径规整 / 目录展开 / 默认输出目录 → 交给 `_shared`；
  * 行程判定、聚合、HTML 渲染 → 一律调 `core.trip*`，此处不复现任何算法。

与既有行程单的两处**刻意不同**（都是用户点名的口径）：

1. **产物文件名固定**为「行程汇总单.html」，**不得**带 `(1)(2)` 后缀。
   所以这里不用 `trainmap_html._unique_path`，而是直接写进
   `preview.new_run_dir("trip")` 每次新建的时间戳子目录（本来就不会撞名）。
2. **不预先创建 `out_dir`**：目录只在用户点「保存到本地」时才落盘
   （与 `/api/train/run` 注释同一条规矩）。
"""
from __future__ import annotations

import os
import sys

# ── 让 `core` 可被导入（与 train.py 同一套规则，兼容 PyInstaller 解包目录）──
if getattr(sys, "frozen", False):
    _SERVER = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
else:
    _SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from fastapi import APIRouter  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from core import hotel as core_hotel      # noqa: E402
from core import train as core_train      # noqa: E402
from core import trip as core_trip        # noqa: E402
from core import trip_html as core_trip_html    # noqa: E402
from core import trip_summary as core_trip_summary  # noqa: E402
from core import vault as core_vault  # noqa: E402

from . import _shared                     # noqa: E402
from . import preview                     # noqa: E402

router = APIRouter()

#: 预览工具条上显示的名字，同时也是 `preview.decorate(out_sub=...)` 的默认落盘子目录。
OUT_NAME = "行程单"


@router.post("/run")
async def run(payload: dict):
    """生成《行程汇总单》HTML（交通 + 住宿一张页面）。"""
    p = payload or {}
    paths = _shared.resolve_inputs(p.get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(OUT_NAME)
    # 注意：此处**不**预先创建 out_dir —— 行程单走浏览器预览，
    # 目录只在用户真的点「保存到本地」时才落盘。

    dedup = bool(p.get("dedup", True))
    include_map = bool(p.get("include_map", True))
    include_charts = bool(p.get("include_charts", True))

    invoices, skipped = core_trip_summary.collect_invoices(
        paths, dedup=dedup, trip_only=True)
    if not invoices:
        return JSONResponse({"ok": False, "error": "没有可用于行程单的交通或住宿票"}, status_code=400)

    # 前端手动补全的行程（按 filepath 逐条套用；缺项静默跳过）
    trip_applied = core_trip.apply_trip_overrides(invoices, p.get("trip_overrides") or {})

    payload_data = core_trip_summary.build_trip_payload(
        invoices, include_map=include_map, include_charts=include_charts,
        skipped=skipped)

    # 编辑入口只带发票库编号进生成页；本机绝对路径不写进可保存的 HTML。
    for item in [*(payload_data.get("timeline") or []), *(payload_data.get("stay") or [])]:
        source = item.pop("sourcePath", "")
        item["vaultId"] = core_vault.item_id_for_path(source) if source else None

    totals = payload_data["totals"]
    link_n = len(payload_data["transit"]["linked"])
    if not (link_n or payload_data["transit"]["unlinked"] or payload_data["stay"]
            or payload_data["excluded"]):
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    # 产物写进「预览目录」：浏览器新标签页直接看，点「保存到本地」才落到桌面。
    preview_dir = preview.new_run_dir("trip")
    try:
        artifact = core_trip_html.build_trip_payload_html(
            payload_data, preview_dir,
            include_map=include_map, include_charts=include_charts,
            filename=core_trip_html.TRIP_FILENAME)
        kind = "html"
    except Exception as e:
        # 降级：单类输入退到既有 PDF 生成器（浏览器同样能直接打开）。
        # 交通 + 住宿混合输入没有现成的 PDF 版式，直接报错说明，不硬造。
        try:
            if payload_data["stay"] and not (link_n or payload_data["transit"]["unlinked"]):
                artifact = core_hotel.build_hotel_itinerary_pdf(
                    _hotel_records(payload_data["stay"]),
                    round(totals["stayAmount"], 2), preview_dir)
            elif (link_n or payload_data["transit"]["unlinked"]) and not payload_data["stay"]:
                artifact = core_train.build_train_itinerary_pdf(
                    invoices, round(totals["transitAmount"], 2), 0.0, 0.0,
                    round(totals["transitAmount"], 2), preview_dir, include_map=False)
            else:
                raise RuntimeError("交通与住宿混合输入暂无 PDF 降级版式")
        except Exception as e2:
            return JSONResponse(
                {"ok": False, "error": f"行程汇总单生成失败：{e} / 降级 PDF 也失败：{e2}"},
                status_code=500)
        if not artifact:
            return JSONResponse({"ok": False, "error": f"行程汇总单生成失败：{e}"}, status_code=500)
        kind = "pdf"

    if kind == "html":
        preview.decorate(artifact, label="行程汇总单", out_sub=OUT_NAME)

    return {
        "ok": True,
        "preview_url": preview.preview_url(artifact),
        "filename": os.path.basename(artifact),
        "kind": kind,
        "count": len(invoices),
        "transit_count": link_n + len(payload_data["transit"]["unlinked"]),
        "stay_count": len(payload_data["stay"]),
        "total_amount": round(totals["grandAmount"], 2),
        "skipped": skipped,
        "review_count": payload_data.get("reviewCount", 0),
        "out_dir": out_dir,
        "trip_applied": trip_applied,
    }


def _hotel_records(stays):
    """住宿腿 → `core.hotel.build_hotel_itinerary_pdf` 认的记录键名（仅降级路径用）。"""
    return [{
        "seq": s.get("seq"),
        "hotel_name": s.get("hotelName") or "",
        "guest_name": "",
        "checkin_date": "" if s.get("checkin") == "—" else (s.get("checkin") or ""),
        "checkout_date": "" if s.get("checkout") == "—" else (s.get("checkout") or ""),
        "amount": s.get("amount") or 0.0,
        "invoice_no": s.get("invoiceNo") or "",
        "invoice_date": s.get("invoiceDate") or "",
        "item_name": s.get("item") or "",
        "filename": s.get("filename") or "",
        "needs_review": bool(s.get("review")),
        "review_reason": "",
    } for s in (stays or [])]
