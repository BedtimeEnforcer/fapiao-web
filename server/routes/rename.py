# -*- coding: utf-8 -*-
"""routes/rename.py — 「智能重命名」模块的 HTTP 接口。

流程：先预览（解析每张票 → 给出规范文件名），用户在界面上确认/微调后再执行。
这样「先看再改」是两步，绝不在用户没确认前动文件。

接口
----
  POST /api/rename/scan       传路径 → 返回「原文件名 → 新文件名」预览
  POST /api/rename/run        传确认后的 [{path,new_name}] + 模式 → 执行重命名
  GET  /api/rename/default_out 默认输出目录（仅复制模式用）
"""
from __future__ import annotations

import os
import shutil

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared                                   # 导入即完成 sys.path 设置
from core.rename import parse_invoice_for_rename        # noqa: E402
from core.parse import _unique_path                     # noqa: E402

router = APIRouter()

OUT_NAME = "发票重命名"


def apply_rename(fp: str, new_name: str, mode: str = "inplace", out_dir: str = "") -> dict:
    """**重命名的唯一落盘实现** —— copy / inplace 两条路径都走这里。

    调用方：
      * `routes/rename.py` 的 `POST /run`（重命名模块）；
      * `routes/pipeline.py` 的 `_stage_rename`（「点一次干完」流水线，用户 2026-10-11
        第⑪条「它不叫『移动原件』而是『修改原件』，这个是被允许的」）。

    复用而不是各写一套：inplace 的语义（改原件名、绝不删文件、绝不改内容）只有
    一处实现，两边不会漂移。

    返回 `{"ok": True, "new_path": ..., "new_name": ..., "skipped"?: True}` 或
    `{"ok": False, "reason": "..."}` —— **不抛异常**，调用方按 `failure_policy` 处置。
    """
    if not fp or not os.path.isfile(fp):
        return {"ok": False, "reason": "文件不存在"}
    name = (new_name or "").strip()
    if not name:
        return {"ok": False, "reason": "新文件名为空"}
    # 去掉用户可能误加的分隔符，并保留原扩展名（避免把 .pdf 改没了）
    name = os.path.basename(name.replace("\\", "/"))
    if not os.path.splitext(name)[1]:
        name += os.path.splitext(fp)[1]

    use_copy = (mode or "inplace").lower() == "copy"
    try:
        if use_copy:
            target = _unique_path(os.path.join(out_dir, name))
            shutil.copy2(fp, target)
        else:
            if name == os.path.basename(fp):          # 名字没变，跳过
                return {"ok": True, "new_path": fp, "skipped": True}
            target = _unique_path(os.path.join(os.path.dirname(fp), name))
            os.rename(fp, target)
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "new_path": target, "new_name": os.path.basename(target)}


@router.get("/default_out")
def default_out():
    return {"ok": True, "dir": _shared.default_out_dir(OUT_NAME)}


@router.post("/scan")
async def scan(payload: dict):
    """解析每张发票，给出建议的规范文件名（只读预览，不动任何文件）。"""
    paths = _shared.resolve_inputs((payload or {}).get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未找到可解析的发票文件"}, status_code=400)

    rows = []
    for i, fp in enumerate(paths):
        d = parse_invoice_for_rename(fp)
        rows.append({
            "id": i,
            "path": fp,
            "filename": _shared.display_name(fp) or (d.get("current_name", "") or ""),
            "new_name": d.get("new_name", "") or "",
            "ext": os.path.splitext(fp)[1].lower().lstrip("."),
            "type": d.get("type", "") or "未知",
            "amount": float(d.get("amount", 0.0) or 0.0),
            "date": d.get("date", "") or "",
            "error": d.get("error"),
            "kind": "rename",
        })

    ok = sum(1 for r in rows if not r["error"])
    changed = sum(1 for r in rows if not r["error"] and r["new_name"]
                  and r["new_name"] != r["filename"])
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "parsed": ok,
        "changed": changed,
        "default_out": _shared.default_out_dir(OUT_NAME),
    }


@router.post("/run")
async def run(payload: dict):
    """执行重命名。mode='inplace' 改原文件名；mode='copy' 复制到 out_dir（保留原件）。"""
    p = payload or {}
    items = p.get("items") or []
    if not items:
        return JSONResponse({"ok": False, "error": "没有可重命名的文件"}, status_code=400)

    mode = (p.get("mode") or "inplace").lower()
    use_copy = mode == "copy"
    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(OUT_NAME)
    if use_copy:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"输出目录不可用：{e}"}, status_code=400)

    results = []
    success = failed = 0
    for it in items:
        fp = (it or {}).get("path") or ""
        res = apply_rename(fp, ((it or {}).get("new_name") or ""), mode, out_dir)
        res["path"] = fp
        results.append(res)
        if res.get("ok"):
            success += 1
        else:
            failed += 1

    return {
        "ok": True,
        "results": results,
        "success": success,
        "failed": failed,
        "mode": mode,
        "out_dir": out_dir if use_copy else "",
    }
