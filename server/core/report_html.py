# -*- coding: utf-8 -*-
"""core/report_html.py — 行程单 HTML 的共享外壳、样式与组件。

背景
----
火车票与住宿票两个模块的行程单**不再生成 PDF**，统一改为输出单文件自包含 HTML
（见 `trainmap_html.py` / `hotel_html.py`）。两个模块共用同一套版面骨架，
所以把共用的 CSS、页面模板与明细组件收在这里，避免同一套样式在多个模块里各写一遍。

设计约束
--------
* **单文件自包含**：样式/脚本/数据全内联，零外部请求（行程含个人信息，不上传）。
* **颜色注入**：`palette` 由调用方给出（GUI 侧从 `ui/theme.py` 读令牌后注入，
  core 不得 import ui）；本模块只负责把色值填进 CSS 变量。
* **打印友好**：`@media print` 把 CSS 变量整体切成浅色、隐藏带 `no-print`
  的交互板块，并强制展示明细表 —— 「打印 / 存 PDF」即可当行程单交差。
"""
from __future__ import annotations

import html as _html
import re

__all__ = [
    "esc", "date_parts", "short_date", "time_part", "fmt_km", "fmt_money",
    "SHARED_CSS", "BASE_APP_JS", "render_page", "money_strip", "DASH",
]

#: 破折号占位（空值统一用它，别用 '-'
DASH = "—"

_DATE_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


# ─────────────────────── 小工具 ───────────────────────

def esc(s):
    return _html.escape(str(s if s is not None else ""), quote=True)


def date_parts(s):
    """从任意日期串里抠出 (年, 月, 日)，失败返回 (0, 0, 0)。"""
    m = _DATE_RE.search(str(s or ""))
    if not m:
        return (0, 0, 0)
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    except (TypeError, ValueError):
        return (0, 0, 0)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return (0, 0, 0)
    return (y, mo, d)


def short_date(s):
    y, mo, d = date_parts(s)
    return f"{y:04d}-{mo:02d}-{d:02d}" if y else (str(s) if s else "")


def time_part(s):
    """从时间串里抠出 HH:MM（兼容「2026年03月27日 08:12开」「2026-03-27 08:12」）。"""
    m = _TIME_RE.search(str(s or ""))
    if not m:
        return ""
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"


def fmt_km(v):
    return f"{float(v or 0):,.0f}"


def fmt_money(v):
    return f"￥{float(v or 0):,.2f}"


# ─────────────────────── 共享样式 ───────────────────────

