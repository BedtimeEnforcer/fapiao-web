# -*- coding: utf-8 -*-
"""core/vault.py — 「发票库」（历史发票留存）的存储层。

设计约束（用户已拍板，改之前先读这一节）
----------------------------------------
1. **磁盘原件一律不动**（硬红线）。入库 = 把原件**复制一份**进软件自己的目录。
   本文件里出现的所有 `os.remove` 目标都只可能是 `data/vault/` 下的副本。
2. **删记录 = 连库内副本一起删**（这样「占用」数字才真的会下降）。
   ⛔ 任何时候都不许去删 `orig_path` 指向的东西。
3. 开发态库放项目 `data/`；打包后放当前用户的应用数据目录，软件更新不覆盖。
4. 单机单人工具：**不做**登录 / 权限 / 回收站 / 软删除。删除就是真删。

存储布局（已冻结）
------------------
* 库根 `LIB_ROOT`：开发态 `<项目根>\\data`；打包后 `%LOCALAPPDATA%\\发票助手\\data`。
* 数据库 `DB`：`<LIB_ROOT>\\vault.db`（SQLite）
* 副本目录 `VAULT_DIR`：`<LIB_ROOT>\\vault\\<YYYY-MM>\\<原文件名>`
  `YYYY-MM` 取**入库那一刻**的年月；同名冲突在扩展名前加 ` (2)`、` (3)` …

并发
----
FastAPI 把同步 `def` 丢线程池执行，所以**所有写操作**都在模块级 `_LOCK` 里；
读写都用**短连接**（`with closing(sqlite3.connect(...))`），不留全局长连接
（长连接配 `check_same_thread=False` 会在并发写时撞出 "database is locked"）。

金额语义（`0` 与「取不到」必须能区分）
-------------------------------------
`core.facets.extract_invoice_meta` 的 `amount` 默认值是 `0.0`，所以 `0.0`
一律按**取不到**处理，落库为 `NULL`（SQLite）→ 接口回 JSON `null`。
⛔ 不要把它写成 `0.0` 冒充金额 —— 那会让「合计」把没识别出金额的票当成 0 元票。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import uuid
from contextlib import closing

from . import journey_info, parse

# ─────────────────────────── 路径 ───────────────────────────


def project_root() -> str:
    """项目根目录（其下应有 server/ 与 data/）。

    * **打包后**：返回 `sys.executable` 所在目录，供代码/资源定位使用；
      发票库本身由 `_default_lib_root()` 放在当前用户应用数据目录。
    * **开发态**：`server/core/vault.py` → 上溯三级即项目根。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


ROOT = project_root()
def _default_lib_root() -> str:
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(appdata, "发票助手", "data")
    return os.path.join(ROOT, "data")


#: 打包后放当前用户目录，避免安装位置只读或软件更新覆盖数据。
#: `FAPIAO_LIB_ROOT` 供测试沙盒隔离，也可供明确指定存储位置。
#: 为什么必须留这个口子（2026-10-12 出过一次真事故）：`tests/test_vault.py` 与
#: `tests/verify_vault_ui.mjs` 都要调 `POST /api/vault/clear` 才能验证「一键清空」，
#: 它们此前硬编码打 8765 —— 也就是用户正在用的那个服务，于是跑一次回归就把
#: **他库里的真发票连同副本一起删掉了**。现在这两个测试自己起一个 8799 的沙盒服务、
#: 库指向临时目录（见 `tests/_sandbox.py`），一行也不碰 `<项目根>/data`。
LIB_ROOT = os.environ.get("FAPIAO_LIB_ROOT", "").strip() or _default_lib_root()
#: SQLite 数据库文件
DB = os.path.join(LIB_ROOT, "vault.db")
#: 副本目录（清空时只处理已登记的副本，绝不递归删除整个目录）
VAULT_DIR = os.path.join(LIB_ROOT, "vault")

#: 表结构版本，落在 meta 表里。
#:   v1 → 原始建库版本（无 `meta_json` 列）
#:   v2 → `invoices.meta_json`（缓存票面解析结果，见「解析结果缓存」一节）
SCHEMA_VERSION = 2

#: 所有写操作的互斥锁（FastAPI 同步 def 跑在线程池里，会真并发）
_LOCK = threading.Lock()

#: 「清空发票库」的确认口令（与 routes/vault.py 校验的是同一个值）
CLEAR_PHRASE = "清空发票库"

