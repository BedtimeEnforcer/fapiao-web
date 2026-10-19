# -*- coding: utf-8 -*-
"""routes/preview.py — 生成结果的「浏览器内预览」基础设施（带保存入口）。

为什么需要它
------------
行程单 / 核对结果这类**给人看**的产物，用户不想每次都往桌面堆文件，
而是希望「生成完直接在浏览器新标签页看，需要时再点保存」。

做法
----
1. 每次生成都落进一个**新的报告子目录**（`%LOCALAPPDATA%/发票助手/reports/<模块>_<时间戳>/`），
   既不会互相覆盖，也不污染桌面；
2. 经 `/preview/<子目录>/<文件>` 静态挂载，前端 `window.open` 直接打开；
3. 页面里注入一个浮动工具条：
   * 「保存到本地」→ POST `/api/preview/save` → 复制到桌面输出目录；
   * 「打印 / 存为 PDF」→ 浏览器原生打印（打印时工具条自动隐藏）；
4. 报告持久保存；服务启动时只复制旧临时目录里的历史报告，不自动清理。

本文件只做「文件搬运 + 注入工具条」，不生成任何业务内容 —— 内容由 `core/*_html.py` 产出。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared

router = APIRouter()

def _default_preview_dir() -> str:
    appdata = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(appdata, "发票助手", "reports")


#: 报告放当前用户的持久目录；沙盒通过环境变量使用独立临时目录。
PREVIEW_DIR = os.environ.get("FAPIAO_PREVIEW_ROOT", "").strip() or \
    _default_preview_dir()

#: 静态挂载前缀（与 main.py 保持一致）
URL_PREFIX = "/preview"

#: 最多保留的预览子目录数（按时间倒序，超出即删）
_MAX_KEEP = 12


# ─────────────────────────── 目录管理 ───────────────────────────

def ensure_dir() -> None:
    os.makedirs(PREVIEW_DIR, exist_ok=True)


def migrate_legacy_preview(legacy_dir: str | None = None) -> int:
    """首次启动时复制旧临时预览页，保留原 URL 路径；原目录不动。"""
    if os.environ.get("FAPIAO_PREVIEW_ROOT", "").strip():
        return 0                         # 测试沙盒绝不接触用户的旧报告
    source = legacy_dir or os.path.join(tempfile.gettempdir(), "fapiao_web_preview")
    if not os.path.isdir(source) or os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(PREVIEW_DIR)):
        return 0
    ensure_dir()
    copied = 0
    for name in os.listdir(source):
        src = os.path.join(source, name)
        dst = os.path.join(PREVIEW_DIR, name)
        if not os.path.isdir(src) or os.path.islink(src) or os.path.exists(dst):
            continue
        try:
            shutil.copytree(src, dst)
            copied += 1
        except OSError:
            # 单个旧报告损坏不阻断服务；源文件始终留在原处。
            pass
    return copied


def new_run_dir(module: str) -> str:
    """为一次生成分配新的预览子目录（`<模块>_<时间戳>`）。"""
    ensure_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    d = os.path.join(PREVIEW_DIR, f"{module}_{stamp}")
    n = 1
    while os.path.isdir(d):                       # 同一秒内多次生成也不冲突
        d = os.path.join(PREVIEW_DIR, f"{module}_{stamp}_{n}")
        n += 1
    os.makedirs(d, exist_ok=True)
    return d


def preview_url(path: str) -> str:
    """把预览文件的绝对路径转成浏览器可访问的 URL。"""
    rel = os.path.relpath(path, PREVIEW_DIR).replace(os.sep, "/")
    return f"{URL_PREFIX}/{rel}"


def clear_old(keep: int = _MAX_KEEP) -> int:
    """清理旧预览目录，只保留最近 keep 个。返回删除的目录数。"""
    if not os.path.isdir(PREVIEW_DIR):
        return 0
    dirs = []
    for name in os.listdir(PREVIEW_DIR):
        p = os.path.join(PREVIEW_DIR, name)
        if os.path.isdir(p):
            dirs.append((os.path.getmtime(p), p))
    dirs.sort(reverse=True)
    removed = 0
    for _, p in dirs[keep:]:
        try:
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def clear_all() -> None:
    """清空预览目录（服务启动时调用，保证每次启动都是干净状态）。"""
    if os.path.isdir(PREVIEW_DIR):
        shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
    ensure_dir()


# ─────────────────────────── 工具条注入 ───────────────────────────

# 预览页也带上自己的图标，别让浏览器标签页显示默认的「地球」。
_FAVICON = ('<link rel="icon" type="image/svg+xml" href="/static/assets/favicon.svg">')

_BAR = r"""
<style>
#__wb_bar{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:2147483000;
  display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:999px;
  background:rgba(12,20,36,.93);border:1px solid rgba(255,255,255,.14);
  box-shadow:0 14px 44px rgba(0,0,0,.48);
  font:13px/1.4 system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#E9EFFA;
  max-width:calc(100vw - 40px)}
#__wb_bar .wb-tip{color:#93A4C4;white-space:nowrap}
#__wb_bar button{font:inherit;font-weight:600;border-radius:999px;padding:7px 15px;
  border:1px solid transparent;cursor:pointer;transition:.18s;white-space:nowrap}
