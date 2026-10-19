# -*- coding: utf-8 -*-
"""core/merge.py — 发票 PDF 合并模块。依赖 pypdf。"""
import os

from pypdf import PdfWriter

from .parse import _unique_path, validate_output_folder

def merge_invoices(file_paths, output_folder, progress_cb=None):
    if not file_paths:
        return None, "未选择文件"
    output_folder, output_error = validate_output_folder(output_folder)
    if output_error:
        return None, output_error
    merged_path = _unique_path(os.path.join(output_folder, "合并发票.pdf"))
    writer = PdfWriter()
    failed_files = []
    for i, fp in enumerate(file_paths):
        if progress_cb:
            progress_cb(i + 1, len(file_paths), f"合并: {os.path.basename(fp)[:30]}...")
        try:
            writer.append(fp)
        except Exception as e:
            failed_files.append({"filename": os.path.basename(fp), "reason": str(e)})
    if len(writer.pages) == 0:
        return None, "无法读取任何发票文件"
    with open(merged_path, 'wb') as f:
        writer.write(f)
    return {
        "merged_path": merged_path,
        "page_count": len(writer.pages),
        "merged_files": len(file_paths) - len(failed_files),
        "failed_files": failed_files,
    }, None


def detect_special(text):
    if '退票' in text: return '退票'
    if '改签' in text or '始发改签' in text: return '改签'
    return None