#: `ingest` 的 errors 最多回传多少条（前端列表放不下更多）
MAX_ERRORS = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    md5          TEXT NOT NULL UNIQUE,
    size         INTEGER NOT NULL,
    orig_path    TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    ext          TEXT NOT NULL,
    kind         TEXT,
    label        TEXT,
    mid          TEXT,
    invoice_no   TEXT,
    invoice_date TEXT,
    seller       TEXT,
    buyer        TEXT,
    tax_id       TEXT,
    amount       REAL,
    created_at   TEXT NOT NULL,
    meta_json    TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_created ON invoices(created_at);
"""


# ─────────────────────────── 连接 / 建库 ───────────────────────────


def _connect() -> sqlite3.Connection:
    """开一条短连接（调用方负责关）。

    `check_same_thread=False`：本模块一律「开短连接、当次用完即关」（不做全局
    复用），连接不会跨线程存活；带上该开关只是不让 sqlite3 的线程归属检查挡在
    线程池前面。写操作真正的并发保护是模块级 `_LOCK`。
    """
    con = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _init() -> None:
    """建表（幂等）+ 结构升级（幂等）。每次写操作前调用。

    `CREATE TABLE IF NOT EXISTS` 对**已存在**的表不会补列，所以老库（v1）必须单独
    走一次 `_migrate`：`PRAGMA table_info` 看不到 `meta_json` 才 `ALTER TABLE`。
    老库升完**一条记录都不丢**（`ADD COLUMN` 只加列、不动既有行）。
    """
    os.makedirs(LIB_ROOT, exist_ok=True)
    with closing(_connect()) as con:
        con.executescript(_SCHEMA)
        _migrate(con)
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)))
        con.commit()


def _migrate(con: sqlite3.Connection) -> None:
    """把老库原地升到当前 `SCHEMA_VERSION`（**只加列，不删不改既有数据**）。

    * v1 → v2：`invoices.meta_json TEXT`（票面解析结果缓存）。
      只在 `PRAGMA table_info` 里**没有**该列时才 ALTER —— 新库（`_SCHEMA` 已带该列）
      走这里是空操作，重复调用也是空操作，天然幂等。
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(invoices)")}
    if "meta_json" not in cols:
        con.execute("ALTER TABLE invoices ADD COLUMN meta_json TEXT")


# ─────────────────────────── 小工具 ───────────────────────────


def _now_iso() -> str:
    """入库时间：本地时区的 ISO8601（带偏移，例如 2026-10-11T02:30:00+08:00）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _month_dir() -> str:
    """入库那一刻的年月目录名，例如 `2026-10`。"""
    return datetime.datetime.now().strftime("%Y-%m")


def _rel(path: str) -> str:
    """副本相对库根的路径，统一 `/` 分隔，打包后仍以 `vault/` 开头。"""
    try:
        rel = os.path.relpath(path, LIB_ROOT)
    except ValueError:                      # 不同盘符 → relpath 会抛
        return os.path.basename(path).replace("\\", "/")
    if rel == ".." or rel.startswith(".." + os.sep):
        return os.path.basename(path).replace("\\", "/")
    return rel.replace("\\", "/")


def _text(v):
    """空串 → `None`（「取不到」不许用空串冒充，便于前端区分）。"""
    s = "" if v is None else str(v).strip()
    return s or None


def _norm_key(path) -> str:
    """路径查找用的归一化键：`abspath` + `normcase`。

    只用来**比对**（`metas` 的键 ↔ 本次入库的路径、调用方路径 ↔ 库里的
    `stored_path`/`orig_path`），**绝不**用来回传给调用方 —— 接口对外一律用
    调用方自己传进来的原字符串作键（见 `cached_meta_map` / `/api/vault/meta`）。
    `normcase` 在 Windows 上顺带折平大小写与 `/` `\\` 差异。
    """
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (TypeError, ValueError):
        return str(path)


def _amount(v):
    """金额：取不到 / 0 → `None`；否则 round(..., 2)。

    ⛔ `0` 与「取不到」必须能区分，见模块头「金额语义」。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == 0:
        return None
    return round(f, 2)