SHARED_CSS = r"""
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:"PingFang SC","HarmonyOS Sans SC","Microsoft YaHei","Hiragino Sans GB",system-ui,-apple-system,"Segoe UI",sans-serif;
  background:var(--bg-from); color:var(--text);
  -webkit-font-smoothing:antialiased; overflow-x:hidden;
  font-variant-numeric:tabular-nums;
}
[hidden]{display:none!important}
/* ---------- 背景光晕 ---------- */
.bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}
.bg::before{
  content:"";position:absolute;inset:0;
  background:
    radial-gradient(900px 620px at 12% -6%, rgba(56,189,248,.16), transparent 62%),
    radial-gradient(820px 560px at 92% 4%, rgba(34,211,238,.13), transparent 60%),
    radial-gradient(1200px 800px at 50% 118%, rgba(59,130,246,.14), transparent 64%),
    linear-gradient(160deg, var(--bg-from) 0%, var(--bg-to) 100%);
}
.bg::after{
  content:"";position:absolute;inset:0;opacity:.5;
  background-image:linear-gradient(rgba(255,255,255,.028) 1px, transparent 1px),
                   linear-gradient(90deg, rgba(255,255,255,.028) 1px, transparent 1px);
  background-size:52px 52px;
  mask-image:radial-gradient(circle at 50% 30%, #000 0%, transparent 78%);
  -webkit-mask-image:radial-gradient(circle at 50% 30%, #000 0%, transparent 78%);
}
/* 背景光斑：用 radial-gradient 直接画出柔和衰减，替掉 filter:blur(70px)。
   ⚠️ 实测 blur 滤镜会让「地图漫游（滚轮缩放/拖拽）」时的帧率从 60 掉到 31.7，
   而地图拖不动、线条跟不上手正是帧率不足的直接后果。渐变不额外花模糊开销。
   动画只跑 transform 并提升为合成层，不做重排重绘。 */
.orb{position:absolute;border-radius:50%;pointer-events:none;will-change:transform;
     animation:float 16s ease-in-out infinite}
.orb.a{width:820px;height:820px;background:radial-gradient(circle closest-side,rgba(56,189,248,.46) 0%,rgba(56,189,248,.30) 36%,rgba(56,189,248,0) 74%);top:-260px;left:-240px;animation-delay:-2s}
.orb.b{width:700px;height:700px;background:radial-gradient(circle closest-side,rgba(34,211,238,.40) 0%,rgba(34,211,238,.24) 36%,rgba(34,211,238,0) 74%);top:16%;right:-230px;animation-delay:-7s}
.orb.c{width:600px;height:600px;background:radial-gradient(circle closest-side,rgba(79,123,232,.40) 0%,rgba(79,123,232,.24) 36%,rgba(79,123,232,0) 74%);bottom:-210px;left:34%;animation-delay:-11s}
@keyframes float{0%,100%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(28px,-34px,0) scale(1.09)}}

.wrap{position:relative;z-index:1;max-width:1560px;margin:0 auto;padding:30px 32px 56px}

/* ---------- 头部 ---------- */
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;flex-wrap:wrap;
  padding-bottom:22px;border-bottom:1px solid var(--panel-border);margin-bottom:22px}
.hero h1{font-size:38px;font-weight:800;letter-spacing:.5px;line-height:1.12;
  background:linear-gradient(96deg,var(--accent-soft) 0%,var(--accent) 42%,var(--accent2) 108%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 6px 22px rgba(56,189,248,.24))}
.hero h1 .ic{display:inline-flex;align-items:center;margin-right:14px;vertical-align:-.06em;
  filter:drop-shadow(0 0 16px rgba(56,189,248,.45))}
.hero h1 .ic svg{display:block}
.ic-svg{display:inline-block;vertical-align:-.14em;flex:none}
.hero .sub{margin-top:9px;color:var(--text-dim);font-size:14px;letter-spacing:.3px}
.chips{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end;max-width:640px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:8px 14px;border-radius:999px;
  background:rgba(255,255,255,.045);border:1px solid var(--panel-border);
  font-size:12.5px;color:var(--text-dim);white-space:nowrap}
.chip b{color:var(--text);font-weight:600}
.chip .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 10px var(--accent)}

/* ---------- KPI ---------- */
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:16px;margin-bottom:20px}
.kpi{position:relative;padding:20px 20px 18px;border-radius:var(--radius);overflow:hidden;
  background:linear-gradient(168deg, rgba(255,255,255,.062), rgba(255,255,255,.018));
  border:1px solid var(--panel-border);
  box-shadow:0 18px 42px -26px rgba(0,0,0,.9), inset 0 1px 0 rgba(255,255,255,.05);
  transition:transform .35s cubic-bezier(.2,.7,.3,1), border-color .35s}
.kpi::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent, var(--k-c,var(--accent)), transparent);opacity:.85}
.kpi::after{content:"";position:absolute;right:-42px;bottom:-52px;width:132px;height:132px;border-radius:50%;
  background:radial-gradient(circle, var(--k-c,var(--accent)) 0%, transparent 70%);opacity:.16}
.kpi:hover{transform:translateY(-4px);border-color:rgba(56,189,248,.42)}
.kpi .lab{font-size:12.5px;color:var(--text-faint);letter-spacing:.6px;margin-bottom:10px}
.kpi .val{font-size:34px;font-weight:800;line-height:1;letter-spacing:-.5px;
  color:var(--text);text-shadow:0 4px 18px rgba(0,0,0,.4);word-break:break-all}
.kpi .unit{font-size:13px;font-weight:600;color:var(--text-dim);margin-left:5px;letter-spacing:0}
.kpi .sub{margin-top:9px;font-size:11.5px;color:var(--text-faint)}
.kpi.txt .val{font-size:24px}

/* ---------- 面板 ---------- */
.grid{display:grid;grid-template-columns:minmax(0,1.62fr) minmax(0,1fr);gap:18px}
.col{display:flex;flex-direction:column;gap:18px;min-width:0}
.panel{position:relative;border-radius:var(--radius);padding:18px 20px 20px;
  background:linear-gradient(170deg, rgba(255,255,255,.055), rgba(255,255,255,.014));
  border:1px solid var(--panel-border);
  box-shadow:0 22px 54px -32px rgba(0,0,0,.95), inset 0 1px 0 rgba(255,255,255,.05)}
.panel h2{font-size:15px;font-weight:700;display:flex;align-items:center;gap:9px;letter-spacing:.4px}
.panel h2::before{content:"";width:3px;height:14px;border-radius:2px;
  background:linear-gradient(180deg,var(--accent),var(--accent2))}
.panel .hint{font-size:11.5px;color:var(--text-faint);font-weight:400;margin-left:auto}
.panel-head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.tools{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.btn{cursor:pointer;font:inherit;font-size:11.5px;padding:6px 12px;border-radius:9px;
  color:var(--text-dim);background:rgba(255,255,255,.05);border:1px solid var(--panel-border);
  transition:.24s}
.btn:hover{color:var(--text);border-color:rgba(56,189,248,.5);background:rgba(56,189,248,.12)}
.btn.on{color:#0b1120;background:linear-gradient(100deg,var(--accent-soft),var(--accent));
  border-color:transparent;font-weight:700}

/* ---------- 报销金额汇总 ---------- */
.money-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 10px}
.money-head b{font-size:13.5px;letter-spacing:.6px;color:var(--text)}
.money-head .mh-sub{font-size:11.5px;color:var(--text-faint)}
.money{display:grid;grid-template-columns:repeat(auto-fit,minmax(146px,1fr));gap:10px;margin-bottom:16px}
.m-cell{padding:11px 13px;border-radius:12px;background:rgba(255,255,255,.04);
  border:1px solid var(--panel-border)}
.m-cell span{display:block;font-size:11px;color:var(--text-faint);letter-spacing:.5px;margin-bottom:6px}
.m-cell b{font-size:17px;font-weight:700;color:var(--text);letter-spacing:-.2px}
.m-cell.ok b{color:#34D399}
.m-cell.warn b{color:#FCA5A5}
.m-cell.dim b{color:var(--text-faint)}
.m-cell.wide{grid-column:span 2}
.m-cell.wide b{font-size:14px;color:var(--accent-soft);letter-spacing:1px;line-height:1.4}

/* ---------- 列表条 ---------- */
.rank{display:flex;flex-direction:column;gap:11px;margin-top:4px}
.rank-row{display:grid;grid-template-columns:26px 1fr auto;gap:10px;align-items:center;font-size:13px}
.rank-row .no{width:24px;height:24px;border-radius:7px;display:grid;place-items:center;
  font-size:11.5px;font-weight:700;color:var(--text-dim);background:rgba(255,255,255,.05);
  border:1px solid var(--panel-border)}
.rank-row.top1 .no{color:#0b1120;background:linear-gradient(120deg,var(--accent-soft),var(--accent));border-color:transparent}
.rank-row .nm{color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rank-row .nm small{color:var(--text-faint);margin-left:7px;font-size:11px}
.rank-row .cnt{color:var(--text-dim);font-size:12px;white-space:nowrap}
.bar{height:6px;border-radius:4px;background:rgba(255,255,255,.07);overflow:hidden;margin-top:6px}
.bar span{display:block;height:100%;border-radius:4px;
  background:linear-gradient(90deg,var(--accent2),var(--accent));
  box-shadow:0 0 12px rgba(56,189,248,.5);width:0;transition:width 1.1s cubic-bezier(.2,.8,.25,1)}

.chart{width:100%;height:266px}
.chart.sm{height:236px}
.chart.md{height:250px}

/* ---------- 行程时间轴 ----------
   刻意不做 max-height 滚动：本页要替代纸质行程单，明细需一眼看全
   （滚动框里的"折叠"会让人误以为只有前几条）。长页由页面滚动承担。 */
.tl{margin-top:6px}
.tl::-webkit-scrollbar{width:8px}
.tl::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:4px}
.tl-day{position:sticky;top:0;z-index:2;padding:8px 0 6px;font-size:11.5px;color:var(--text-faint);
  letter-spacing:1px;background:linear-gradient(180deg, var(--bg-to) 55%, transparent)}
.tl-day span{padding:2px 9px;border-radius:7px;background:rgba(255,255,255,.05);border:1px solid var(--panel-border)}
.tl-item{position:relative;display:grid;grid-template-columns:14px 1fr;gap:12px;padding:0 0 12px}
.tl-item::before{content:"";position:absolute;left:6px;top:14px;bottom:-2px;width:1px;
  background:linear-gradient(180deg,var(--panel-border),transparent)}
.tl-item:last-child::before{display:none}
.tl-node{width:9px;height:9px;margin-top:6px;border-radius:50%;justify-self:center;
  background:var(--accent);box-shadow:0 0 0 3px rgba(56,189,248,.16), 0 0 12px rgba(56,189,248,.55)}
.tl-item.muted .tl-node{background:var(--text-faint);box-shadow:0 0 0 3px rgba(140,160,190,.12)}
.tl-body{border-radius:12px;padding:11px 14px;background:rgba(255,255,255,.035);
  border:1px solid var(--panel-border);transition:.28s}
.tl-item:hover .tl-body{border-color:rgba(56,189,248,.42);background:rgba(56,189,248,.07);transform:translateX(3px)}
.tl-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tl-seq{font-size:10.5px;font-weight:700;color:var(--text-faint);background:rgba(255,255,255,.06);
  border:1px solid var(--panel-border);border-radius:6px;padding:2px 6px;letter-spacing:.6px}
.tl-train{font-weight:700;font-size:13.5px;color:var(--accent-soft);letter-spacing:.4px}
.tl-seat{font-size:11px;color:var(--text-dim);background:rgba(255,255,255,.04);
  border:1px solid var(--panel-border);border-radius:6px;padding:2px 8px}
.tl-time{margin-left:auto;font-size:11.5px;color:var(--text-dim);letter-spacing:.4px;flex:none}
.tag{font-style:normal;font-size:10.5px;padding:2px 8px;border-radius:6px;letter-spacing:.3px}
.tag-mod{color:#FDE68A;background:rgba(245,158,11,.16);border:1px solid rgba(245,158,11,.34)}
.tag-out{color:#A5F3FC;background:rgba(34,211,238,.14);border:1px solid rgba(34,211,238,.32)}
.tag-warn{color:#FCA5A5;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.32)}
.tl-route{display:flex;align-items:center;gap:9px;margin:7px 0 5px;font-size:14.5px;font-weight:600}
.tl-route .arw{color:var(--accent);font-size:15px;transition:transform .3s}
.tl-item:hover .tl-route .arw{transform:translateX(5px)}
.tl-route .tl-km{margin-left:auto;font-size:11.5px;font-weight:500;color:var(--text-faint);flex:none}
.tl-sub{display:flex;flex-wrap:wrap;gap:5px 16px;margin-top:9px;padding-top:9px;
  border-top:1px dashed var(--panel-border);font-size:11px;color:var(--text-faint)}
.tl-sub span{display:inline-flex;gap:5px;min-width:0}
.tl-sub i{font-style:normal;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis}
.tl-sub .id i{letter-spacing:.5px}
.tl-meta{font-size:11.5px;color:var(--text-faint);display:flex;justify-content:space-between;
  gap:10px;align-items:flex-end;margin-top:8px}
.tl-meta .amt{color:var(--accent-soft);font-weight:700;font-size:14px;text-align:right;line-height:1.25}
.tl-meta .amt .lb{display:block;font-size:10px;font-weight:400;color:var(--text-faint);letter-spacing:.4px}

/* ---------- 报销明细表 ---------- */
.dt-wrap{max-height:560px;overflow:auto;border-radius:12px;border:1px solid var(--panel-border);
  margin-top:2px}
table.dt{width:100%;border-collapse:collapse;font-size:11.5px;white-space:nowrap}
table.dt th{position:sticky;top:0;z-index:1;background:rgba(17,26,45,.97);color:var(--text-dim);
  font-weight:600;text-align:left;padding:9px 10px;border-bottom:1px solid var(--panel-border);
  letter-spacing:.5px}
table.dt td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.05);color:var(--text);
  vertical-align:top}
table.dt td.d{color:var(--text-dim)}
table.dt td.d i{display:block;font-style:normal;color:var(--text-faint);font-size:10.5px;margin-top:2px}
table.dt td.seq{color:var(--text-faint)}
table.dt td.tr{color:var(--accent-soft);font-weight:600}
table.dt td.num{text-align:right;font-variant-numeric:tabular-nums}
table.dt td.inv{font-size:10.5px;color:var(--text-dim);letter-spacing:.3px}
table.dt .amt{color:var(--accent-soft);font-weight:600}
table.dt .kbd{display:block;font-style:normal;margin-top:3px;font-size:10px;padding:1px 6px;
  border-radius:5px;width:max-content}
table.dt .k-refund{color:#FCA5A5;background:rgba(239,68,68,.16)}
table.dt .k-change{color:#FDE68A;background:rgba(245,158,11,.16)}
table.dt .k-warn{color:#FCD34D;background:rgba(245,158,11,.12)}
table.dt tbody tr:hover td{background:rgba(56,189,248,.07)}
table.dt tr.refund td{background:rgba(239,68,68,.075)}
table.dt tr.change td{background:rgba(245,158,11,.07)}
table.dt tfoot td{position:sticky;bottom:0;background:rgba(12,19,34,.97);font-weight:700;
  border-top:1px solid var(--panel-border);color:var(--text);padding:10px}
.det-note{margin-top:10px;font-size:11.5px;color:var(--text-faint);line-height:1.8}

/* ---------- 备注 ---------- */
.notes{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.note{border-radius:14px;padding:14px 16px;border:1px solid var(--panel-border);
  background:rgba(255,255,255,.03);font-size:12.5px;color:var(--text-dim);line-height:1.85}
.note b{color:var(--text)}
.note.warn{border-color:rgba(245,158,11,.36);background:rgba(245,158,11,.075)}
.note.info{border-color:rgba(34,211,238,.3);background:rgba(34,211,238,.06)}
.note .names{color:var(--text)}
.empty{padding:26px;text-align:center;color:var(--text-faint);font-size:13px}

footer{margin-top:26px;padding-top:16px;border-top:1px solid var(--panel-border);
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;
  font-size:11.5px;color:var(--text-faint);line-height:1.9}
footer .r{text-align:right}
.seal{display:inline-flex;align-items:center;gap:6px;color:var(--text-dim)}
.seal i{width:6px;height:6px;border-radius:50%;background:#34D399;box-shadow:0 0 9px #34D399;font-style:normal}

.reveal{opacity:0;transform:translateY(14px)}
.reveal.in{opacity:1;transform:none;transition:opacity .7s ease, transform .7s cubic-bezier(.2,.8,.25,1)}

@media (max-width:1280px){
  .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
  .grid{grid-template-columns:minmax(0,1fr)}
}
@media (max-width:760px){
  .wrap{padding:20px 16px 40px}
  .hero h1{font-size:26px}
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .kpi .val{font-size:27px}
  .m-cell.wide{grid-column:span 1}
}

/* ---------- 打印 / 存 PDF：切成浅色，只留汇总与明细 ---------- */
@page{margin:12mm}
@media print{
  :root{
    --bg-from:#ffffff; --bg-to:#ffffff; --panel:#ffffff; --panel-border:#c9d3e3;
    --text:#111827; --text-dim:#3f4a5e; --text-faint:#6b7280;
    --accent:#0369A1; --accent-soft:#075985; --accent2:#0E7490;
  }
  html,body{height:auto}
  body{background:#fff;color:#111827}
  .bg,.orb,.tools,.no-print{display:none!important}
  .wrap{max-width:none;padding:0}
  /* 打印走单列：A4 宽度下双列会把明细挤窄、还容易在栏间断页 */
  .grid{grid-template-columns:minmax(0,1fr)!important}
  .hero{margin-bottom:14px;padding-bottom:12px}
  .hero h1{filter:none}
  .kpi,.panel,.tl-body,.m-cell,.note{background:#fff!important;box-shadow:none!important;
    border-color:#c9d3e3!important}
  .kpi::after{display:none}
  .kpi .val{color:#111827;text-shadow:none}
  .chip{background:#f8fafc;border-color:#c9d3e3;color:#3f4a5e}
  .chip b{color:#111827}
  .chip .dot{box-shadow:none}
  .tl{max-height:none;overflow:visible}
  #timeline{display:none!important}
  #detailTable{display:block!important}
  .dt-wrap{max-height:none;overflow:visible;border-color:#c9d3e3}
  table.dt tr.refund td,table.dt tr.change td{background:#fff!important}
  table.dt tr.refund td{color:#7f1d1d}
  table.dt th{background:#f1f5f9;color:#111827;border-color:#c9d3e3}
  table.dt td{border-color:#e2e8f0;color:#111827}
  table.dt th,table.dt tfoot td{position:static}
  table.dt tfoot td{background:#f1f5f9!important;color:#111827}
  .panel,.tl-item,.dt-wrap{break-inside:avoid}
  footer{margin-top:16px}
}
"""


