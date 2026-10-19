# -*- coding: utf-8 -*-
"""routes/train.py — 「火车票行程单」模块的 HTTP 接口。

胶水层：把 HTTP 请求翻译成 `core.train` / `core.trainmap_html` 的调用，
本文件不含任何解析或版面逻辑。

接口
----
  POST /api/train/scan        传路径（目录/文件混合）→ 解析车票 → 返回行程行
  POST /api/train/run         生成行程单 HTML（可选：合并原始票为 PDF）
  GET  /api/train/default_out 默认输出目录
  GET  /api/train/build       构建标识（测试脚本启动前校验服务是否为最新代码）
"""
from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared                           # 导入即完成 sys.path 设置
from . import preview                           # noqa: E402  预览目录 / 工具条注入
from core import train as core_train            # noqa: E402
from core.trainmap_html import build_trainmap_html  # noqa: E402

router = APIRouter()

#: 输出子目录名
OUT_NAME = "火车票行程单"


# ── 构建标识：给测试脚本一道「服务跑的是旧代码」护栏 ──────────────────
# 本机曾出过事故：凌晨起的陈旧开发服务跑着旧代码，怎么导都是「0 个非电子发票」，
# 却让人对着错误结果反复排查。测试脚本启动前拿这里的标识与本地代码比对，
# 不一致就报错退出，而不是带着旧代码把测试跑完。
#
# ⚠️ 用 server/ 下所有 .py 的**内容哈希**，不用 git HEAD —— 提交会让 HEAD 前移
#    但代码没变，拿 HEAD 比对会把「代码没动、只是 commit 了」误判成旧服务。
_BUILD_SKIP_DIRS = {"__pycache__"}


