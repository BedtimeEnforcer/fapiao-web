"""按本次选择生成处理结果页——单文件、纯函数、离线、零外部请求。

勾选模块点「开始」后直接打开 HTML 结果页；页面仅展示已选择的模块。

设计要点
- 只调 `report_html.render_page` 出骨架，与行程单 / 核对页共用同一套 CSS 与脚本，
  所以视觉语言天然一致；整页**零外部 URL**（无 CDN、无远程字体）。
- 品牌色是天蓝 `#38BDF8` / 柔光 `#7DD3FC`（副色青 `#22D3EE`）；琥珀金 `#F59E0B`
  **只用于语义警告**——行程单 2026-10-11 把品牌色从金改成天蓝，就是为了让金色
  只表示「警告」这一个意思，这里继续守这条规矩。
- 纯函数：不读盘、不写盘、不联网。数据全部来自 `/api/pool/run` 的返回体
  （`payload`）与它的 `warnings`。
- 金额口径：`None` / 空串 / 非数字 → `—`；`0.0` 照实渲染 `￥0.00`。行内金额
  允许合法为 0（行程单行），所以「未知」与「零」必须看得出区别。
"""

from __future__ import annotations

import json
import math
import os

from . import report_html as R
from .report_html import esc as _e

try:  # 大写金额只是装饰，取不到不能让整页出不来
    from .parse import amount_upper as _amount_upper
except Exception:  # pragma: no cover - 防御性
    _amount_upper = None

__all__ = ["build_check_report_html", "CHECK_REPORT_PALETTE", "report_icon_svg"]

#: 与行程单同源的深色 + 天蓝品牌色。金色只出现在下面的「警告」规则里。
CHECK_REPORT_PALETTE = {
    "bg_from": "#060A14",
    "bg_to": "#0D1526",
    "panel": "#0F1A2E",
    "panel_border": "#22314F",
    "accent": "#38BDF8",        # 品牌色：天蓝
    "accent_soft": "#7DD3FC",   # 柔光天蓝（标题渐变起点）
    "accent2": "#22D3EE",       # 副色青（渐变终点 / 查重）
    "text": "#E9EFFA",
    "text_dim": "#93A4C4",
    "text_faint": "#5C6E8F",
    "ok": "#34D399",
    "warn": "#F59E0B",          # ⚠️ 语义警告专用，不是品牌色
    "err": "#F87171",
    "muted": "#5C6E8F",
}

_DASH = R.DASH


# ─────────────────────────── 小工具 ───────────────────────────

def _txt(v) -> str:
    """转字符串（None → ""）。"""
    return "" if v is None else str(v)


def _dash(v) -> str:
    """空值显示 `—`（不显示空单元格）。"""
    s = _txt(v).strip()
    return _e(s) if s else _DASH