# ─────────────────────── 共享脚本 ───────────────────────
#: 头部徽章 / KPI 卡 / 入场动画 / 明细视图切换 / 打印 —— 两个模块共用。
#: 依赖页面模板已声明：P（payload）、pal、$、fmt。
BASE_APP_JS = r"""
/* ================= 头部 chips ================= */
function renderChips(list){
  $('#chips').innerHTML = (list || []).map(c =>
    '<span class="chip"><span class="dot"></span>' + c[0] + '：<b>' + c[1] + '</b></span>').join('');
}

/* ================= KPI 卡片 ================= */
function renderKpis(cards){
  $('#kpis').innerHTML = (cards || []).map(c => {
    const inner = (c.text !== undefined && c.text !== null && c.text !== '')
      ? String(c.text)
      : '<span data-count="' + c.count + '"' +
        (c.dec ? ' data-dec="' + c.dec + '"' : '') + '>0</span>';
    return '<div class="kpi reveal ' + (c.cls || '') + '" style="--k-c:' + (c.color || pal.accent) + '">' +
      '<div class="lab">' + c.lab + '</div>' +
      '<div class="val">' + inner + (c.unit ? '<span class="unit">' + c.unit + '</span>' : '') + '</div>' +
      '<div class="sub">' + (c.sub || '') + '</div>' +
    '</div>';
  }).join('');
}

/* ================= 入场动画 + 数字滚动 ================= */
function initReveal(){
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
  }), {threshold: .12});
  document.querySelectorAll('.reveal, .panel').forEach(el => io.observe(el));
  document.querySelectorAll('.bar span').forEach(el => setTimeout(() => { el.style.width = el.dataset.w + '%'; }, 220));
  const t0 = performance.now();
  document.querySelectorAll('[data-count]').forEach(el => {
    const target = parseFloat(el.dataset.count) || 0, dec = parseInt(el.dataset.dec || '0', 10);
    (function step(t){
      const kk = Math.min(1, (t - t0) / 1200), e = 1 - Math.pow(1 - kk, 3);
      el.textContent = fmt(target * e, dec);
      if (kk < 1) requestAnimationFrame(step);
    })(t0);
  });
}

/* ================= 明细视图切换 + 打印 ================= */
function bindDetailView(){
  const bTl = $('#btn-view-tl'), bTb = $('#btn-view-tb');
  const tl = $('#timeline'), tb = $('#detailTable');
  const setView = v => {
    if (bTl) bTl.classList.toggle('on', v === 'tl');
    if (bTb) bTb.classList.toggle('on', v === 'tb');
    if (tl) tl.hidden = (v !== 'tl');
    if (tb) tb.hidden = (v !== 'tb');
  };
  if (bTl) bTl.onclick = () => setView('tl');
  if (bTb) bTb.onclick = () => setView('tb');
  const bp = $('#btn-print');
  if (bp) bp.onclick = () => window.print();
}

/* ================= 自适应 ================= */
window.addEventListener('resize', () => {
  document.querySelectorAll('.chart').forEach(el => {
    const ins = (typeof echarts !== 'undefined') ? echarts.getInstanceByDom(el) : null;
    if (ins) ins.resize();
  });
  if (typeof mapChart !== 'undefined' && mapChart) mapChart.resize();
});
"""


