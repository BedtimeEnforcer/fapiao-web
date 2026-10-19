# -*- coding: utf-8 -*-
"""core/hotel.py — 酒店发票解析与行程单生成。依赖 pdfplumber / pypdf / reportlab / PIL。"""
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

from .config import FONT_NAME
from .parse import (_unique_path, amount_upper, extract_invoice_fields,
                    extract_pdf_texts, parse_date, collect_files, DedupRegistry)
from . import charts

_RE_STAY = re.compile(
    r'(\d{4})[年.\-](\d{1,2})[月.\-](\d{1,2})日?\s*[至到\-]\s*'
    r'(\d{4})[年.\-]?(\d{1,2})[月.\-]?(\d{1,2})日?')
_RE_STAY2 = re.compile(
    r'(\d{4}[年.\-]\d{1,2}[月.\-]\d{1,2}日?)\s*[至到\-]\s*'
    r'(\d{4}[年.\-]\d{1,2}[月.\-]\d{1,2}日?)')


def _extract_stay_dates(remarks, raw_text):
    """从备注或全文提取入住-退房日期区间（住宿发票特有）。"""
    rm = _RE_STAY.search(remarks or '')
    if rm:
        checkin = f"{int(rm.group(1))}年{int(rm.group(2)):02d}月{int(rm.group(3)):02d}日"
        checkout = f"{int(rm.group(4))}年{int(rm.group(5)):02d}月{int(rm.group(6)):02d}日"
        return checkin, checkout
    rm = _RE_STAY2.search(raw_text or '')
    if rm:
        return rm.group(1), rm.group(2)
    return '', ''


def parse_hotel_invoices(folder_or_files, is_folder, deduplicate, progress_cb=None):
    """解析住宿发票（支持 .pdf 与 .ofd）。复用 parse.collect_files / DedupRegistry。"""
    file_list = collect_files(folder_or_files, is_folder, ('.pdf', '.ofd'))
    if not file_list:
        return None, "未找到发票文件"

    reg = DedupRegistry() if deduplicate else None
    records, failed_files = [], []

    for idx, (fp, filename) in enumerate(file_list):
        if progress_cb:
            progress_cb(idx + 1, len(file_list), f"解析: {filename[:25]}...")
        if reg and reg.check_md5(fp, filename):
            continue

        raw_text, read_error = extract_pdf_texts(fp)
        if read_error:
            failed_files.append({"filename": filename, "reason": read_error})
            continue
        info = extract_invoice_fields(raw_text)

        checkin, checkout = _extract_stay_dates(info.get('remarks', ''), raw_text)

        review_fields = []
        if not (info['seller_name'] or info['buyer_name']):
            review_fields.append("购销方")
        if not checkin or not checkout:
            review_fields.append("住宿日期")
        if not (checkin or checkout or info['invoice_date']):
            review_fields.append("开票日期")
        if info['amount'] <= 0:
            review_fields.append("金额")

        # 业务指纹去重
        if reg:
            fp_str = f"{info.get('seller_name','')}|{checkin or info.get('invoice_date','')}|{info['amount']:.2f}"
            if fp_str in reg._fp and info['seller_name']:
                reg.skipped += 1
                reg.dedup_details.append({
                    "kept": reg._fp_files.get(fp_str, "?"), "removed": filename,
                    "reason": f"酒店{info.get('seller_name','')[:15]}、日期{checkin or info.get('invoice_date','')}、金额{info['amount']:.2f}元均相同"})
                continue
            reg._fp.add(fp_str)
            reg._fp_files[fp_str] = filename

        # 票面没有住宿区间时，按业务口径用开票日作为退房日；不推算入住日。
        inferred_checkout = bool(not checkin and not checkout and info['invoice_date'])
        records.append(dict(
            seq=idx,
            hotel_name=info['seller_name'] or '未知酒店',
            guest_name='',
            buyer_name=info['buyer_name'] or '',
            checkin_date=checkin,
            checkout_date=checkout or (info['invoice_date'] if inferred_checkout else ''),
            checkout_inferred=inferred_checkout,
            amount=info['amount'], invoice_date=info['invoice_date'],
            item_name=info['item_name'],
            # 新增结构化字段（丰富展示与核对）
            invoice_no=info.get('invoice_no', ''),
            tax_id=info.get('tax_id', ''),
            seller_tax_id=info.get('seller_tax_id', ''),
            drawer=info.get('drawer', ''),
            filename=filename, filepath=fp,
            needs_review=bool(review_fields), review_reason="、".join(review_fields),
        ))

    if not records:
        return None, ("去重后无发票" if deduplicate else "未找到有效住宿发票")

    records.sort(key=lambda x: parse_date(x['checkin_date'] or x['checkout_date']
                                           or x['invoice_date']))
    for i, r in enumerate(records, 1):
        r['seq'] = i

    total = sum(r['amount'] for r in records)
    return dict(
        records=records, total_amount=total, skipped=reg.skipped if reg else 0,
        failed_files=failed_files, review_count=sum(1 for r in records if r['needs_review']),
        dedup_details=reg.dedup_details if reg else [],
    ), None


