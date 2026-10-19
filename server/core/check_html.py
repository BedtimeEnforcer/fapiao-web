# -*- coding: utf-8 -*-
"""core/check_html.py — 发票核对结果的单文件自包含 HTML 生成器。

为什么单独做一个
----------------
核对的产物原来只有 PDF 报告；但核对结果本质是「逐张判定表」，
用户更需要**在浏览器里直接看**（能搜索、能放大、能看长税号），
需要留档时再「打印 / 存为 PDF」。本模块生成的就是这样一个页面。

版面骨架复用 `report_html.py`（与火车票行程单、住宿行程单同一套设计语言），
主色取核对模块的识别色 —— 紫罗兰 `#A78BFA`。

数据资产：本页不依赖任何外部资源（无 ECharts、无 GeoJSON），单文件自包含。
"""
from __future__ import annotations

import datetime
import json
import os

from . import report_html as R
from .parse import _unique_path, amount_upper
from .report_html import esc as _esc

__all__ = [
    "DEFAULT_CHECK_PALETTE",
    "build_check_payload",
    "build_check_html",
    "check_icon_svg",
]

#: 默认色板 —— 键集合与火车票/住宿行程单保持一致，只把主色换成核对的紫罗兰。
DEFAULT_CHECK_PALETTE = {
    "bg_from":      "#060A14",
    "bg_to":        "#0D1526",
    "panel":        "#0F1A2E",
    "panel_border": "#22314F",
    "accent":       "#A78BFA",
    "accent_soft":  "#DDD6FE",
    "accent2":      "#22D3EE",
    "text":         "#E9EFFA",
    "text_dim":     "#93A4C4",
    "text_faint":   "#5C6E8F",
    "map_fill":     "#12203A",
    "map_border":   "#2E4470",
    "map_dash":     "#93A9CE",
    "map_glow":     "#22D3EE",
    "route_hi":     "#34D399",
    "route_mid":    "#10B981",
    "route_lo":     "#3E5C8F",
    "route_flow":   "#A7F3D0",
    "station":      "#34D399",
    "station_ring": "#080D1A",
}

_PRIVACY = "本页仅在本机生成与查看，核对数据不上传任何第三方服务"
_MAX_DETAILS = 2000


def check_icon_svg(size=30, palette=None):
    """核对标识：一个方框里打勾（扁平描边，不引用外部资源）。"""
    pal = _palette(palette)
    a = pal["accent"]
    inner = (
        '<rect x="7" y="7" width="50" height="50" rx="11" fill="none" '
        'stroke="%s" stroke-width="3.4" opacity=".85"/>'
        '<path d="M19 33l9 9 17-19" fill="none" stroke="%s" stroke-width="5" '
        'stroke-linecap="round" stroke-linejoin="round"/>' % (a, a))
    return ('<svg class="ico" viewBox="0 0 64 64" width="%d" height="%d" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">%s</svg>'
            % (size, size, inner))


def _palette(palette):
    pal = dict(DEFAULT_CHECK_PALETTE)
    if palette:
        pal.update({k: v for k, v in palette.items() if v})
    return pal


# ─────────────────────── 数据组装 ───────────────────────