def _money(v) -> str:
    """金额：未知 → `—`，已知（含 0.0）→ `￥1,234.00`。

    ⚠️ 不能直接用 `R.fmt_money`：它把 `None` 也当 0，会把「没读到金额」和
    「金额就是 0」混成同一个 `￥0.00`。
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return _DASH
    if isinstance(v, bool):
        return _DASH
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _DASH
    if not math.isfinite(f):
        return _DASH
    return f"￥{f:,.2f}"


def _num(v) -> float:
    """数值化（失败 → 0.0），只用于求和 / 计数。"""
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _as_text(x) -> str:
    """`warnings` / `issues` 里的元素可能是 str，也可能是 dict，统一成一行文字。"""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("error", "reason", "msg", "message", "detail"):
            if x.get(k):
                return _txt(x[k])
        return json.dumps(x, ensure_ascii=False)
    return _txt(x)


def _merge_unique(*lists) -> list:
    """按首次出现顺序去重合并（`warnings` 与 `payload["warnings"]` 常是同一个列表）。"""
    out, seen = [], set()
    for lst in lists:
        for item in (lst or []):
            s = _as_text(item).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def _short(p, n: int = 46) -> str:
    """长路径缩到尾部，避免表格被一条路径撑爆。"""
    s = _txt(p)
    base = os.path.basename(s.replace("\\", "/")) or s
    return base if len(base) <= n else base[: n - 1] + "…"


def report_icon_svg(size: int = 30, palette: dict | None = None) -> str:
    """页眉图标：单据 + 放大镜（核对）。

    刻意**不写** `xmlns="http://www.w3.org/2000/svg"`：内联在 HTML 里的 `<svg>`
    由 HTML 解析器自动放进 SVG 命名空间，不需要声明；写了反而会在产物里留下
    一个 `http://`，验收时无法断言「零外部 URL」。
    """
    p = palette or CHECK_REPORT_PALETTE
    return (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        f'stroke="{p["accent"]}" stroke-width="1.7" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">'
        f'<path d="M14.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7.5z"/>'
        f'<path d="M14.5 3v4.5H19"/>'
        f'<path d="M8.5 11.5h4M8.5 14.5h2.5"/>'
        f'<circle cx="15.2" cy="15.2" r="2.6" stroke="{p["accent_soft"]}"/>'
        f'<path d="m17.1 17.1 2 2" stroke="{p["accent_soft"]}"/>'
        f'</svg>'
    )


# ─────────────────────────── 判读 ───────────────────────────

#: 核对状态 → (pills 类, 中文结论)。`unknown` 既表示「没做核对」也表示「读不出来」，
#: 两种都归到「无法判定」这一档，靠备注列说清具体原因。
_CHECK_LABEL = {
    "pass": ("ok", "通过"),
    "fail": ("err", "不符"),
    "review": ("warn", "待确认"),
    "unknown": ("dim", "无法判定"),
}


def _verdict(row: dict) -> tuple:
    """一行的核对结论 → `(css 类, 结论文字, 结论原因)`。

    优先级：副本（本步被剔除）> 不计入（行程单/材料）> 核对状态。
    被剔除的行虽然也带着扫描期算出的 `check`，但那份结论与本次运行无关，
    显示出来只会误导，所以先让位给「副本」。
    """
    run_status = _txt(row.get("run_status")).strip()
    if run_status == "dup_skipped":
        return "dim", "副本已剔除", "与代表件是同一张票，未计入金额、未参与合并"
    if row.get("discarded"):
        return "dim", "不计入", _txt(row.get("discard_reason")).strip() or "未匹配到行程单/材料"
    chk = row.get("check") or {}
    st = _txt(chk.get("status")).strip() or "unknown"
    cls, text = _CHECK_LABEL.get(st, ("dim", "无法判定"))
    return cls, text, _txt(chk.get("reason")).strip()


def _dup_basis(row: dict) -> str:
    """归档同一张票的判据（与 `pool._copy_key` 同源口径）。"""
    no = _txt(row.get("invoice_no")).strip()
    if no:
        return f"发票号 {no} 相同"
    return "文件名主干与金额相同"


# ─────────────────────────── 片段 ───────────────────────────

def _pill(cls: str, text: str) -> str:
    """结论 pill。原因**不放在 pill 里**——表格已经有「备注（核对原因）」列，
    两处都写同一句话会显得啰嗦（截图实测过）。"""
    return f'<i class="vd {cls}">{_e(text)}</i>'


def _panel(title: str, body: str, hint: str = "", cls: str = "") -> str:
    h = f'<div class="hint">{_e(hint)}</div>' if hint else ""
    return (
        f'<div class="panel reveal {cls}">'
        f"<h2>{_e(title)}</h2>{h}{body}</div>"
    )


def _kv_rows(pairs) -> str:
    return '<div class="kv-box">' + "".join(
        f'<div class="kv-row"><span>{_e(k)}</span><b class="mono">{v}</b></div>'
        for k, v in pairs
    ) + "</div>"


def _detail_table(rows: list, total: float, skipped_rows: int, notes: dict) -> str:
    """核对明细：序号 · 文件名 · 发票类型 · 开票日期 · 销售方 · 金额 · 结论 · 备注。"""
    trs = []
    for i, r in enumerate(rows, 1):
        cls, text, why = _verdict(r)
        note = _txt(notes.get(r.get("id"))).strip() or why
        new_path = _txt(r.get("new_path")).strip()
        fn = _txt(r.get("filename")) or _txt(r.get("path"))
        fn_html = f'<span class="fn" title="{_e(_txt(r.get("path")))}">{_e(_short(fn, 52))}</span>'
        new_base = os.path.basename(new_path.replace("\\", "/"))
        if new_path and new_base and new_base != fn:
            fn_html += f'<small class="fn-new">→ {_e(_short(new_base, 52))}</small>'
        trs.append(
            f'<tr class="{"row-dup" if cls == "dim" else ""}">'
            f'<td class="seq">{i}</td>'
            f'<td class="fn-cell">{fn_html}</td>'
            f'<td class="ty">{_dash(r.get("label") or r.get("type"))}</td>'
            f'<td class="dt">{_dash(R.short_date(_txt(r.get("date")).strip()))}</td>'
            f'<td class="sel" title="{_e(_txt(r.get("seller")))}">{_e(_short(r.get("seller"), 26)) or _DASH}</td>'
            f'<td class="num"><span class="amt">{_money(r.get("amount"))}</span></td>'
            f'<td class="vd-cell">{_pill(cls, text)}</td>'
            f'<td class="note-cell">{_e(note) if note else _DASH}</td>'
            f"</tr>"
        )
    body = "".join(trs) or (
        '<tr><td colspan="8" class="empty">本次没有选中的发票</td></tr>'
    )
    foot = (
        f'<tr><td colspan="5">合计（去重后，仅代表件计入）</td>'
        f'<td class="num"><span class="amt">{_money(total)}</span></td>'
        f'<td colspan="2">{skipped_rows} 行已跳过 / 剔除，未计入</td></tr>'
    )
    return (
        '<div class="dt-wrap" id="detailTable"><table class="dt">'
        "<thead><tr>"
        '<th class="seq">#</th><th class="fn-cell">文件名</th><th class="ty">发票类型</th>'
        '<th class="dt">开票日期</th><th class="sel">销售方</th><th class="num">金额</th>'
        '<th class="vd-cell">核对结论</th><th class="note-cell">备注（核对原因）</th>'
        "</tr></thead>"
        f"<tbody>{body}</tbody><tfoot>{foot}</tfoot>"
        "</table></div>"
    )


def _dup_section(rows: list, dup_warns: list, notes: dict) -> str:
    """查重结果：扫描期已归并的副本组 + 本步剔除的重复件 + 同票号金额不一致的提示。"""
    blocks = []

    for r in rows:
        copies = list(r.get("dup_paths") or [])
        if not copies:
            continue
        fn = _txt(r.get("filename")) or _txt(r.get("path"))
        lis = "".join(
            f'<li><span class="tag dim">副本</span>{_e(_short(c, 60))}'
            f'<span class="dim"> · 同一张票，整行不进主列表，只在代表件的详情里可见</span></li>'
            for c in copies
        )
        blocks.append(
            '<div class="dup-group">'
            f'<div class="dup-head"><span class="tag ok">代表件</span>'
            f'<b class="mono" title="{_e(_txt(r.get("path")))}">{_e(_short(fn, 60))}</b>'
            f'<span class="dim"> · {_e(_dup_basis(r))}</span></div>'
            f'<ul class="dup-list">{lis}</ul>'
            "</div>"
        )

    step_dups = []
    for r in rows:
        if _txt(r.get("run_status")).strip() != "dup_skipped":
            continue
        fn = _txt(r.get("filename")) or _txt(r.get("path"))
        why = _txt(notes.get(r.get("id"))).strip() or _verdict(r)[2]
        step_dups.append(
            '<div class="dup-group">'
            f'<div class="dup-head"><span class="tag warn">本步剔除</span>'
            f'<b class="mono">{_e(_short(fn, 60))}</b></div>'
            f'<div class="dup-why">{_e(why)}</div>'
            "</div>"
        )
    blocks.extend(step_dups)

    if dup_warns:
        blocks.append(
            '<div class="dup-notes">'
            + "".join(f'<div class="dup-note">{_e(w)}</div>' for w in dup_warns)
            + "</div>"
        )

    if not blocks:
        return '<div class="empty">本次没有发现重复件（发票号与内容指纹都是唯一的）。</div>'
    return "".join(blocks)


def _alert_section(alerts: list, dup_moved: int) -> str:
    if not alerts:
        return '<div class="empty">本次没有警告，也没有异常。</div>'
    items = "".join(f'<div class="note warn">{_e(a)}</div>' for a in alerts)
    tail = (
        f'<div class="note info">查重相关的 {dup_moved} 条提示已并入上方「查重结果」，此处不重复列出。</div>'
        if dup_moved else ""
    )
    return f'<div class="notes">{items}{tail}</div>'


_ACTION_NAMES = {"dedup": "查重", "check": "核对", "rename": "重命名", "merge": "合并"}


def report_actions(payload: dict) -> dict[str, bool]:
    """旧运行记录没有 actions 时沿用原有全模块报告口径。"""
    selected = payload.get("actions")
    if not isinstance(selected, dict):
        selected = {}
    return {name: bool(selected.get(name, True)) for name in _ACTION_NAMES}


def report_title(payload: dict) -> str:
    actions = report_actions(payload)
    selected = [label for name, label in _ACTION_NAMES.items() if actions[name]]
    return f"{'、'.join(selected)}结果" if selected else "处理结果"


def _rename_table(rows: list, notes: dict) -> str:
    labels = {"renamed": "成功", "rename_same": "已是规范名",
              "rename_skipped": "跳过", "rename_failed": "失败"}
    cells = []
    for row in rows:
        status = _txt(row.get("run_status"))
        if status == "dup_skipped":
            continue
        old = _txt(row.get("original_path") or row.get("filename") or row.get("path"))
        new = _txt(row.get("new_path"))
        note = _txt(notes.get(row.get("id")))
        cells.append(
            "<tr>"
            f"<td class='fn-cell'>{_e(_short(old, 60))}</td>"
            f"<td class='fn-cell'>{_e(_short(new, 60)) if new else _DASH}</td>"
            f"<td>{_e(labels.get(status, '未处理'))}</td>"
            f"<td>{_e(note) if note else _DASH}</td></tr>"
        )
    return ('<div class="dt-wrap"><table class="dt"><thead><tr>'
            '<th class="fn-cell">原文件</th><th class="fn-cell">目标文件名</th><th>结果</th><th>原因</th>'
            '</tr></thead><tbody>' + ("".join(cells) or '<tr><td colspan="4" class="empty">本次没有可重命名的发票</td></tr>')
            + '</tbody></table></div>')


# ─────────────────────────── 主函数 ───────────────────────────

def build_check_report_html(payload: dict, warnings: list, *,
                            title: str | None = None) -> str:
    """把一次 `/api/pool/run` 的结果按所选模块渲染成自包含结果页。

    参数
        payload  : `/api/pool/run` 的返回体（用 `rows` / `stages` / `skipped` /
                   `warnings` / `total_amount` / `run_id` / `out_dir` 等键）。
        warnings : 与 `payload["warnings"]` 合并去重后的警告列表（两者常是同一个对象）。
        title    : 页面主标题；不指定时从 actions 推导。

    返回
        完整 HTML 字符串（单文件、零外部 URL）。函数本身不碰文件系统。
    """
    payload = payload or {}
    actions = report_actions(payload)
    title = title or report_title(payload)
    rows = list(payload.get("rows") or [])
    stages = payload.get("stages") or {}
    ck = stages.get("check") or {}
    dd = stages.get("dedup") or {}
    rn = stages.get("rename") or {}
    mg = stages.get("merge") or {}
    total = payload.get("total_amount")
    out_dir = _txt(payload.get("out_dir"))
    run_id = _txt(payload.get("run_id"))
    elapsed = payload.get("elapsed_ms")
    mode = _txt((payload.get("rename_mode") or (payload.get("actions") or {}).get("rename_mode"))).strip()

    # 按模块保存原因，避免核对结论串到重命名结果里。
    skipped = list(payload.get("skipped") or [])
    notes = {name: {} for name in _ACTION_NAMES}
    for s in skipped:
        if isinstance(s, dict) and s.get("id") is not None and s.get("step") in notes:
            notes[s["step"]].setdefault(s["id"], _txt(s.get("reason")))
    skipped_rows = 0
    for r in rows:
        if _txt(r.get("run_status")).strip() in ("dup_skipped", "rename_skipped", "rename_failed", "check_fail", "check_review"):
            skipped_rows += 1

    # 查重口径：扫描期归并的副本（rows[].dup_paths）+ 本步剔除的重复件。
    scan_copies = sum(len(r.get("dup_paths") or []) for r in rows)
    step_copies = int(_num(dd.get("dups_skipped")))
    copies = scan_copies + step_copies
    reps = int(_num(dd.get("representatives"))) or len([r for r in rows if _txt(r.get("run_status")).strip() != "dup_skipped"])

    # 警告与异常：warnings + payload["warnings"] + payload["issues"] + 合并失败明细。
    all_warns = [w for w in _merge_unique(warnings, payload.get("warnings"))
                 if not any(w.startswith(f"{label}：") and not actions[name]
                            for name, label in _ACTION_NAMES.items())]
    dup_warns = [w for w in all_warns if w.startswith("查重：")]
    alerts = [w for w in all_warns if not w.startswith("查重：")]
    alerts += _merge_unique(payload.get("issues"))
    failed_merge = [x for x in (mg.get("failed") or []) if x]
    if actions["merge"] and failed_merge:
        alerts.append("合并：" + str(len(failed_merge)) + " 个 PDF 未能并入「合并发票.pdf」：" + "、".join(_short(x, 40) for x in failed_merge[:8]))
    if payload.get("kind") and payload.get("kind") != "html":
        alerts.append(f"结果页类型异常：kind={payload.get('kind')}")

    pal = CHECK_REPORT_PALETTE

    # ── 概要 ──
    passed = int(_num(ck.get("pass")))
    failed = int(_num(ck.get("fail")))
    review = int(_num(ck.get("review")))
    unknown = int(_num(ck.get("unknown")))
    upper = ""
    if _amount_upper is not None:
        try:
            upper = _txt(_amount_upper(_num(total))).strip()
        except Exception:  # pragma: no cover - 装饰性
            upper = ""
    money_cells = []
    if actions["check"]:
        money_cells.extend([
            ("核对通过", f"{passed} 张", "ok"),
            ("核对不符", f"{failed} 张", "warn" if failed else "dim"),
            ("待确认", f"{review} 张", "warn" if review else "dim"),
            ("无法判定", f"{unknown} 张", "dim"),
        ])
    if actions["dedup"]:
        money_cells.append(("查重剔除", f"{copies} 份", "dim"))
    if actions["rename"]:
        money_cells.extend([
            ("重命名成功", f"{int(_num(rn.get('renamed')))} 张", "ok"),
            ("重命名跳过", f"{int(_num(rn.get('skipped')))} 张", "dim"),
            ("重命名失败", f"{int(_num(rn.get('failed')))} 张", "warn"),
        ])
    if actions["merge"]:
        money_cells.append(("合并页数", f"{int(_num(mg.get('page_count')))} 页", "ok"))
    kv = [("处理", f"选中 {len(rows)} 张")]
    if actions["dedup"]:
        kv.append(("查重", f"代表件 {reps} 张 · 剔除副本 {copies} 份"))
    if actions["check"]:
        kv.append(("核对", f"通过 {passed} · 不符 {failed} · 待确认 {review} · 无法判定 {unknown}"))
    if actions["rename"]:
        kv.append(("重命名", f"成功 {int(_num(rn.get('renamed')))} · 跳过 {int(_num(rn.get('skipped')))} · 失败 {int(_num(rn.get('failed')))}"
                             + (f"（{'修改原件' if mode == 'inplace' else '复制原件'}）" if mode else "")))
    if actions["merge"]:
        kv.append(("合并", f"{_e(_short(mg.get('merged_path'), 60))} · {int(_num(mg.get('page_count')))} 页"
                   if mg.get("merged_path") else "没有生成合并文件"))
    kv.append(("输出目录", _e(out_dir) if out_dir else _DASH))
    if elapsed:
        kv.append(("耗时", f"{_num(elapsed) / 1000:.2f} 秒" if _num(elapsed) > 1000 else f"{int(_num(elapsed))} 毫秒"))
    summary_body = (
        R.money_strip(
            "本次处理汇总",
            f"共 {len(rows)} 张（含被跳过 / 剔除的行）",
            money_cells,
            "票面金额合计（大写）",
            _e(upper) if upper else _DASH,
        )
        + _kv_rows(kv)
    )

    # ── 各 section ──
    body = '<section class="kpis" id="kpis"></section>' + _panel("概要", summary_body)
    if actions["dedup"]:
        body += _panel("查重结果", _dup_section(rows, dup_warns, notes["dedup"]),
                       "同一张票只留一份代表件，副本不参与后续处理")
    if actions["check"]:
        body += _panel("核对明细", _detail_table(rows, total, skipped_rows, notes["check"]),
                       "结论取抬头 / 税号与预设的比对结果")
    if actions["rename"]:
        body += _panel("重命名结果", _rename_table(rows, notes["rename"]))
    if actions["merge"]:
        merge_body = (_kv_rows([("合并文件", _e(mg.get("merged_path")) if mg.get("merged_path") else _DASH),
                                ("合并页数", f"{int(_num(mg.get('page_count')))} 页"),
                                ("失败文件", f"{len(failed_merge)} 个")]))
        body += _panel("合并结果", merge_body)
    if alerts:
        body += _panel("警告与异常", _alert_section(alerts, len(dup_warns) if actions["dedup"] else 0))

    chips = [
        ["运行号", _e(run_id[:8] if run_id else "—")],
        ["处理张数", f"{len(rows)} 张"],
    ]
    if actions["dedup"]:
        chips.extend([["代表件", f"{reps} 张"], ["剔除副本", f"{copies} 份"]])
    if actions["rename"] and mode:
        chips.append(["重命名", "修改原件" if mode == "inplace" else "复制原件"])
    if actions["merge"] and mg.get("merged_path"):
        chips.append(["合并", f"{int(_num(mg.get('page_count')))} 页"])

    kpi_cards = [
        {"lab": "处理张数", "count": len(rows), "unit": "张",
         "sub": "本次选中", "color": pal["accent"]},
    ]
    if actions["check"]:
        kpi_cards.extend([
        {"lab": "核对通过", "count": passed, "unit": "张",
         "sub": "抬头 / 税号与预设一致", "color": pal["ok"]},
        {"lab": "待确认", "count": review, "unit": "张",
         "sub": "有字段没读出来，请人工看", "color": pal["warn"]},
        {"lab": "解析失败", "count": unknown, "unit": "张",
         "sub": "未设预设或无法判定", "color": pal["muted"]},
        ])
    if actions["dedup"]:
        kpi_cards.append(
        {"lab": "查重剔除副本数", "count": copies, "unit": "份",
         "sub": f"扫描期 {scan_copies} · 本步 {step_copies}", "color": pal["accent2"]})
    if actions["rename"]:
        kpi_cards.append({"lab": "重命名成功", "count": int(_num(rn.get("renamed"))), "unit": "张",
                          "sub": f"跳过 {int(_num(rn.get('skipped')))} · 失败 {int(_num(rn.get('failed')))}", "color": pal["ok"]})
    if actions["merge"]:
        kpi_cards.append({"lab": "合并页数", "count": int(_num(mg.get("page_count"))), "unit": "页",
                          "sub": f"失败文件 {len(failed_merge)} 个", "color": pal["accent2"]})
    kpi_cards.append(
        {"lab": "金额合计", "count": _num(total), "dec": 2, "unit": "元",
         "sub": "本次参与处理", "color": pal["accent_soft"]})

    subtitle = f"本次处理 {len(rows)} 张"
    if actions["dedup"]:
        subtitle += f" · 剔除副本 {copies} 份"
    if actions["check"]:
        subtitle += f" · 核对通过 {passed} · 待确认 {review}"
    if actions["rename"]:
        subtitle += f" · 重命名成功 {int(_num(rn.get('renamed')))}"
    if actions["merge"]:
        subtitle += f" · 合并 {int(_num(mg.get('page_count')))} 页"
    subtitle += f" · 合计 {_money(total)}"
    if out_dir:
        subtitle += f" · 输出 {_short(out_dir, 40)}"

    js_payload = {"palette": pal, "chips": chips, "kpiCards": kpi_cards}
    payload_json = json.dumps(js_payload, ensure_ascii=False,
                              separators=(",", ":")).replace("</", "<\\/")

    footer_left = f"运行号 {_e(run_id) if run_id else _DASH} · 输出目录 {_e(out_dir) if out_dir else _DASH}"
    footer_right = "本页仅在本机生成与查看 · 不联网"

    return R.render_page(
        head_title=f"{title} · 发票处理",
        palette=pal,
        module_css=_MODULE_CSS,
        hero_icon=report_icon_svg(30, pal),
        title=title,
        subtitle=subtitle,
        body=body,
        footer_left=footer_left,
        footer_right=footer_right,
        app_js=(
            "renderChips(P.chips);\n"
            "renderKpis(P.kpiCards);\n"
            "initReveal();\n"
            "bindDetailView();\n"
        ),
        echarts_js=None,
        payload_json=payload_json,
    )


# ─────────────────────────── 模块 CSS ───────────────────────────
#: 只补共享骨架没有的东西：核对明细表的列宽、结论 pills、查重分组。
#: 颜色全部走 `--accent*`（天蓝），琥珀 `#F59E0B` 只出现在 `.dup-note` / `.note.warn`
#: 这类**警告**语义上，绝不是品牌色。
_MODULE_CSS = r"""
/* 模块数量随勾选变化；宽屏每行最多三张卡。 */
@media (min-width:1281px){
  section.kpis{grid-template-columns:repeat(3,minmax(0,1fr));}
}