def build_hotel_itinerary_pdf(records, total_amount, output_folder):
    out = _unique_path(os.path.join(output_folder, "住宿行程单.pdf"))
    doc = SimpleDocTemplate(out, pagesize=A4, topMargin=0.8 * cm, bottomMargin=1.2 * cm)
    elements, styles = [], getSampleStyleSheet()
    elements.append(Paragraph("住宿行程报销单",
        ParagraphStyle('T', parent=styles['Heading1'], fontName=FONT_NAME, fontSize=22,
                       alignment=TA_CENTER, textColor=colors.HexColor('#1a1a2e'), spaceAfter=2)))
    elements.append(Paragraph("Hotel Accommodation Itinerary Report",
        ParagraphStyle('ST', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10,
                       alignment=TA_CENTER, textColor=colors.HexColor('#7f8c8d'), spaceAfter=8)))
    elements.append(HRFlowable(width="80%", thickness=2, color=colors.HexColor('#e67e22'), spaceAfter=10))

    dates = [r['checkin_date'] for r in records if r['checkin_date']]
    dr = f"{dates[0]} 至 {dates[-1]}" if len(dates) > 1 else (dates[0] if dates else '票面未载明')

    ss = ParagraphStyle('S', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10, leading=18)
    left = (f"<b>住宿期间：</b>{dr}<br/><b>发票数量：</b>{len(records)} 张")
    right = f"<b>总金额：</b>￥{total_amount:.2f}<br/><b>住宿票据：{len(records)} 张</b>"
    t = Table([[Paragraph(left, ss), Paragraph(right, ss)]], [8.5 * cm, 8.5 * cm])
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 16), ('TOPPADDING', (0, 0), (-1, -1), 12)]))
    elements.append(t); elements.append(Spacer(1, 12))

    # 明细表（含入住/退房）
    hs = ParagraphStyle('H', parent=styles['Normal'], fontName=FONT_NAME, fontSize=9,
                         textColor=colors.white, alignment=TA_CENTER)
    cs = ParagraphStyle('C', parent=styles['Normal'], fontName=FONT_NAME, fontSize=8.5,
                         alignment=TA_CENTER, leading=11)
    rows = [[Paragraph(x, hs) for x in
             ['序号', '入住日期', '退房日期', '酒店名称', '金额']]]
    for r in records:
        rows.append([Paragraph(str(r['seq']), cs),
                     Paragraph(r['checkin_date'] or '-', cs),
                     Paragraph(r['checkout_date'] or '-', cs),
                     Paragraph(r['hotel_name'], cs),
                     Paragraph(f"￥{r['amount']:.2f}", cs)])
    ts = ParagraphStyle('TS', parent=styles['Normal'], fontName=FONT_NAME, fontSize=9, alignment=TA_CENTER)
    rows.append([Paragraph('', ts)] * 4 + [Paragraph(f'<b>￥{total_amount:.2f}</b>', ts)])
    tb = Table(rows, [1.1 * cm, 3.0 * cm, 3.0 * cm, 6.0 * cm, 2.6 * cm])
    ts_list = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e67e22')),
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
    for ri, r in enumerate(records, 1):
        if ri % 2 == 0:
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#fef9f0')))
    tb.setStyle(TableStyle(ts_list)); elements.append(tb); elements.append(Spacer(1, 10))

    # 开票单位（酒店）信息
    seen = {}
    for r in records:
        key = (r['hotel_name'], r.get('seller_tax_id', ''))
        if key not in seen:
            seen[key] = True
    if seen:
        elements.append(Paragraph("🏢 开票单位信息",
            ParagraphStyle('HT', parent=styles['Normal'], fontName=FONT_NAME, fontSize=12,
                           textColor=colors.HexColor('#1a1a2e'), spaceAfter=4)))
        info_rows = [[Paragraph("酒店名称", hs), Paragraph("销售方税号", hs)]]
        for (name, tid) in seen:
            info_rows.append([Paragraph(name, cs),
                              Paragraph(tid or '-', cs)])
        itb = Table(info_rows, [9.0 * cm, 7.0 * cm])
        itb.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#d35400')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTNAME', (0, 0), (-1, -1), FONT_NAME),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
        ]))
        elements.append(itb); elements.append(Spacer(1, 12))

    # 数据可视化
    elements.append(Paragraph("📊 数据可视化分析",
        ParagraphStyle('VT', parent=styles['Normal'], fontName=FONT_NAME, fontSize=13,
                       textColor=colors.HexColor('#1a1a2e'), spaceAfter=6)))
    bar_labels = [str(r['seq']) for r in records]
    elements.append(charts.bar_chart([round(r['amount'], 2) for r in records],
                                     bar_labels, "各票据金额（元）", "#6366f1"))
    elements.append(Spacer(1, 8))
    # 按酒店汇总饼图
    hotel_tot = {}
    for r in records:
        hotel_tot[r['hotel_name']] = hotel_tot.get(r['hotel_name'], 0.0) + r['amount']
    pie_labels = list(hotel_tot.keys())
    pie_slices = [round(v, 2) for v in hotel_tot.values()]
    pie_colors = ["#6366f1", "#10b981", "#f59e0b", "#0ea5e9", "#f43f5e",
                  "#8b5cf6", "#14b8a6", "#fb923c"]
    elements.append(charts.pie_chart(pie_slices, pie_labels, "各酒店金额占比", pie_colors))
    elements.append(Spacer(1, 8))
    cum, run = [], 0.0
    for r in records:
        run += r['amount']; cum.append(round(run, 2))
    date_labels = [charts.short_date_label(r['checkin_date'] or r['checkout_date']
                                           or r['invoice_date']) for r in records]
    elements.append(charts.line_chart(cum, date_labels, "累计金额（元）", "#8b5cf6"))
    elements.append(Spacer(1, 12))

    upper = amount_upper(total_amount)
    elements.append(Paragraph(f"合计（大写）：<b>{upper}</b>",
        ParagraphStyle('UP', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10,
                       textColor=colors.HexColor('#c0392b'), alignment=TA_CENTER, spaceAfter=6)))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#dee2e6')))
    elements.append(Paragraph(
        f"共 {len(records)} 张 | 生成日期：{datetime.now().strftime('%Y-%m-%d %H:%M')} | 由「报销助手」生成",
        ParagraphStyle('FT', parent=styles['Normal'], fontName=FONT_NAME, fontSize=7,
                       textColor=colors.HexColor('#999999'), alignment=TA_CENTER)))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#e67e22'), spaceBefore=4))
    doc.build(elements)
    return out