# ─────────────────────── 组件 ───────────────────────

def money_strip(head, sub, cells, wide_label, wide_value):
    """报销汇总条。

    cells: [(标签, 值, 修饰类)]；修饰类可选 ok（绿）/ warn（红）/ dim（灰）。
    """
    body = "".join(
        f'<div class="m-cell {cls}"><span>{esc(lab)}</span><b>{esc(val)}</b></div>'
        for lab, val, cls in cells)
    body += (f'<div class="m-cell wide"><span>{esc(wide_label)}</span>'
             f'<b>{esc(wide_value)}</b></div>')
    return (f'<div class="money-head"><b>{esc(head)}</b>'
            f'<span class="mh-sub">{esc(sub)}</span></div>'
            f'<div class="money">{body}</div>')


# ─────────────────────── 页面骨架 ───────────────────────

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__HEAD_TITLE__</title>
<style>
:root{
  --bg-from:__BG_FROM__; --bg-to:__BG_TO__;
  --panel:__PANEL__; --panel-border:__PANEL_BORDER__;
  --accent:__ACCENT__; --accent-soft:__ACCENT_SOFT__; --accent2:__ACCENT2__;
  --text:__TEXT__; --text-dim:__TEXT_DIM__; --text-faint:__TEXT_FAINT__;
  --radius:18px;
}
__SHARED_CSS__
__MODULE_CSS__
</style>
</head>
<body>
<div class="bg"><span class="orb a"></span><span class="orb b"></span><span class="orb c"></span></div>

