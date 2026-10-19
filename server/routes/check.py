# -*- coding: utf-8 -*-
"""routes/check.py — 「发票信息核对」模块的 HTTP 接口。

核对逻辑：把每张发票票面的「购买方名称 / 纳税人识别号」与用户预设值比对，
归一化后一致即通过。真正的比对实现在 `core.check`，此处只做胶水。

接口
----
  POST /api/check/scan        传路径 → 返回文件列表（仅登记，不解析票面）
  POST /api/check/run         传路径 + 预设值 → 执行核对 → 返回逐张结果
  POST /api/check/report      传核对结果 → 导出核对报告 PDF
  GET  /api/check/default_out 默认输出目录
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared                       # 导入即完成 sys.path 设置
from . import preview                       # noqa: E402  预览目录 / 工具条注入
from core import check as core_check        # noqa: E402
from core.check_html import build_check_html  # noqa: E402

router = APIRouter()

OUT_NAME = "发票核对"


@router.get("/default_out")
def default_out():
    return {"ok": True, "dir": _shared.default_out_dir(OUT_NAME)}


@router.post("/scan")
async def scan(payload: dict):
    """登记待核对的文件（只做存在性/扩展名校验，票面解析推迟到实际核对时）。"""
    paths = _shared.resolve_inputs((payload or {}).get("paths") or [])
    rows = []
    for i, fp in enumerate(paths):
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = 0
        rows.append({
            "id": i, "path": fp,
            "filename": _shared.display_name(fp),
            "ext": os.path.splitext(fp)[1].lower().lstrip("."),
            "size": size,
            "kind": "check",
        })
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "default_out": _shared.default_out_dir(OUT_NAME),
    }


@router.post("/run")
async def run(payload: dict):
    """执行核对。至少需要「购买方名称」或「纳税人识别号」之一。"""
    p = payload or {}
    paths = _shared.resolve_inputs(p.get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    exp_name = (p.get("expected_name") or "").strip()
    exp_tax = (p.get("expected_tax_id") or "").strip()
    if not exp_name and not exp_tax:
        return JSONResponse({"ok": False, "error": "请先填写购买方名称或纳税人识别号"},
                            status_code=400)

    res, err = core_check.check_invoices(paths, exp_name, exp_tax)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    rows = []
    for i, r in enumerate(res["results"]):
        # core 的 results 与入参 paths 一一对应；带上 path 后才能「再次核对」而不丢路径。
        fp = paths[i] if i < len(paths) else ""
        rows.append({
            "id": i,
            "path": fp,
            "filename": _shared.display_name(fp) or (r.get("filename", "") or ""),
            "buyer_name": r.get("buyer_name", "") or "",
            "tax_id": r.get("tax_id", "") or "",
            "seller_name": r.get("seller_name", "") or "",
            "date": r.get("invoice_date", "") or "",
            "amount": float(r.get("amount", 0.0) or 0.0),
            "name_ok": bool(r.get("name_ok")),
            "tax_ok": bool(r.get("tax_ok")),
            "passed": bool(r.get("passed")),
            "needs_review": bool(r.get("needs_review")),
            "review_reason": r.get("review_reason", "") or "",
            "kind": "check",
        })

    # 核对结果生成 HTML 到「预览目录」：浏览器里直接看逐张判定，
    # 需要留档时在页面上点「保存到本地」（不自动往桌面堆文件）。
    preview_url = html_name = ""
    try:
        preview_dir = preview.new_run_dir("check")
        html_path = build_check_html(res["results"], preview_dir,
                                     expected_name=exp_name, expected_tax_id=exp_tax)
        preview.decorate(html_path, label="发票核对结果", out_sub=OUT_NAME)
        preview_url = preview.preview_url(html_path)
        html_name = os.path.basename(html_path)
    except Exception:
        # 结果页生成失败不影响表格结论 —— 前端仍能展示逐张判定。
        preview_url = html_name = ""

    return {
        "ok": True,
        "rows": rows,
        "preview_url": preview_url,
        "html_filename": html_name,
        "total": res["total"],
        "passed": res["passed"],
        "failed": res["failed"],
        "review_count": res.get("review_count", 0),
        "failed_files": res.get("failed_files", []),
        "expected_name": exp_name,
        "expected_tax_id": exp_tax,
    }


@router.post("/report")
async def report(payload: dict):
    """把已核对的结果导出为 PDF 报告（前端把结果回传，避免重复解析）。"""
    p = payload or {}
    rows = p.get("rows") or []
    if not rows:
        return JSONResponse({"ok": False, "error": "没有可导出的核对结果"}, status_code=400)

    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(OUT_NAME)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"输出目录不可用：{e}"}, status_code=400)

    # 还原成 core 期望的字段结构（build_check_report_pdf 直接消费 results）
    results = [{
        "filename": r.get("filename", ""), "buyer_name": r.get("buyer_name", ""),
        "tax_id": r.get("tax_id", ""), "amount": r.get("amount", 0.0),
        "seller_name": r.get("seller_name", ""), "invoice_date": r.get("date", ""),
        "invoice_no": r.get("invoice_no", ""), "tax_rate": r.get("tax_rate", ""),
        "name_ok": r.get("name_ok"), "tax_ok": r.get("tax_ok"),
        "passed": r.get("passed"), "needs_review": r.get("needs_review"),
        "review_reason": r.get("review_reason", ""),
    } for r in rows]

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    try:
        path = core_check.build_check_report_pdf(
            results, (p.get("expected_name") or "").strip(),
            (p.get("expected_tax_id") or "").strip(),
            total, passed, failed, out_dir)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"报告生成失败：{e}"}, status_code=500)

    if not path:
        return JSONResponse({"ok": False, "error": "报告生成失败"}, status_code=500)

    return {"ok": True, "path": path, "filename": os.path.basename(path),
            "out_dir": out_dir, "total": total, "passed": passed, "failed": failed}