def build_check_payload(results, expected_name="", expected_tax_id="", palette=None, *,
                        title="发票核对结果", subtitle=None, source_note=None):
    """把核对结果整理成页面需要的结构化数据。

    results 的字段与 `core.check.check_invoices()` 的输出一致：
    filename / buyer_name / tax_id / amount / seller_name / invoice_date /
    invoice_no / name_ok / tax_ok / passed / needs_review / review_reason
    """
    pal = _palette(palette)
    details = []
    for i, r in enumerate(results or [], 1):
        if not isinstance(r, dict):
            continue
        passed = bool(r.get("passed"))
        review = bool(r.get("needs_review"))
        details.append({
            "seq": i,
            "filename": str(r.get("filename") or ""),
            "buyer": str(r.get("buyer_name") or ""),
            "tax_id": str(r.get("tax_id") or ""),
            "seller": str(r.get("seller_name") or ""),
            "date": str(r.get("invoice_date") or ""),
            "invoice_no": str(r.get("invoice_no") or ""),
            "amount": round(float(r.get("amount") or 0.0), 2),
            "passed": passed,
            "review": review,
            "reason": str(r.get("review_reason") or ""),
            # 三态：通过 / 待确认（字段没识别出来）/ 不符
            "verdict": "通过" if passed else ("待确认" if review else "不符"),
        })

    n = len(details)
    passed_n = sum(1 for d in details if d["passed"])
    review_n = sum(1 for d in details if d["review"] and not d["passed"])
    failed_n = n - passed_n - review_n
    total_amt = round(sum(d["amount"] for d in details), 2)
    passed_amt = round(sum(d["amount"] for d in details if d["passed"]), 2)
    bad_amt = round(total_amt - passed_amt, 2)
    review_amt = round(sum(d["amount"] for d in details if d["review"] and not d["passed"]), 2)

    chips = []
    if expected_name:
        chips.append(["预设购买方", expected_name])
    if expected_tax_id:
        chips.append(["预设税号", expected_tax_id])
    chips.append(["核对张数", f"{n} 张"])
    if n:
        chips.append(["通过率", f"{passed_n / n * 100:.0f}%"])

    ok_card = ({"lab": "通过", "count": passed_n, "unit": "张",
                "sub": f'金额 {R.fmt_money(passed_amt)}', "color": "#34D399"})
    bad_card = ({"lab": "不符", "count": failed_n, "unit": "张",
                 "sub": f'金额 {R.fmt_money(bad_amt)}',
                 "color": "#F87171"} if failed_n else
                {"lab": "不符", "text": R.DASH, "cls": "txt",
                 "sub": "全部通过", "color": "#F87171"})
    review_card = ({"lab": "待确认", "count": review_n, "unit": "张",
                    "sub": "字段未识别完整", "color": "#F59E0B"} if review_n else
                   {"lab": "待确认", "text": R.DASH, "cls": "txt",
                    "sub": "无需人工复核", "color": "#F59E0B"})
    kpi_cards = [
        {"lab": "核对总数", "count": n, "unit": "张",
         "sub": "本次提交的发票", "color": pal["accent"]},
        ok_card,
        bad_card,
        review_card,
        {"lab": "票面金额", "count": total_amt, "dec": 2, "unit": "元",
         "sub": "全部发票合计", "color": pal["accent2"]},
    ]

    money = {
        "count": n, "total": total_amt,
        "passed": passed_n, "failed": failed_n, "review": review_n,
        "passedAmount": passed_amt, "badAmount": bad_amt, "reviewAmount": review_amt,
        "upper": amount_upper(total_amt),
    }

    return {
        "title": title,
        "subtitle": subtitle or "本次提交发票与预设抬头/税号的比对结论",
        "palette": pal,
        "chips": chips,
        "kpiCards": kpi_cards,
        "money": money,
        "details": details[:_MAX_DETAILS],
        "detailsTotal": n,
        "detailsTruncated": n > _MAX_DETAILS,
        "expected": {"name": expected_name, "tax_id": expected_tax_id},
        "sourceNote": source_note or "",
    }


# ─────────────────────── 服务端渲染 ───────────────────────

_VERDICT_CLS = {"通过": "ok", "不符": "err", "待确认": "warn"}


def _money_html(payload):
    m = payload["money"]
    exp = payload.get("expected") or {}
    sub_bits = []
    if exp.get("name"):
        sub_bits.append(f'预设购买方 {exp["name"]}')
    if exp.get("tax_id"):
        sub_bits.append(f'预设税号 {exp["tax_id"]}')
    sub = "　·　".join(sub_bits) or "未设置预设条件"
    return R.money_strip(
        "核对汇总", sub,
        [("核对张数", f'{m["count"]} 张', ""),
         ("通过", f'{m["passed"]} 张', "ok" if m["passed"] else ""),
         ("不符", f'{m["failed"]} 张', "warn" if m["failed"] else ""),
         ("待确认", f'{m["review"]} 张', "warn" if m["review"] else "")],
        "票面金额合计（大写）", m["upper"] or "零元整")


