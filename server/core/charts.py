# -*- coding: utf-8 -*-
"""core/charts.py — 行程单用的高级可视化组件（柱状图 / 饼图 / 折线图）。

基于 reportlab.graphics 内置图表，无需额外依赖。供 train / hotel 行程单复用。
所有函数返回 reportlab Drawing（可直接 append 到 platypus elements）。

设计目标：干净留白、柔和网格、圆角卡片、数值标签、品牌配色 —— 取代旧版简陋图表。
"""
from reportlab.lib import colors
from reportlab.graphics.shapes import (
    Drawing, String, Rect, Line, Circle, Polygon, PolyLine,
)
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.linecharts import HorizontalLineChart

from .config import FONT_NAME

_CARD_BORDER = colors.HexColor('#e8edf3')
_CARD_BG = colors.HexColor('#ffffff')
_TITLE = colors.HexColor('#1f2a44')
_LABEL = colors.HexColor('#64748b')
_GRID = colors.HexColor('#eef2f7')
_SUB = colors.HexColor('#94a3b8')


def _card(d, width, height):
    d.add(Rect(0, 0, width, height,
               fillColor=_CARD_BG, strokeColor=_CARD_BORDER, strokeWidth=1,
               rx=10, ry=10))


def _title(d, width, height, text):
    d.add(String(14, height - 16, text,
                 fontName=FONT_NAME, fontSize=10.5, fillColor=_TITLE))


def _fmt(v):
    """金额简洁显示：整数不带小数，大数加千分位。"""
    try:
        f = float(v)
    except Exception:
        return str(v)
    if abs(f - round(f)) < 1e-6:
        return f"{int(round(f)):,}"
    return f"{f:,.1f}"


def _wrap_label(text, max_chars):
    """按字符数折行（中文按 1 字符计），返回行列表。"""
    text = str(text)
    if len(text) <= max_chars:
        return [text]
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def short_date_label(s):
    """将 '2026年07月21日' / '2026-07-21 14:30' 等统一缩为 'MM-DD'。"""
    if not s:
        return ''
    s = str(s).replace('年', '-').replace('月', '-').replace('日', '').strip('-')
    parts = [p for p in s.split('-') if p]
    if len(parts) >= 3:
        day = parts[2].split(' ')[0]
        return f"{parts[1]}-{day}"
    return s


def bar_chart(values, labels, title, color_hex, width=470, height=180):
    """每张票据金额柱状图（圆角柱 + 数值标签 + 柔和网格）。"""
    d = Drawing(width, height)
    _card(d, width, height)
    if not values:
        _title(d, width, height, title + "（暂无数据）")
        return d

    pad_l, pad_r, pad_t, pad_b = 42, 16, 34, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_b
    mx = max(values) or 1

    # 水平网格
    for i in range(5):
        gy = base_y + plot_h * i / 4
        d.add(Line(pad_l, gy, width - pad_r, gy,
                   strokeColor=_GRID, strokeWidth=0.5))
        d.add(String(pad_l - 6, gy - 2.5,
                     _fmt(mx * i / 4),
                     fontName=FONT_NAME, fontSize=6.5,
                     fillColor=_SUB, textAnchor='end'))

    n = len(values)
    gap = plot_w / n
    bw = min(30, gap * 0.6)
    col = colors.HexColor(color_hex)
    for i, v in enumerate(values):
        x = pad_l + gap * i + (gap - bw) / 2
        h = max(2.0, plot_h * (v / mx))
        d.add(Rect(x, base_y, bw, h,
                   fillColor=col, strokeColor=None, rx=4, ry=4))
        d.add(String(x + bw / 2, base_y + h + 4,
                     _fmt(v), fontName=FONT_NAME, fontSize=7.5,
                     fillColor=_LABEL, textAnchor='middle'))
        d.add(String(x + bw / 2, base_y - 12,
                     str(labels[i]) if labels and i < len(labels) else '',
                     fontName=FONT_NAME, fontSize=7,
                     fillColor=_LABEL, textAnchor='middle'))
    _title(d, width, height, title)
    return d


