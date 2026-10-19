# -*- coding: utf-8 -*-
"""core/train.py — 火车票解析与行程单生成。依赖 pdfplumber / pypdf / reportlab / PIL。"""
import os
import re
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                 Paragraph, Spacer, HRFlowable, Flowable,
                                 Image as RLImage)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.graphics.shapes import Drawing, Line, Circle, String, PolyLine, Rect

from .config import FONT_NAME
from .merge import detect_special
from .parse import (_unique_path, amount_upper, extract_pdf_texts, file_md5,
                    parse_date, collect_files, DedupRegistry,
                    _normalize_cjk, _flatten)
from . import trainmap

# ── 预编译正则（模块级） ──
_RE_TRAIN_INVOICE_NO = re.compile(r'发票号码[:：]\s*(\d{15,20})')
_RE_TRAIN_INVOICE_DATE = re.compile(r'开票日期[:：]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_RE_TRAIN_STATION = re.compile(r'([一-鿿]+)\s+[A-Z]\d+\s+([一-鿿]+)')
_RE_TRAIN_NO = re.compile(r'(G\d+|D\d+|K\d+|Z\d+|T\d+|C\d+)')
_RE_TRAIN_DEPART = re.compile(r'(\d{4}年\d{2}月\d{2}日\s*\d{2}:\d{2})开')
_RE_TRAIN_DATE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_RE_TRAIN_SEAT = re.compile(r'(\d+)车(\d+[A-F]?)号')
_RE_TRAIN_SEAT_CLASS = re.compile(
    r'(无座|二等座|一等座|商务座|特等座|软卧|硬卧|硬座|软座|'
    r'下铺|上铺|中铺|动卧|高级软卧|一等卧|二等卧)')
_RE_TRAIN_AMT_LABELED = re.compile(r'[¥￥]\s*(\d+\.?\d*)\s*(?:票价|退票费|改签费)')
_RE_TRAIN_AMT = re.compile(r'[¥￥](\d+\.?\d*)')
# 乘客 = 脱敏证件号（含 ****）之后紧邻的姓名（修复旧 bug：误抓税务局印章）
# 懒惰匹配 + 边界，避免吞掉后续「电子客票号」等标签文字
_RE_TRAIN_ID_NAME = re.compile(
    r'(\d+\*{3,}\d+)\s*([一-鿿·]{2,4}?)(?=\s*电子客票号|\s*购票|\s*买票|$)')
_RE_TRAIN_NAME = re.compile(r'(?:乘客|姓名|旅客)[:：]?\s*([一-鿿·]{2,4})')
_RE_TRAIN_TICKET_NO = re.compile(r'电子客票号[:：]\s*(\d+)')
_RE_TRAIN_FN_AMOUNT = re.compile(r'(\d{6})_(\d+\.?\d*)_')
# 车次标准形态：G/D/C/K/T/Z + 2~5 位数字。
# ⚠️ 两字符的「T3」是网约车车型、「T2航站楼」是机场航站楼，都不是车次。
_RE_TRAIN_NO_STRICT = re.compile(r'^[GDCKTZ]\d{2,5}$')
_RE_CJK = re.compile(r'[一-鿿]')


def _valid_trip(info):
    """出口校验：行程三要素（车次 / 发站 / 到站）是否都像一张真的火车票。

    原则：**宁可判为「无法解析」，也不返回垃圾**。
    v0.4 的「交通费」模块要让打车 / 机票 / 行程单也填 from_/to_ 并画进地图，
    只要这里漏出垃圾值（把网约车车型「T3」当成车次、把机场航站楼「T2」当成车次），
    地图上就会画出一条根本不存在的路线 —— 假数据比解析失败更误导用户。
    """
    if not _RE_TRAIN_NO_STRICT.match(info.get('train') or ''):
        return False                       # 车次形态不对（含 "T3" 这类两字符车型）
    f = info.get('from_') or ''
    t = info.get('to_') or ''
    if f in ('', '未知') or t in ('', '未知'):
        return False                       # 起/终点为空
    if f == t:
        return False                       # 起终点相同
    if len(f) < 2 or len(t) < 2:
        return False                       # 单字碎片
    # 站名至少 2 个汉字：车型「T3」、航站楼「T2航站楼」等碎片凑不出 2 个汉字
    if len(_RE_CJK.findall(f)) < 2 or len(_RE_CJK.findall(t)) < 2:
        return False
    return True


def parse_train_ticket_text(raw_text, filename=''):
    """从单张火车票（PDF 或 OFD 提取文本）解析结构化字段。

    返回字典——保留旧键（train_date, from_, to_, train, seat, amount, passenger,
    special_type, invoice_no 等）并新增：invoice_date, car_no, seat_no, seat_class,
    id_no(脱敏), ticket_no(电子客票号), fare_label。
    """
    nt = _normalize_cjk(raw_text)
    info = {
        'invoice_no': '', 'invoice_date': '', 'train_date': '未知',
        'from_': '未知', 'to_': '未知', 'train': '未知',
        'car_no': '', 'seat_no': '', 'seat_class': '-', 'seat': '-',
        'amount': 0.0, 'fare_label': '票价',
        'passenger': '未知', 'id_no': '', 'ticket_no': '', 'special_type': None,
        'trip_valid': False,
    }

    # 发票号码 / 开票日期
    m = _RE_TRAIN_INVOICE_NO.search(nt)
    if m:
        info['invoice_no'] = m.group(1)
    m = _RE_TRAIN_INVOICE_DATE.search(nt)
    if m:
        info['invoice_date'] = f"{int(m.group(1))}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"

    # 出发站 / 到达站（站名可能与「站」粘连，去掉尾随 站）
    # 先剔除「国家税务总局 / X市税务局」等开票方前缀，避免被误并入站名
    # （例如文本被压成「北京市税务局北京南 G8941 天津站站」会误识别成「北京市税务局北京南」）
    nt_station = re.sub(r'国家税务总局|[一-鿿]{2,6}?市税务局', ' ', nt)
    for m in _RE_TRAIN_STATION.finditer(nt_station):
        f = m.group(1).rstrip('站')
        t = m.group(2).rstrip('站')
        # 兜底：若仍夹带开票方字样则跳过本条匹配
        if '税务局' in f or '国家' in f or '总局' in f:
            continue
        if len(f) >= 2 and len(t) >= 2:
            info['from_'], info['to_'] = f, t
            break

    # 车次
    m = _RE_TRAIN_NO.search(nt)
    if m:
        info['train'] = m.group(1)

    # 开车日期时间（优先带「开」的乘车日期）
    m = _RE_TRAIN_DEPART.search(nt)
    if m:
        info['train_date'] = m.group(1)
    else:
        m = _RE_TRAIN_DATE.search(nt)
        if m:
            info['train_date'] = f"{int(m.group(1))}年{int(m.group(2)):02d}月{int(m.group(3)):02d}日"

    # 车厢 / 座位号（与席别分开提取，兼容「下铺/无座」等无「座」字情形）
    m = _RE_TRAIN_SEAT.search(nt)
    if m:
        info['car_no'] = m.group(1)
        info['seat_no'] = m.group(2)
    m = _RE_TRAIN_SEAT_CLASS.search(nt)
    if m:
        info['seat_class'] = m.group(1)
    parts = []
    if info['car_no']:
        parts.append(f"{info['car_no']}车{info['seat_no']}号")
    elif info['seat_no']:
        parts.append(f"{info['seat_no']}号")
    if info['seat_class'] != '-':
        parts.append(info['seat_class'])
    info['seat'] = ' '.join(parts) if parts else '-'

    # 类型（退票/改签）
    info['special_type'] = detect_special(nt)
    if info['special_type'] == '退票':
        info['fare_label'] = '退票费'
    elif info['special_type'] == '改签':
        info['fare_label'] = '改签费'
    else:
        info['fare_label'] = '票价'

    # 金额：优先带「票价/退票费/改签费」标签的 ¥
    m = _RE_TRAIN_AMT_LABELED.search(nt)
    if m:
        info['amount'] = float(m.group(1))
    else:
        amts = [float(x) for x in _RE_TRAIN_AMT.findall(nt)]
        if amts:
            info['amount'] = max(amts)

    # 乘客 + 脱敏证件号（锚定 ****，杜绝印章污染）
    m = _RE_TRAIN_ID_NAME.search(nt)
    if m:
        info['id_no'] = m.group(1)
        info['passenger'] = m.group(2)
    else:
        m = _RE_TRAIN_NAME.search(nt)
        if m:
            info['passenger'] = m.group(1)

    # 电子客票号
    m = _RE_TRAIN_TICKET_NO.search(nt)
    if m:
        info['ticket_no'] = m.group(1)

    # ── 出口校验：行程三要素不像真火车票就整体判空 ──
    # 非火车票（网约车行程单 / 机票 / 住宿票…）会被 _RE_TRAIN_NO 之类宽松正则抓出
    # 假车次（T3 / T2 / C2 / D60 / Z01…），这里一律清掉，只留 trip_valid=False 供上层判断。
    info['trip_valid'] = _valid_trip(info)
    if not info['trip_valid']:
        info.update(from_='未知', to_='未知', train='未知', train_date='未知',
                    car_no='', seat_no='', seat_class='-', seat='-',
                    amount=0.0, passenger='未知', id_no='', ticket_no='',
                    special_type=None, fare_label='票价')
    return info


def parse_train_tickets(folder_or_files, is_folder, deduplicate, progress_cb=None):
    """解析火车票（支持 .pdf 与 .ofd）。复用 parse.collect_files / DedupRegistry。"""
    file_list = collect_files(folder_or_files, is_folder, ('.pdf', '.ofd'))
    if not file_list:
        return None, "未找到发票文件"

    reg = DedupRegistry() if deduplicate else None
    invoices, all_passengers, failed_files = [], set(), []

    for idx, (fp, filename) in enumerate(file_list):
        if progress_cb:
            progress_cb(idx + 1, len(file_list), f"解析: {filename[:25]}...")

        if reg and reg.check_md5(fp, filename):
            continue

        raw_text, read_error = extract_pdf_texts(fp)
        if read_error:
            failed_files.append({"filename": filename, "reason": read_error})
            continue

        # 文件名金额提示（兜底）
        fn_amount = 0.0
        nm = _RE_TRAIN_FN_AMOUNT.match(filename)
        if nm:
            fn_amount = float(nm.group(2))

        d = parse_train_ticket_text(raw_text, filename)
        if fn_amount > 0 and d['amount'] <= 0:
            d['amount'] = fn_amount

        # 业务指纹去重（车次+日期+出发→到达+金额）
        if reg:
            fp_str = f"{d['train']}|{d['train_date']}|{d['from_']}|{d['to_']}|{d['amount']:.2f}"
            if d['train'] != '未知' and reg.check_fingerprint(
                    fp_str, filename,
                    f"车次{d['train']}、日期{d['train_date']}、{d['from_']}→{d['to_']}、金额{d['amount']:.2f}元均相同"):
                continue

        review_fields = []
        if d['train'] == '未知':
            review_fields.append('车次')
        if d['train_date'] == '未知':
            review_fields.append('出发时间')
        if d['from_'] == '未知' or d['to_'] == '未知':
            review_fields.append('站点')
        if d['amount'] <= 0:
            review_fields.append('金额')
        d.update(
            seq=idx,
            train_date_sortable=parse_date(d['train_date']),
            filename=filename, filepath=fp,
            needs_review=bool(review_fields),
            review_reason='、'.join(review_fields),
        )
        if d['passenger'] != '未知':
            all_passengers.add(d['passenger'])
        invoices.append(d)

    if not invoices:
        return None, ("去重后无发票" if deduplicate else "未找到有效火车票")

    invoices.sort(key=lambda x: x['train_date_sortable'])
    for i, inv in enumerate(invoices, 1):
        inv['seq'] = i

    total = sum(x['amount'] for x in invoices)
    refund = sum(x['amount'] for x in invoices if x['special_type'] == '退票')
    change = 0  # 改签不产生额外费用，归入正常车票
    normal = total - refund

    return dict(
        invoices=invoices, total_amount=total,
        refund_amount=refund, change_amount=change, normal_amount=normal,
        passengers=list(all_passengers), skipped=reg.skipped if reg else 0,
        failed_files=failed_files,
        review_count=sum(1 for x in invoices if x['needs_review']),
        dedup_details=reg.dedup_details if reg else [],
    ), None


def build_train_itinerary_pdf(invoices, total_amount, refund_amount,
                               change_amount, normal_amount, output_folder,
                               map_palette=None, include_map=True):
    """生成《火车票行程报销单》PDF。

    include_map=True 时，在数据可视化之后追加「行程流转地图」+ 年度数据卡；
    map_palette 由 ui 层从设计令牌读出后注入（core 不得 import ui）。
    """
    out = _unique_path(os.path.join(output_folder, "火车票行程单.pdf"))
    doc = SimpleDocTemplate(out, pagesize=A4, topMargin=0.8 * cm, bottomMargin=1.2 * cm)
    elements, styles = [], getSampleStyleSheet()
    elements.append(Paragraph("火车票行程报销单",
        ParagraphStyle('T', parent=styles['Heading1'], fontName=FONT_NAME, fontSize=22,
                       alignment=TA_CENTER, textColor=colors.HexColor('#1a1a2e'), spaceAfter=2)))
    elements.append(Paragraph("Train Ticket Itinerary Report",
        ParagraphStyle('ST', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10,
                       alignment=TA_CENTER, textColor=colors.HexColor('#7f8c8d'), spaceAfter=8)))
    elements.append(HRFlowable(width="80%", thickness=2, color=colors.HexColor('#4472C4'), spaceAfter=10))

    dates = [i['train_date'] for i in invoices if i['train_date'] != '未知']
    dr = (f"{dates[0].split('日')[0]} 至 {dates[-1].split('日')[0]}"
          if len(dates) > 1 else (dates[0] if dates else '未知'))
    passengers = sorted({i['passenger'] for i in invoices
                         if i['passenger'] not in ('未知', '')})

    ss = ParagraphStyle('S', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10, leading=18)
    left = (f"<b>行程日期：</b>{dr}<br/><b>发票数量：</b>{len(invoices)} 张"
            + (f"<br/><b>乘车人：</b>{'、'.join(passengers)}" if passengers else ""))
    right = (f"<b>票面总金额：</b>￥{total_amount:.2f}<br/><b>正常车票：</b>￥{normal_amount:.2f}")
    if refund_amount > 0:
        right += f"<br/><font color='#cc0000'><b>退票费：</b>￥{refund_amount:.2f}</font>"
    t = Table([[Paragraph(left, ss), Paragraph(right, ss)]], [8.5 * cm, 8.5 * cm])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 16), ('TOPPADDING', (0, 0), (-1, -1), 12)]))
    elements.append(t); elements.append(Spacer(1, 12))


    # 明细表（含乘车人）
    hs = ParagraphStyle('H', parent=styles['Normal'], fontName=FONT_NAME, fontSize=9,
                         textColor=colors.white, alignment=TA_CENTER)
    cs = ParagraphStyle('C', parent=styles['Normal'], fontName=FONT_NAME, fontSize=8.5,
                         alignment=TA_CENTER, leading=11)
    rows = [[Paragraph(x, hs) for x in
             ['序号', '开车时间', '出发站', '到达站', '车次', '席别', '乘客', '金额']]]
    for inv in invoices:
        dd = inv['train_date'].replace('年', '-').replace('月', '-').replace('日', '').strip()
        if dd == '未知':
            dd = '-'
        amt = f"￥{inv['amount']:.2f}"
        if inv['special_type'] == '退票':
            amt = Paragraph(f'<font color="#cc0000"><b>{amt} 退票</b></font>', cs)
        elif inv['special_type'] == '改签':
            amt = Paragraph(f'<font color="#ff6600"><b>{amt} 改签</b></font>', cs)
        else:
            amt = Paragraph(amt, cs)
        psg = inv['passenger'] if inv['passenger'] not in ('未知', '') else '-'
        rows.append([Paragraph(str(inv['seq']), cs), Paragraph(dd, cs),
                     Paragraph(inv['from_'], cs), Paragraph(inv['to_'], cs),
                     Paragraph(inv['train'], cs), Paragraph(inv['seat'] or '-', cs),
                     Paragraph(psg, cs), amt])
    ts = ParagraphStyle('TS', parent=styles['Normal'], fontName=FONT_NAME, fontSize=9, alignment=TA_CENTER)
    rows.append([Paragraph('', ts)] * 7 + [Paragraph(f'<b>￥{total_amount:.2f}</b>', ts)])
    tb = Table(rows, [1.1 * cm, 2.6 * cm, 2.0 * cm, 2.0 * cm, 1.4 * cm, 1.9 * cm, 2.0 * cm, 2.0 * cm])
    ts_list = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTNAME', (0, 0), (-1, 0), FONT_NAME),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10),
        ('FONTNAME', (0, 1), (-1, -2), FONT_NAME),
        ('TOPPADDING', (0, 1), (-1, -2), 6), ('BOTTOMPADDING', (0, 1), (-1, -2), 6),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, -1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
    ]
    ri = 1
    for inv in invoices:
        if inv['special_type'] == '退票':
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#ffe0e0')))
        elif inv['special_type'] == '改签':
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#fff0d0')))
        elif ri % 2 == 0:
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#f0f5ff')))
        ri += 1
    tb.setStyle(TableStyle(ts_list)); elements.append(tb); elements.append(Spacer(1, 12))

    # 注：原「📊 数据可视化分析」板块（各票据金额柱状图 / 金额构成饼图 / 累计金额折线图）
    # 已按需求整体移除 —— 交付形态改为《火车票行程流转图》HTML，行程类可视化统一在那里呈现。

    # 行程流转地图（火车轨迹，纯矢量手绘，离线本地渲染）——HTML 生成失败时的回退
    if include_map:
        try:
            elements.extend(trainmap.build_trainmap_section(
                invoices, palette=map_palette))
        except Exception:
            # 地图是增值内容，任何异常都不应让行程单生成失败
            from .config import _log_error
            import traceback as _tb
            _log_error("行程流转地图生成失败: %s\n%s" % ("error", _tb.format_exc()))

    upper = amount_upper(total_amount)
    elements.append(Paragraph(f"合计（大写）：<b>{upper}</b>",
        ParagraphStyle('UP', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10,
                       textColor=colors.HexColor('#c0392b'), alignment=TA_CENTER, spaceAfter=6)))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#dee2e6')))
    elements.append(Paragraph(
        f"共 {len(invoices)} 张 | 生成日期：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 由「报销助手」生成",
        ParagraphStyle('FT', parent=styles['Normal'], fontName=FONT_NAME, fontSize=7,
                       textColor=colors.HexColor('#999999'), alignment=TA_CENTER)))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#4472C4'), spaceBefore=4))
    doc.build(elements)
    return out