/* ── 核对明细表 ── 共享 table.dt 是 white-space:nowrap，
   所以固定布局下必须逐列给宽度（只给 max-width 是无效的）。 */
.dt-wrap{overflow:auto;border-radius:12px;}
table.dt{table-layout:fixed;width:100%;}
table.dt th.seq,table.dt td.seq{width:46px;text-align:right;}
/* `发票类型` 的实际值是「电子发票（普通发票）」这种长标签，而共享 table.dt 是
   white-space:nowrap —— 只给窄宽度会让它**溢出压到「开票日期」上**（截图实测）。
   所以这两列都改成可换行 + 足够宽度。 */
table.dt th.ty,table.dt td.ty{width:134px;white-space:normal;word-break:break-all;line-height:1.5;}
table.dt th.dt,table.dt td.dt{width:100px;}
table.dt th.sel,table.dt td.sel{width:180px;white-space:normal;word-break:break-all;}
table.dt th.num,table.dt td.num{width:112px;}
table.dt th.vd-cell,table.dt td.vd-cell{width:118px;}
table.dt th.note-cell,table.dt td.note-cell{width:210px;white-space:normal;word-break:break-all;line-height:1.5;color:var(--text-dim);}
table.dt td.fn-cell{white-space:normal;word-break:break-all;line-height:1.5;}
table.dt td.fn-cell .fn{color:var(--text);}
table.dt td.fn-cell .fn-new{display:block;margin-top:3px;font-size:11px;color:var(--accent);font-family:ui-monospace,Consolas,monospace;}
table.dt tfoot td{color:var(--text-dim);font-size:12.5px;}
table.dt tbody tr.row-dup td{background:rgba(92,110,143,.06);}
table.dt tbody tr.row-dup td.fn-cell{color:var(--text-dim);}