def _compute_build_id():
    """server/ 下所有 .py 文件内容的 SHA1（排序后整体再哈希）。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # → server/
    items = []
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in _BUILD_SKIP_DIRS]
        for fn in fns:
            if fn.endswith(".py"):
                fp = os.path.join(dp, fn)
                rel = os.path.relpath(fp, base).replace(os.sep, "/")
                try:
                    with open(fp, "rb") as f:
                        h = hashlib.sha1(f.read()).hexdigest()
                except OSError:
                    continue
                items.append("%s:%s" % (rel, h))
    items.sort()
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()


_BUILD_ID = _compute_build_id()


@router.get("/build")
def build():
    """构建标识：测试脚本启动前拿它与本地代码比对，防止连上陈旧的旧服务。"""
    return {"ok": True, "build": _BUILD_ID, "source": "content-sha1:server/**/*.py"}


def _row(inv: dict, idx: int) -> dict:
    """把 core 的行程记录转成前端表格行的结构。"""
    fp = inv.get("filepath", "") or ""
    return {
        "id": idx,
        "path": fp,
        "filename": _shared.display_name(fp) or (inv.get("filename", "") or ""),
        "ext": os.path.splitext(fp)[1].lower().lstrip("."),
        # 行程核心字段
        "train": inv.get("train", "") or "",
        "from_": inv.get("from_", "") or "",
        "to_": inv.get("to_", "") or "",
        "date": inv.get("train_date", "") or "",
        "amount": float(inv.get("amount", 0.0) or 0.0),
        "passenger": inv.get("passenger", "") or "",
        "seat_class": inv.get("seat_class", "") or "",
        "seat_no": inv.get("seat_no", "") or "",
        "car_no": inv.get("car_no", "") or "",
        "special_type": inv.get("special_type", "") or "",
        # 解析质量标记（前端高亮「待确认」行）
        "needs_review": bool(inv.get("needs_review")),
        "review_reason": inv.get("review_reason", "") or "",
        "kind": "train",
    }


def _norm_key(p):
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _apply_trip_overrides(invoices, overrides):
    """把前端「手动补全行程」按 filepath 套到解析结果上（逐条、可缺省）。

    用户口径：机票解析不到行程时给用户一个入口自己填，**不填也没关系**。

    - 只认 from_/to_/date/no/mode 五个键；
    - 缺 from_ 或 to_ → **静默跳过**（不报错、不留空行、不进轨迹）；
    - mode 只在有值时写成该记录自己的**逐条显式 mode**（resolve_mode 优先级 1）——
      绝不拿它当整页 mode，否则同一页混排火车/航班时另一半会全画错；
    - 返回实际套用的条数。
    """
    if not isinstance(overrides, dict) or not overrides:
        return 0
    norm = {}
    for k, v in overrides.items():
        if isinstance(v, dict):
            nk = _norm_key(k)
            if nk:
                norm[nk] = v
    if not norm:
        return 0

    applied = 0
    for inv in invoices or []:
        if not isinstance(inv, dict):
            continue
        ov = norm.get(_norm_key(inv.get("filepath")))
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


@router.get("/default_out")
def default_out():
    return {"ok": True, "dir": _shared.default_out_dir(OUT_NAME)}


@router.post("/scan")
async def scan(payload: dict):
    """解析火车票 → 返回行程行（用于表格展示）。"""
    paths = _shared.resolve_inputs((payload or {}).get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未找到可解析的发票文件"}, status_code=400)

    dedup = bool((payload or {}).get("dedup", True))
    res, err = core_train.parse_train_tickets(paths, False, dedup)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    rows = [_row(inv, i) for i, inv in enumerate(res["invoices"])]
    return {
        "ok": True,
        "rows": rows,
        "count": len(rows),
        "total_amount": round(res["total_amount"], 2),
        "refund_amount": round(res.get("refund_amount", 0.0), 2),
        "passengers": res.get("passengers", []),
        "skipped": res.get("skipped", 0),
        "review_count": res.get("review_count", 0),
        "failed_files": res.get("failed_files", []),
        "default_out": _shared.default_out_dir(OUT_NAME),
    }


@router.post("/run")
async def run(payload: dict):
    """生成《火车票行程单》HTML；可选把原始票据合并成一个 PDF。"""
    p = payload or {}
    paths = _shared.resolve_inputs(p.get("paths") or [])
    if not paths:
        return JSONResponse({"ok": False, "error": "未选择有效发票文件"}, status_code=400)

    out_dir = (p.get("out_dir") or "").strip() or _shared.default_out_dir(OUT_NAME)
    # 注意：此处**不**预先创建 out_dir —— 行程单现在走浏览器预览，
    # 目录只在用户真的点「保存到本地」或勾选的合并产物时才落盘。

    dedup = bool(p.get("dedup", True))
    res, err = core_train.parse_train_tickets(paths, False, dedup)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)

    # 前端手动补全的行程（按 filepath 逐条套用；缺项静默跳过）
    tripped = _apply_trip_overrides(res["invoices"], p.get("trip_overrides") or {})

    include_map = bool(p.get("include_map", True))
    save_name = f"火车票行程单{'' if include_map else '_轻量'}.html"

    # 行程单生成到「预览目录」：浏览器新标签页直接看，点「保存到本地」才落到桌面。
    # 每次运行都是新的子目录，既不互相覆盖，也不往桌面堆文件。
    preview_dir = preview.new_run_dir("train")
    try:
        artifact = build_trainmap_html(
            res["invoices"], preview_dir, include_map=include_map, filename=save_name)
        kind = "html"
    except Exception as e:
        # 降级：生成 PDF（同样落在预览目录，浏览器也能直接打开看）
        try:
            artifact = core_train.build_train_itinerary_pdf(
                res["invoices"], res["total_amount"], res["refund_amount"],
                res["change_amount"], res["normal_amount"], preview_dir, include_map=False)
        except Exception as e2:
            return JSONResponse(
                {"ok": False, "error": f"行程单生成失败：{e} / 降级 PDF 也失败：{e2}"},
                status_code=500)
        if not artifact:
            return JSONResponse({"ok": False, "error": f"行程单生成失败：{e}"}, status_code=500)
        kind = "pdf"

    if kind == "html":
        preview.decorate(artifact, label="火车票行程单", out_sub=OUT_NAME)

    # 可选：把原始票据合并成一个 PDF（报销时一并提交；这才是真正要落盘的产物）
    merged_path = ""
    if p.get("merge"):
        try:
            os.makedirs(out_dir, exist_ok=True)
            from pypdf import PdfWriter
            mp = os.path.join(out_dir, "火车票行程报销_合并发票.pdf")
            w = PdfWriter()
            for inv in res["invoices"]:
                fp = inv.get("filepath")
                if fp and os.path.exists(fp):
                    w.append(fp)
            if len(w.pages) > 0:
                with open(mp, "wb") as f:
                    w.write(f)
                merged_path = mp
        except Exception:
            pass  # 合并是附加产物，失败不影响行程单

    return {
        "ok": True,
        "preview_url": preview.preview_url(artifact),
        "filename": os.path.basename(artifact),
        "kind": kind,
        "merged_path": merged_path,
        "out_dir": out_dir,
        "count": len(res["invoices"]),
        "total_amount": round(res["total_amount"], 2),
        "skipped": res.get("skipped", 0),
        "review_count": res.get("review_count", 0),
        "trip_applied": tripped,
    }