def pie_chart(slices, labels, title, color_hexes, width=470, height=None):
    """构成占比环形图（带中心合计 + 图例百分比），按名称长度自适应高度与折行。"""
    if not slices or sum(slices) <= 0:
        d = Drawing(width, 180)
        _card(d, width, 180)
        _title(d, width, 180, title + "（暂无数据）")
        return d

    total = float(sum(slices))
    pie_d = 130
    legend_x = pie_d + 30
    legend_w = max(120, width - legend_x - 14)
    max_chars = max(8, int(legend_w / 7.2))

    # 预计算图例行（支持多行标签 + 百分比）
    rows = []
    for lab, val in zip(labels, slices):
        wl = _wrap_label(lab, max_chars)
        pct = val / total * 100 if total else 0
        rows.append((wl, val, pct))
    line_h = 14
    legend_h = sum(max(1, len(wl)) for wl, _, _ in rows) * line_h + 8
    height = height or max(pie_d + 44, legend_h + 16)

    d = Drawing(width, height)
    _card(d, width, height)

    # 左侧饼图
    pie = Pie()
    pie.x = (legend_x - pie_d) / 2 - 6
    pie.y = (height - pie_d) / 2
    pie.width = pie_d
    pie.height = pie_d
    pie.data = slices
    pie.innerRadiusFraction = 0.6
    pie.slices.strokeWidth = 2
    pie.slices.strokeColor = colors.white
    for i, c in enumerate(color_hexes[:len(slices)]):
        pie.slices[i].fillColor = colors.HexColor(c)
    d.add(pie)
    d.add(String(pie.x + pie.width / 2, pie.y + pie.height / 2 - 3,
                 "￥%s" % _fmt(total),
                 fontName=FONT_NAME, fontSize=9,
                 fillColor=_TITLE, textAnchor='middle'))

    # 图例（多行标签 + 百分比）
    y = height - 14
    for i, (wl, val, pct) in enumerate(rows):
        n = len(wl)
        d.add(Rect(legend_x, y - 9, 10, 10,
                   fillColor=colors.HexColor(color_hexes[i % len(color_hexes)]),
                   strokeColor=colors.white, strokeWidth=0.5, rx=2, ry=2))
        for j, ln in enumerate(wl):
            txt = "%s  ￥%s (%d%%)" % (ln, _fmt(val), round(pct)) if j == n - 1 else ln
            d.add(String(legend_x + 14, y - 2 - j * line_h,
                         txt, fontName=FONT_NAME, fontSize=8, fillColor=_LABEL))
        y -= n * line_h + 4
    _title(d, width, height, title)
    return d


def line_chart(values, labels, title, color_hex, width=470, height=180):
    """累计金额折线图（渐变面积 + 折线 + 圆点 + 起止数值）。"""
    d = Drawing(width, height)
    _card(d, width, height)
    if not values:
        _title(d, width, height, title + "（暂无数据）")
        return d

    pad_l, pad_r, pad_t, pad_b = 42, 16, 34, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_b
    mx = max(values) or 1

    for i in range(5):
        gy = base_y + plot_h * i / 4
        d.add(Line(pad_l, gy, width - pad_r, gy,
                   strokeColor=_GRID, strokeWidth=0.5))
        d.add(String(pad_l - 6, gy - 2.5,
                     _fmt(mx * i / 4),
                     fontName=FONT_NAME, fontSize=6.5,
                     fillColor=_SUB, textAnchor='end'))

    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad_l + (plot_w * (i / (n - 1)) if n > 1 else plot_w / 2)
        y = base_y + plot_h * (v / mx)
        pts.append((x, y))

    col = colors.HexColor(color_hex)
    # 面积（Polygon 同样需要扁平数字列表 [x1,y1,x2,y2,...]）
    area = [pad_l, base_y]
    for x, y in pts:
        area.extend([x, y])
    area.extend([pts[-1][0], base_y])
    d.add(Polygon(area, fillColor=col, fillOpacity=0.12, strokeColor=None))
    # 折线（PolyLine 需要扁平数字列表 [x1,y1,x2,y2,...]，非坐标对）
    flat = []
    for x, y in pts:
        flat.extend([x, y])
    d.add(PolyLine(flat, strokeColor=col, strokeWidth=2.2))
    # 圆点 + 起止数值 + 稀疏刻度
    step = max(1, n // 6)
    for i, (x, y) in enumerate(pts):
        d.add(Circle(x, y, 2.6, fillColor=colors.white,
                     strokeColor=col, strokeWidth=1.5))
        if i in (0, n - 1):
            d.add(String(x, y + 5, _fmt(values[i]),
                         fontName=FONT_NAME, fontSize=7,
                         fillColor=_LABEL, textAnchor='middle'))
        if labels and i % step == 0:
            d.add(String(x, base_y - 12,
                         str(labels[i]) if i < len(labels) else '',
                         fontName=FONT_NAME, fontSize=7,
                         fillColor=_LABEL, textAnchor='middle'))
    _title(d, width, height, title)
    return d
