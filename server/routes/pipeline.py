# -*- coding: utf-8 -*-
"""routes/pipeline.py — R3「点一次，开始干完」流水线后端（v0.4.0）。

一次 `POST /api/pool/run` 把「核对 → 查重 → 重命名 → 合并 → 收尾」串成六步，
中间不再要人点任何按钮。

设计要点（与契约一一对应，改动前请先读这一节）
----------------------------------------------
1. **数据源唯一**：一切来自 `POST /api/pool/scan` 的进程内快照 `_SCAN_CACHE`。
   本文件**不重新扫描、不重新读盘解析** —— 票面原文随快照一起存
   （`pool._pool_row` 的 `_raw` 字段），重命名要用的 `info` 从原文算出。
   理由见 pool.py `_scan_cache_put` 上方注释：解析次数与「下标漂移」都是事故。
2. **同步 `def`**：整条链路是 pdfplumber / pypdf 的同步 CPU+IO 循环。
   写成 `async def` 会把事件循环堵死，`/api/pool/run/status` 轮询与
   `/api/health` 全程超时。FastAPI 对同步 def 自动丢线程池，现有 7 个 run
   都是这个写法 —— 沿用。
3. **白名单**：`selected_ids` 是**服务端行 id 的下标集合**，每个都要在
   `scan_id → paths_set` 里出现才放行。否则这个接口就是「任意路径改名/合并器」。
4. **查重键**（`_dedup_key`）：主键 `(归一化发票号, 金额)`，金额缺失退回
   `(发票号,)`；⛔ 刻意**不用** `pool._copy_key` 的 stem 回退 —— 重命名会改掉
   stem，会让同一张票的 pdf/ofd 被判成两张、合并两遍。⛔ 也刻意给 NO 键带上
   金额，与 `pool._copy_key` 相反：「同票号不同金额的重开票」在这里不能被误并。
   **无票号**的行才退回 `parse.file_md5`（内容级）——「有票号」的组内只留一份
   代表件、不比 md5（pdf/ofd 内容天然不同，比了就两份都进合并件）。
5. **重命名一律 copy**：磁盘原件绝对不动（硬约束 3）。
   `new_name` 一律服务端用 `rename.generate_invoice_filename` 算，不接受前端传名。
6. **失败不静默**：任何一步出问题都进 `warnings` 或 `skipped`，
   机制性失败（无文字层、目标已存在）带 `need_human: true`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import traceback
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import _shared
from . import pool as pool_routes

from core.rename import generate_invoice_filename  # noqa: E402
from core.check_report import build_check_report_html, report_title  # noqa: E402

router = APIRouter()

# ─────────────────────────── 常量 ───────────────────────────

#: 运行结果缓存（幂等键 → 结果）保留时长，与 scan 快照同量级。
_RUN_TTL = 30 * 60
#: 运行结果缓存上限（LRU 淘汰最旧）。
_RUN_MAX = 20
#: 进度字典上限：只留最近这些跑完的 run，防止长时间运行后内存无限涨。
_PROGRESS_MAX = 20

#: 重命名冲突时的固定理由（前端按字面量识别，别改）。
REASON_EXISTS = "目标已存在"

#: 六步时序的名称（进度轮询/stages 计数共用）。⚠️ 第 ⑦ 步「入库」不在 STAGES 里，它不报进度、失败只记 warning（见 :939）。
STAGES = ("preflight", "dedup", "check", "rename", "merge", "finalize")

#: `.pdf` 优先于 `.ofd` —— 与 `pool._COPY_RANK` 同源（代表件选取口径必须一致）。
_RANK = pool_routes._COPY_RANK


# ─────────────────────────── 进度（内存 + Lock）───────────────────────────

_PROGRESS = {}
_PROGRESS_LOCK = threading.Lock()


class _Progress:
    """一次 run 的内存进度。所有写入都在 Lock 里 —— 轮询线程会并发读。

    ⚠️ `data` 这个 dict 就是 `_PROGRESS[run_id]` 里**同一个对象**（见 `_register`），
       `put` 是原地 update，不是换对象。否则轮询 `/run/status` 永远读到初始值。
    """

    def __init__(self) -> None:
        self.data = {
            "stage": STAGES[0], "index": 0, "total": 0,
            "current": "", "done": False, "error": "",
        }

    def register(self, run_id: str) -> None:
        """把自己挂进全局进度表（对象共享，供 /run/status 轮询）。"""
        with _PROGRESS_LOCK:
            _PROGRESS[run_id] = self.data

    def put(self, **kw) -> None:
        with _PROGRESS_LOCK:
            self.data.update(kw)

    def snapshot(self) -> dict:
        with _PROGRESS_LOCK:
            return dict(self.data)


def _progress_sweep() -> None:
    """只保留最近 `_PROGRESS_MAX` 条**已完成**的进度记录。"""
    with _PROGRESS_LOCK:
        done = [k for k, v in _PROGRESS.items() if v.get("done")]
        if len(_PROGRESS) <= _PROGRESS_MAX:
            return
        for k in done[:-_PROGRESS_MAX]:
            _PROGRESS.pop(k, None)


# ─────────────────────────── 幂等 / 结果缓存 ───────────────────────────

_RUNS = {}
_RUNS_LOCK = threading.Lock()


def _idem_key(scan_id, selected_ids, actions, out_dir, rename_mode,
              expected_name, expected_tax_id, failure_policy, dry_run) -> str:
    """幂等键：同一批输入重复点「开始」不重复写盘。

    刻意把 `dry_run` 也算进去 —— 预演与真跑是两种结果，不能互相命中。
    """
    raw = "|".join([
        str(scan_id), ",".join(str(i) for i in sorted(selected_ids)),
        json.dumps(actions, sort_keys=True, ensure_ascii=False),
        str(out_dir), str(rename_mode), str(expected_name), str(expected_tax_id),
        str(failure_policy), "1" if dry_run else "0",
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _runs_get(key: str):
    with _RUNS_LOCK:
        item = _RUNS.get(key)
        if not item:
            return None
        if item["state"] == "done" and time.time() - item["created_at"] > _RUN_TTL:
            _RUNS.pop(key, None)
            return None
        return item


def _runs_claim(key: str, run_id: str, progress: _Progress):
    """原子地占用唯一运行槽位，避免两个请求同时通过幂等检查。"""
    with _RUNS_LOCK:
        now = time.time()
        for old_key, item in list(_RUNS.items()):
            if item["state"] == "done" and now - item["created_at"] > _RUN_TTL:
                _RUNS.pop(old_key, None)
        hit = _RUNS.get(key)
        if hit:
            return hit["state"], hit
        active = next((item for item in _RUNS.values()
                       if item["state"] == "running"), None)
        if active:
            return "busy", active
        _RUNS[key] = {"created_at": now, "state": "running", "run_id": run_id,
                      "progress": progress, "payload": None}
        return "claimed", _RUNS[key]


def _runs_put(key: str, state: str, run_id, progress, payload) -> None:
    """写入/更新幂等槽位（running 与 done 共用一格，前端 409 时能拿到进度）。"""
    with _RUNS_LOCK:
        now = time.time()
        for k in [k for k, v in _RUNS.items()
                  if v.get("state") == "done" and now - v["created_at"] > _RUN_TTL]:
            _RUNS.pop(k, None)
        _RUNS[key] = {"created_at": now, "state": state, "run_id": run_id,
                      "progress": progress, "payload": payload}
        done_keys = [k for k, v in _RUNS.items() if v.get("state") == "done"]
        while len(done_keys) > _RUN_MAX:
            _RUNS.pop(done_keys.pop(0), None)


# ─────────────────────────── 小工具 ───────────────────────────

def _now_ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _norm_no(v) -> str:
    """发票号码归一化：去空白 + 大写（票面/文件名里的号码大小写不稳定）。"""
    return re.sub(r"\s+", "", str(v or "")).upper()


def _row_amount(row) -> float:
    try:
        return round(float(row.get("amount") or 0.0), 2)
    except (TypeError, ValueError):
        return 0.0


def _abspath_norm(p: str) -> str:
    """比较路径用（去大小写/分隔符差异）。**只用于比较，不用于落盘**。"""
    return os.path.normcase(os.path.abspath(str(p or "")))


def _dedup_key(row):
    """本流水线的查重主键：``("NO", 归一化发票号)``；无号码时返回 None（交给内容指纹）。

    **为什么不带金额**（2026-10-11 主理人裁定，**推翻契约第 5 条**）：
    发票号码在票据体系里**全局唯一**，同号 ⇒ 同一张票，不可能是两张。带金额会制造两个方向的错：
      ① 同一张票的 pdf / ofd / xml 各格式解析出的金额偶有出入 —— `pool._copy_key` 的注释
         （`pool.py:310-331`）记录了实测反例「一组 ofd+pdf+xml 三副本、号码逐字相同但金额解析不一致」，
         带金额恰恰会放过最该废弃的那一份，**同一张票被算两遍**；
      ② 真正的「红冲重开」会拿到**新的**发票号，根本不需要靠金额区分。
    ⇒ 与 `pool._copy_key` 的 NO 分支口径**刻意保持一致**（这一点是刻意对齐，不是「顺手」）。
    金额不一致时**不拆分**，只记一条 warning 让用户看得见（见 `_stage_dedup`）。

    ⚠️ 与 `pool._copy_key` 仍有两处刻意差异，别「顺手对齐」：
      * 这里**没有 stem 回退** → 避免重命名改了 stem 之后同一张票被判成两张；
      * 这里**不把发票号当成唯一身份**（见 `_stage_dedup` 的两类分组口径）。
    """
    no = _norm_no(row.get("invoice_no"))
    if not no:
        return None                                   # 没票号 → 只能靠 file_md5 证明是同一份
    return ("NO", no)


def _selected_rows(scan: dict, selected_ids, warnings: list, issues: list):
    """按 selected_ids 从快照取行，返回 (rows, skipped, error)。

    ⛔ 白名单是**安全边界**，不是「尽力而为」：
       只有出现在 `paths_set` 里的路径才允许进入后续改名/合并。
       任何被拒绝的选中项都进 `issues`（→ 整次 run 判失败），**不静默跳过** ——
       「用户点了 5 行、系统只处理了 3 行、其余没吭声」是这条流水线最危险的状态。
       （契约里的 failure_policy='skip' 管的是**处理失败**要不要中断，
        不是「伪造/越界的选择要不要吭声」。）
    """
    rows = scan["rows"]
    paths_set = scan["paths_set"]
    picked, skipped = [], []

    if not isinstance(selected_ids, (list, tuple)) or not selected_ids:
        return [], [], "selected_ids 为空"

    for raw_id in selected_ids:
        try:
            idx = int(raw_id)
        except (TypeError, ValueError):
            reason = f"非法的行 id：{raw_id!r}"
            issues.append({"step": "preflight", "reason": reason})
            skipped.append({"id": raw_id, "filename": "", "step": "preflight",
                            "reason": reason, "need_human": True})
            continue
        if idx < 0 or idx >= len(rows):
            reason = f"行 id {idx} 超出本次扫描范围（0..{len(rows) - 1}）"
            issues.append({"step": "preflight", "reason": reason})
            skipped.append({"id": idx, "filename": "", "step": "preflight",
                            "reason": reason, "need_human": True})
            continue
        row = rows[idx]
        path = str(row.get("path") or "")
        if path not in paths_set:
            reason = f"路径不在本次扫描快照内，已拒绝：{path}"
            issues.append({"step": "preflight", "reason": reason})
            skipped.append({"id": idx, "filename": pool_routes._row_path(row),
                            "step": "preflight", "reason": reason,
                            "need_human": True})
            continue
        picked.append(row)

    if not picked:
        return [], skipped, "没有任何有效的选中行"
    return picked, skipped, ""


def _row_info(row) -> dict:
    """该行的解析结果 —— **扫描时已算好，不再第二次调用 extract_invoice_fields**。

    `pool._pool_row` 每张票只解析一次（pool.py:169），结果挂在 `row["_info"]`；
    这里直接取。只有拿到老格式行（`_info` 缺失，例如手工造的行或历史快照）
    才退回票面原文重算一次，保证不会算出空文件名 / 空核对结论。
    """
    info = row.get("_info")
    if not isinstance(info, dict) or "amount" not in info:
        info = pool_routes.extract_invoice_fields(row.get("_raw") or "")
        row["_info"] = info
    return info


# ─────────────────────────── ② 查重（只读）───────────────────────────

def _stage_dedup(rows: list, warnings: list):
    """只读归并：返回 (representatives, dups_skipped, skipped)。

    分组口径（两类，刻意的，别合并成一套）：
      * **有票号** → 键 `(归一化票号)`，**不带金额**（发票号全局唯一，见 `_dedup_key` 的裁定说明），
        **组内只留一份代表件**。这一步**不比对 md5**：同一张票的 pdf / ofd 是两种不同格式，
        内容天然不同（md5 必不相等），拿 md5 当归并条件会让它们**两份都进合并件**。
        组内金额解析不一致时**不拆分**，只追加一条 warning 供人工核对。
      * **没票号** → 没有身份可依，只能靠 `file_md5`（内容级，不受改名影响）：
        内容完全相同才算同一份，不同内容一律各自独立。
        这样既不会把「同金额但不同票」误并，也能收拾无号码的重复副本。
      * 代表件选取：`.pdf` 优先于 `.ofd`（沿用 `pool._COPY_RANK`），同级取路径更短者
        （稳定可复现，不会今天留 a.pdf、明天留 b.pdf）。

    即使 `actions.dedup=false` 这一步也照跑：它决定第 ⑤ 步合并喂哪些文件。
    """
    groups = {}                                       # 键 → [行]；有票号与无票号用不同键型
    dups_skipped = 0
    skipped = []
    representatives = []
    for r in rows:
        if r.get("discarded"):
            continue                                  # 非发票不参与归并
        key = _dedup_key(r)
        if key is None:                               # 没票号 → 用内容指纹当键
            path = pool_routes._row_path(r)
            try:
                key = ("MD5", pool_routes.file_md5(path))
            except OSError as e:
                # 读不出内容 → 不能证明它重复，保守留下，但要说出来
                # ⚠️ 同时进 skipped + need_human：这正是「机器判不了、必须人看」的情形
                #    （扫描件/损坏文件，无 OCR）。只写 warning 会淹没在字符串列表里，
                #    前端渲染不出来。
                warnings.append(f"查重：{os.path.basename(path)} 内容指纹读取失败（{e}），已按独立文件处理")
                skipped.append({
                    "id": r.get("id"), "filename": path, "step": "dedup",
                    "reason": f"内容指纹读取失败（{e}），无法判定是否重复",
                    "need_human": True,
                })
                key = ("SOLO", _abspath_norm(path))   # 各自独立，不与任何行同组
        groups.setdefault(key, []).append(r)

    for key, grp in groups.items():
        grp = sorted(grp, key=lambda r: (_RANK.get(pool_routes._row_ext(r), 9),
                                         len(pool_routes._row_path(r)),
                                         pool_routes._row_path(r)))
        rep = grp[0]
        rep["run_status"] = "representative"
        representatives.append(rep)
        if len(grp) == 1:
            continue
        rep_name = os.path.basename(pool_routes._row_path(rep))
        rep_kind = pool_routes._row_ext(rep).lstrip(".") or "文件"
        if key[0] == "NO":
            detail = f"票号 {key[1]}"
            amts = {_row_amount(r) for r in grp}
            if len(amts) > 1:                        # 同号不同金额：仍归并，但必须让用户看得见
                warnings.append(
                    f"查重：票号 {key[1]} 的 {len(grp)} 份文件解析出的金额不一致（"
                    + "、".join(f"{a:.2f}" for a in sorted(amts))
                    + f"），已按同一张票归并（发票号全局唯一）；保留 {rep_name}，如与实际不符请人工核对"
                )
        else:
            detail = "内容指纹（MD5）相同"
        for r in grp[1:]:
            dups_skipped += 1
            r["run_status"] = "dup_skipped"
            skipped.append({
                "id": r.get("id"), "filename": pool_routes._row_path(r),
                "step": "dedup",
                "reason": f"与 {rep_name} 是同一份（{detail}），只保留 {rep_kind} 那份",
                "need_human": False,
            })

    return representatives, dups_skipped, skipped


# ─────────────────────────── ③ 核对（逐张，复用解析结果）───────────────────────────

def _check_one(row: dict, expected_name: str, expected_tax_id: str):
    """单张核对：把结果写回行，返回 (status, 报告用的结果字典)。

    只做比对，字段一律来自扫描时已解析好的 `_info`
    （`pool._check_status` 的 `info` 参数就是为此加的，避免二次解析）。
    真源是 `core.check.check_invoices` 的判定口径 —— 这里与 `pool._check_status`
    共用同一套三态（pass / fail / review），不另立一套。
    """
    path = pool_routes._row_path(row)
    info = _row_info(row)
    status = pool_routes._check_status(row.get("_raw") or "", expected_name,
                                       expected_tax_id, info)
    row["check"] = status
    st = status.get("status")
    row["run_status"] = "check_" + str(st)
    result = {
        "filename": _shared.display_name(path),
        "buyer_name": info.get("buyer_name", ""),
        "tax_id": info.get("tax_id", ""),
        "amount": info.get("amount", 0.0) or 0.0,
        "seller_name": info.get("seller_name", ""),
        "invoice_date": info.get("invoice_date", ""),
        "invoice_no": info.get("invoice_no", ""),
        "tax_rate": info.get("tax_rate", ""),
        "name_ok": bool(status.get("name_ok")),
        "tax_ok": bool(status.get("tax_ok")),
        "passed": bool(status.get("passed")),
        "needs_review": st == "review",
        "review_reason": status.get("reason", "") if st == "review" else "",
    }
    return st, result


# ────────────────────── ④ 重命名（copy / inplace）───────────────────────

def _stage_rename(rows: list, out_dir: str, warnings: list, mode: str = "copy"):
    """逐张改名落盘。返回 (counter, skipped)。

    * `new_name` 全部服务端算（`replace.generate_invoice_filename`），不接受前端命名；
    * `mode` 两种（用户 2026-10-11 第⑪条）：
        - `copy`    （默认）复制到 `out_dir`，**原件一动不动**；
        - `inplace` 「**修改原件**」—— 改的是原文件名，文件还在原目录、内容一字不动，
          行上的 `path` / `filename` 同步更新（改完的文件就是原件本身）。
      落盘动作统一走 `routes/rename.apply_rename`（inplace 实现只有那一处，不另写一套）。
    * 目标已存在 → `skipped`（`reason="目标已存在"`），**绝不**走 `_unique_path`
      加 `(1)(2)` 后缀，也绝不覆盖已有文件。

    ⚠️「目标已存在」的判定就是**磁盘上真的有这个文件**，不做「是不是上一轮产物」的
    特判 —— 契约写死的口径。同一批里两张票算出同名时，先处理的写入、后一张正好
    看到「已存在」被跳过，两种冲突走同一条路径，不需要额外的占用表。
    """
    from .rename import apply_rename                  # 唯一的落盘实现（copy + inplace）

    inplace = (mode or "copy").lower() == "inplace"
    counter = {"renamed": 0, "skipped": 0, "failed": 0}
    skipped = []

    for r in rows:
        path = pool_routes._row_path(r)
        try:
            new_name = generate_invoice_filename(
                r.get("_raw") or "", _row_info(r), path)
        except Exception as e:                        # 单张算名失败不拖垮整批
            r["run_status"] = "rename_failed"
            counter["failed"] += 1
            skipped.append({"id": r.get("id"), "filename": path, "step": "rename",
                            "reason": f"生成新文件名失败：{e}", "need_human": True})
            continue

        # inplace：目标就在原件旁边；copy：落在 out_dir
        base_dir = os.path.dirname(path) if inplace else out_dir
        dst = os.path.join(base_dir, str(new_name or os.path.basename(path)))
        r["original_path"] = path
        r["new_path"] = dst

        if _abspath_norm(dst) == _abspath_norm(path):
            # 本来就叫这个名字：不是失败，也不是重复劳动 —— 记「已是最新」
            r["run_status"] = "rename_same"
            counter["skipped"] += 1
            skipped.append({"id": r.get("id"), "filename": path, "step": "rename",
                            "reason": "文件名已是规范名，无需复制" if not inplace
                                      else "文件名已是规范名，无需修改",
                            "need_human": False})
            continue

        if os.path.exists(dst):
            # 契约：目标已存在（上一轮产物，或本批另一张票算出同名）→ 跳过 + 让人看见。
            # ⛔ 不加 (1)(2) 后缀、不覆盖 —— 覆盖会让用户丢掉上一轮的结果，
            #    加后缀会让他打开目录看到两份不知道哪份算数。
            r["run_status"] = "rename_skipped"
            counter["skipped"] += 1
            skipped.append({"id": r.get("id"), "filename": path, "step": "rename",
                            "reason": REASON_EXISTS, "need_human": True})
            continue

        try:
            if inplace:
                # ⚠️ 磁盘原件的写操作：**只改文件名**。apply_rename 只做 os.rename，
                #    不删文件、不动内容；目标不存在时 _unique_path 原样返回 → 不加后缀。
                res = apply_rename(path, os.path.basename(dst), mode="inplace")
                if not res.get("ok"):
                    raise OSError(res.get("reason") or "改名失败")
                final_path = res.get("new_path") or path
            else:
                _copy_file(path, dst)
                final_path = dst
        except OSError as e:
            r["run_status"] = "rename_failed"
            counter["failed"] += 1
            skipped.append({"id": r.get("id"), "filename": path, "step": "rename",
                            "reason": f"{'改名' if inplace else '复制'}失败：{e}",
                            "need_human": True})
            continue

        if inplace:
            # 行上 path / filename 必须跟着走：后续合并、预览、以及返回给前端的
            # rows 都直接读它们，不同步就会指向一个已经不存在的旧路径。
            r["path"] = final_path
            r["filename"] = _shared.display_name(final_path)
            r["new_path"] = final_path

        r["run_status"] = "renamed"
        counter["renamed"] += 1

    return counter, skipped


def _copy_file(src: str, dst: str) -> None:
    """原样复制（含二进制内容），保留修改时间。

    ⛔ 用 `shutil.copy2` 而不是 `copy`：发票原件的时间戳对用户有用
    （按日期找文件），而且 copy2 一样不动源文件。
    """
    import shutil
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


# ─────────────────────────── ⑤ 合并（只喂代表件）───────────────────────────

def _stage_merge(rows: list, out_dir: str, source_norm: set, warnings: list):
    """把代表件里的 PDF 合并成 `<out_dir>/合并发票.pdf`。

    * 只喂 `.pdf`：pypdf 读不了 .ofd（那会整批 fail 并留一堆噪音）；
    * 单个文件读失败 → 进 `failed` 列表（不静默吞）；
    * 目标已存在 → **覆盖**（这是本流水线自己的产物名，重跑必须拿到新的合并件；
      否则用户看到的是上一轮的旧文件却以为这次成功了 —— 契约只为**重命名**规定了
      「已存在就跳过」，合并件没有这条）；
    * ⛔ 唯一的例外：`合并发票.pdf` 正好是本次运行读进来的**原件**之一
      （用户把输出目录指到了发票所在目录，里面真有一份叫「合并发票.pdf」的票）
      → 拒绝，绝不覆盖原件（硬约束 3）。
    * 先写 `<目标>.incomplete` 再 `os.replace`：中途失败不会留下半份坏 PDF。
    """
    from core import pdf_merge

    stage = {"merged_path": None, "page_count": 0, "failed": []}
    skipped = []
    pdfs = [r for r in rows
            if pool_routes._row_ext(r) == ".pdf" and not r.get("discarded")
            and r.get("run_status") != "dup_skipped"]

    if not pdfs:
        warnings.append("合并：本批没有可合并的 PDF（仅有 OFD 或全部被跳过），已跳过")
        return stage, skipped

    merged_path = os.path.join(out_dir, "合并发票.pdf")
    if _abspath_norm(merged_path) in source_norm:
        warnings.append(f"合并：{merged_path} 是本次读入的原件，拒绝覆盖（请换输出目录）")
        skipped.append({"id": None, "filename": merged_path, "step": "merge",
                        "reason": "目标路径与原件同名，已拒绝覆盖", "need_human": True})
        return stage, skipped

    replacing = os.path.exists(merged_path)
    tmp_path = merged_path + ".incomplete"
    try:
        os.makedirs(out_dir, exist_ok=True)
        result, err = pdf_merge.merge_to([pool_routes._row_path(r) for r in pdfs],
                                         tmp_path)
    except Exception as e:
        _safe_unlink(tmp_path)
        warnings.append(f"合并失败：{e}｜{traceback.format_exc(limit=2).splitlines()[-1]}")
        return stage, skipped

    if err:
        _safe_unlink(tmp_path)
        warnings.append(f"合并失败：{err}")
        return stage, skipped

    try:
        os.replace(tmp_path, merged_path)
    except OSError as e:
        _safe_unlink(tmp_path)
        warnings.append(f"合并：写盘失败（{e}）；临时文件已清理")
        return stage, skipped

    if replacing:
        warnings.append(f"合并：已覆盖上一次的 {merged_path}")
    stage["merged_path"] = merged_path
    stage["page_count"] = result["page_count"]
    stage["failed"] = result.get("failed_files", [])
    for f in stage["failed"]:
        warnings.append(f"合并：{f.get('filename')} 读取失败（{f.get('reason')}）")
    return stage, skipped


def _safe_unlink(path: str) -> None:
    """删不掉也不抛（清理临时文件而已，别把整次运行带崩）。"""
    try:
        os.remove(path)
    except OSError:
        pass


# ─────────────────────────── ⑥ 收尾（预览页 + 记录）───────────────────────────

def _preview_html(payload: dict) -> str:
    """生成与主报告一致的备用预览页。"""
    return build_check_report_html(payload, payload.get("warnings") or [])


def _write_preview(payload: dict, warnings: list):
    """把本次运行的人类可读产物写进预览目录，返回 `(preview_url, report_url)`。

    `preview_url` 与 `report_url` 都按本次选择生成内容；后一页使用模块名称命名。
    两页只写临时预览目录，分别容错以维持已有接口契约。
    """
    from . import preview as preview_routes
    preview_url = report_url = ""
    try:
        d = preview_routes.new_run_dir("pipeline")
    except Exception as e:                            # 目录都建不出来，两页一起放弃
        warnings.append(f"收尾：预览目录创建失败（{e}），结果数据仍已返回")
        return preview_url, report_url
    try:
        title = report_title(payload)
        p = os.path.join(d, "发票处理结果.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(_preview_html(payload))
        preview_routes.decorate(p, label=title, out_sub="发票处理输出")
        preview_url = preview_routes.preview_url(p)
    except Exception as e:                            # 预览失败不影响主流程，但必须说出来
        warnings.append(f"收尾：预览页生成失败（{e}），结果数据仍已返回")
    try:
        title = report_title(payload)
        p = os.path.join(d, f"{title}.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(build_check_report_html(payload, warnings, title=title))
        preview_routes.decorate(p, label=title, out_sub="发票处理输出")
        report_url = preview_routes.preview_url(p)
    except Exception as e:
        warnings.append(f"收尾：结果页生成失败（{e}），结果数据仍已返回")
    return preview_url, report_url


def _run_record_path(run_id: str) -> str:
    """运行记录落 `%APPDATA%\\发票处理工具\\runs\\<run_id>.json`。

    ⛔ 刻意不写项目里的 `data/` —— 那里有 vault / samples 语义，
       混进运行日志会让「哪些是用户原件」这件事变模糊。
    """
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "发票处理工具", "runs", f"{run_id}.json")


def _write_run_record(run_id: str, payload: dict, warnings: list) -> None:
    try:
        p = _run_record_path(run_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except OSError as e:
        warnings.append(f"收尾：运行记录写入失败（{e}）（不影响本次处理结果）")


# ─────────────────────────── 主流程 ───────────────────────────

def _clean_row(row: dict) -> dict:
    """给响应/落盘的行去掉内部字段（`_raw` 动辄几 KB，不该出现在响应里）。"""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _run_pipeline(scan: dict, payload: dict, prog: _Progress,
                  run_id: str | None = None) -> dict:
    """按勾选执行处理步骤，再生成结果页和运行记录。"""
    t0 = time.time()
    warnings = []
    skipped = []

    selected_ids = payload.get("selected_ids") or []
    out_dir = str(payload.get("out_dir") or "").strip()
    actions = payload.get("actions") or {}
    if not isinstance(actions, dict):
        actions = {}
    # 未显式给的动作一律按「做」处理 —— R3 的语义是「点一次全干完」
    act = {
        "check": bool(actions.get("check", True)),
        "rename": bool(actions.get("rename", True)),
        "dedup": bool(actions.get("dedup", True)),
        "merge": bool(actions.get("merge", True)),
    }
    chk = payload.get("check") or {}
    expected_name = str(chk.get("expected_name") or "").strip()
    expected_tax_id = str(chk.get("expected_tax_id") or "").strip()
    rename_mode = str((payload.get("rename") or {}).get("mode") or "copy").lower()
    failure_policy = str(payload.get("failure_policy") or "skip").lower()
    dry_run = bool(payload.get("dry_run"))

    if not out_dir and not dry_run:
        # 留空时使用当前用户桌面的默认路径；真正写文件时才创建目录。
        out_dir = _shared.default_out_dir()

    if run_id is None:  # 兼容直接调用业务函数的旧测试
        run_id = str(uuid.uuid4())
        prog.register(run_id)

    # ── ① 前置校验：不建目录、不写盘 ───────────────────────────────
    prog.put(stage="preflight", current="校验输入")
    issues = []
    needs_output = act["merge"] or (act["rename"] and rename_mode == "copy")
    if not selected_ids:
        issues.append({"step": "preflight", "reason": "未选择任何行（selected_ids 为空）"})
    if not out_dir:
        issues.append({"step": "preflight", "reason": "未指定输出目录 out_dir"})
    elif needs_output:
        # 只读检查最近的现有父目录；允许新目录在真正复制或合并时创建。
        existing = os.path.abspath(out_dir)
        while not os.path.exists(existing):
            parent = os.path.dirname(existing)
            if parent == existing:
                break
            existing = parent
        if not os.path.isdir(existing):
            issues.append({"step": "preflight", "reason": f"输出路径不可用：{out_dir}"})
        elif not os.access(existing, os.W_OK):
            issues.append({"step": "preflight", "reason": f"输出目录没有写入权限：{existing}"})
    if rename_mode not in ("copy", "inplace"):
        issues.append({"step": "preflight",
                       "reason": f"rename.mode 只支持 'copy'（复制原件）或 'inplace'"
                                 f"（修改原件）（收到 {rename_mode!r}）"})
    if failure_policy not in ("skip",):
        issues.append({"step": "preflight",
                       "reason": f"failure_policy 只支持 'skip'（收到 {failure_policy!r}）"})
    if act["check"] and not (expected_name or expected_tax_id):
        warnings.append("核对：未填预设抬头/税号，本步只标记为 unknown（不影响重命名与合并）")

    # 白名单交叉校验（在任何写盘动作之前）
    picked, pre_skipped, sel_err = _selected_rows(scan, selected_ids, warnings, issues)
    if sel_err:
        issues.append({"step": "preflight", "reason": sel_err})
    skipped.extend(pre_skipped)

    total_sel = len(picked)
    prog.put(total=total_sel)

    # 合计口径（**唯一真源**）：实际参与处理的代表件。
    #   不含 discarded（非发票）；查重被剔除的副本在 ② 之后从 total 里减掉。
    total_amount = round(sum(_row_amount(r) for r in picked if not r.get("discarded")), 2)

    if dry_run:
        prog.put(stage="finalize", done=True, current="预演结束")
        return {
            "ok": not issues,
            "run_id": run_id,
            "dry_run": True,
            "issues": issues,
            "warnings": warnings,
            "rows": [_clean_row(r) for r in picked],
            "skipped": skipped,
            "total_amount": total_amount,
            "elapsed_ms": _now_ms(t0),
        }

    if issues:
        # 真跑时前置校验失败 → 直接结束（**不建目录、不写盘**）
        prog.put(stage="finalize", done=True, error="；".join(i["reason"] for i in issues))
        return {
            "ok": False,
            "run_id": run_id,
            "error": "；".join(i["reason"] for i in issues),
            "issues": issues,
            "warnings": warnings,
            "rows": [],
            "skipped": skipped,
            "total_amount": 0.0,
            "elapsed_ms": _now_ms(t0),
        }

    # 本次运行允许读写的原件白名单（归一化）—— 合并件落盘前用它挡「覆盖原件」
    source_norm = {_abspath_norm(p) for p in (scan.get("paths_set") or ())}

    # ── ② 查重（仅在勾选后执行）────────────────────────────────
    if act["dedup"]:
        prog.put(stage="dedup", index=0, total=total_sel, current="查重归并")
        representatives, dups_skipped, dup_skipped_rows = _stage_dedup(picked, warnings)
        skipped.extend(dup_skipped_rows)
    else:
        representatives = [r for r in picked if not r.get("discarded")]
        dups_skipped = 0
    total_amount = round(sum(_row_amount(r) for r in representatives), 2)

    # ── 进度总账（用户第 ① 条「进度条真的随进度动」）───────────────
    # 每一步只报自己的 `index/total`，进度条会在步骤切换时**跳回 0%**
    # （核对走到 100%，一转重命名又回 0%），看着像坏了。所以先把「开了的每一步 ×
    # 代表件数」累成一条总进度 `_grand`，`_base` 是当前这一步的起点件数。
    # ⚠️ 必须放在 `_stage_dedup` **之后** —— `representatives` 是它产出的。
    _plan = [n for name, n in (("check", len(representatives)),
                               ("rename", len(representatives)),
                               ("merge", len(representatives)))
             if act.get(name)]
    _grand = max(1, sum(_plan))
    _base = 0

    def _tap(rows, stage):
        """逐件推进进度：先把票交给这一步处理，处理完再推一格 —— 不抢跑、不撒谎。

        `rows` 是代表件列表；这一步的每一处都只遍历它一次，喂生成器安全。
        """
        i = 0
        for r in rows:
            yield r
            i += 1
            prog.put(stage=stage, index=_base + i, total=_grand,
                     current=os.path.basename(pool_routes._row_path(r)))


    # ── ③ 核对（逐张，复用 scan 已解析结果）───────────────────────
    # ⚠️ 契约给的三个键之外**多一个 `unknown`**（只增不改，见交付报告「与契约不一致」
    #    一节）：没设预设抬头/税号时 `_check_status` 返回 unknown，不计进 pass/fail/review
    #    的话三个数加起来不等于票数，前端和用户都会以为漏算了。
    check_counter = {"pass": 0, "fail": 0, "review": 0, "unknown": 0}
    check_results = []                                 # 供核对报告 PDF 用（如做）
    if act["check"]:
        prog.put(stage="check", index=_base, total=_grand)
        for r in _tap(representatives, "check"):
            st, result = _check_one(r, expected_name, expected_tax_id)
            if st in check_counter:
                check_counter[st] += 1
            check_results.append(result)
            if st == "fail":
                skipped.append({"id": r.get("id"), "filename": pool_routes._row_path(r),
                                "step": "check",
                                "reason": r.get("check", {}).get("reason") or "核对不符",
                                # 核对不符是**业务判断**：票本身能读能处理，
                                # 不该逼人工介入（need_human 留给「机器判不了」的情形）
                                "need_human": False})
            elif st == "review":
                # review = 票面字段没识别出来（扫描件/无文字层 PDF，无 OCR）→
                # 机器判不了，**必须**带 need_human 出现在跳过清单里（契约 7-7）。
                # 只有「未设预设」的 unknown 不进清单 —— 那是每张都一样的全局状态，
                # 46 张票刷 46 条同样的记录只会把清单淹掉，已在 warnings 里说明。
                skipped.append({"id": r.get("id"), "filename": pool_routes._row_path(r),
                                "step": "check",
                                "reason": r.get("check", {}).get("reason") or "待人工确认",
                                "need_human": True})
        _base += len(representatives)

    # ── ④ 重命名（copy / inplace）───────────────────────────────
    rename_counter = {"renamed": 0, "skipped": 0, "failed": 0}
    if act["rename"]:
        prog.put(stage="rename", index=_base, total=_grand)
        rename_counter, rename_skipped = _stage_rename(
            _tap(representatives, "rename"), out_dir, warnings, rename_mode)
        skipped.extend(rename_skipped)
        _base += len(representatives)

    # ── ⑤ 合并（只喂代表件）─────────────────────────────────────
    merge_stage = {"merged_path": None, "page_count": 0, "failed": []}
    if act["merge"]:
        prog.put(stage="merge", index=_base, total=_grand, current="合并代表件")
        merge_stage, merge_skipped = _stage_merge(
            _tap(representatives, "merge"), out_dir, source_norm, warnings)
        skipped.extend(merge_skipped)
        _base += len(representatives)

    # ── ⑥ 收尾（所选模块结果页 + 跳过清单 + 最终合计）─────────────
    prog.put(stage="finalize", index=_grand, total=_grand, current="生成结果页")
    payload_out = {
        "run_id": run_id,
        "ok": True,
        "stages": {
            "check": check_counter,
            "dedup": {"dups_skipped": dups_skipped, "representatives": len(representatives)},
            "rename": rename_counter,
            "merge": merge_stage,
        },
        "rows": [_clean_row(r) for r in picked],       # 全部选中行（含被跳过的）都回写
        "skipped": skipped,
        "warnings": warnings,
        "total_amount": total_amount,
        "elapsed_ms": _now_ms(t0),
        # 结果页要用的上下文（核对预设 / 勾了哪几步 / 重命名方式）——放在写页之前，
        # 这样核对结果页能把「核对据什么判、重命名是复制还是改原件」写清楚。
        "expected": {"name": expected_name, "tax_id": expected_tax_id},
        "actions": dict(act),
        "rename_mode": rename_mode,
        "out_dir": out_dir,                            # 结果页要显示输出目录
    }
    # 入库结果要进入报告和运行记录；ingest 会按文件返回失败数，未必抛异常。
    try:
        from core import vault as _vault
        vault_result = _vault.ingest([r["path"] for r in picked if r.get("path")])
        vault_failed = int(vault_result.get("failed") or 0)
        if vault_failed:
            reasons = vault_result.get("errors") or []
            detail = f"：{reasons[0]}" if reasons else ""
            warnings.append(f"发票库入库失败 {vault_failed} 个文件{detail}")
    except Exception as exc:            # 可能已部分入库，失败数未知，不能冒充全部失败
        vault_failed = None
        warnings.append(f"发票库入库结果未确认：{exc}")
    payload_out["vault_failed"] = vault_failed

    preview_url, report_url = _write_preview(payload_out, warnings)
    payload_out["preview_url"] = preview_url
    # 🔒 `report_url` + `kind` 是 2026-10-12 与前端工程师**冻结**的契约键：
    #    前端拿到就 `window.open(report_url)` 打开核对结果页，不再弹核对结果模态框。
    #    `kind` 只在报告页真的写出来之后才是 "html"——写失败时留空串，
    #    前端按 `kind !== 'html'` 退回原提示，绝不会去 open 一个空 URL。
    payload_out["report_url"] = report_url
    payload_out["kind"] = "html" if report_url else ""
    payload_out["elapsed_ms"] = _now_ms(t0)            # 预览耗时也算进去

    _write_run_record(run_id, payload_out, warnings)

    prog.put(index=_grand, total=_grand, done=True, current="完成")
    _progress_sweep()
    return payload_out


# ─────────────────────────── 接口 ───────────────────────────

@router.post("/run")
def run(payload: dict):
    """点一次，从当前扫描结果一路干到合并件（见本文件顶部设计要点）。

    同步 `def`：链路里有 pdfplumber / pypdf 的同步循环，写成 async 会堵死
    事件循环（进度轮询 / /api/health 会超时）。
    """
    payload = payload or {}
    scan_id = str(payload.get("scan_id") or "").strip()
    scan, err = pool_routes._scan_cache_get(scan_id)
    if not scan:
        # 进程内状态易失：重启或超过 30 分钟就是这条，必须让用户能看懂
        return JSONResponse(
            {"ok": False, "error": err or "扫描快照不可用", "need_rescan": True},
            status_code=410)

    selected_ids = payload.get("selected_ids") or []
    out_dir = str(payload.get("out_dir") or "").strip()
    actions = payload.get("actions") if isinstance(payload.get("actions"), dict) else {}
    chk = payload.get("check") or {}
    rename_mode = str((payload.get("rename") or {}).get("mode") or "copy").lower()
    failure_policy = str(payload.get("failure_policy") or "skip").lower()
    dry_run = bool(payload.get("dry_run"))

    try:
        key = _idem_key(scan_id, selected_ids, actions, out_dir, rename_mode,
                        chk.get("expected_name"), chk.get("expected_tax_id"),
                        failure_policy, dry_run)
    except TypeError:
        return JSONResponse({"ok": False, "error": "请求参数无法序列化"}, status_code=400)

    requested_run_id = str(payload.get("run_id") or uuid.uuid4())
    try:
        requested_run_id = str(uuid.UUID(requested_run_id))
    except ValueError:
        return JSONResponse({"ok": False, "error": "run_id 格式不正确"}, status_code=400)

    prog = _Progress()
    state, hit = _runs_claim(key, requested_run_id, prog)
    if state != "claimed":
        if state == "done":
            # 幂等命中：直接回上次结果，**不重复写盘**
            out = dict(hit["payload"])
            out["idempotent"] = True
            out["run_id"] = hit["run_id"]
            return out
        active_prog = hit.get("progress")
        return JSONResponse(
            {"ok": False, "error": "已有处理任务正在运行，请等待完成后重试",
             "run_id": hit.get("run_id"),
             "progress": active_prog.snapshot() if active_prog else None},
            status_code=409)

    prog.register(requested_run_id)
    try:
        result = _run_pipeline(scan, payload, prog, requested_run_id)
    except Exception as e:
        prog.put(done=True, error=str(e))
        with _RUNS_LOCK:
            _RUNS.pop(key, None)
        return JSONResponse({"ok": False, "error": f"处理失败：{e}"},
                            status_code=500)

    # 首次执行：显式给出 `idempotent=False`（而不是让它缺席）——
    # 前端只需看这一个键就能区分「刚跑完」与「命中上次结果」。
    result.setdefault("idempotent", False)
    _runs_put(key, "done", result.get("run_id"), prog, result)
    return result


@router.get("/run/status")
def run_status(run_id: str = ""):
    """内存进度：`{ok, found, run_id, stage, index, total, current, done, error}`。

    查不到就明确报「没有这次运行」—— 进程重启后进度就没了，别让前端以为在跑。

    ⚠️ 两处 2026-10-12（第 7 轮 ①）改的，都有原因：
    前端先生成 run_id，再以该 ID 查询本次运行。省略时仍支持旧调用方，
    返回最近注册的一次进度记录。
    ② 响应体补 `ok: True / found: True`：原先只回裸的 `{stage,index,...}`，
       前端 `data.ok !== true` 的判定会把它当失败，连真数据也一起丢掉。
    """
    run_id = (run_id or "").strip()
    with _PROGRESS_LOCK:
        key = run_id
        if not key and _PROGRESS:
            key = next(reversed(_PROGRESS))
        data = _PROGRESS.get(key) if key else None
        snap = dict(data) if data else None
    if snap is None:
        return JSONResponse(
            {"ok": False, "error": "没有这次运行的进度记录（服务可能已重启）",
             "stage": "", "index": 0, "total": 0, "current": "", "done": True, "error": ""},
            status_code=404)
    out = {"ok": True, "found": True, "run_id": key}
    out.update(snap)
    return out
