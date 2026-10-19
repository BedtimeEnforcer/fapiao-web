# -*- coding: utf-8 -*-
"""core/check.py — 发票核对模块。依赖 pypdf / reportlab。"""
import os
import re
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                 Paragraph, Spacer, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

from .config import FONT_NAME
from .parse import (_normalize_company_name, _normalize_tax_id, _unique_path,
                    extract_invoice_fields, extract_pdf_texts)

def check_invoices(file_paths, expected_name, expected_tax_id, progress_cb=None):
    if not expected_name and not expected_tax_id:
        return None, "请先在默认设置中填入核对信息"

    results, failed_files = [], []
    for i, fp in enumerate(file_paths):
        if progress_cb:
            progress_cb(i + 1, len(file_paths), f"检查: {os.path.basename(fp)[:30]}...")
        raw_text, read_error = extract_pdf_texts(fp)
        if read_error:
            failed_files.append({"filename": os.path.basename(fp), "reason": read_error})
            results.append(dict(
                filename=os.path.basename(fp), buyer_name='', tax_id='', amount=0,
                seller_name='', invoice_date='', invoice_no='', name_ok=False, tax_ok=False,
                passed=False, needs_review=True, review_reason=read_error,
            ))
            continue
        info = extract_invoice_fields(raw_text)
        # 铁路电子客票的购买方税号：票面印着「购买方名称:… 统一社会信用代码:…」，
        # 但通用增值税解析器按「票面只有一个税号 ⇒ 归销售方」处理（对代开机票是对的），
        # 于是 46 张火车票被报成「税号未识别」而进待确认。火车票税号位置锚死在
        # 「购买方名称」之后 ⇒ 只在这一处覆盖，**不改**通用口径。（见 facets.train_buyer_tax）
        _tt = ''
        try:
            from .facets import train_buyer_tax as _tbt
            _tt = _tbt(raw_text or '')
        except Exception:
            _tt = ''
        if _tt:
            info = dict(info)
            info['tax_id'] = _tt
        name_ok = True
        tax_ok = True
        if expected_name:
            name_ok = (_normalize_company_name(info['buyer_name']) == _normalize_company_name(expected_name))
        if expected_tax_id:
            tax_ok = (_normalize_tax_id(info['tax_id']) == _normalize_tax_id(expected_tax_id))

        review_fields = []
        if expected_name and not info['buyer_name']:
            review_fields.append("购买方名称未识别")
        if expected_tax_id and not info['tax_id']:
            review_fields.append("税号未识别")

        results.append(dict(
            filename=os.path.basename(fp), buyer_name=info['buyer_name'],
            tax_id=info['tax_id'], amount=info['amount'],
            seller_name=info['seller_name'], invoice_date=info['invoice_date'],
            invoice_no=info.get('invoice_no', ''), tax_rate=info.get('tax_rate', ''),
            name_ok=name_ok, tax_ok=tax_ok, passed=name_ok and tax_ok,
            needs_review=bool(review_fields), review_reason="；".join(review_fields),
        ))

    passed = sum(1 for r in results if r['passed'])
    failed = len(results) - passed
    return dict(results=results, total=len(results), passed=passed, failed=failed,
                failed_files=failed_files,
                review_count=sum(1 for r in results if r['needs_review']),
                expected_name=expected_name, expected_tax_id=expected_tax_id), None


