# -*- coding: utf-8 -*-
"""routes/hotel.py — 「住宿票行程单」模块的 HTTP 接口。

胶水层：把 HTTP 请求翻译成 `core.hotel` / `core.hotel_html` 的调用。

接口
----
  POST /api/hotel/scan        传路径（目录/文件混合）→ 解析住宿发票 → 返回行
  POST /api/hotel/run         生成住宿行程单 HTML（可选：合并原始票为 PDF）
  GET  /api/hotel/default_out 默认输出目录
"""
from __future__ import annotations

import datetime
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared                           # 导入即完成 sys.path 设置
from . import preview                           # noqa: E402  预览目录 / 工具条注入
from core import hotel as core_hotel            # noqa: E402
from core.hotel_html import build_hotel_html    # noqa: E402

router = APIRouter()

OUT_NAME = "住宿行程单"


def _nights(checkin: str, checkout: str) -> int:
    """按入住/退房日期算晚数；任一侧缺失或不可解析返回 0（票面确实未含区间）。"""
    try:
        a = datetime.datetime.strptime((checkin or "")[:10], "%Y-%m-%d")
        b = datetime.datetime.strptime((checkout or "")[:10], "%Y-%m-%d")
        return max((b - a).days, 0)
    except Exception:
        return 0


def _row(rec: dict, idx: int) -> dict:
    """把 core 的住宿记录转成前端表格行的结构。"""
    fp = rec.get("filepath", "") or ""
    checkin = rec.get("checkin_date", "") or ""
    checkout = rec.get("checkout_date", "") or ""
    return {
        "id": idx,
        "path": fp,
        "filename": _shared.display_name(fp) or (rec.get("filename", "") or ""),
        "ext": os.path.splitext(fp)[1].lower().lstrip("."),
        # 住宿核心字段
        "hotel_name": rec.get("hotel_name", "") or "",
        "guest_name": rec.get("guest_name", "") or "",
        "date": checkin,
        "checkout": checkout,
        "nights": _nights(checkin, checkout),
        "amount": float(rec.get("amount", 0.0) or 0.0),
        "invoice_no": rec.get("invoice_no", "") or "",
        "needs_review": bool(rec.get("needs_review")),
        "review_reason": rec.get("review_reason", "") or "",
        "kind": "hotel",
    }


@router.get("/default_out")
def default_out():
    return {"ok": True, "dir": _shared.default_out_dir(OUT_NAME)}


@router.post("/scan")
async def scan(payload: dict):
    """解析住宿发票 → 返回明细行（用于表格展示）。"""
    paths = _shared.resolve_inputs((payload or {}).get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未找到可解析的发票文件"}, status_code=400)

    dedup = bool((payload or {}).get("dedup", True))
    res, err = core_hotel.parse_hotel_invoices(paths, False, dedup)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    rows = [_row(rec, i) for i, rec in enumerate(res["records"])]
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_amount": round(res["total_amount"], 2),
        "total_nights": sum(r["nights"] for r in rows),
        "skipped": res.get("skipped", 0),
        "review_count": res.get("review_count", 0),
        "failed_files": res.get("failed_files", []),
        "default_out": _shared.default_out_dir(OUT_NAME),
    }


@router.post("/run")
async def run(payload: dict):
    """生成《住宿行程单》HTML；可选把原始票据合并成一个 PDF。"""
    p = payload or {}
    paths = _shared.resolve_inputs(p.get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(OUT_NAME)
    # 不预先创建 out_dir —— 住宿行程单现在走浏览器预览，目录只在真的保存/合并时才建。

    dedup = bool(p.get("dedup", True))
    res, err = core_hotel.parse_hotel_invoices(paths, False, dedup)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    # 住宿行程单生成到「预览目录」：浏览器直接看，点「保存到本地」才落桌面
    preview_dir = preview.new_run_dir("hotel")
    try:
        artifact = build_hotel_html(res["records"], preview_dir)
        kind = "html"
    except Exception as e:
        try:
            artifact = core_hotel.build_hotel_itinerary_pdf(
                res["records"], res["total_amount"], preview_dir)
        except Exception as e2:
            return JSONResponse(
                {"ok": False, "error": f"行程单生成失败：{e} / 降级 PDF 也失败：{e2}"},
                status_code=500)
        if not artifact:
            return JSONResponse({"ok": False, "error": f"行程单生成失败：{e}"}, status_code=500)
        kind = "pdf"

    if kind == "html":
        preview.decorate(artifact, label="住宿行程单", out_sub=OUT_NAME)

    # 可选：合并原始票据（真正要落盘的产物）
    merged_path = ""
    if p.get("merge"):
        try:
            os.makedirs(out_dir, exist_ok=True)
            from pypdf import PdfWriter
            mp = os.path.join(out_dir, "住宿发票_合并.pdf")
            w = PdfWriter()
            for rec in res["records"]:
                fp = rec.get("filepath")
                if fp and os.path.exists(fp):
                    w.append(fp)
            if len(w.pages) > 0:
                with open(mp, "wb") as f:
                    w.write(f)
                merged_path = mp
        except Exception:
            pass

    return {
        "ok": True,
        "preview_url": preview.preview_url(artifact),
        "filename": os.path.basename(artifact),
        "kind": kind,
        "merged_path": merged_path,
        "out_dir": out_dir,
        "count": len(res["records"]),
        "total_amount": round(res["total_amount"], 2),
        "skipped": res.get("skipped", 0),
    }
