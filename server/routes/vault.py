# -*- coding: utf-8 -*-
"""routes/vault.py — 「发票库」（历史发票留存）的 HTTP 接口。

业务规则与存储细节全在 `core/vault.py`，本文件只做 HTTP ⇄ core 的翻译。

接口
----
  POST   /api/vault/ingest   传路径 → 复制入库（MD5 去重）
  GET    /api/vault/list     列表（q / kind / mid / from / to / limit / offset）
  DELETE /api/vault/items    删记录 + 连库内副本一起删
  POST   /api/vault/clear    清空（要口令）
  GET    /api/vault/file     库内副本内嵌预览（inline）
  GET    /api/vault/stats    总量
  GET    /api/vault/meta     按路径批量读**已缓存的解析结果**（不重新解析）

⚠️ 磁盘原件全程只读：删除动作只处理库内登记副本，core 有路径边界校验。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

from . import _shared                       # 导入即完成 sys.path 设置
from core import vault as _vault            # noqa: E402

router = APIRouter()

#: 内嵌预览的媒体类型（与 `routes/pool.py` 的 `/api/file` 同源口径）
_MEDIA = {
    ".pdf": "application/pdf",
    ".ofd": "application/octet-stream",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _err(msg: str, code: int):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# ─────────────────────────── ① 入库 ───────────────────────────

@router.post("/ingest")
def ingest(payload: dict):
    """把一批原件复制进库。请求 `{"paths": ["绝对路径", ...]}`。

    逐份算 MD5；已在库里的一律 `skipped`（**绝不覆盖**）。
    解析失败但文件可读**仍然入库**（元数据留空，计入 `added`）；
    文件读不了才计入 `failed` 并给出原因。
    """
    payload = payload or {}
    paths = payload.get("paths")
    if paths is None:
        paths = []
    if not isinstance(paths, (list, tuple)):
        return _err("paths 必须是路径数组", 400)

    try:
        res = _vault.ingest(paths)
    except Exception as exc:                    # noqa: BLE001 —— 读库/建库失败要说清楚
        return _err(f"入库失败：{exc}", 500)
    return {"ok": True, **res}


# ─────────────────────────── ② 列表 ───────────────────────────

@router.get("/list")
def list_items(q: str = "", kind: str = "", mid: str = "",
               from_: str = Query("", alias="from"),
               to: str = Query("", alias="to"),
               limit: int = 200, offset: int = 0):
    """列表。

    ⚠️ `from` / `to` 是 Python 关键字，只能借 `Query(alias=...)` 映射
    （**不能**写成 `**params` —— FastAPI 会把 `**kwargs` 当成一个名叫
    `params` 的必填查询参数，请求直接 422）。
    """
    try:
        res = _vault.list_rows(q=q, kind=kind, mid=mid, date_from=from_,
                               date_to=to, limit=limit, offset=offset)
    except Exception as exc:                    # noqa: BLE001
        return _err(f"查询失败：{exc}", 500)
    return {"ok": True, **res}


# ─────────────────────────── ③ 删除 ───────────────────────────

@router.delete("/items")
def delete_items(payload: dict):
    """删记录 + **连库内副本一起删**（绝不碰原件）。请求 `{"ids": [1,2,3]}`。"""
    payload = payload or {}
    ids = payload.get("ids")
    if not isinstance(ids, (list, tuple)):
        return _err("ids 必须是 id 数组", 400)
    try:
        res = _vault.delete_items(ids)
    except Exception as exc:                    # noqa: BLE001
        return _err(f"删除失败：{exc}", 500)
    return {"ok": True, **res}


# ─────────────────────────── ④ 清空 ───────────────────────────

@router.post("/clear")
def clear(payload: dict):
    """清空发票库。必须逐字传对确认口令，否则 400。"""
    phrase = str((payload or {}).get("confirm") or "")
    if phrase != _vault.CLEAR_PHRASE:
        return _err("确认口令不正确", 400)
    try:
        res = _vault.clear_all()
    except Exception as exc:                    # noqa: BLE001
        return _err(f"清空失败：{exc}", 500)
    return {"ok": True, **res}


# ─────────────────────────── ⑤ 内嵌预览 ───────────────────────────

@router.get("/file")
def file(id: int = 0):
    """把**库内副本**交给浏览器内嵌渲染（不是原件，也不是下载）。

    ⚠️ `inline` 不是风格问题：FileResponse 默认发
    `Content-Disposition: attachment`，浏览器不渲染、直接弹下载。
    本机单人使用，但只允许读库里登记过的那一份副本。
    """
    item = _vault.get_item(id)
    if not item:
        return _err("库内没有这条记录", 404)
    path = item.get("stored_path") or ""
    if not os.path.isfile(path):
        return _err("库内副本文件缺失（可能已被手工删除）", 404)
    ext = os.path.splitext(path)[1].lower()
    return FileResponse(path,
                        media_type=_MEDIA.get(ext, "application/octet-stream"),
                        filename=os.path.basename(path),
                        content_disposition_type="inline")


# ─────────────────────────── ⑥ 统计 ───────────────────────────

@router.get("/stats")
def stats():
    try:
        res = _vault.stats()
    except Exception as exc:                    # noqa: BLE001
        return _err(f"读取统计失败：{exc}", 500)
    return {"ok": True, **res}


# ─────────────────────────── ⑦ 读已缓存的解析结果 ───────────────────────────

@router.get("/meta")
def meta(paths: list[str] = Query(default=[], alias="paths")):
    """按路径批量读**入库时缓存下来的解析结果** —— 用户第④条 B 半段
    「再次从发票库导入不用二次解析」。

    两种传法（都支持，可同时用）：
      * `?paths=<abs1>|<abs2>|...`  竖线分隔（推荐：路径里不会有 `|`）；
      * `?paths=<abs1>&paths=<abs2>` 同一个键重复出现（FastAPI `Query(list)`）。

    ⚠️ 只声明**一个** `paths` 参数，不要同时写 `paths: str | None = None` 和
       `Query(alias="paths")` —— 那样是同一个查询键被绑了两遍，FastAPI 会把它
       同时当成「取最后一个」的标量和「取全部」的列表，`?paths=a&paths=b` 就会
       收到 4 条（实测 500 条会被数成 501 条而误报 400）。列表里再自行拆 `|`。

    路径可以是**库内副本**（`stored_path`）也可以是**当初的原件**（`orig_path`）——
    只参与查找，绝不回传 `orig_path`（`/list` 的 17 键冻结契约里本来就没有它）。

    返回 `{"ok": true, "rows": {"<调用方原样传进来的那个字符串>": {meta}}}`：
      * 键就是调用方传的字符串（不做归一化回传，否则调用方拿自己的路径查不到）；
      * 库里没有的路径**直接不出现**（不是错误 —— 「没缓存」与「出错」必须能区分）；
      * 超过 `_vault.MAX_META_PATHS` 条 → 400。

    ⚠️ 本接口**只读库**，全程不碰磁盘原件、不触发任何解析。
    """
    raw: list[str] = []
    for chunk in (paths or []):
        raw.extend(str(chunk or "").split("|"))
    wanted = [p.strip() for p in raw if p.strip()]
    if len(wanted) > _vault.MAX_META_PATHS:
        return _err(f"paths 最多 {_vault.MAX_META_PATHS} 条（收到 {len(wanted)} 条）", 400)
    try:
        rows = _vault.cached_meta_map(wanted)
    except Exception as exc:                    # noqa: BLE001
        return _err(f"读取解析缓存失败：{exc}", 500)
    # ⚠️ 库内那份解析结果还带了两个 **内部** 键（`__raw__` 票面原文 / `__info__`
    #    字段解析结果）——那是 `pool.scan` 自己消费的「零读盘」载荷，**不许出网**：
    #    票面原文比元数据大一个数量级，而且这个接口对外承诺的就是「8 键元数据」。
    rows = {k: {kk: vv for kk, vv in (v or {}).items() if not kk.startswith("__")}
            for k, v in (rows or {}).items()}
    return {"ok": True, "rows": rows}


@router.post("/journey")
def save_journey(payload: dict):
    """保存手工补充的行程信息；只改数据库解析缓存，不改票据文件。"""
    p = payload or {}
    try:
        row = _vault.update_journey(item_id=p.get("id"), path=p.get("path"),
                                    updates=p.get("updates"))
    except ValueError as exc:
        return _err(str(exc), 400)
    except LookupError as exc:
        return _err(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        return _err(f"保存行程信息失败：{exc}", 500)
    return {"ok": True, "row": row}