<div class="wrap">
  <header class="hero">
    <div>
      <h1><span class="ic">__HERO_ICON__</span>__TITLE__</h1>
      <div class="sub">__SUBTITLE__</div>
    </div>
    <div class="chips" id="chips"></div>
  </header>

  <section class="kpis" id="kpis"></section>

__BODY__

  <footer>
    <div>__FOOTER_LEFT__</div>
    <div class="r">__FOOTER_RIGHT__<br><span class="seal"><i></i>本机离线渲染 · 无外部请求</span></div>
  </footer>
</div>

__ECHARTS_BLOCK__
<script>
const P = __PAYLOAD__;
const pal = P.palette;
const $ = s => document.querySelector(s);
const fmt = (n, d) => Number(n).toLocaleString('zh-CN', {minimumFractionDigits: d||0, maximumFractionDigits: d||0});
__APP_JS__
</script>
</body>
</html>
"""


def render_page(*, head_title, palette, module_css, hero_icon, title, subtitle,
                body, footer_left, footer_right, app_js, echarts_js=None,
                payload_json=""):
    """把数据与资源填进页面骨架，返回完整 HTML 文本。"""
    pal = palette
    doc = (PAGE_TEMPLATE
           .replace("__HEAD_TITLE__", esc(head_title))
           .replace("__BG_FROM__", pal["bg_from"]).replace("__BG_TO__", pal["bg_to"])
           .replace("__PANEL__", pal["panel"]).replace("__PANEL_BORDER__", pal["panel_border"])
           .replace("__ACCENT_SOFT__", pal["accent_soft"]).replace("__ACCENT2__", pal["accent2"])
           .replace("__ACCENT__", pal["accent"])
           .replace("__TEXT_DIM__", pal["text_dim"]).replace("__TEXT_FAINT__", pal["text_faint"])
           .replace("__TEXT__", pal["text"])
           .replace("__SHARED_CSS__", SHARED_CSS)
           .replace("__MODULE_CSS__", module_css)
           .replace("__HERO_ICON__", hero_icon)
           .replace("__TITLE__", esc(title))
           .replace("__SUBTITLE__", esc(subtitle))
           .replace("__BODY__", body)
           .replace("__FOOTER_LEFT__", footer_left)
           .replace("__FOOTER_RIGHT__", footer_right)
           .replace("__PAYLOAD__", payload_json)
           .replace("__APP_JS__", BASE_APP_JS + "\n" + app_js))
    if echarts_js:
        doc = doc.replace("__ECHARTS_BLOCK__", "<script>" + echarts_js + "</script>")
    else:
        doc = doc.replace("__ECHARTS_BLOCK__", "")
    return doc