def build_check_report_pdf(results, expected_name, expected_tax_id, total, passed, failed, output_folder):
    out = _unique_path(os.path.join(output_folder, "发票核对报告.pdf"))
    doc = SimpleDocTemplate(out, pagesize=A4, topMargin=1.0 * cm, bottomMargin=1.2 * cm)
    elements, styles = [], getSampleStyleSheet()

    elements.append(Paragraph("发票信息核对报告",
        ParagraphStyle('T', parent=styles['Heading1'], fontName=FONT_NAME, fontSize=22,
                       alignment=TA_CENTER, textColor=colors.HexColor('#1a1a2e'), spaceAfter=2)))
    elements.append(Paragraph("Invoice Information Check Report",
        ParagraphStyle('ST', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10,
                       alignment=TA_CENTER, textColor=colors.HexColor('#7f8c8d'), spaceAfter=8)))
    elements.append(HRFlowable(width="80%", thickness=2, color=colors.HexColor('#27ae60'), spaceAfter=10))

    ss = ParagraphStyle('S', parent=styles['Normal'], fontName=FONT_NAME, fontSize=10, leading=20)
    elements.append(Paragraph(
        f"<b>预设购买方名称：</b>{expected_name or '(未设)'}<br/>"
        f"<b>预设纳税人识别号：</b>{expected_tax_id or '(未设)'}<br/>"
        f"<b>核对结果：</b><font color='#27ae60'><b>通过 {passed} 张</b></font>"
        + (f"， <font color='#e74c3c'><b>不符 {failed} 张</b></font>" if failed > 0 else "， 全部通过"),
        ss))
    elements.append(Spacer(1, 10))

    # 表格
    hs = ParagraphStyle('H', parent=styles['Normal'], fontName=FONT_NAME, fontSize=8,
                         textColor=colors.white, alignment=TA_CENTER)
    cs = ParagraphStyle('C', parent=styles['Normal'], fontName=FONT_NAME, fontSize=7,
                         alignment=TA_CENTER, leading=10)
    headers = ['序号','文件名','购买方名称','纳税人识别号','销售方','金额','名称核对','税号核对','结果']
    rows = [[Paragraph(x, hs) for x in headers]]

    for i, r in enumerate(results, 1):
        name_mark = '<font color="#27ae60">正确</font>' if r['name_ok'] else '<font color="#e74c3c">不符</font>'
        tax_mark = '<font color="#27ae60">正确</font>' if r['tax_ok'] else '<font color="#e74c3c">不符</font>'
        result_text = '<font color="#27ae60">通过</font>' if r['passed'] else '<font color="#e74c3c">不符</font>'
        rows.append([
            Paragraph(str(i), cs),
            Paragraph(r['filename'][:18], cs),
            Paragraph(r['buyer_name'][:14] if r['buyer_name'] else '-', cs),
            Paragraph(r['tax_id'] if r['tax_id'] else '-', cs),
            Paragraph(r['seller_name'][:14] if r['seller_name'] else '-', cs),
            Paragraph(f"￥{r['amount']:.2f}" if r['amount'] > 0 else '-', cs),
            Paragraph(name_mark, cs),
            Paragraph(tax_mark, cs),
            Paragraph(result_text, cs),
        ])

    col_w = [0.7*cm, 2.7*cm, 2.6*cm, 2.6*cm, 2.4*cm, 1.5*cm, 1.1*cm, 1.1*cm, 0.9*cm]
    tb = Table(rows, col_w)
    ts_list = [
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#27ae60')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'), ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,0), (-1,0), FONT_NAME),
        ('BOTTOMPADDING', (0,0), (-1,0), 8), ('TOPPADDING', (0,0), (-1,0), 8),
        ('FONTNAME', (0,1), (-1,-1), FONT_NAME),
        ('TOPPADDING', (0,1), (-1,-1), 4), ('BOTTOMPADDING', (0,1), (-1,-1), 4),
        ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#d5d8dc')),
    ]
    for ri, r in enumerate(results, 1):
        if not r['passed']:
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#ffeaea')))
        elif ri % 2 == 0:
            ts_list.append(('BACKGROUND', (0, ri), (-1, ri), colors.HexColor('#f5fcf5')))
    tb.setStyle(TableStyle(ts_list)); elements.append(tb)
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#dee2e6')))
    elements.append(Paragraph(
        f"共 {total} 张 | 通过 {passed} 张 | 不符 {failed} 张 | 生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ParagraphStyle('FT', parent=styles['Normal'], fontName=FONT_NAME, fontSize=7,
                       textColor=colors.HexColor('#999999'), alignment=TA_CENTER)))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#27ae60'), spaceBefore=4))
    doc.build(elements)
    return out
