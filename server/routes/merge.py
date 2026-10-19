# -*- coding: utf-8 -*-
"""routes/merge.py — 「发票合并」模块的 HTTP 接口。

本文件是**胶水层**：把 HTTP 请求翻译成 `core/` 的纯函数调用，再把结果转成 JSON。
所有真正的解析 / 合并逻辑都在 `core/`，此处不复现任何算法。

接口一览
--------
  POST /api/merge/scan       接收文件路径列表 → 解析元数据 → 返回表格数据
  POST /api/merge/upload     接收浏览器上传的文件（拖拽用）→ 存临时目录 → 返回路径
  POST /api/merge/run        执行合并 → 返回合并结果
  POST /api/merge/pick       唤起系统文件对话框（选文件 / 选目录）→ 返回路径列表
  GET  /api/merge/default_out 返回默认输出目录
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

# ── 让 `core` 可被导入（routes/ 与 core/ 同在 server/ 下）─────────────
# 打包后 core / routes 会被 PyInstaller 收进解包目录，__file__ 上溯两级即为该目录。
if getattr(sys, "frozen", False):
    _SERVER = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
else:
    _SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from core import facets, merge as core_merge, parse as core_parse  # noqa: E402
from . import _shared  # noqa: E402

router = APIRouter()

#: 浏览器上传的临时目录（拖拽方式走这里）—— 与各模块共用同一份定义
UPLOAD_DIR = _shared.UPLOAD_DIR

#: 支持的发票扩展名
# ⛔ 不要直接跟 DEFAULT_EXTS：合并只认 PDF/OFD。跟过去的话，采集白名单将来
#    再加新格式（历史上就加过 .xml），会让用户一选目录就多出一堆「合并失败」。
#    这里显式钉死成可合并的格式，DEFAULT_EXTS 再加新格式也不会连坐。
SUPPORTED_EXTS = ('.pdf', '.ofd')


# ─────────────────────────── 工具函数 ───────────────────────────

def _norm_paths(paths) -> list:
    """把前端传来的路径规整为「存在的文件」列表，保持顺序、去重。"""
    return [p for p in _shared.norm_paths(paths) if p.lower().endswith(SUPPORTED_EXTS)]


def _expand_dir(folder: str) -> list:
    """把一个目录展开成其中的发票文件列表（按文件名排序）。"""
    return [p for p in _shared.expand_dir(folder) if p.lower().endswith(SUPPORTED_EXTS)]


def _meta_row(fp: str, idx: int) -> dict:
    """调 core 抽取单张发票元数据，转成前端表格要用的行结构。"""
    try:
        m = facets.extract_invoice_meta(fp)
    except Exception as e:
        m = {"kind": "unknown", "type": "解析失败", "amount": 0.0, "date": "",
             "seller": "", "invoice_no": ""}
    return {
        "id": idx,
        "path": fp,
        "filename": _shared.display_name(fp),
        "ext": os.path.splitext(fp)[1].lower().lstrip("."),
        "size": os.path.getsize(fp) if os.path.exists(fp) else 0,
        "kind": m.get("kind", "unknown"),
        "type": m.get("type", "未知"),
        "amount": float(m.get("amount", 0.0) or 0.0),
        "date": m.get("date", "") or "",
        "seller": m.get("seller", "") or "",
        "buyer": m.get("buyer", "") or "",
        "invoice_no": m.get("invoice_no", "") or "",
        # 各票种专属字段（详情面板用）
        "detail": {
            "from_": m.get("from_", ""), "to_": m.get("to_", ""),
            "train": m.get("train", ""), "train_date": m.get("train_date", ""),
            "car_no": m.get("car_no", ""), "seat_no": m.get("seat_no", ""),
            "seat_class": m.get("seat_class", ""), "passenger": m.get("passenger", ""),
            "hotel_name": m.get("hotel_name", ""), "guest_name": m.get("guest_name", ""),
            "checkin": m.get("checkin", ""), "checkout": m.get("checkout", ""),
            "tax_amount": m.get("tax_amount", 0.0),
            "invoice_code": m.get("invoice_code", ""),
            "drawer": m.get("drawer", ""),
        },
    }


def _default_out_dir() -> str:
    """默认输出目录：桌面下的「发票合并输出」。"""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(desktop):
        desktop = os.path.expanduser("~")
    return os.path.join(desktop, "发票合并输出")


# ─────────────────────────── 接口 ───────────────────────────

@router.get("/default_out")
def default_out():
    return {"ok": True, "dir": _default_out_dir()}


@router.post("/scan")
async def scan(payload: dict):
    """接收路径列表（可含目录），解析元数据后返回表格行。"""
    raw = payload.get("paths") or []
    file_paths = []
    for p in raw:
        if isinstance(p, str) and os.path.isdir(p):
            file_paths.extend(_expand_dir(p))
        else:
            file_paths.append(p)
    paths = _norm_paths(file_paths)

    rows, errors = [], []
    for i, fp in enumerate(paths):
        try:
            rows.append(_meta_row(fp, i))
        except Exception as e:
            errors.append({"filename": os.path.basename(fp), "reason": str(e)})

    total = sum(r["amount"] for r in rows)
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_amount": round(total, 2),
        "errors": errors,
        "default_out": _default_out_dir(),
    }


def _ask_dialog(mode: str, title: str):
    """弹出一个系统对话框，返回用户选择。线程安全前提下在主线程直接调用。

    返回 (kind, value)：kind 为 'files' / 'dir' / 'none'。
    """
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if mode == "dir":
            chosen = filedialog.askdirectory(title=title)
            return ("dir", chosen) if chosen else ("none", None)
        chosen = filedialog.askopenfilenames(
            title=title,
            filetypes=[("发票文件", "*.pdf *.ofd"), ("PDF", "*.pdf"),
                       ("OFD", "*.ofd"), ("全部文件", "*.*")])
        return ("files", list(chosen or []))
    finally:
        root.destroy()


@router.post("/pick")
async def pick(payload: dict):
    """唤起系统文件/目录选择对话框（服务在本机，Tk 对话框可直接弹出）。

    mode: 'files' 选多个文件（返回文件列表）
          'folder' 选一个目录（返回该目录内的发票文件）
    """
    mode = (payload or {}).get("mode", "files")
    try:
        if mode == "folder":
            kind, folder = _ask_dialog("dir", "选择发票所在文件夹")
            paths = _expand_dir(folder) if kind == "dir" else []
        else:
            kind, picked = _ask_dialog("files", "选择发票文件")
            paths = _norm_paths(picked) if kind == "files" else []
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"系统对话框不可用：{e}"}, status_code=500)
    return {"ok": True, "paths": paths}


@router.post("/pickdir")
async def pickdir():
    """只选一个目录（用于「输出目录」），不做发票展开。"""
    try:
        kind, folder = _ask_dialog("dir", "选择输出目录")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"系统对话框不可用：{e}"}, status_code=500)
    return {"ok": True, "dir": folder if kind == "dir" else ""}


@router.post("/upload")
async def upload(files: list[UploadFile] = File(default=[])):
    """接收浏览器上传（拖拽）的文件，留存到当前用户目录后返回本机路径。"""
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except OSError as exc:
        return JSONResponse({"ok": False, "paths": [], "count": 0,
                             "failed": len(files), "error": f"上传目录不可用：{exc}"},
                            status_code=500)
    saved = []
    errors = []
    for f in files:
        name = os.path.basename(f.filename or "upload.pdf")
        if not name.lower().endswith(SUPPORTED_EXTS):
            errors.append({"filename": name, "reason": "仅支持 PDF / OFD 文件"})
            continue
        dst = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{name}")
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(dir=UPLOAD_DIR, prefix=".upload_",
                                             suffix=".part", delete=False) as out:
                temporary = out.name
                shutil.copyfileobj(f.file, out)
            os.rename(temporary, dst)  # Windows 上目标已存在时失败，不覆盖既有文件
            saved.append(dst)
        except Exception as exc:
            errors.append({"filename": name, "reason": f"保存失败：{exc}"})
            if temporary:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
    return {"ok": bool(saved), "paths": saved, "count": len(saved),
            "failed": len(errors), "errors": errors,
            "error": "文件未能保存，请检查磁盘空间和目录权限" if not saved and errors else ""}


@router.post("/run")
async def run(payload: dict):
    """执行发票合并 —— 直接调 core.merge_invoices()。"""
    paths = _norm_paths(payload.get("paths") or [])
    out_dir = (payload.get("out_dir") or "").strip() or _default_out_dir()
    if not paths:
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"输出目录不可用：{e}"}, status_code=400)

    try:
        result, err = core_merge.merge_invoices(paths, out_dir)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"合并失败：{e}"}, status_code=500)

    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    return {
        "ok": True,
        "merged_path": result["merged_path"],
        "filename": os.path.basename(result["merged_path"]),
        "page_count": result["page_count"],
        "merged_files": result["merged_files"],
        "failed_files": result["failed_files"],
        "out_dir": out_dir,
    }


@router.post("/open_folder")
async def open_folder(payload: dict):
    """在资源管理器中打开输出目录。"""
    d = (payload or {}).get("dir", "")
    if d and os.path.isdir(d):
        try:
            os.startfile(d)  # noqa: S606  (Windows)
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": False, "error": "目录不存在"}, status_code=404)
