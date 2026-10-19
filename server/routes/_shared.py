# -*- coding: utf-8 -*-
"""routes/_shared.py — 各模块路由共用的小工具。

职责边界（与 `merge.py` 完全一致）：
  * 本层只做「路径规整 / 目录展开 / 弹系统对话框 / 默认输出目录」；
  * 任何解析、合并、生成逻辑一律调用 `core/`，此处不复现任何算法。

之所以独立成模块，是让 `train` / `hotel` / `check` / `rename` 四个路由共用，
避免同一段路径处理代码被复制四份（改一处要改四处，必然会漏）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

# ── 让 `core` 可被导入（与 merge.py 同一套规则，兼容 PyInstaller 解包目录）──
if getattr(sys, "frozen", False):
    _SERVER = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
else:
    _SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from core import parse as core_parse  # noqa: E402

#: 支持的发票扩展名 —— 与 core 同源，避免两处各写一份。
SUPPORTED_EXTS = tuple(core_parse.DEFAULT_EXTS)

def _default_upload_dir() -> str:
    appdata = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(appdata, "发票助手", "uploads")


#: 浏览器拖拽上传的原件永久留存；测试可用 FAPIAO_UPLOAD_ROOT 隔离。
UPLOAD_DIR = os.environ.get("FAPIAO_UPLOAD_ROOT", "").strip() or _default_upload_dir()


def migrate_legacy_uploads(legacy_dir: str | None = None) -> int:
    """复制旧临时上传原件到持久目录，不删源文件或覆盖已迁移文件。"""
    if os.environ.get("FAPIAO_UPLOAD_ROOT", "").strip():
        return 0
    source = legacy_dir or os.path.join(tempfile.gettempdir(), "fapiao_web_uploads")
    if not os.path.isdir(source) or os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(UPLOAD_DIR)):
        return 0
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    copied = 0
    for name in os.listdir(source):
        src = os.path.join(source, name)
        dst = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(src) or os.path.islink(src) or not name.lower().endswith(SUPPORTED_EXTS) or os.path.exists(dst):
            continue
        temporary = ""
        try:
            with tempfile.NamedTemporaryFile(dir=UPLOAD_DIR, prefix=".migrating_", delete=False) as f:
                temporary = f.name
            shutil.copy2(src, temporary)
            os.replace(temporary, dst)
            copied += 1
        except OSError:
            if temporary and os.path.isfile(temporary):
                os.remove(temporary)
            # 保留旧原件；下次启动可以继续迁移
    return copied

_UPLOAD_PREFIX_RE = re.compile(r"^(?:[0-9a-f]{8}|[0-9a-f]{32})_")


def display_name(fp: str) -> str:
    """取「给人看」的文件名。

    拖拽上传的发票会被存成 `<8或32位hex>_原名.pdf`（防重名），把哈希串直接显示给用户
    毫无意义。这里剥掉该前缀 —— 但**只对上传目录内的文件生效**：
    真实目录里恰好叫 `deadbeef_xx.pdf` 的文件不会被误伤。
    """
    base = os.path.basename(fp or "")
    if not base:
        return ""
    try:
        if os.path.dirname(os.path.abspath(fp)) == os.path.abspath(UPLOAD_DIR):
            return _UPLOAD_PREFIX_RE.sub("", base) or base
    except (OSError, ValueError):
        pass
    return base


def norm_paths(paths) -> list:
    """把前端传来的路径规整为「确实存在的发票文件」列表：保序、去重、过滤非法项。"""
    out, seen = [], set()
    for p in paths or []:
        if not isinstance(p, str):
            continue
        p = p.strip().strip('"')
        if not p or p in seen:
            continue
        if os.path.isfile(p) and p.lower().endswith(SUPPORTED_EXTS):
            seen.add(p)
            out.append(p)
    return out


def expand_dir(folder: str) -> list:
    """把一个目录展开成其中的发票文件列表（按文件名排序）。"""
    if not folder or not os.path.isdir(folder):
        return []
    return [fp for fp, _ in core_parse.collect_files(folder, True, SUPPORTED_EXTS)]


def resolve_inputs(raw) -> list:
    """把「目录 + 文件」混合输入展开成扁平文件列表。

    前端允许用户既拖整个文件夹又拖单个文件，这里统一摊平为 core 可直接消费的
    文件路径列表（顺带做存在性与扩展名校验）。
    """
    flat = []
    for p in raw or []:
        if isinstance(p, str) and os.path.isdir(p):
            flat.extend(expand_dir(p))
        else:
            flat.append(p)
    return norm_paths(flat)


# ── 真实桌面（Windows 已知文件夹）────────────────────────────────────────
# 背景（用户 2026-10-11 第⑩条）：`expanduser("~")/Desktop` 在**桌面被 OneDrive
# 重定向**的机器上会指向一个还在、但用户早就不用的旧目录（真桌面已经搬去
# `...\OneDrive\桌面`）。所以先问系统「桌面到底在哪」，问不到才退回拼路径。
_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def _windows_desktop() -> str:
    """当前用户真实桌面路径；拿不到返回 `''`。**绝不抛异常。**"""
    # ① SHGetKnownFolderPath —— 唯一权威来源，OneDrive 重定向也返回真路径
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        guid = _GUID()
        ole32 = ctypes.windll.ole32
        if ole32.CLSIDFromString(ctypes.c_wchar_p(_FOLDERID_DESKTOP),
                                 ctypes.byref(guid)) == 0:
            buf = ctypes.c_wchar_p()
            # 0 = KF_FLAG_DEFAULT
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                path = buf.value or ""
                try:
                    ole32.CoTaskMemFree(buf)
                except Exception:
                    pass
                if path and os.path.isdir(path):
                    return path
    except Exception:
        pass

    # ② 注册表 Shell Folders —— 已知文件夹 API 不可用时的等价备份
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        try:
            path, _kind = winreg.QueryValueEx(key, "Desktop")
        finally:
            winreg.CloseKey(key)
        path = os.path.expandvars(str(path or ""))
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    return ""


def default_out_dir(sub: str = "") -> str:
    """只计算默认输出路径；实际保存文件时才创建目录。

    解析顺序（用户 2026-10-11 第⑩条）：
      ① Windows 已知文件夹（`SHGetKnownFolderPath`，退回注册表 `Shell Folders`）——
         桌面被 OneDrive 重定向时只有它能给出真路径；
      ② `~/Desktop`；③ `~`。
    此函数也用于扫描响应和默认路径查询，不能产生文件系统副作用。
    """
    sandbox_out = os.environ.get("FAPIAO_DEFAULT_OUT_ROOT", "").strip()
    if sandbox_out:
        return os.path.join(sandbox_out, sub or "发票处理输出")

    desktop = _windows_desktop()
    if not desktop:
        home = os.path.expanduser("~")
        cand = os.path.join(home, "Desktop")
        desktop = cand if os.path.isdir(cand) else home
    return os.path.join(desktop, sub or "发票处理输出")


def ask_dialog(mode: str, title: str, *, filetypes=None):
    """弹出一个系统对话框（服务在本机运行，Tk 窗口可直接弹出）。

    mode: 'files' 选多个文件 | 'dir' 选目录
    返回 (kind, value)：kind ∈ {'files', 'dir', 'none'}。
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
            filetypes=filetypes or [
                ("发票文件", "*.pdf *.ofd"), ("PDF", "*.pdf"),
                ("OFD", "*.ofd"), ("全部文件", "*.*"),
            ])
        return ("files", list(chosen or []))
    finally:
        root.destroy()
