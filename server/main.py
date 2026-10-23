# -*- coding: utf-8 -*-
"""server/main.py — 报销助手 FastAPI 入口。

只做三件事：建服务、挂路由、发静态文件，业务逻辑在 server/core/ 和各路由里。
只监听 127.0.0.1，发票数据不出本机。

启动：python server/main.py，或双击项目根目录的「启动.bat」。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

# 路径解析：同时支持「开发态」与「PyInstaller 打包后」
def _app_root() -> str:
    """应用根目录（其下应有 web/ 与 server/）。

    打包后用 PyInstaller 的一次性解包目录 sys._MEIPASS
    （web/ 由 spec 的 datas 放进去，core / routes 是顶层包）；
    开发态就是项目根目录（本文件的上上级）。

    注意：打包后 __file__ 指向解包目录内的脚本路径，上溯两级通常算不对，
    因此必须优先判断 sys.frozen。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_ROOT = _app_root()
_SERVER = os.path.join(_ROOT, "server")
for _p in (_ROOT, _SERVER):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from fastapi import FastAPI, Request
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse, JSONResponse
    from starlette.middleware.trustedhost import TrustedHostMiddleware
except ImportError:  # pragma: no cover
    print("缺少依赖：请先运行  python -m pip install -r requirements.txt")
    raise

from routes import merge as merge_routes      # noqa: E402
from routes import train as train_routes      # noqa: E402
from routes import hotel as hotel_routes      # noqa: E402
from routes import check as check_routes      # noqa: E402
from routes import rename as rename_routes    # noqa: E402
from routes import preview as preview_routes  # noqa: E402
from routes import pool as pool_routes        # noqa: E402  v0.4 统一解析入口（R2）
from routes import pipeline as pipeline_routes  # noqa: E402  v0.4 R3「点一次干完」流水线
from routes import trip as trip_routes        # noqa: E402  v0.4 R4 行程汇总单（交通+住宿一张页面）
from routes import vault as vault_routes      # noqa: E402  发票库（历史发票留存）
from routes import _shared                    # noqa: E402  旧上传原件迁移

# 常量
HOST = "127.0.0.1"          # 只绑定本机回环，绝不 0.0.0.0
#: 源码版和便携版分开 origin，避免共享浏览器设置。端口冲突时不打开其他实例。
#: 测试通过 FAPIAO_PORT 显式指定隔离端口，禁止访问真实实例。
EDITION = "desktop" if getattr(sys, "frozen", False) else "source"
PORT = int(os.environ.get("FAPIAO_PORT", "").strip() or (8766 if EDITION == "desktop" else 8765))
WEB_DIR = os.path.join(_ROOT, "web")
APP_TITLE = "报销助手"
VERSION = "0.4.1"

app = FastAPI(title=APP_TITLE, version=VERSION, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware,
                   allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_origin_only(request: Request, call_next):
    """拒绝其它网页发起的写请求，避免本机服务被跨站页面调用。"""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if origin and origin not in allowed:
            return JSONResponse({"ok": False, "error": "请求来源不受信任"}, status_code=403)
    return await call_next(request)

# 路由
# 五个业务模块各挂一个前缀，互不干扰；每个 routes 只做 HTTP ⇄ core 的翻译。
app.include_router(merge_routes.router,   prefix="/api/merge",   tags=["merge"])
app.include_router(train_routes.router,   prefix="/api/train",   tags=["train"])
app.include_router(hotel_routes.router,   prefix="/api/hotel",   tags=["hotel"])
app.include_router(check_routes.router,   prefix="/api/check",   tags=["check"])
app.include_router(rename_routes.router,  prefix="/api/rename",  tags=["rename"])
app.include_router(preview_routes.router, prefix="/api/preview", tags=["preview"])
# v0.4：发票工作台的统一池（一次解析，各功能共用）
app.include_router(pool_routes.router, prefix="/api/pool", tags=["pool"])
# v0.4 · R3：「点一次，开始干完」流水线（挂在同一个 /api/pool 前缀下：
#   POST /api/pool/run、GET /api/pool/run/status —— 与 scan 共用一个 scan_id 快照）
app.include_router(pipeline_routes.router, prefix="/api/pool", tags=["pipeline"])
# v0.4 · R4：行程汇总单（交通费 + 住宿合并成一张预览页，POST /api/trip/run）
app.include_router(trip_routes.router,    prefix="/api/trip",   tags=["trip"])
app.include_router(vault_routes.router,   prefix="/api/vault",  tags=["vault"])


@app.get("/api/health")
def health():
    """探活：前端启动时确认后端已就绪。"""
    return {"ok": True, "app": APP_TITLE, "version": VERSION, "edition": EDITION}


@app.get("/")
def index():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as page:
        html = (page.read().replace("__ASSET_VERSION__", str(time.time_ns()))
                .replace("__APP_EDITION__", EDITION))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# 静态资源（css / js / assets）
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# 预览目录（行程单 / 核对结果在浏览器里看的临时页）
# StaticFiles 要求目录已存在，所以先建再挂。
preview_routes.ensure_dir()
app.mount("/preview", StaticFiles(directory=preview_routes.PREVIEW_DIR), name="preview")


def _open_browser_when_ready(url: str, server) -> None:
    """确认本次服务已经监听后再打开，端口冲突时避免进入别的实例。"""
    def _open():
        for _ in range(300):
            if server.started:
                webbrowser.open(url)
                return
            if server.should_exit:
                return
            time.sleep(0.1)
    threading.Thread(target=_open, daemon=True).start()


def _browser_disabled() -> bool:
    """是否跳过「自动开浏览器」—— 给测试/无头环境用。

    背景（2026-10-10 踩坑）：自动化测试里若发现 8765 没服务会自己拉起
    ``server/main.py``；而它一启动就弹 GUI 浏览器，在无显示环境（沙箱/CI）里
    会把自己连同父进程一起拖死（表现为命令无输出、被 SIGTERM 杀掉），
    让「测试自起服务」这条链路整个不可用。

    设 ``FAPIAO_NO_BROWSER=1`` 即可只起服务、不开浏览器。
    ⛔ **默认（不设该变量）行为一字不变** —— 用户双击启动时照常自动打开页面。
    """
    return bool(os.environ.get("FAPIAO_NO_BROWSER", "").strip())


def main() -> None:
    import uvicorn

    # 报告永久保留在当前用户的应用数据目录；首次启动复制旧临时目录中的报告。
    preview_routes.ensure_dir()
    preview_routes.migrate_legacy_preview()
    _shared.migrate_legacy_uploads()

    url = f"http://{HOST}:{PORT}/"
    print("=" * 58)
    print(f"  {APP_TITLE}  v{VERSION}")
    print(f"  服务地址：{url}")
    print("  只监听本机回环（127.0.0.1），数据不出本机。")
    print("  关闭此窗口即停止服务。")
    print("=" * 58)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    if not _browser_disabled():
        _open_browser_when_ready(f"{url}?launch={time.time_ns()}", server)
    server.run()


if __name__ == "__main__":
    main()