/* ── 结论 pills ── */
.vd{display:inline-block;padding:2px 9px;border-radius:999px;border:1px solid transparent;
    font-size:11.5px;font-style:normal;line-height:1.7;}
.vd.ok{color:#34D399;border-color:rgba(52,211,153,.5);background:rgba(52,211,153,.12);}
.vd.warn{color:#FBBF24;border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.12);}
.vd.err{color:#F87171;border-color:rgba(248,113,113,.5);background:rgba(248,113,113,.12);}
.vd.dim{color:var(--text-faint);border-color:rgba(92,110,143,.45);background:rgba(92,110,143,.10);}

/* ── 概要里的键值行 ── */
.kv-box{display:grid;gap:9px;margin-top:16px;}
.kv-row{display:flex;gap:12px;align-items:baseline;font-size:13px;}
.kv-row>span{flex:none;width:66px;color:var(--text-dim);}
.kv-row>b{font-weight:600;word-break:break-all;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;}

/* ── 查重结果 ── */
.dup-group{border:1px solid var(--panel-border);border-left:2px solid var(--accent);
  border-radius:12px;padding:12px 14px;margin-bottom:12px;background:rgba(56,189,248,.045);}
.dup-head{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;font-size:13px;}
.dup-head .dim{color:var(--text-faint);font-size:12px;}
.dup-list{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:6px;}
.dup-list li{font-size:12.5px;color:var(--text-dim);word-break:break-all;}
.dup-why{margin-top:7px;font-size:12.5px;color:var(--text-dim);line-height:1.6;}
.dup-notes{margin-top:4px;display:grid;gap:8px;}
.dup-note{border:1px solid rgba(245,158,11,.36);background:rgba(245,158,11,.07);
  border-radius:10px;padding:9px 12px;font-size:12.5px;color:#FCD9A0;line-height:1.6;}
.tag{flex:none;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid transparent;}
.tag.ok{color:#34D399;border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.12);}
.tag.warn{color:#FBBF24;border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.12);}
.tag.dim{color:var(--text-faint);border-color:rgba(92,110,143,.45);background:rgba(92,110,143,.10);}
"""