def _table_html(payload):
    rows = payload.get("details") or []
    if not rows:
        return '<div class="empty">本次没有可列示的核对结果。</div>'
    head = ["序号", "文件名", "购买方", "纳税人识别号", "金额", "核对结果"]
    th = "".join(f'<th>{_esc(h)}</th>' for h in head)
    trs = []
    for e in rows:
        cls = _VERDICT_CLS.get(e["verdict"], "")
        mark = f'<i class="vd {cls}">{_esc(e["verdict"])}</i>'
        if e.get("reason"):
            mark += f'<small class="vd-why">{_esc(e["reason"])}</small>'
        trs.append(
            f'<tr class="{"row-err" if cls == "err" else ""}">'
            f'<td class="seq">{e["seq"]}</td>'
            f'<td class="fn">{_esc(e["filename"])}</td>'
            f'<td>{_esc(e["buyer"] or R.DASH)}</td>'
            f'<td class="inv">{_esc(e["tax_id"] or R.DASH)}</td>'
            f'<td class="num"><span class="amt">{R.fmt_money(e["amount"])}</span></td>'
            f'<td class="vd-cell">{mark}</td>'
            f'</tr>')
    foot = (f'<tfoot><tr><td colspan="4">合计 {len(rows)} 张'
            f'{"（仅展示前 " + str(len(rows)) + " 条）" if payload.get("detailsTruncated") else ""}'
            f'</td><td class="num"><span class="amt">'
            f'{R.fmt_money(payload["money"]["total"])}</span></td>'
            f'<td class="vd-cell">—</td></tr></tfoot>')
    return (f'<table class="dt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(trs)}</tbody>{foot}</table>')


def _detail_note(payload):
    m = payload["money"]
    bits = [f'共核对 {m["count"]} 张发票',
            f'通过 {m["passed"]} 张',
            f'不符 {m["failed"]} 张']
    if m["review"]:
        bits.append(f'待确认 {m["review"]} 张')
    bits.append(f'票面合计 {R.fmt_money(m["total"])}')
    txt = "　·　".join(bits)
    if payload.get("detailsTruncated"):
        txt += f'　·　仅展示前 {len(payload["details"])} 条，共 {payload["detailsTotal"]} 条'
    return _esc(txt)


def _expected_html(payload):
    exp = payload.get("expected") or {}
    rows = []
    rows.append('<div class="kv-row"><span>购买方名称</span>'
                f'<b>{_esc(exp.get("name") or R.DASH)}</b></div>')
    rows.append('<div class="kv-row"><span>纳税人识别号</span>'
                f'<b class="mono">{_esc(exp.get("tax_id") or R.DASH)}</b></div>')
    return f'<div class="kv-box">{"".join(rows)}</div>'


def _notes_html(payload):
    m = payload["money"]
    exp = payload.get("expected") or {}
    box = []
    both = bool(exp.get("name")) and bool(exp.get("tax_id"))
    box.append('<div class="note info"><b>判定口径</b><br>'
               + ('购买方名称与纳税人识别号<b>两项都填</b>时须<b>同时匹配</b>才算「通过」；'
                  if both else '名称或税号只填一项时，按该项比对。')
               + '比对前会做归一化处理（去空格、全角转半角、括号与大小写统一）。</div>')
    box.append(f'<div class="note"><b>本次结果</b><br>共 {m["count"]} 张发票，'
               f'通过 {m["passed"]} 张、不符 {m["failed"]} 张'
               + (f'、待确认 {m["review"]} 张' if m["review"] else '')
               + f'；通过票面金额 {R.fmt_money(m["passedAmount"])}，'
                 f'未通过票面金额 {R.fmt_money(m["badAmount"])}。</div>')
    if m["review"]:
        box.append(f'<div class="note warn"><b>待确认（{m["review"]} 张）</b><br>'
                   '这些发票的购买方名称或税号<b>未能从票面识别出来</b>'
                   '（PDF 为扫描件、或字段位置特殊），并非一定不符，请人工核对。</div>')
    box.append('<div class="note"><b>数据来源</b><br>票面信息由本机解析 PDF/OFD 得到，'
               '全程不联网、不上传。</div>')
    return "".join(box)


# ─────────────────────── 模块样式 / 主体 ───────────────────────