def _date(v):
    """日期统一成 `YYYY-MM-DD`；拿不到就 `None`（带日期筛选时这类行不出现）。"""
    s = _text(v)
    if not s:
        return None
    import re
    m = re.match(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if not m:
        return s
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_dst(directory: str, name: str) -> str:
    """同名冲突 → 在**扩展名前**加 ` (2)`、` (3)` …（存储布局冻结口径）。"""
    base, ext = os.path.splitext(name)
    dst = os.path.join(directory, name)
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return dst


def _ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def _read(src: str) -> bytes:
    with open(src, "rb") as f:
        return f.read()


#: `_read` 失败时的哨兵（None 与「文件内容为空」要区分开）
_UNREADABLE = object()


# ─────────────────────────── 元数据 ───────────────────────────


def _empty_meta() -> dict:
    return {"kind": None, "label": None, "mid": None, "invoice_no": None,
            "invoice_date": None, "seller": None, "buyer": None,
            "tax_id": None, "amount": None}


def _meta_from(m: dict) -> dict:
    """`core.facets.extract_invoice_meta` 的 dict → 库里的列。

    键名映射（facets 的键与库的列**故意不完全同名**，照抄 pool.py `_pool_row` 的口径）：
      * `label`     ← `type`（短类型标签：火车票 / 住宿票 / 电子发票 …）
      * `mid`       ← `mid_class(label)`（品名中类，票种=「其他」时前端才显示）
      * `invoice_date` ← `date`（facets 已归一成 `YYYY-MM-DD`）
      * `tax_id`    ← `buyer_tax_id`，退回 `seller_tax_id`，再退回 `tax_id`
      （facets **没有** `check_code` 之外的税号通用键，所以这里显式挑；
        这与 `routes/check.py` 取税号的口径一致。）
      * `amount`    ← `amount`，`0`/取不到 → `None`（见模块头「金额语义」）
    """
    if not isinstance(m, dict) or not m:
        return _empty_meta()
    try:
        from core.rename import mid_class
    except Exception:                            # 标签算法出问题不该拖垮入库
        mid_class = None
    label = _text(m.get("type"))
    mid = None
    if mid_class is not None and label:
        try:
            mid = _text(mid_class(label))
        except Exception:
            mid = None
    return {
        "kind": _text(m.get("kind")),
        "label": label,
        "mid": mid,
        "invoice_no": _text(m.get("invoice_no")),
        "invoice_date": _date(m.get("date")),
        "seller": _text(m.get("seller")),
        "buyer": _text(m.get("buyer")),
        "tax_id": (_text(m.get("buyer_tax_id")) or _text(m.get("seller_tax_id"))
                   or _text(m.get("tax_id"))),
        "amount": _amount(m.get("amount")),
    }


def _parse_meta(path: str) -> dict:
    """复用项目现成的票面解析（**不自己写正则**）。

    解析失败（扫描件 / 无文字层 / 损坏）**不抛**，返回全 `None` 的元数据 ——
    契约：解析失败但文件可读，仍要**入库**（元数据留空），并计入 added。
    """
    try:
        from core.facets import extract_invoice_meta
        return _meta_from(extract_invoice_meta(path))
    except Exception:
        return _empty_meta()


def _raw_meta(path: str) -> dict:
    """`core.facets.extract_invoice_meta(path)` 的**原始返回**（不映射、不裁剪）。

    与 `_parse_meta` 的区别：`_parse_meta` 是「facets → 库的 9 个列」的映射，
    这里拿的是**完整解析结果**，用来落 `meta_json`（缓存的原样就是解析的原样，
    将来 `meta_json` 的消费者不用猜我们做了哪些键名映射）。
    解析失败同样不抛，返回 `{}`（空缓存 = 「没有可复用的解析结果」）。
    """
    try:
        from core.facets import extract_invoice_meta
        m = extract_invoice_meta(path)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _cols_from_cached(m) -> dict:
    """一份**已解析**的 meta → 库的 9 个列（`_meta_from` 的直通，语义一字不改）。

    存在的意义：`metas` 命中时**不许再调 `extract_invoice_meta`**，但落库的
    `kind/label/mid/...` 这些列还得照旧由 `_meta_from` 的映射口径产生 ——
    所以把缓存值喂给它，而不是自己另写一份映射。
    """
    return _meta_from(m) if isinstance(m, dict) else _empty_meta()


def _to_json(meta) -> str | None:
    """一份原始解析结果 → `meta_json` 文本（不可序列化 → `None`，不抛）。"""
    if not isinstance(meta, dict) or not meta:
        return None
    try:
        return json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _from_json(raw) -> dict | None:
    """`meta_json` 文本 → dict；空/损坏 → `None`（损坏的缓存按「没缓存」处理）。"""
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return m if isinstance(m, dict) and m else None


def _from_columns(r: sqlite3.Row) -> dict:
    """没有（或读不出）`meta_json` 时的兜底：从库里 9 个列拼回同形状的 dict。

    ⚠️ 这是**兜底**，不是主路径：`_from_columns` 拼出来的键集与 `_meta_from` 的
    输出逐字一致（9 个键），但 facets 的完整字段（车次 / 座号 / 税号明细…）
    本来就没进列，这里自然也补不回来。老库（v1 升上来的历史行）没有 `meta_json`，
    读回时走的就是这一支 —— 能拿到 9 个键，拿不到更多。
    """
    return {
        "kind": r["kind"], "label": r["label"], "mid": r["mid"],
        "invoice_no": r["invoice_no"], "invoice_date": r["invoice_date"],
        "seller": r["seller"], "buyer": r["buyer"], "tax_id": r["tax_id"],
        "amount": r["amount"],
    }


def _cached_or_cols(r: sqlite3.Row) -> dict:
    """一行 → 「该文件的解析结果」：优先 `meta_json`，没有就退回 9 个列。"""
    return _from_json(r["meta_json"]) or _from_columns(r)


# ─────────────────────────── 行 → dict ───────────────────────────


def _row_dict(r: sqlite3.Row) -> dict:
    """接口用的行对象，键名**冻死**（前端已按此实现）。"""
    meta = _cached_or_cols(r)
    ofd = (parse.ofd_display_fields(r["stored_path"])
           if str(r["ext"] or "").lower().lstrip(".") == "ofd" else {})
    if ofd:
        meta = dict(meta, **ofd)
    return {
        "id": r["id"],
        "file_name": r["file_name"],
        "stored_path": r["stored_path"],
        "rel_path": r["rel_path"],
        "ext": r["ext"],
        "kind": r["kind"],
        "label": r["label"],
        "mid": r["mid"],
        "invoice_no": ofd.get("invoice_no") or r["invoice_no"],
        "invoice_date": ofd.get("invoice_date") or r["invoice_date"],
        "seller": ofd.get("seller", r["seller"]),
        "buyer": ofd.get("buyer", r["buyer"]),
        "parse_status": ofd.get("parse_status") or meta.get("parse_status") or "",
        "parse_error": ofd.get("parse_error") or meta.get("parse_error") or "",
        "tax_id": r["tax_id"],
        "amount": ofd.get("amount") or r["amount"],
        "size": r["size"],
        "created_at": r["created_at"],
        "md5": r["md5"],
        "journey": journey_info.journey(meta),
        "missing_info": journey_info.missing_fields(meta),
        "completeness": journey_info.completeness(meta),
    }


def update_journey(*, item_id=None, path=None, updates=None) -> dict:
    """保存库内解析记录的手工行程字段，原始发票和库内副本均不改。"""
    if item_id is None and not str(path or "").strip():
        raise ValueError("未指定要修改的发票")
    _init()
    with _LOCK:
        with closing(_connect()) as con:
            row = None
            if item_id is not None:
                try:
                    identity = int(item_id)
                except (TypeError, ValueError):
                    raise ValueError("发票编号无效") from None
                row = con.execute("SELECT * FROM invoices WHERE id = ?", (identity,)).fetchone()
            else:
                supplied = str(path).strip()
                key = _norm_key(supplied)
                column = "LOWER({0})" if os.name == "nt" else "{0}"
                rows = con.execute(
                    f"SELECT * FROM invoices WHERE {column.format('stored_path')} = ? "
                    f"OR {column.format('orig_path')} = ? ORDER BY id DESC", (key, key))
                try:
                    digest = _md5_file(supplied)
                except OSError:
                    raise ValueError("发票文件已不可读取，请重新导入") from None
                row = next((candidate for candidate in rows if candidate["md5"] == digest), None)
            if row is None:
                raise LookupError("发票库中找不到这张票，请重新导入")

            meta = _from_json(row["meta_json"])
            if meta is None or "type" not in meta:
                # 老记录没有完整缓存时只解析这张库内副本，不批量重读整个发票库。
                meta = _raw_meta(row["stored_path"])
                if not meta:
                    meta = dict(_from_columns(row))
                    meta["type"] = row["label"] or "发票"
            meta.setdefault("kind", row["kind"])
            edited = journey_info.apply_updates(meta, updates)
            con.execute("UPDATE invoices SET meta_json = ? WHERE id = ?",
                        (_to_json(edited), row["id"]))
            con.commit()
            saved = con.execute("SELECT * FROM invoices WHERE id = ?", (row["id"],)).fetchone()
            return _row_dict(saved)


# ─────────────────────────── 写入：入库 / 删除 ───────────────────────────


#: `metas` 参数：`{归一化路径: 该路径已解析好的 meta}`（见 `_meta_index`）
MetaMap = dict


def _meta_index(metas) -> dict:
    """`ingest(metas=...)` 的入参 → `{_norm_key(路径): meta}`。

    键的归一化口径＝`_norm_key`（`abspath` + `normcase`），所以调用方传
    `os.path.abspath(path)`、相对路径、或大小写/斜杠写法不同的同一文件，都能命中。
    只收 `dict` 值；`None` / 非 dict 一律当作「这份没缓存」，落库时照旧现解析。
    """
    if not isinstance(metas, dict) or not metas:
        return {}
    out = {}
    for k, v in metas.items():
        s = str(k or "").strip()
        if s and isinstance(v, dict) and v:
            out[_norm_key(s)] = v
    return out


def ingest(paths, metas: MetaMap | None = None) -> dict:
    """把一批原件**复制**进库并登记。

    规则（契约逐条）：
      * 逐份算 MD5；**MD5 已在库里 → skipped+1，绝不覆盖**；
      * 解析失败但文件可读 → **仍入库**（元数据留空），计入 `added`；
      * 文件读不了 → `failed+1`，原因进 `errors`（最多 `MAX_ERRORS` 条）；
      * `paths` 为空 → `added=0`（不建库、不写盘）。

    参数
    ----
    `metas`（**可选**，`{路径: 已解析 meta}`）：命中的路径**直接用它**，不再调
    `core.facets.extract_invoice_meta`（这正是用户第④条「解析过的发票缓存解析结果」）；
    没命中的路径照旧现解析。键按 `_norm_key` 归一化后比对（详见 `_meta_index`）。
    `metas=None`（默认）→ 与加该参数之前**逐字节等价**，老调用方无需改动。

    返回 `{added, skipped, failed, errors, count, bytes}`。
    其中 `count` / `bytes` 是**入库后的总张数 / 总字节**。
    """
    items = [str(p) for p in (paths or []) if str(p or "").strip()]
    cache = _meta_index(metas)
    added = skipped = failed = 0
    errors = []

    if items:
        _init()
        for src in items:
            name = os.path.basename(src)
            data = _read(src) if os.path.isfile(src) else _UNREADABLE
            if data is _UNREADABLE:
                failed += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{name}：文件不可读或不存在（{src}）")
                continue

            digest = hashlib.md5(data).hexdigest()
            # 先查一次库（快路径：绝大多数重复件在这里就返回，不做解析/复制）
            if _exists(digest):
                skipped += 1
                continue

            # 解析与复制都在锁外做（PDF 解析慢，锁内只做「查重 + 落盘 + 登记」）
            #
            # 缓存口径（`metas` 命中时**解析这一步整个跳过**）：
            #   * `meta`（库的 9 个列）仍由 `_meta_from` 从同一份缓存值映射出来，
            #     列口径与现解析时**完全一致**（只是不重新解析）；
            #   * `meta_json` 存的是**这份缓存值本身**（facets 的原始返回）——
            #     将来读回时不需要「9 个列 → 反推 facets」这种有损还原。
            #   未命中时 `raw` 就是现解析的结果，`meta_json` 与列同源、必然一致。
            hit = cache.get(_norm_key(src))
            if hit is not None:
                meta = _cols_from_cached(hit)
                raw = hit
            else:
                raw = _raw_meta(src)
                meta = _meta_from(raw)
            if not os.path.isfile(src):
                failed += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{name}：文件已不可读（{src}）")
                continue
            month_dir = os.path.join(VAULT_DIR, _month_dir())
            os.makedirs(month_dir, exist_ok=True)
            dst = _unique_dst(month_dir, name)
            copied = ""
            try:
                copied = _copy(src, dst)
                with _LOCK:
                    _init()
                    with closing(_connect()) as con:
                        try:
                            con.execute(
                                "INSERT INTO invoices (md5, size, orig_path, stored_path,"
                                " rel_path, file_name, ext, kind, label, mid, invoice_no,"
                                " invoice_date, seller, buyer, tax_id, amount, created_at,"
                                " meta_json)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (digest, len(data), os.path.abspath(src), copied,
                                 _rel(copied), os.path.basename(copied),
                                 _ext_of(copied), meta["kind"], meta["label"], meta["mid"],
                                 meta["invoice_no"], meta["invoice_date"], meta["seller"],
                                 meta["buyer"], meta["tax_id"], meta["amount"], _now_iso(),
                                 _to_json(raw)))
                            con.commit()
                        except sqlite3.IntegrityError:
                            # 两次查库之间被别人塞进去了（同一文件并发入库）→ 按重复处理
                            raise _Duplicate()
            except _Duplicate:
                _safe_remove(copied)
                skipped += 1
                continue
            except OSError as exc:
                _safe_remove(copied)             # 复制/写库失败别留半份孤儿副本
                failed += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{name}：入库失败（{exc}）")
                continue
            except Exception as exc:             # noqa: BLE001 —— 单份失败不拖垮整批
                _safe_remove(copied)
                failed += 1
                if len(errors) < MAX_ERRORS:
                    errors.append(f"{name}：入库失败（{exc}）")
                continue
            added += 1

    s = stats()
    return {"added": added, "skipped": skipped, "failed": failed,
            "errors": errors, "count": s["count"], "bytes": s["bytes"]}


