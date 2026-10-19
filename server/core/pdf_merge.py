# -*- coding: utf-8 -*-
"""core/pdf_merge.py — 按**指定文件名**合并 PDF（R3 流水线专用）。

为什么不能直接用 `core/merge.py:merge_invoices()`
--------------------------------------------------
它内部走 `parse._unique_path()`：目标已存在就自动落成 `合并发票(1).pdf`。
R3 流水线的契约明确**禁止**加 `(1)(2)` 后缀 —— 冲突要变成一条「已存在」的
跳过记录让用户看见，而不是悄悄生成第二个文件（否则用户以为合并成功了，
打开桌面却有两份，不知道哪份算数）。

`core/merge.py` 是现有 5 个旧模块的内核，**一行不动**（硬约束 2）；
这里只把「命名策略」换掉，合页逻辑与它同源（同样的 pypdf PdfWriter.append）。

本模块**不做目录创建、不做任何路径推断** —— 调用方（routes/pipeline.py）
必须先把 `out_dir` 校验好。dry_run 不建目录那条约束由此天然成立。
"""
from __future__ import annotations

import os

from pypdf import PdfWriter


def merge_to(file_paths, out_file: str, progress_cb=None):
    """把 ``file_paths`` 依次追加成 ``out_file``（**绝不**加 (1)(2) 后缀）。

    返回 ``(result, error)``；`result` 形如
    ``{"merged_path": str, "page_count": int, "merged_files": int,
       "failed_files": [{"filename": str, "reason": str}, ...]}``。

    ⚠️ 目标已存在时**不做去重改名**：调用方负责在调用前判断冲突
    （流水线里 `合并发票.pdf` 已被占用记一条 skipped）。
    """
    if not file_paths:
        return None, "未选择文件"

    writer = PdfWriter()
    failed_files = []
    for i, fp in enumerate(file_paths):
        if progress_cb:
            progress_cb(i + 1, len(file_paths), f"合并: {os.path.basename(fp)[:30]}...")
        try:
            writer.append(fp)
        except Exception as e:                       # 单个坏文件不拖垮整批
            failed_files.append({"filename": os.path.basename(fp), "reason": str(e)})

    if len(writer.pages) == 0:
        return None, "无法读取任何发票文件"

    with open(out_file, "wb") as f:
        writer.write(f)

    return {
        "merged_path": out_file,
        "page_count": len(writer.pages),
        "merged_files": len(file_paths) - len(failed_files),
        "failed_files": failed_files,
    }, None