#__wb_bar #__wb_save{background:#22D3EE;color:#04121C}
#__wb_bar #__wb_save:hover{filter:brightness(1.08)}
#__wb_bar #__wb_print{background:transparent;color:#E9EFFA;border-color:rgba(255,255,255,.22)}
#__wb_bar #__wb_print:hover{border-color:rgba(255,255,255,.45)}
#__wb_bar button:disabled{opacity:.6;cursor:default}
#__wb_bar .wb-msg{color:#6EE7B7;max-width:min(46vw,460px);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
@media print{#__wb_bar{display:none!important}}
</style>
<div id="__wb_bar">
  <span class="wb-tip">__LABEL__ · 已保存到报告目录</span>
  <button id="__wb_save">保存到本地</button>
  <button id="__wb_print">打印 / 存为 PDF</button>
  <span class="wb-msg" id="__wb_msg"></span>
</div>
<script>
(function () {
  var NAME = "__SAVE_NAME__";
  var SUB = "__SAVE_SUB__";
  var btn = document.getElementById('__wb_save');
  var prt = document.getElementById('__wb_print');
  var msg = document.getElementById('__wb_msg');
  if (prt) prt.onclick = function () { window.print(); };
  if (!btn) return;
  btn.onclick = function () {
    var old = btn.textContent;
    btn.disabled = true; btn.textContent = '保存中…'; msg.textContent = '';
    fetch('/api/preview/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: NAME, out_sub: SUB })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { btn.textContent = '已保存 ✓'; msg.textContent = d.path || ''; }
      else { btn.disabled = false; btn.textContent = old; msg.textContent = (d && d.error) || '保存失败'; }
    }).catch(function (e) {
      btn.disabled = false; btn.textContent = old; msg.textContent = '保存失败：' + e.message;
    });
  };
})();
</script>
"""


def _js_str(s: str) -> str:
    """把字符串安全地塞进 JS 双引号字面量。"""
    return (str(s).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r"))


def _html_str(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def decorate(path: str, *, label: str, out_sub: str) -> str:
    """给生成的 HTML 注入「保存 / 打印」浮动工具条（就地改写），返回同一路径。

    `path` 必须位于 PREVIEW_DIR 下 —— 保存接口要用的相对路径由此推出，调用方不必自己算。
    调用方（routes/train.py 等）在 core 生成完 HTML 之后调一次即可；
    注入失败不影响主流程（页面照样能看，只是少了保存按钮）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return path

    rel = os.path.relpath(path, PREVIEW_DIR).replace(os.sep, "/")
    bar = (_BAR.replace("__SAVE_NAME__", _js_str(rel))
               .replace("__SAVE_SUB__", _js_str(out_sub))
               .replace("__LABEL__", _html_str(label)))
    # 图标：预览页同源，直接引主程序挂载的 /static 资源
    if _FAVICON not in html:
        if "</head>" in html:
            html = html.replace("</head>", _FAVICON + "\n</head>", 1)
        elif "<body" in html:
            html = html.replace("<body", _FAVICON + "\n<body", 1)
    # 必须插在**最后**一个 </body> 前：行程单页里还嵌着一段 `b.document.write('…</body>')`
    # 的导出脚本，用第一个 </body> 会把工具条注进那个 JS 字符串里 —— 既不出现在页面上，
    # 又污染了导出功能（实测踩过）。
    idx = html.rfind("</body>")
    html = (html[:idx] + bar + "\n" + html[idx:]) if idx != -1 else html + bar
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError:
        pass
    return path


# ─────────────────────────── 接口 ───────────────────────────

@router.post("/save")
async def save(payload: dict):
    """把预览文件保存到「桌面 / <模块子目录>」。

    前端只传预览文件的**相对路径**与目标子目录名；本接口负责还原干净文件名
    （去掉预览目录层级、必要时加序号避免覆盖）后复制。
    """
    p = payload or {}
    raw = (p.get("name") or "").strip().replace("\\", "/")
    if not raw:
        return JSONResponse({"ok": False, "error": "缺少预览文件名"}, status_code=400)

    # 防目录穿越：规范化后必须仍落在 PREVIEW_DIR 内
    src = os.path.normpath(os.path.join(PREVIEW_DIR, raw))
    if not src.startswith(os.path.normpath(PREVIEW_DIR) + os.sep) or not os.path.isfile(src):
        return JSONResponse({"ok": False, "error": "预览文件已失效，请重新生成"},
                            status_code=404)

    out_sub = (p.get("out_sub") or "发票处理输出").strip()
    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(out_sub)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"输出目录不可用：{e}"}, status_code=400)

    dst = _unique_dest(os.path.join(out_dir, os.path.basename(src)))
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"保存失败：{e}"}, status_code=500)

    return {"ok": True, "path": dst, "filename": os.path.basename(dst), "out_dir": out_dir}


def _unique_dest(path: str) -> str:
    """目标已存在时自动加 (1)(2)…，绝不静默覆盖用户既有文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}({i}){ext}"):
        i += 1
    return f"{stem}({i}){ext}"
