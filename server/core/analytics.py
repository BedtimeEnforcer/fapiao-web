# -*- coding: utf-8 -*-
"""core/analytics.py — 发票聚合统计与 JSON 导出。纯标准库。"""
import os
import json
from datetime import datetime

def summarize_invoices(records):
    """对一组已解析的发票记录做聚合统计（纯函数，无副作用）。

    records: list[dict]，每个元素至少含 seller_name / amount / tax_amount / invoice_date。
    返回：总张数、价税合计、税额合计、税前合计、按销售方汇总、按开票月份汇总。
    """
    if not records:
        return {'count': 0, 'total_amount': 0.0, 'total_tax': 0.0,
                'total_excl_tax': 0.0, 'by_seller': {}, 'by_month': {}}
    total_amount = 0.0
    total_tax = 0.0
    total_excl_tax = 0.0
    by_seller = {}
    by_month = {}
    count = 0
    for r in records:
        # 废弃材料（非电子发票且没挂上宿主）不是发票：不进金额合计，也不进张数。
        # 与 core/attach.py 的出口清零是同一条口径的两道闸 —— 那边保证 amount
        # 压根不存在，这边保证即便有人直接喂原始记录也不会被算进去。
        if r.get('discarded'):
            continue
        count += 1
        amt = float(r.get('amount') or 0.0)
        tax = float(r.get('tax_amount') or 0.0)
        excl = float(r.get('amount_excl_tax') or 0.0)
        total_amount += amt
        total_tax += tax
        total_excl_tax += excl
        seller = r.get('seller_name') or '未知销售方'
        by_seller.setdefault(seller, {'count': 0, 'amount': 0.0})
        by_seller[seller]['count'] += 1
        by_seller[seller]['amount'] += amt
        month = (r.get('invoice_date') or '')[:7]  # YYYY年MM月 前 7 字符
        if month:
            by_month.setdefault(month, {'count': 0, 'amount': 0.0})
            by_month[month]['count'] += 1
            by_month[month]['amount'] += amt
    return {
        'count': count,
        'total_amount': round(total_amount, 2),
        'total_tax': round(total_tax, 2),
        'total_excl_tax': round(total_excl_tax, 2),
        'by_seller': {k: {'count': v['count'], 'amount': round(v['amount'], 2)}
                      for k, v in by_seller.items()},
        'by_month': {k: {'count': v['count'], 'amount': round(v['amount'], 2)}
                     for k, v in by_month.items()},
    }


def export_invoices_json(records, filepath):
    """将解析结果导出为 JSON 文件，便于二次处理与审计。

    返回写入的文件路径；失败时抛出 IOError。
    """
    import json as _json
    data = {
        'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(records),
        'records': records,
    }
    with open(filepath, 'w', encoding='utf-8') as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    return filepath
