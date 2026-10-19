# -*- coding: utf-8 -*-
"""routes/pool.py — 「发票工作台」的统一池接口（v0.4 · R2）。

为什么要有它
------------
v0.3 里五个模块各自 scan 一遍：合并 / 火车票 / 住宿票 / 核对 / 重命名，
同一张票拖进两个模块就要解析两次，且各模块返回的行字段互不相同
（火车票给车次、住宿票给晚数、核对给判定…），无法放进同一张列表。

本模块提供**唯一一次**解析入口：`/api/pool/scan` 对每张票只调一次
`core.facets.extract_invoice_meta()`，把票种标签、金额、日期、销售方、
行程候选、核对结论、详情字段一次抽全，各功能（含 R1 统一列表）都从
这份池子里取数，不再各自扫一遍。

职责边界
--------
本文件只做「HTTP ⇄ core」的翻译与字段拼装，**不实现任何解析算法**：
  * 元数据        → core.facets.extract_invoice_meta()
  * 票种标签      → core.rename.get_invoice_type_label()
  * 抬头/税号比对 → core.parse._normalize_company_name / _normalize_tax_id

文件选择 / 上传复用 /api/merge/pick 与 /api/merge/upload（五个模块一直如此，
不再复制第三份同样代码）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, JSONResponse

# ── 让 `core` 可被导入（routes/ 与 core/ 同在 server/ 下）─────────────
if getattr(sys, "frozen", False):
    _SERVER = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
else:
    _SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

from core import attach as core_attach, airports, facets, journey_info  # noqa: E402
from core import trip as core_trip  # noqa: E402  v0.4 R4：行程准入判定的唯一真源
from core.parse import (  # noqa: E402
    _normalize_company_name,
    _normalize_tax_id,
    extract_invoice_fields,
    extract_pdf_texts,
    file_md5,
    ofd_display_fields,
)
from core.rename import get_invoice_type_label, mid_class  # noqa: E402
from . import _shared  # noqa: E402

# ── 发票库（第 7 轮 ④：导入即入库 + 解析结果缓存）────────────────────────
# ⚠️ try/except 兜住：发票库是**可选**能力，它坏了绝不许连累扫描本身 ——
#    scan 是工作台的第一入口，挂整个软件就废了。
try:                                          # pragma: no cover
    from core import vault as core_vault       # noqa: E402
except Exception:                             # noqa: BLE001
    core_vault = None

#: 缓存能不能拿来当「已解析结果」用的判据。
#: `vault.cached_meta_map` 对**老库（v1 历史行，meta_json 为 NULL）**会退回
#: 「9 个列拼出的窄形状」——那个形状**没有 `type` 键**，拿它当 `m` 会让票种退化成
#: 「未知」、`buyer_tax_id` 丢失（火车票核对会从「通过」掉成「待确认」）。
#: 所以只认**带 `type` 的完整解析结果**，窄形状一律当没缓存、重新解析。
_META_CACHE_GUARD_KEY = "type"

router = APIRouter()


@router.get("/airports")
def airport_catalog():
    return airports.catalog()

#: 详情侧栏「票面预览」允许内嵌的文件类型（这两个才是发票形态）
_PREVIEW_EXTS = (".pdf", ".ofd")

#: 预览的 Content-Type
_MEDIA = {".pdf": "application/pdf", ".ofd": "application/ofd"}


# ─────────────────────────── 字段拼装 ───────────────────────────

def _check_status(raw: str, expected_name: str, expected_tax_id: str, info: dict = None) -> dict:
    """按预设抬头 / 税号判定单张票的核对结论。

    与 `core.check.check_invoices()` 口径一致：
      * 两项都对 → pass（通过）
      * 任一项**取不到**（票面没识别出来）→ review（待确认）
      * 都取得到但不匹配 → fail（不符）

    只做比对，不重复实现解析 —— 字段一律来自 `core.parse.extract_invoice_fields`。

    `info` 是**可选**的已解析结果（2026-10-10 补）：`_pool_row` 现在把
    `extract_invoice_fields` 的结果只算一次并传进来，避免同一张票被解析两遍
    （原来 242 张票 = 484 次解析）。不传则退回老行为，独立调用本函数仍然正确。
    """
    if not expected_name and not expected_tax_id:
        return {"status": "unknown", "reason": "未设置核对预设"}

    if info is None:
        info = extract_invoice_fields(raw or "")
    reasons = []
    name_ok, tax_ok = True, True

    if expected_name:
        got = _normalize_company_name(info.get("buyer_name", ""))
        name_ok = bool(got) and got == _normalize_company_name(expected_name)
        if not got:
            reasons.append("购买方名称未识别")
    if expected_tax_id:
        got = _normalize_tax_id(info.get("tax_id", ""))
        tax_ok = bool(got) and got == _normalize_tax_id(expected_tax_id)
        if not got:
            reasons.append("税号未识别")

    if name_ok and tax_ok:
        return {"status": "pass", "reason": ""}
    if reasons:
        return {"status": "review", "reason": "；".join(reasons)}
    # 抬头完全匹配、只有税号对不上，且票面识别到的税号**位数就不是 18** → 降级 review。
    # 2026-10-11 修：实测 5 张真票的文字层里税号是 19 位（`91XXXXXXXXXXXXXXXXX`，
    # 比正确的 18 位多一个 `1`），而抬头一字不差。原来直接判「不符」，用户很可能
    # 把这 5 张真票当错票剔掉 —— 那是真的丢票丢钱。
    # ⚠️ 只降级、不猜值：绝不替他删掉一位数字去凑匹配，宁可标「待确认」让他人工看。
    if name_ok and not tax_ok:
        got_tax = _normalize_tax_id(info.get("tax_id", ""))
        if got_tax and len(got_tax) != 18:
            return {"status": "review",
                    "reason": "购买方税号疑似位数不对（票面识别到 %d 位：%s），请人工确认"
                              % (len(got_tax), got_tax)}
    # 具名说清到底哪一项不一致 —— 原本文案「抬头或税号与预设不一致」不区分字段，
    # 用户在列表里看不出错在哪（要的就是「哪个字段、票面值是什么」）。
    bad = []
    if not name_ok:
        bad.append("抬头「%s」" % (_normalize_company_name(info.get("buyer_name", "")) or "空"))
    if not tax_ok:
        bad.append("税号「%s」" % (_normalize_tax_id(info.get("tax_id", "")) or "空"))
    if not bad:
        return {"status": "fail", "reason": "抬头或税号与预设不一致"}
    return {"status": "fail", "reason": "与预设不一致：" + "、".join(bad)}


def _trip(m: dict) -> dict:
    r"""行程候选：把票面能支撑「出行」的字段挑出来供后续行程单消费。

    有明确出发地+到达地 → `mode='transit'`（可连线）；
    只有入住区间 → `mode='stay'`；两者都没有 → `available=False`。

    **实现已下移到 `core/trip.py::classify_trip`**（v0.4 R4 行程汇总单）：
    行程汇总页需要同一套准入判定，而 `core/` 不能 import `routes/`，所以把这段
    逻辑搬到 core 当唯一真源，这里只做薄委托，**行为一字未变**（`tests/` 有依赖）。
    历史地雷与「宁可空也不返回垃圾」的来龙去脉，随函数一起搬到了
    `core/trip.py::classify_trip` 的 docstring 里。
    """
    return core_trip.classify_trip(m)


def _pool_row(fp: str, idx: int, expected_name: str, expected_tax_id: str,
              meta: dict | None = None) -> dict:
    """解析单张票 → 工作台行。**每张票只解析一次**（文本层有缓存）。

    2026-10-10 修正：`extract_invoice_fields` 原来被 `_check_status` 再调一次
    （242 张票 → 484 次解析，违反本行上方那句注释的原意）。现在只算一次，
    结果同时喂给核对列与票种标签兜底。

    2026-10-12（第 7 轮 ④）：`meta` 非空 = 调用方已经把这张票的解析结果**从发票库
    缓存里**取出来了（`vault.cached_meta_map`），此时**不再调 facets 解析**。
    形状必须与 `facets.extract_invoice_meta` 的返回逐键相同，否则宁可传 None。
    """
    m = journey_info.effective_meta(
        meta if isinstance(meta, dict) and meta else facets.extract_invoice_meta(fp))
    # ── 第 7 轮 ④：缓存命中时连**票面原文**都从库里取，整张票不读盘、不解析 ──
    #    用户原话「再次从发票库导入，还要二次解析，应该解析过的发票缓存解析结果吧」——
    #    只省掉 facets 是不够的：`extract_pdf_texts`（pdfplumber 抽文字，最贵的一趟）
    #    原来照样跑。所以 `vault` 里那份解析结果额外带了 `__raw__` / `__info__`，
    #    命中时**一个字节都不读盘**，结果与重新解析逐字相同。
    c_raw = m.get("__raw__") if isinstance(m, dict) else None
    if isinstance(c_raw, str) and c_raw:
        raw, _err = c_raw, None
        c_info = m.get("__info__")
        info = c_info if isinstance(c_info, dict) and c_info else extract_invoice_fields(
            raw, source='ofd' if fp.lower().endswith('.ofd') else '')
    else:
        raw, _err = extract_pdf_texts(fp)      # 文本走 _text_cache，不会重复读盘
        # ⚠️ 只算一次：核对面 + 标签兜底共用（extra_meta 传了 info 就走 info 的键）
        info = extract_invoice_fields(raw or "", source='ofd' if fp.lower().endswith('.ofd') else '')
    if fp.lower().endswith('.ofd'):
        # 旧库的 __raw__/__info__ 可能来自旧解析器；只读复核，不修改原件或缓存。
        raw, _err = extract_pdf_texts(fp)
        info = extract_invoice_fields(raw or '', source='ofd')
        m = dict(m)
        m.update(ofd_display_fields(fp))
    # ① 铁路电子客票的购买方税号：票面印着「购买方名称:… 统一社会信用代码:…」，
    #    但通用增值税解析器按第③条「票面只有一个税号 ⇒ 归销售方」处理（对代开机票
    #    是对的），于是 46 张火车票的核对列从「通过」掉成「待确认（税号未识别）」。
    #    火车票的税号位置是锚死的 ⇒ 只在这一支里用 facets 取到的真值覆盖核对用的
    #    info，**不改** ③ 的通用口径。（`dict(...)` 是浅拷贝，不动缓存里的对象。）
    if m.get("buyer_tax_id"):
        info = dict(info)
        info["tax_id"] = m.get("buyer_tax_id")
    # ⚠️ 文本留着给「点一次干完」流水线的重命名阶段用（generate_invoice_filename
    #    需要票面原文）—— 缓存里存 raw，避免运行阶段再去读盘/重复解析。
    try:
        size = os.path.getsize(fp)
    except OSError:
        size = 0

    # 票种标签**只算一次**：下面的 `label` 与新的 `mid`（品名中类）共用这一个结果，
    # 避免为了 mid 再跑一遍 get_invoice_type_label 里的星号正则。
    inv_label = (get_invoice_type_label(
        raw or "", {"seller_name": info.get("seller_name") or m.get("seller", "")})
        or m.get("type", "未知"))

    row = {
        "id": idx,
        "path": fp,
        "filename": _shared.display_name(fp),
        "ext": os.path.splitext(fp)[1].lower().lstrip("."),
        "size": size,
        # ── 票种 ──
        "kind": m.get("kind", "unknown"),
        "type": m.get("type", "未知"),
        # ⚠️ 销售方简称兜底必须在这里自己拼出 seller_name：
        #    core.rename.get_invoice_type_label 的兜底段读的是 info['seller_name']，
        #    而 facets.extract_invoice_meta 返回的键叫 **seller**（不是 seller_name）。
        #    「把 m 当 info 传」看起来给了参数、实际键名对不上取到空 → 兜底整段静默跳过，
        #    没有 *分类*明细 星号结构的航空票会退化成「发票」。
        #    全库 355 份实测：传 m 变化 0（空改），拼 seller_name 变化 1（发票→机票）。
        #
        #    `or m.get("type", "未知")` 是历史遗留死代码：左半边最差也返回真值「发票」，
        #    右半段永远走不到。暂保留 —— 它记录了「本意想让 facets 的 type 兜底」这个
        #    意图，等 m['type'] 与本行标签口径统一（m['type'] 现过 _short_vat_type，
        #    属另一种口径，直接换会让 67 张标签变化）后再启用。
        "label": inv_label,
        # 品名中类（用户第 7 条）：票种大类之外的**第二层**，只在票种=「其他」时
        # 由前端显示。`None` = 这个 label 是票种/费用名（住宿票 / 火车票 / 快递费…），
        # 第一层已经说清楚了，前端继续显示 label 本身。
        # `label` **原样保留** —— 向后兼容 + 可回滚 + chip 的 title 还要用它。
        "mid": mid_class(inv_label),
        # ── 通用列 ──
        "amount": float(m.get("amount", 0.0) or 0.0),
        "date": m.get("date", "") or "",
        "seller": m.get("seller", "") or "",
        "buyer": m.get("buyer", "") or "",
        "invoice_no": m.get("invoice_no", "") or "",
        "parse_status": m.get("parse_status") or "",
        "parse_error": m.get("parse_error") or "",
        "journey": journey_info.journey(m),
        "missing_info": journey_info.missing_fields(m),
        "completeness": journey_info.completeness(m),
        "journey_edited": bool(m.get("journey_edited")),
        # ── 附属材料（行程单挂到本张发票上；没有则为 None）──
        "attachment": None,
        # ── 废弃标记：非电子发票匹配不上任何发票时为 True（仍留在列表里）──
        #    与核对模块的 review/pass/fail 是两套语义，**不可混用**：
        #    那三态描述「抬头税号对不对」，这个描述「这文件根本不是发票」。
        "discarded": False,
        # 废弃原因（前端按它出徽章文案）：
        #   ''           未废弃
        #   'noninvoice' 它不是发票（行程单 / 报销单据 / 报销辅助材料）—— scan() 里补齐
        # 注：'dup_pdf' / 'dup_copy' 两个取值随「副本不再留在列表」一起取消了 ——
        #     副本行现在整行不进列表，没有载体（见 _dedup_copies）。
        "discard_reason": "",
        # ── 契约 1（2026-10-11 用户第②条）：票面文字层里**确实读不出来** ──
        #    判据严格按 `not invoice_no and not date`（号码与日期两个都空）。
        #    True 只说明「这张票的这两项没被识别出来」，**不等于**不是发票 ——
        #    行程单（kind=noninvoice）本来就是这个形态，前端按自己的口径区分。
        #    放在行上（与 discard_reason 同级），detail 里不重复放。
        "unreadable": not (m.get("invoice_no") or "") and not (m.get("date") or ""),
        # 同票多副本：**只有代表件**带这个字段，列出被剔除副本的完整路径。
        # 前端详情靠它显示「+N 副本」（长度就是 N）；将来排查「这份文件去哪了」也靠它。
        "dup_paths": [],
        # ── 核对 ──
        "check": _check_status(raw, expected_name, expected_tax_id, info),
        # ── 行程候选 ──
        "trip": _trip(m),
        # ── 详情侧栏 ──
        "detail": {
            "kind": m.get("kind", "unknown"),
            "invoice_date": m.get("invoice_date", "") or "",
            "invoice_no": m.get("invoice_no", "") or "",
            "invoice_code": m.get("invoice_code", "") or "",
            "buyer": m.get("buyer", "") or "",
            "buyer_tax_id": m.get("buyer_tax_id", "") or "",
            "seller_tax_id": m.get("seller_tax_id", "") or "",
            "tax_amount": float(m.get("tax_amount", 0.0) or 0.0),
            "amount_without_tax": float(m.get("amount_without_tax", 0.0) or 0.0),
            "tax_rate": m.get("tax_rate", "") or "",
            "drawer": m.get("drawer", "") or "",
            "from_": m.get("from_", "") or "",
            "to_": m.get("to_", "") or "",
            "train": m.get("train", "") or "",
            "train_date": m.get("train_date", "") or "",
            "car_no": m.get("car_no", "") or "",
            "seat_no": m.get("seat_no", "") or "",
            "seat_class": m.get("seat_class", "") or "",
            "passenger": m.get("passenger", "") or "",
            "hotel_name": m.get("hotel_name", "") or "",
            "guest_name": m.get("guest_name", "") or "",
            "checkin": m.get("checkin", "") or "",
            "checkout": m.get("checkout", "") or "",
            # 票面实际明细（代订机票 / 网约车服务费…），仅展示用，不参与票种分组
            "item_name": m.get("item_name", "") or "",
            "items": m.get("items", []) or [],
            # ── 附属材料：网约车行程单挂到本张发票上时的行程明细 ──
            #    行程单本身不是发票：不占列表一行、金额不进合计、地点不进地图。
            "attachment": None,
            "attachment_note": "",
            # ── 网约车行程单：地点/时间/里程在这里展示，金额走 trip_amount（不进发票合计）
            "trips": m.get("trips", []) or [],
            "trip_amount": float(m.get("trip_amount", 0.0) or 0.0),
            "trip_count": int(m.get("trip_count") or 0),
            "channel": m.get("channel", "") or "",
            # ── 废弃详情：与行上的字段同源，侧栏从 detail 读 ──
            #    （原来这里还有 'dup_of'，随「副本不进列表」一起删掉了 —— 没有载体）
            "discard_reason": "",
        },
        # ── 内部字段（下划线开头：前端不消费，scan_id 缓存存取用）──
        #    ⚠️ 里存的是**票面原文**，让「点一次干完」的重命名阶段不必重新读盘，
        #       也不必把 242 张票的 info 字典全留在内存里（一份约 1KB）。
        #       同一行可能既有 '发票' 又有 '行程单' 两页文本，scan 阶段由
        #       extract_pdf_texts 拼好，这里原样透传。
        "_raw": raw or "",
        # ── 解析结果整份留着，给「点一次干完」的核对/重命名阶段直接用 ──
        #    ⚠️ 2026-10-10 追加：上面 L169 那句「每张票只解析一次」原来只管 scan 阶段，
        #       流水线跑的时候又调了一次 extract_invoice_fields（同一张票两遍）。
        #       这里把**已经算好的那份**挂到行上，流水线 `_row_info()` 直接取，
        #       全程零重复解析。
        #       内存口径：整份 info 约 1KB，而同一行的 `_raw` 票面原文是 2~5KB ——
        #       真正的大头是 `_raw`（重命名必须用它做火车票站名/星号明细提取，
        #       删不掉），所以多存一份 info 只是零头，不构成压力。
        #       只摘四个键（amount/seller_name/invoice_date/item_name）的写法**不能要**：
        #       核对还要 buyer_name / tax_id，缺键会静默退化成「未识别→review」，
        #       与 `/api/check/run` 的结论悄悄不一致。
        "_info": info if isinstance(info, dict) else {},
        # ── 第 7 轮 ④：facets 的**完整解析结果**（41 键）原样留着 ──
        #    扫描结束时拿它喂 `vault.ingest(paths, metas=...)` —— 发票库存的就是这份
        #    「解析过的结果」，下次从发票库导入时直接读回来，**不再解析第二遍**。
        #    ⚠️ 必须与 `facets.extract_invoice_meta` 逐键同形：窄形状（9 键）存进去，
        #       取回来会让票种退化（见 _META_CACHE_GUARD_KEY 的注释）。
        "_meta": m if isinstance(m, dict) else {},
    }
    return row


# ── 同票多副本去重（PDF / OFD）─────────────────────────────────
# 用户原话（2026-10-10）：
#   「同一张发票可能会有 PDF 格式，有 OFD 格式，如果重复的话，把其中的 OFD 废弃掉，
#     可以给它标注『检测到重复的 PDF 发票』」
# ⚠️ 同日稍晚他改了展示口径：副本**不再留在列表里标灰**，而是「直接从列表里消失，
#    只在正式那行的详情里看到『+2 副本』」—— 见 _dedup_copies() 的 docstring。
# 📌 .xml 已于同日从采集层摘掉（parse.DEFAULT_EXTS 不再收 .xml），所以这里通常只有
#    pdf / ofd 两种格式并存；_COPY_RANK 里不再列 .xml，未知扩展名一律排最后。

#: 同一张票多种格式并存时，留哪一份：pdf > ofd。
#: 同一优先级里再按「路径更短者优先」定序 —— 稳定可复现，不会因为目录遍历顺序变化
#: 而换代表件（否则今天留 a.pdf、明天留 b.pdf，用户看到的文件会飘）。
_COPY_RANK = {".pdf": 0, ".ofd": 1}


def _row_path(row: dict) -> str:
    return str(row.get("path") or row.get("filename") or "")


def _row_ext(row: dict) -> str:
    return os.path.splitext(_row_path(row))[1].lower()


def _copy_key(row: dict):
    """同票归并键：``('NO', 发票号码)``；发票号码为空时回退 ``('STEM', 主干名, 金额)``。

    ⛔ 键里必须含**发票号码**：只按金额或只按文件名归并，会把不同的发票并成一张
       （同一趟出差两张金额相同的票太常见了）。
    ⛔ **NO 分支不带金额**（2026-10-10 修正）：发票号码本身唯一，同一张票的 pdf / ofd /
       xml 共用同一个号码才是常态。而且三种格式的解析金额**可能就是不一致的**
       —— 那正是我们这两天在修的 OFD 解析老问题（见 19119ce）。带上金额的话，
       恰恰会放过**最该废弃的那一份**（解析质量差的那份对不上金额 → 不归并 → 它活下来），
       后果是同一张票 PDF 行 + OFD 行都进合计 → **同一张票被算两遍**。
       实测：全样本里有 1 组三副本（ofd+pdf+xml）号码逐字相同、买卖方/日期/票种全同，
       仅金额不一致，就是被这条规则漏掉的。
    ✅ 「宁可漏并，不可误并」只适用于 **STEM 分支**：没有号码时，文件名主干完全可能
       撞车（同名不同票），这时金额是唯一能佐证「是不是同一张」的信息，必须留着。
       所以两个分支的取舍**刻意不同**，别把金额加回 NO 分支。
    """
    no = str(row.get("invoice_no") or "").strip()
    if no:
        return ("NO", no)
    amount = round(float(row.get("amount") or 0.0), 2)
    stem = os.path.splitext(os.path.basename(_row_path(row)))[0]
    return ("STEM", stem.lower(), amount)


def _dedup_copies(rows: list, warnings: list | None = None) -> list:
    """同一张发票的 PDF / OFD 多副本 → **只留一份代表件，副本行从列表里剔除**。

    ⚠️ 口径是用户 2026-10-10 改定的（原话：「让那几样直接从列表里消失，只在正式那行的
       详情里看到『+2 副本』」）。他此前选过「标灰留在列表」，**后来改主意了** ——
       所以副本行不再是「discarded 行」，而是**根本不进列表**：
       详情里的「+N 副本」靠代表件上的 ``dup_paths``（它的长度就是 N）。

    与 ``core_attach.attach_noninvoices`` 的分工（两件事不搅在一起）：
      · 那一步管「非电子发票（行程单 / 报销单据）挂到宿主发票上」
      · 这一步管「同一张发票的多种格式副本」
    所以这里只看 ``kind != 'noninvoice'`` 且尚未 discarded 的行。

    ⛔ 位置是硬约束：必须在**行装配层**（本函数由 scan() 调用）。
       放到采集层（collect_files / 解析阶段）会让被去重的文件**永远不被解析**；
       只在 web/js/app.js 里分组则会撞 tests/verify_v04_batch1.mjs 里
       「表格渲染行数 == wb.rows 长度」那条护栏。

    这里**不需要**给副本清零金额：整行都不在列表里，自然不进合计
    （`total_amount` 是对返回后的行求和）。

    返回：**新列表**（代表件 + 所有未参与归并的行），行数 = 原行数 − 被剔除的副本数；
    id 已按 0..n-1 重排（前端拿 id 当行键，留空洞会错位）。
    """
    groups = {}
    for r in rows:
        if r.get("kind") == "noninvoice" or r.get("discarded"):
            continue
        groups.setdefault(_copy_key(r), []).append(r)

    dropped = set()                       # 用 id() 记录：dict 不可哈希，且路径可能重复
    for grp in groups.values():
        if len(grp) < 2:
            continue
        # 代表件：pdf > ofd（同级取路径更短者）—— 稳定可复现，代表件不会今天明天不一样
        grp.sort(key=lambda r: (_COPY_RANK.get(_row_ext(r), 9),
                                len(_row_path(r)), _row_path(r)))
        rep = grp[0]
        if grp[0].get("invoice_no"):
            differences = []
            if len({round(float(r.get("amount") or 0), 2) for r in grp}) > 1:
                differences.append("金额")
            if len({str(r.get("seller") or "").strip() for r in grp}) > 1:
                differences.append("销售方")
            if differences:
                message = (f"发票号 {rep['invoice_no']} 的同票文件"
                           f"{'、'.join(differences)}不一致；已自动归并，"
                           "请核对保留的代表件及下方副本")
                rep["dup_warnings"] = [message]
                if warnings is not None:
                    warnings.append(message)
        # 前端详情靠这个列表显示「+N 副本」，长度就是 N —— 绝不能丢
        rep["dup_paths"] = [_row_path(r) for r in grp[1:]]
        dropped.update(id(r) for r in grp[1:])

    out = [r for r in rows if id(r) not in dropped]
    for i, r in enumerate(out):
        r["id"] = i
    return out


def _vault_ingest(rows: list, errors: list) -> int:
    """把这一批**代表件**灌进发票库（用户 m03155 第④条 A 半段）。

    为什么在这里（而不是等 `/api/pool/run`）：
      用户原话「现在只有把发票导入完，点击开始后才导入到发票库，应该只要导入了
      就进入发票库」——导入的落点就是 `POST /api/pool/scan`，钩子挂在这里才对得上。

    三条口径
    --------
    * **只灌代表件**：调用点在全批 `_dedup_copies` 之后，同票副本（同号 PDF/OFD）
      已经不占行了 —— 同一张发票在库里存两份是噪音。
    * **metas 用行上算好的 `_meta`**：入库阶段**零解析**（`vault.ingest` 拿到 metas
      就直接写 `meta_json`，一次 facets 都不调）。
    * **失败绝不许挡住导入**：发票库是可选能力，异常只记进 `errors`（工作台会显示），
      扫描本身照常返回。
    """
    if not rows:
        return 0
    paths = [r["path"] for r in rows if r.get("path")]
    if not paths:
        return 0
    if core_vault is None:
        errors.append({"filename": "（发票库入库）", "reason": "发票库不可用，本批文件未入库"})
        return len(paths)
    # `__raw__` / `__info__` 是给「下次从发票库导入」用的（见 _pool_row 的缓存命中分支）：
    # 带上它们，下次这张票连 pdfplumber 抽文字那趟都省了。⚠️ 在**这里**拼、不写回行上，
    # 免得同一份原文在内存里存两遍（171 张 × 3KB ≈ 0.5MB，纯浪费）。
    metas = {}
    for r in rows:
        if not r.get("path"):
            continue
        base = r.get("_meta") if isinstance(r.get("_meta"), dict) else {}
        if not base:
            continue
        entry = dict(base)
        entry["__raw__"] = r.get("_raw") or ""
        entry["__info__"] = r.get("_info") if isinstance(r.get("_info"), dict) else {}
        metas[r["path"]] = entry
    try:
        result = core_vault.ingest(paths, metas=metas or None)
        if result.get("failed"):
            reasons = result.get("errors") or []
            for reason in reasons:
                errors.append({"filename": "（发票库入库）", "reason": str(reason)})
            if not reasons:
                errors.append({"filename": "（发票库入库）",
                               "reason": f"{result['failed']} 个文件入库失败"})
        return int(result.get("failed") or 0)
    except Exception as e:                           # noqa: BLE001
        errors.append({"filename": "（发票库入库）", "reason": str(e)})
        return len(paths)


# ── scan 结果进程内缓存（供 /api/pool/run 用）────────────────────
# 为什么必须缓存而不是让 run 重新扫：
#   1. 每张票解析一次是硬要求（见 _pool_row 注释）；run 重新扫 = 解析第三遍；
#   2. 前端 selected_ids 是**扫描那一刻**的下标；重扫会让下标漂移，
#      用户勾了第 3 行、实际处理第 7 行 —— 这是数据事故，不是性能问题；
#   3. 白名单校验需要「这次扫描究竟见过哪些路径」，只有快照能给。

_SCAN_TTL = 30 * 60        # 30 分钟
_SCAN_MAX = 20             # LRU 上限 20 份扫描快照
_SCAN_CACHE = {}           # scan_id -> {"rows": [...], "created_at": float, "paths_set": frozenset}
_SCAN_LOCK = threading.Lock()


def _scan_cache_put(scan_id: str, rows: list, paths_set: frozenset) -> None:
    """写入扫描快照；超上限时按 created_at 淘汰最旧的一份。"""
    with _SCAN_LOCK:
        now = time.time()
        # 先清过期项，再判上限 —— 否则 20 个死快照能把活快照挤掉
        for k in [k for k, v in _SCAN_CACHE.items() if now - v["created_at"] > _SCAN_TTL]:
            _SCAN_CACHE.pop(k, None)
        _SCAN_CACHE[scan_id] = {"rows": rows, "created_at": now, "paths_set": paths_set}
        while len(_SCAN_CACHE) > _SCAN_MAX:
            oldest = min(_SCAN_CACHE, key=lambda k: _SCAN_CACHE[k]["created_at"])
            _SCAN_CACHE.pop(oldest, None)


def _scan_cache_get(scan_id: str):
    """取扫描快照；不存在或已过期返回 (None, 原因)。

    过期就**删掉**并返回明确原因：进程内状态易失，重启即丢，
    必须让前端能提示「请重新扫描」，不能静悄悄地拿旧下标去改盘上文件。
    """
    if not scan_id:
        return None, "缺少 scan_id"
    with _SCAN_LOCK:
        item = _SCAN_CACHE.get(scan_id)
        if not item:
            return None, "扫描快照不存在或服务已重启，请重新扫描"
        if time.time() - item["created_at"] > _SCAN_TTL:
            _SCAN_CACHE.pop(scan_id, None)
            return None, "扫描快照已过期（超过 30 分钟），请重新扫描"
        return item, ""


# ── 契约 2 / 3：导入解析的**真实进度**（2026-10-11 用户第⑦条）──────────────
# 范式照抄 `server/routes/pipeline.py:58-110` 的 `_Progress`
# （`_PROGRESS_MAX` / `_PROGRESS` / `_PROGRESS_LOCK` + register/put/snapshot +
# 超额淘汰），但**不能直接 import** —— pipeline.py 反过来 `from . import pool as
# pool_routes`，共用一份注册表会成环。前端按 `job_id` 轮询，拿不到就回退转圈。
_SCAN_PROGRESS_MAX = 20
_SCAN_PROGRESS = {}
_SCAN_PROGRESS_LOCK = threading.Lock()


class _ScanProgress:
    """单次导入解析的进度快照（同步 def 的解析循环里每张票 put 一次）。"""

    def __init__(self):
        self.data = {"index": 0, "total": 0, "current": "", "done": False, "elapsed_ms": 0}
        self._t0 = time.time()

    def register(self, job_id):
        """把**同一个 dict 对象**挂进注册表，put 原地更新、轮询侧立刻可见。"""
        if not job_id:
            return
        with _SCAN_PROGRESS_LOCK:
            _SCAN_PROGRESS[job_id] = self.data
            # 超额淘汰：只踢别的已完成/旧任务，绝不踢自己
            while len(_SCAN_PROGRESS) > _SCAN_PROGRESS_MAX:
                for k, v in list(_SCAN_PROGRESS.items()):
                    if v is not self.data:
                        _SCAN_PROGRESS.pop(k, None)
                        break
                else:
                    break

    def put(self, **kw):
        with _SCAN_PROGRESS_LOCK:
            self.data.update(kw)
            self.data["elapsed_ms"] = int((time.time() - self._t0) * 1000)


def _scan_progress_snapshot(job_id):
    with _SCAN_PROGRESS_LOCK:
        data = _SCAN_PROGRESS.get(job_id)
        return dict(data) if data else None


# ─────────────────────────── 接口 ───────────────────────────

@router.post("/scan")
def scan(payload: dict):
    """统一解析入口：一次扫全字段，各功能从池中取数。

    ⚠️ 同步 def：解析是 CPU 密集的 pdfplumber 循环，写成 `async def` 会把
    事件循环堵死（进度轮询 / /api/health 全程超时）。FastAPI 对同步 def
    自动丢线程池 —— 现有 7 个 run 都是这个写法，沿用。

    入参
    ----
    paths           发票文件路径（可含目录，自动展开）
    expected_name   预设购买方名称（可选；给了才算核对列）
    expected_tax_id 预设纳税人识别号（可选）
    job_id          导入进度标识（**可选**，前端生成，形如 scan_<ts>_<rand>）。
                    带了就为该 id 注册进度、每解析一张更新一次（见契约 2/3，
                    轮询接口 `GET /scan/status`）；不带行为与以前完全一致。

    返回
    ----
    scan_id / rows / count / total_amount / errors
    """
    payload = payload or {}
    paths = _shared.resolve_inputs(payload.get("paths") or [])
    expected_name = (payload.get("expected_name") or "").strip()
    expected_tax_id = (payload.get("expected_tax_id") or "").strip()

    # 契约 2：job_id 缺省 → prog=None，下面所有进度动作整体跳过，行为零变化。
    job_id = str(payload.get("job_id") or "").strip()
    prog = None
    if job_id:
        prog = _ScanProgress()
        prog.register(job_id)
        prog.put(index=0, total=len(paths), current="", done=False)

    rows, errors, warnings = [], [], []

    # ── 第 7 轮 ④-B：这批路径里有没有**已经解析过、已经进了发票库**的 ──
    #    有就直接拿库里的解析结果，`facets.extract_invoice_meta`（pdfplumber 抽文字
    #    那一趟）整张省掉。命不中的照旧解析，行为与以前一致。
    cached_meta = {}
    if core_vault is not None:
        try:
            _raw_map = core_vault.cached_meta_map(paths) or {}
            cached_meta = {p: v for p, v in _raw_map.items()
                           if isinstance(v, dict) and _META_CACHE_GUARD_KEY in v}
        except Exception as e:                       # noqa: BLE001
            # 发票库坏了**不许**挡住导入 —— 退化成「这一批全部重新解析」。
            cached_meta = {}
            errors.append({"filename": "（发票库缓存）", "reason": str(e)})

    for i, fp in enumerate(paths):
        if prog is not None:
            # index 从 1 开始；current 用 _shared.display_name 口径（剥上传前缀）
            prog.put(index=i + 1, total=len(paths), current=_shared.display_name(fp))
        try:
            rows.append(_pool_row(fp, i, expected_name, expected_tax_id,
                                  meta=cached_meta.get(fp)))
        except Exception as e:                       # 单张失败不拖垮整批
            errors.append({"filename": os.path.basename(fp), "reason": str(e)})

    if prog is not None:
        prog.put(index=len(paths), total=len(paths), current="", done=True)

    # ── 非电子发票（行程单 / 报销单据 / 报销辅助材料）：先挂接、再标废弃 ──
    # ⚠️ 两步顺序不能反：先挂后判，否则宿主已经不在候选里了。
    #    这一步必须在**全批行都建好之后**做 —— core.attach 要拿金额/文件名/日期
    #    跟其它记录比，单文件解析阶段（facets.extract_invoice_meta）看不到全局。
    #    挂上的进宿主发票（自己不占一行），挂不上的标 discarded=True 留在列表里。
    rows = core_attach.attach_noninvoices(rows)

    # 废弃原因口径补齐：attach 只标 discarded=True，不写原因；而前端要按原因出徽章文案，
    # 所以这里给它补上 'noninvoice'。（同票副本不在这里，它们整行不进列表。）
    for r in rows:
        if r.get("discarded") and not r.get("discard_reason"):
            r["discard_reason"] = "noninvoice"
            r["detail"]["discard_reason"] = "noninvoice"

    # ── 同票多副本去重（PDF / OFD）：保留代表件，副本行整行剔除 ──
    # ⚠️ 必须在 attach_noninvoices **之后**：那一步会剔除挂上的材料并重排 id，
    #    先把列表定下来、再对剩下的发票做去重，两边不会打架。
    rows = _dedup_copies(rows, warnings)

    # ── 第 7 轮 ④-A：导入了就进发票库（不再等「点开始」）──────────────
    #    ⚠️ 必须放在 `_dedup_copies` **之后**：同票副本已经不占行了，只把代表件
    #       灌进库 —— 同一张发票在库里存两份（PDF 一份、OFD 一份）纯属噪音。
    vault_failed = _vault_ingest(rows, errors)
    if core_vault is not None:
        try:
            ids = core_vault.item_ids_for_paths(r["path"] for r in rows)
            for row in rows:
                row["vault_id"] = ids.get(row["path"])
        except Exception as exc:  # noqa: BLE001
            errors.append({"filename": "（发票库关联）", "reason": str(exc)})

    # ── 快照入缓存：/api/pool/run 靠它白名单校验 + 复用解析结果 ──
    scan_id = str(uuid.uuid4())
    paths_set = frozenset(r["path"] for r in rows)
    _scan_cache_put(scan_id, rows, paths_set)

    # ⛔ 响应里**不能**带下划线内部字段（`_raw` 是每张票的全文、`_info` 是解析结果）：
    #    实测 46 张火车票 130.5KB → 去掉后 65.6KB（`_raw` 单独就 26.8KB），
    #    而且这些票面文字没必要发到浏览器。缓存里那份**保留**原始行（run 要复用解析），
    #    所以这里只做浅拷贝剥字段，绝不改动缓存里的对象。
    return {
        "ok": True,
        "scan_id": scan_id,
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows],
        "count": len(rows),
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "errors": errors,
        "vault_failed": vault_failed,
        "warnings": warnings,
        # 只返回默认路径供界面展示；导入发票时不创建桌面输出目录。
        "default_out": _shared.default_out_dir(),
    }


@router.get("/scan/status")
def scan_status(job_id: str = ""):
    """契约 3：轮询 `POST /scan` 的真实进度。

    返回固定形状（**不抛 500**）：
    `{"ok": true, "found": bool, "index": int, "total": int,
      "current": str, "done": bool, "elapsed_ms": int}`

    `found=false` = 这个 job_id 没见过（前端据此回退到普通转圈）；
    `current` = 当前正在解析的文件名（`_shared.display_name` 口径）。
    """
    jid = (job_id or "").strip()
    snap = _scan_progress_snapshot(jid) if jid else None
    if not snap:
        return {"ok": True, "found": False, "index": 0, "total": 0,
                "current": "", "done": False, "elapsed_ms": 0}
    return {
        "ok": True,
        "found": True,
        "index": int(snap.get("index") or 0),
        "total": int(snap.get("total") or 0),
        "current": str(snap.get("current") or ""),
        "done": bool(snap.get("done")),
        "elapsed_ms": int(snap.get("elapsed_ms") or 0),
    }


# ── 契约 4：PDF 第 1 页栅格化成 PNG ────────────────────────────────────────
# `max` = 输出图像最长边像素上限，默认 1400，夹取到 [_THUMB_MIN, _THUMB_MAX]。
_THUMB_DEFAULT = 1400
_THUMB_MIN = 200
_THUMB_MAX = 4000


def _preview_allowed(path: str) -> bool:
    """仅允许当前有效扫描快照中的发票进入预览接口。"""
    target = os.path.normcase(os.path.realpath(path))
    now = time.time()
    with _SCAN_LOCK:
        return any(
            now - scan["created_at"] <= _SCAN_TTL
            and any(os.path.normcase(os.path.realpath(row.get("path") or "")) == target
                    for row in scan["rows"])
            for scan in _SCAN_CACHE.values()
        )


@router.get("/thumb")
def thumb(path: str = "", max: str = ""):
    """契约 4：把本机 PDF 的第 1 页栅格化成 PNG 返回（列表行的小图 / 详情缩略图）。

    - 成功：HTTP 200、`Content-Type: image/png`、`Content-Disposition: inline`，
      **响应体就是 PNG 字节**。
    - 失败（文件不存在 / 不是 PDF / OFD / 渲染异常）：HTTP 404 +
      JSON `{"ok": false, "error": "..."}`。
    - 渲染用 **pypdfium2**（`requirements.txt` 已声明 `pypdfium2>=4.30.0`）。
      ⛔ 不用 PyMuPDF/fitz —— 项目里 0 处引用，不引入新依赖。
    - 只读：不往项目里写任何缓存（单页渲染很快，直接吐内存字节）。
    """
    p = (path or "").strip().strip('"')
    if not p or not os.path.isfile(p) or not _preview_allowed(p):
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    ext = os.path.splitext(p)[1].lower()
    if ext == ".ofd":
        # 轻量退路：只认同目录、同主干名的 PDF；不安装渲染器，也不改动 OFD。
        from core.parse import _sibling_pdf_path
        sibling = _sibling_pdf_path(p)
        if not sibling or os.path.realpath(os.path.dirname(sibling)) != os.path.realpath(os.path.dirname(p)):
            return JSONResponse({"ok": False, "error": "这份 OFD 没有同名 PDF，暂无法页内预览"}, status_code=404)
        p = sibling
    elif ext != ".pdf":
        return JSONResponse({"ok": False, "error": "仅支持 PDF 与带同名 PDF 的 OFD"}, status_code=404)
    try:
        limit = int(str(max).strip())
    except (TypeError, ValueError):
        # 传空 / 传垃圾（?max=abc）→ 用默认值，不要拿 422 去烦前端
        limit = _THUMB_DEFAULT
    # ⚠️ 形参名就叫 `max`（契约写死 `?max=`），这里不能用内建 max()/min() ——
    #    参数把它遮住了（TypeError: 'int' object is not callable）。手写夹取。
    if limit < _THUMB_MIN:
        limit = _THUMB_MIN
    elif limit > _THUMB_MAX:
        limit = _THUMB_MAX

    try:
        import io

        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(p)
        try:
            if len(doc) < 1:
                raise ValueError("PDF 没有页面")
            page = doc[0]
            try:
                w, h = page.get_size()
                _w, _h = float(w or 0.0), float(h or 0.0)
                longest = (_w if _w > _h else _h) or 1.0
                # pdfium 的 render(scale) 是相对 72dpi 的倍数：输出像素 ≈ pt × scale。
                # `- 0.5` 是给 pdfium 的取整留余量：直接 `limit / longest` 实测会
                # 输出 4001（上限 4000 被顶破 1px，round/ceil 都会）。
                bitmap = page.render(scale=(limit - 0.5) / longest)
                try:
                    img = bitmap.to_pil()
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    png = buf.getvalue()
                finally:
                    try:
                        bitmap.close()
                    except Exception:
                        pass
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        finally:
            try:
                doc.close()
            except Exception:
                pass
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"渲染失败：{e}"}, status_code=404)

    return Response(content=png, media_type="image/png",
                    headers={"Content-Disposition": "inline"})


@router.get("/file")
def file(path: str = ""):
    """详情侧栏的「票面预览」：把本机发票文件交给浏览器内嵌渲染。

    本机单人使用、只监听 127.0.0.1，但仍守住两道边界：
      1. 只允许 .pdf / .ofd（发票形态），其他扩展名一律拒绝；
      2. 必须是确实存在的文件。
    """
    p = (path or "").strip().strip('"')
    ext = os.path.splitext(p)[1].lower()
    if ext not in _PREVIEW_EXTS or not os.path.isfile(p) or not _preview_allowed(p):
        return JSONResponse({"ok": False, "error": "文件不可用（仅支持本机 PDF / OFD）"},
                            status_code=404)
    # ⚠️ `inline` 不是风格问题（用户 2026-10-11 第 5 条）：详情侧栏用 <iframe> 内嵌
    # 这张 PDF，而 FileResponse 默认发 `Content-Disposition: attachment` —— 浏览器
    # 不渲染，直接弹下载。用户每点一行就被下载一次，而原件本来就在他自己盘上。
    # OFD 也改 `inline`（用户 2026-10-11 复述第 1 条：「即使是 OFD 也不自动下载，
    # 浏览器打不开就打不开吧」）—— 弹下载比打不开更烦人。
    return FileResponse(p, media_type=_MEDIA.get(ext, "application/octet-stream"),
                        filename=os.path.basename(p),
                        content_disposition_type="inline")