class _Duplicate(Exception):
    """内部信号：INSERT 撞 UNIQUE(md5) —— 说明并发重复，按 skipped 处理。"""


def _safe_remove(path: str) -> None:
    """删库内副本；删不掉也不抛（**只用于 `data/vault/` 下的副本**）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _copy(src: str, dst: str) -> str:
    """复制原件 → 库内副本（`shutil.copy2` 保留时间戳；**只读 src，绝不改它**）。

    先写 `dst` 再返回 `dst`；调用方若在登记阶段失败，用 `_safe_remove` 清掉。
    """
    shutil.copy2(src, dst)
    return dst


def _exists(digest: str) -> bool:
    with closing(_connect()) as con:
        return con.execute("SELECT 1 FROM invoices WHERE md5 = ? LIMIT 1",
                           (digest,)).fetchone() is not None


def delete_items(ids) -> dict:
    """按 id 删记录 + **连库内副本一起删**（绝不碰 `orig_path`）。

    返回删除数、失败数及需人工恢复的暂存副本路径。
    """
    wanted = []
    for raw in (ids or []):
        try:
            wanted.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return {"deleted": 0, "missing": 0, "bytes_freed": 0,
                "failed": 0, "cleanup_failed": 0, "recovery_paths": []}
    _init()
    with _LOCK:
        with closing(_connect()) as con:
            return _delete_rows(con, set(wanted))


def _vault_copy_path(path: str) -> bool:
    """只允许处理库内副本，解析符号链接后也必须仍在库目录内。"""
    if not path:
        return False
    try:
        root = os.path.normcase(os.path.realpath(VAULT_DIR))
        target = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath((root, target)) == root and target != root
    except (OSError, ValueError):
        return False


def _delete_rows(con: sqlite3.Connection, wanted: set[int]) -> dict:
    """逐条暂存副本、提交记录删除，失败则把副本放回原位。调用方持有锁。"""
    if not wanted:
        return {"deleted": 0, "missing": 0, "bytes_freed": 0,
                "failed": 0, "cleanup_failed": 0, "recovery_paths": []}
    rows = {}
    wanted_ids = list(wanted)
    for chunk_start in range(0, len(wanted_ids), 500):
        chunk = wanted_ids[chunk_start:chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        for row in con.execute(
                f"SELECT id, stored_path FROM invoices WHERE id IN ({marks})", chunk):
            rows[row["id"]] = row["stored_path"]
    deleted = freed = failed = cleanup_failed = 0
    recovery_paths = []
    for item_id, path in rows.items():
        if not _vault_copy_path(path):
            failed += 1
            continue
        staged = ""
        size = 0
        try:
            if os.path.exists(path):
                size = os.path.getsize(path)
                staged = f"{path}.pending-delete-{uuid.uuid4().hex}"
                os.replace(path, staged)
            con.execute("DELETE FROM invoices WHERE id = ?", (item_id,))
            con.commit()
            deleted += 1
        except (OSError, sqlite3.Error):
            con.rollback()
            if staged:
                try:
                    os.replace(staged, path)
                except OSError:
                    cleanup_failed += 1
                    recovery_paths.append(staged)
            failed += 1
            continue
        if staged:
            try:
                os.remove(staged)
                freed += size
            except OSError:
                cleanup_failed += 1
                recovery_paths.append(staged)
    return {"deleted": deleted, "missing": len(wanted) - len(rows),
            "bytes_freed": freed, "failed": failed,
            "cleanup_failed": cleanup_failed, "recovery_paths": recovery_paths}


def clear_all() -> dict:
    """只清理数据库登记的副本；失败的记录保留，供用户重试。"""
    _init()
    with _LOCK:
        with closing(_connect()) as con:
            wanted = {r[0] for r in con.execute("SELECT id FROM invoices")}
            return _delete_rows(con, wanted)


def _iter_copies():
    for dirpath, _dirnames, filenames in os.walk(VAULT_DIR):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def copy_count() -> int:
    """`data/vault/` 下的副本文件数（自检 / 清空校验用）。"""
    return sum(1 for _ in _iter_copies())


# ─────────────────────────── 读取：列表 / 统计 / 单行 ───────────────────────────


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(q, kind, mid, date_from, date_to):
    """拼 WHERE 子句 + 参数。空条件一律不加（不产生 `1=1` 之类的噪音）。

    * `q` 模糊匹配 `file_name|seller|buyer|invoice_no|orig_path`，大小写不敏感
      （SQLite 的 `LIKE` 对 ASCII 天然大小写不敏感；中文无大小写之分）；
    * `from`/`to` 比的是 `invoice_date`；**`invoice_date` 为空的行在带日期筛选时
      不出现** —— 靠 `invoice_date IS NOT NULL` 显式挡掉，不依赖 SQL 的三值逻辑。
    """
    where, args = [], []
    q = (q or "").strip()
    if q:
        like = f"%{_like_escape(q)}%"
        where.append("(file_name LIKE ? ESCAPE '\\' OR seller LIKE ? ESCAPE '\\'"
                     " OR buyer LIKE ? ESCAPE '\\' OR invoice_no LIKE ? ESCAPE '\\'"
                     " OR orig_path LIKE ? ESCAPE '\\')")
        args.extend([like] * 5)
    kinds = list(dict.fromkeys(part.strip() for part in str(kind or "").split(",") if part.strip()))
    if kinds:
        kind_terms = []
        for selected in kinds:
            if selected.lower() == "unknown":
                # 旧记录的空 kind 在统计接口里显示为 unknown，筛选也须能查到。
                kind_terms.append("(kind IS NULL OR kind = '' OR lower(kind) = 'unknown')")
            else:
                kind_terms.append("kind = ?")
                args.append(selected)
        where.append("(" + " OR ".join(kind_terms) + ")")
    mid = (mid or "").strip()
    if mid:
        where.append("mid = ?")
        args.append(mid)
    d1 = _date(date_from)
    d2 = _date(date_to)
    if d1:
        where.append("invoice_date IS NOT NULL AND invoice_date >= ?")
        args.append(d1)
    if d2:
        where.append("invoice_date IS NOT NULL AND invoice_date <= ?")
        args.append(d2)
    return (" AND ".join(where), args)


def list_rows(q="", kind="", mid="", date_from="", date_to="",
              limit=200, offset=0) -> dict:
    """列表查询。返回 `{total, count, bytes, by_kind, rows}`。

    口径：
      * `total` / `bytes` / `by_kind` 是**筛选后**的全量聚合（不受分页影响）；
      * `count` 是本次返回的行数（`len(rows)`）；
      * `limit` 默认 200、上限 2000；`offset` 默认 0。
    """
    _init()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)

    clause, args = _build_where(q, kind, mid, date_from, date_to)
    tail = f" WHERE {clause}" if clause else ""

    with closing(_connect()) as con:
        total, total_bytes = con.execute(
            f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM invoices{tail}",
            args).fetchone()
        by_kind = {str(k or "unknown"): int(n) for k, n in con.execute(
            f"SELECT kind, COUNT(*) FROM invoices{tail} GROUP BY kind", args)}
        rows = [_row_dict(r) for r in con.execute(
            f"SELECT * FROM invoices{tail} ORDER BY id DESC LIMIT ? OFFSET ?",
            args + [limit, offset])]

    return {"total": int(total), "count": len(rows),
            "bytes": int(total_bytes or 0), "by_kind": by_kind, "rows": rows}


def stats() -> dict:
    """总量：`{count, bytes, by_kind}`（不分页、不过滤）。"""
    _init()
    with closing(_connect()) as con:
        count, total_bytes = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM invoices").fetchone()
        by_kind = {str(k or "unknown"): int(n) for k, n in con.execute(
            "SELECT kind, COUNT(*) FROM invoices GROUP BY kind")}
    return {"count": int(count), "bytes": int(total_bytes or 0), "by_kind": by_kind}


def get_item(item_id) -> dict | None:
    """按 id 取一行 → 行对象；不存在返回 `None`。"""
    _init()
    try:
        i = int(item_id)
    except (TypeError, ValueError):
        return None
    with closing(_connect()) as con:
        r = con.execute("SELECT * FROM invoices WHERE id = ?", (i,)).fetchone()
    return _row_dict(r) if r else None


def item_id_for_path(path: str) -> int | None:
    """只给行程单定位已入库的那份副本；文件内容必须与库记录一致。"""
    if not path or not os.path.isfile(DB):
        return None
    key = _norm_key(path)
    column = "LOWER({0})" if os.name == "nt" else "{0}"
    with closing(_connect()) as con:
        rows = con.execute(
            f"SELECT id, md5 FROM invoices WHERE {column.format('stored_path')} = ? "
            f"OR {column.format('orig_path')} = ? ORDER BY id DESC", (key, key))
        try:
            digest = _md5_file(path)
        except OSError:
            return None
        return next((int(row["id"]) for row in rows if row["md5"] == digest), None)


def item_ids_for_paths(paths) -> dict[str, int]:
    """扫描结束后给工作台行关联库内编号，按文件摘要核对身份。"""
    wanted = [str(path) for path in (paths or []) if str(path or "").strip()]
    if not wanted or not os.path.isfile(DB):
        return {}
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT id, md5, orig_path, stored_path FROM invoices ORDER BY id DESC").fetchall()
    by_path = {}
    for row in rows:
        for value in (row["orig_path"], row["stored_path"]):
            by_path.setdefault(_norm_key(value), []).append(row)
    result = {}
    for path in wanted:
        try:
            digest = _md5_file(path)
        except OSError:
            continue
        record = next((row for row in by_path.get(_norm_key(path), [])
                       if row["md5"] == digest), None)
        if record:
            result[path] = int(record["id"])
    return result


#: `/api/vault/meta` 一次最多接受多少条路径（超出 → 400）
MAX_META_PATHS = 500


def cached_meta_map(paths) -> dict:
    """一批路径 → **库里已缓存的解析结果**（用户第④条 B 半段的服务端入口）。

    `routes/pool.py::scan` 可以直接调本函数，不必走 HTTP。

    两个查法（都按 `_norm_key` 归一化后比对）：
      * `stored_path` —— 调用方传的是**库内副本**路径；
      * `orig_path`   —— 调用方传的是**当初入库的原件**路径
        （`pool` 钩子把「刚扫到的原件路径」拿来问，走的就是这一支）。
      ⚠️ `orig_path` 只在这里参与**查找**，任何对外结构都不回传它（`/list` 的
         17 键冻结契约里没有它，免得泄漏用户的本地路径）。

    返回 `{<调用方传入的那个原字符串>: meta}`：
      * 键**原样回传调用方传进来的字符串**（不归一化、不换大小写），
        否则调用方拿着自己的路径反而查不到自己刚存的那条；
      * 同一路径传两次 → 只出现一次（dict 键天然去重）；
      * 库里没有、文件不存在或内容 MD5 与入库时不同 → **该键直接不出现**；
      * 值 = `meta_json`（facets 原始解析结果）；老库（v1 历史行）没有
        `meta_json` → 退回 9 个列拼出的形状（见 `_from_columns`），
        保证「库里有记录就有结果」，只是字段不如解析结果全。
    """
    wanted = [str(p) for p in (paths or []) if str(p or "").strip()]
    if not wanted:
        return {}

    _init()
    by_norm = {}                     # 归一化路径 → 该行（含 meta_json）
    checked = set()                  # 同一路径只查询和校验一次
    pending = [(p, _norm_key(p)) for p in wanted]
    # ⚠️ 库里的 `orig_path` / `stored_path` 是**原样 `abspath`**（盘符大写、大小写按
    #    文件系统），而 `_norm_key` 在 Windows 上会 `normcase` **折平大小写**
    #    （`C:` → `c:`）—— 直接 `WHERE stored_path = ?` 一条都匹配不上（实测踩过）。
    #    所以比较时对库里那一侧也折平；Linux 上 `normcase` 是 no-op，不加 LOWER，
    #    免得白白把列包进函数里。
    _col = "LOWER({0})" if os.name == "nt" else "{0}"
    with closing(_connect()) as con:
        for _p, nk in pending:
            if nk in checked:
                continue
            checked.add(nk)
            candidates = con.execute(
                f"SELECT * FROM invoices WHERE {_col.format('stored_path')} = ? ORDER BY id DESC",
                (nk,)).fetchall()
            if not candidates:
                candidates = con.execute(
                    f"SELECT * FROM invoices WHERE {_col.format('orig_path')} = ? ORDER BY id DESC",
                    (nk,)).fetchall()
            if candidates:
                # 同一原路径可先后入库不同内容；必须在所有候选中按当前 MD5
                # 找对应记录，不能只取最早的一行，也不能复用已被替换的文件缓存。
                try:
                    digest = _md5_file(_p)
                except OSError:
                    continue
                r = next((row for row in candidates if row["md5"] == digest), None)
                if r is not None:                    # ⚠️ 必须在 with 内读完行
                    by_norm[nk] = _cached_or_cols(r)

    return {p: by_norm[nk] for p, nk in pending if nk in by_norm}