_MODULE_CSS = r"""/* 核对标识 */
.ico{display:inline-block;vertical-align:-.16em;flex:none}

/* 判定徽标（三态） */
.vd{display:inline-block;font-style:normal;font-weight:700;font-size:12px;
  padding:3px 11px;border-radius:999px;border:1px solid;line-height:1.5;white-space:nowrap}
.vd.ok{color:#34D399;border-color:rgba(52,211,153,.5);background:rgba(52,211,153,.12)}
.vd.warn{color:#FBBF24;border-color:rgba(245,158,11,.5);background:rgba(245,158,11,.12)}
.vd.err{color:#F87171;border-color:rgba(248,113,113,.5);background:rgba(248,113,113,.12)}
.vd-why{display:block;margin-top:4px;font-size:10.5px;color:var(--text-faint)}
td.vd-cell{white-space:nowrap}
tr.row-err td{background:rgba(248,113,113,.055)}

/* 列宽：表格整体锁死布局，避免长文件名把「购买方」列挤掉/压住。
   原来只给 td.fn 设了 max-width，但 table.dt 是 white-space:nowrap，
   单元格不许换行时 max-width 形同虚设 —— 文字会直接压到下一列上。 */
table.dt{table-layout:fixed}
table.dt th:nth-child(1),table.dt td:nth-child(1){width:44px}
table.dt th:nth-child(2),table.dt td:nth-child(2){width:30%}
table.dt th:nth-child(3),table.dt td:nth-child(3){width:19%}
table.dt th:nth-child(4),table.dt td:nth-child(4){width:18%}
table.dt th:nth-child(5),table.dt td:nth-child(5){width:92px}
table.dt th:nth-child(6),table.dt td:nth-child(6){width:86px}
/* 这三列内容可能很长：放开换行，宁可折行也不压到隔壁列 */
table.dt td.fn,
table.dt td:nth-child(3),
table.dt td:nth-child(4){
  white-space:normal; word-break:break-all; overflow-wrap:anywhere;
}

/* 预设条件展示 */
.kv-box{display:grid;gap:10px}
.kv-row{display:flex;align-items:baseline;gap:12px;font-size:13px}
.kv-row span{color:var(--text-dim);flex:none;width:96px}
.kv-row b{color:var(--text);font-weight:600;word-break:break-all}
.kv-row b.mono{font-family:ui-monospace,Consolas,monospace;letter-spacing:.02em}"""

_BODY = r"""
  <section class="grid">
    <div class="col">
      <div class="panel">
        <div class="panel-head">
          <h2>核对结论</h2>
          <div class="tools">
            <button class="btn" id="btn-print">打印 / 存 PDF</button>
          </div>
        </div>
        __MONEY_STRIP__
        <div class="dt-wrap" id="detailTable">__DETAIL_TABLE__</div>
        <div class="det-note">__DETAIL_NOTE__</div>
      </div>
    </div>

    <div class="col">
      <div class="panel">
        <div class="panel-head"><h2>预设核对条件</h2><span class="hint">比对基准</span></div>
        __EXPECTED__
      </div>
      <div class="panel">
        <div class="panel-head"><h2>数据说明</h2></div>
        <div class="notes" id="notes">__NOTES__</div>
      </div>
    </div>
  </section>"""

_APP_JS = ""


def render_check_html(payload, *, footer_left="", footer_right=""):
    def js_json(obj):
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    body = (_BODY
            .replace("__MONEY_STRIP__", _money_html(payload))
            .replace("__DETAIL_TABLE__", _table_html(payload))
            .replace("__DETAIL_NOTE__", _detail_note(payload))
            .replace("__EXPECTED__", _expected_html(payload))
            .replace("__NOTES__", _notes_html(payload)))
    return R.render_page(
        head_title=payload.get("title", "发票核对结果"),
        palette=payload["palette"],
        module_css=_MODULE_CSS,
        hero_icon=check_icon_svg(30, payload["palette"]),
        title=payload.get("title", "发票核对结果"),
        subtitle=payload.get("subtitle", ""),
        body=body,
        footer_left=footer_left, footer_right=footer_right,
        app_js="renderChips(P.chips);\nrenderKpis(P.kpiCards);\ninitReveal();\nbindDetailView();\n"
               + _APP_JS,
        echarts_js=None,
        payload_json=js_json(payload))


def build_check_html(results, output_folder, *, expected_name="", expected_tax_id="",
                     palette=None, title="发票核对结果", subtitle=None,
                     filename="发票核对结果.html"):
    """生成单文件自包含的核对结果 HTML，返回落盘路径；失败抛异常（调用方决定降级）。"""
    payload = build_check_payload(results, expected_name, expected_tax_id,
                                  palette=palette, title=title, subtitle=subtitle)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    m = payload["money"]
    footer_left = (f'生成时间：{_esc(now)} &nbsp;|&nbsp; 由「报销助手」生成'
                   f'<br>核对结论按票面解析结果给出，仅供报销审核参考')
    footer_right = (f'本次核对 {m["count"]} 张 &nbsp;|&nbsp; '
                    f'通过 {m["passed"]} / 不符 {m["failed"]}'
                    f'<br>{_esc(_PRIVACY)}')

    doc = render_check_html(payload, footer_left=footer_left, footer_right=footer_right)
    path = _unique_path(os.path.join(output_folder, filename))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path
