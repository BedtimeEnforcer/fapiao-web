# -*- coding: utf-8 -*-
"""core/config.py — 配置 / 日志 / 资源路径 / 国际化(TR)。纯标准库，无第三方依赖。"""
import os
import re
import sys
import json
import hashlib
import zipfile
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

_ERROR_LOGS = []
_RUNTIME_LOGS = []
_LOG_LOCK = threading.Lock()
_MAX_MEMORY_LOGS = 500
_MAX_LOG_FILE_BYTES = 512 * 1024   # error.log 超过 512KB 时自动轮转为 error.log.1


def _log_dir():
    """日志与配置同目录: %APPDATA%\\发票处理工具\\"""
    return os.path.join(os.environ.get('APPDATA', str(Path.home())), '发票处理工具')


def _append_to_file(filename, entry):
    """把一条日志追加到文件（控制台不可见的 EXE 里，这是唯一的现场证据）。

    失败绝不抛出——日志自身的问题不能影响主流程。
    """
    try:
        os.makedirs(_log_dir(), exist_ok=True)
        path = os.path.join(_log_dir(), filename)
        with _LOG_LOCK:
            if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG_FILE_BYTES:
                try:
                    os.replace(path, path + ".1")
                except Exception:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
    except Exception:
        pass


def _push_log(bucket, filename, msg):
    entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    bucket.append(entry)
    if len(bucket) > _MAX_MEMORY_LOGS:
        del bucket[:len(bucket) - _MAX_MEMORY_LOGS]
    _append_to_file(filename, entry)


def _log_error(msg):
    """错误日志：存入内存 + 落盘 error.log"""
    _push_log(_ERROR_LOGS, "error.log", msg)


def _log_runtime(msg):
    """运行日志：存入内存 + 落盘 runtime.log"""
    _push_log(_RUNTIME_LOGS, "runtime.log", msg)


def _get_logs(log_type='error'):
    """获取日志文本"""
    logs = _ERROR_LOGS if log_type == 'error' else _RUNTIME_LOGS
    return '\n'.join(logs) if logs else '（暂无日志记录）'


def _clear_logs():
    """清除内存日志（保留已落盘的日志文件，便于事后追溯）"""
    _ERROR_LOGS.clear()
    _RUNTIME_LOGS.clear()


def _safe_log(func):
    """装饰器：捕获异常并写入 error.log"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            _log_error(f"{func.__name__} 异常: {e}\n{traceback.format_exc()}")
            raise
    return wrapper


def _font_path():
    try:
        base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    for name in ("simhei.ttf", "msyh.ttc", "msyhbd.ttc"):
        fp = os.path.join(base, name)
        if os.path.exists(fp):
            return fp
    return "C:/Windows/Fonts/simhei.ttf"


FONT_NAME = "SimHei"


# ── 中文字体注册（reportlab 生成 PDF 行程单/核对报告用）──────────
# 必须在引用 FONT_NAME 的 ParagraphStyle 渲染前，将 SimHei 注册为
# reportlab 已知字体及其字族，否则会抛出
# "Can't map determine family/bold/italic for simhei"。
def _register_chinese_font():
    """把 SimHei 注册进 reportlab，供行程单/核对报告 PDF 使用。

    仅做一次性注册；失败时静默回退（环境缺少字体文件时不影响其它逻辑）。
    """
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        return
    fp = _font_path()
    if not fp or not os.path.exists(fp):
        return
    try:
        if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(FONT_NAME, fp))
        # 字族映射：SimHei 无独立粗体/斜体文件，统一映射到自身，
        # 使 reportlab 的 ps2tt 能正确解析 family/bold/italic。
        pdfmetrics.registerFontFamily(
            FONT_NAME,
            normal=FONT_NAME,
            bold=FONT_NAME,
            italic=FONT_NAME,
            boldItalic=FONT_NAME,
        )
    except Exception:
        pass


_register_chinese_font()


def _cfg_path():
    appdata = os.path.join(os.environ.get('APPDATA', str(Path.home())), '发票处理工具')
    os.makedirs(appdata, exist_ok=True)
    return os.path.join(appdata, "config.json")


CFG_FILE = _cfg_path()


def load_config():
    defaults = {
        "output_folder": str(Path.home() / "Desktop"),
        "check_buyer_name": "",
        "check_tax_id": "",
        "appearance_mode": "system",
        "language": "zh",
        "presets": [],  # [{"name":"公司A","buyer_name":"...","tax_id":"..."}]
        "active_preset_index": 0,
        # 用户拖动的表格列宽记忆：{"merge": {"name": 260, ...}, "train": {...}}
        "column_widths": {},
    }
    try:
        if os.path.exists(CFG_FILE):
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                # 兼容旧格式
                if "presets" not in loaded:
                    loaded["presets"] = []
                    if loaded.get("check_buyer_name") or loaded.get("check_tax_id"):
                        loaded["presets"].append({
                            "name": "默认预设",
                            "buyer_name": loaded.get("check_buyer_name", ""),
                            "tax_id": loaded.get("check_tax_id", ""),
                        })
                    loaded["active_preset_index"] = 0
                defaults.update(loaded)
                # 列宽记忆必须是 {表名: {列键: 整数}}；损坏或旧格式一律丢弃重来，
                # 否则会把脏数据喂给 InvoiceTable 导致列宽变成 0 / 负数。
                cw = defaults.get("column_widths")
                if not isinstance(cw, dict):
                    defaults["column_widths"] = {}
                else:
                    clean = {}
                    for tbl, cols in cw.items():
                        if not isinstance(cols, dict):
                            continue
                        good = {}
                        for k, v in cols.items():
                            try:
                                iv = int(v)
                            except Exception:
                                continue
                            if 40 <= iv <= 4000:
                                good[str(k)] = iv
                        if good:
                            clean[str(tbl)] = good
                    defaults["column_widths"] = clean
    except Exception:
        pass
    return defaults


def save_config(data):
    try:
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


TR = {
    'app_title':          {'zh': '报销助手',                  'en': 'Invoice Tool'},
    'subtitle':           {'zh': '合并 · 行程单 · 核对  |  高效处理发票报销',
                                                           'en': 'Merge · Itinerary · Check  |  Efficient Invoice Processing'},
    'output_path':        {'zh': '输出路径',                  'en': 'Output Path'},
    'browse':             {'zh': '浏览',                      'en': 'Browse'},
    'preset_btn':         {'zh': '发票核对预设',              'en': 'Check Presets'},
    'preset_summary':     {'zh': '核对: {}',                  'en': 'Check: {}'},
    'preset_none':        {'zh': '未设名称',                  'en': 'Not set'},
    'light':              {'zh': '浅色',                      'en': 'Light'},
    'dark':               {'zh': '深色',                      'en': 'Dark'},
    'tab_merge':          {'zh': '发票合并',                  'en': 'Invoice Merge'},
    'tab_train':          {'zh': '火车票行程单',              'en': 'Train Itinerary'},
    'tab_hotel':          {'zh': '住宿票行程单',              'en': 'Hotel Itinerary'},
    'tab_check':          {'zh': '发票信息核对',              'en': 'Invoice Check'},
    'tab_rename':         {'zh': '发票重命名',                'en': 'Rename Invoices'},
    'merge_title':        {'zh': '发票合并',                  'en': 'Invoice Merge'},
    'merge_desc':         {'zh': '将多张 PDF/OFD 发票按顺序合并为单一文件',
                                                           'en': 'Merge multiple PDF/OFD invoices into a single file in order'},
    'merge_select':       {'zh': '选择发票文件',             'en': 'Select Invoice Files'},
    'merge_clear':        {'zh': '清空列表',                  'en': 'Clear List'},
    'merge_empty':        {'zh': '尚未选择文件',              'en': 'No files selected'},
    'merge_selected':     {'zh': '已选 {} 个文件',            'en': '{} file(s) selected'},
    'train_title':        {'zh': '火车票行程单',              'en': 'Train Itinerary'},
    'train_desc':         {'zh': '自动识别车次、日期、金额，生成行程报销单',
                                                           'en': 'Auto-detect train number, date, and amount; generate itinerary'},
    'train_folder':       {'zh': '选择文件夹',                'en': 'Select Folder'},
    'train_files':        {'zh': '选择多个文件',              'en': 'Select Files'},
    'train_browse_folder':{'zh': '浏览文件夹',                'en': 'Browse Folder'},
    'train_browse_files': {'zh': '选择文件',                  'en': 'Select Files'},
    'train_preview_empty':{'zh': '文件夹内未发现 PDF',        'en': 'No PDF found in folder'},
    'train_preview_found':{'zh': '文件夹内找到 {} 个文件','en': 'Found {} PDF file(s) in folder'},
    'train_preview_sel':  {'zh': '已选 {} 个文件',            'en': '{} file(s) selected'},
    'train_opt_itinerary':{'zh': '生成行程报销单',            'en': 'Generate Itinerary Report'},
    'train_opt_merge':    {'zh': '合并发票',                  'en': 'Merge Invoices'},
    'train_opt_dedup':    {'zh': '智能去重',                  'en': 'Remove Duplicates'},
    #: 行程单为 HTML 交付；此项控制是否在行程单内嵌入交互地图与图表
    'train_opt_map':      {'zh': '内嵌地图',                  'en': 'Embed Map'},
    'hotel_title':        {'zh': '住宿票行程单',              'en': 'Hotel Itinerary'},
    'hotel_desc':         {'zh': '自动识别酒店、入住日期、金额，生成住宿报销单',
                                                           'en': 'Auto-detect hotel, check-in date, and amount; generate itinerary'},
    'hotel_folder':       {'zh': '选择文件夹',                'en': 'Select Folder'},
    'hotel_files':        {'zh': '选择多个文件',              'en': 'Select Files'},
    'hotel_browse_folder':{'zh': '浏览文件夹',                'en': 'Browse Folder'},
    'hotel_browse_files': {'zh': '选择文件',                  'en': 'Select Files'},
    'hotel_preview_empty':{'zh': '文件夹内未发现 PDF',        'en': 'No PDF found in folder'},
    'hotel_preview_found':{'zh': '文件夹内找到 {} 个文件','en': 'Found {} PDF file(s) in folder'},
    'hotel_preview_sel':  {'zh': '已选 {} 个文件',            'en': '{} file(s) selected'},
    'hotel_opt_itinerary':{'zh': '生成住宿行程单',            'en': 'Generate Hotel Itinerary'},
    'hotel_opt_merge':    {'zh': '合并发票',                  'en': 'Merge Invoices'},
    'hotel_opt_dedup':    {'zh': '智能去重',                  'en': 'Remove Duplicates'},
    'check_title':        {'zh': '发票信息核对',              'en': 'Invoice Information Check'},
    'check_desc':         {'zh': '比对购买方名称和纳税人识别号，检查开票信息是否正确',
                                                           'en': 'Verify buyer name and tax ID against presets'},
    'check_select':       {'zh': '选择发票文件',              'en': 'Select Invoice Files'},
    'check_clear':        {'zh': '清空列表',                  'en': 'Clear List'},
    'check_refresh':      {'zh': '刷新预设值',                'en': 'Refresh Presets'},
    'check_empty':        {'zh': '尚未选择文件',              'en': 'No files selected'},
    'check_selected':     {'zh': '已选 {} 个文件',            'en': '{} file(s) selected'},
    'check_name_prefix':  {'zh': '预设购买方名称: ',          'en': 'Preset Buyer Name: '},
    'check_tax_prefix':   {'zh': '预设纳税人识别号: ',        'en': 'Preset Tax ID: '},
    'check_not_set':      {'zh': '(未设置，请点击顶部「发票核对预设」)', 'en': '(Not set, click "Check Presets" above)'},
    'check_tax_not_set':  {'zh': '(未设置)',                  'en': '(Not set)'},
    'action_merge':       {'zh': '开始合并',                  'en': 'Start Merge'},
    'action_generate':    {'zh': '开始生成',                  'en': 'Generate'},
    'action_check':       {'zh': '开始检查',                  'en': 'Start Check'},
    'close':              {'zh': '关闭',                      'en': 'Close'},
    'footer':             {'zh': '报销助手',                 'en': 'Reimbursement Assistant'},
    'preset_win_title':   {'zh': '发票核对预设',              'en': 'Invoice Check Presets'},
    'preset_win_desc':    {'zh': '设置默认的购买方名称和纳税人识别号，用于发票核对',
                                                           'en': 'Set default buyer name and tax ID for invoice verification'},
    'preset_name':        {'zh': '购买方名称',                'en': 'Buyer Name'},
    'preset_tax':         {'zh': '纳税人识别号',              'en': 'Tax ID'},
    'preset_save':        {'zh': '保存',                      'en': 'Save'},
    'preset_cancel':      {'zh': '取消',                      'en': 'Cancel'},
    # 菜单
    'menu_about':         {'zh': '关于',                      'en': 'About'},
    'menu_help':          {'zh': '使用教程',                  'en': 'Tutorial'},
    'menu_intro':         {'zh': '软件介绍',                  'en': 'Introduction'},
    'menu_version':       {'zh': '版本信息',                  'en': 'Version Info'},
    'menu_tut_merge':     {'zh': '发票合并教程',              'en': 'Merge Tutorial'},
    'menu_tut_train':     {'zh': '火车票行程单教程',          'en': 'Train Itinerary Tutorial'},
    'menu_tut_hotel':     {'zh': '住宿票行程单教程',          'en': 'Hotel Itinerary Tutorial'},
    'menu_tut_check':     {'zh': '发票核对教程',              'en': 'Invoice Check Tutorial'},
    'menu_tut_rename':    {'zh': '发票重命名教程',            'en': 'Rename Tutorial'},
    'rename_title':       {'zh': '发票重命名',                'en': 'Rename Invoices'},
    'rename_desc':        {'zh': '智能识别发票内容，自动生成规范文件名',
                                                             'en': 'Auto-detect invoice content and generate standardized filenames'},
    'rename_select':      {'zh': '选择发票文件',              'en': 'Select Invoice Files'},
    'rename_clear':       {'zh': '清空列表',                  'en': 'Clear List'},
    'rename_empty':       {'zh': '尚未选择文件',              'en': 'No files selected'},
    'rename_selected':    {'zh': '已选 {} 个文件',            'en': '{} file(s) selected'},
    'action_rename':      {'zh': '执行重命名',                'en': 'Start Rename'},
    'dlg_rename_done':    {'zh': '重命名完成！\n\n成功: {} 份\n失败: {} 份',
                                                             'en': 'Rename complete!\n\nSuccess: {}\nFailed: {}'},
    # 日志菜单
    'menu_log':           {'zh': '日志',                      'en': 'Logs'},
    'menu_log_error':     {'zh': '错误日志',                  'en': 'Error Log'},
    'menu_log_runtime':   {'zh': '运行日志',                  'en': 'Runtime Log'},
    'menu_log_clear':     {'zh': '清除日志',                  'en': 'Clear Logs'},
    # 设置栏
    'settings_preset_label':{'zh': '发票信息核对预设',        'en': 'Check Preset'},
    'settings_preset_mgr':  {'zh': '核对预设管理',            'en': 'Manage Presets'},
    # 表格提示
    'hint_click_header':  {'zh': '点击表头排序 | 选中行后可用','en': 'Click header to sort | Select row first'},
    'hint_no_files':      {'zh': '暂无文件，请点击上方按钮选择发票文件\n选择后将自动读取发票信息并展示为表格',
                                                             'en': 'No files. Click above to select PDF invoices\nMetadata will be auto-extracted into table'},
    'hint_no_files_train':{'zh': '请选择文件夹或文件',        'en': 'Select folder or files'},
    'hint_no_files_check':{'zh': '暂无文件，请选择需要核对的发票', 'en': 'No files yet, select invoices to check'},
    # 状态栏
    'status_ready':       {'zh': '准备就绪',                  'en': 'Ready'},
    'status_analyzing':   {'zh': '分析中: {}…',               'en': 'Analyzing: {}…'},
    'status_reading':     {'zh': '正在读取发票信息…',         'en': 'Reading invoice data…'},
    'status_processing':  {'zh': '正在处理，请稍候…',         'en': 'Processing, please wait…'},
    # 操作按钮
    'btn_open_output':    {'zh': '打开输出目录',              'en': 'Open Output Folder'},
    'btn_select_files':   {'zh': '选择发票文件',              'en': 'Select Invoice Files'},
    'btn_clear_list':     {'zh': '清空列表',                  'en': 'Clear List'},
    'btn_ok':             {'zh': '确定',                      'en': 'OK'},
    'btn_open_file':      {'zh': '打开文件',                  'en': 'Open File'},
    'btn_open_all':       {'zh': '打开全部文件({})',          'en': 'Open All Files({})'},
    # 预设窗口
    'preset_name_hint':   {'zh': '预设名称（如：畅想 / 畅联）','en': 'Preset Name (e.g. Company A)'},
    'preset_buyer_hint':  {'zh': '购买方名称（公司全称）',    'en': 'Buyer Name (Full Company Name)'},
    'preset_tax_hint':    {'zh': '纳税人识别号（统一社会信用代码）','en': 'Tax ID (Unified Social Credit Code)'},
    'preset_add':         {'zh': '＋ 添加/更新预设',          'en': '+ Add/Update Preset'},
    'preset_delete':      {'zh': '✕ 删除选中预设',            'en': '✕ Delete Selected'},
    'preset_activate':    {'zh': '★ 设为当前使用',            'en': '★ Set as Active'},
    'preset_empty':       {'zh': '（还没有预设，请在下方添加）','en': '(No presets yet. Add one below)'},
    'preset_unnamed':     {'zh': '未命名',                    'en': 'Unnamed'},
    # 对话框
    'dlg_confirm_merge':  {'zh': '即将合并 {} 份 PDF 发票\n\n输出路径：{}','en': 'About to merge {} PDF invoices\n\nOutput: {}'},
    'dlg_confirm_rename': {'zh': '模式：{}\n共 {} 份\n\n{}\n\n确认执行？','en': 'Mode: {}\nTotal: {} files\n\n{}\n\nConfirm?'},
    'dlg_merge_title':    {'zh': '合并完成',                  'en': 'Merge Complete'},
    'dlg_gen_title':      {'zh': '生成完成',                  'en': 'Generation Complete'},
    'dlg_check_title':    {'zh': '核对完成',                  'en': 'Check Complete'},
    'dlg_rename_title':   {'zh': '重命名完成',                'en': 'Rename Complete'},
    'dlg_confirm_title':  {'zh': '确认合并',                  'en': 'Confirm Merge'},
    'dlg_confirm_rename_title':{'zh': '确认重命名',            'en': 'Confirm Rename'},
    'dlg_clear_log_confirm':{'zh': '确定要清除所有错误日志和运行日志吗？','en': 'Clear all error and runtime logs?'},
    'dlg_clear_log_done': {'zh': '日志已清除',                'en': 'Logs cleared'},
    'dlg_log_cleared':    {'zh': '日志已清除',                'en': 'Logs Cleared'},
    'dlg_no_file_msg':    {'zh': '请先选择要合并的发票文件',  'en': 'Please select invoice files first'},
    'dlg_no_file_check':  {'zh': '请先选择要核对的发票文件',  'en': 'Please select invoice files to check'},
    'dlg_no_output':      {'zh': '请先选择至少一种输出文件',  'en': 'Please select at least one output type'},
    'dlg_rename_mode_inplace':{'zh': '原位重命名（修改原文件名）','en': 'In-place rename (modify original)'},
    'dlg_rename_mode_copy':{'zh': '复制为新文件（保留原文件）','en': 'Copy as new file (keep original)'},
    'dlg_rename_mode_copy_to':{'zh': '复制为新文件 → {}',     'en': 'Copy to new file → {}'},
    # 核对模块
    'check_hint_default': {'zh': '选择发票后点击底部「▶ 开始检查」，核对结果将显示在此处',
                                                             'en': 'Select invoices then click bottom \"▶ Start Check\"'},
    'check_hint_selected':{'zh': '已选 {} 份，点击底部「▶ 开始检查」','en': '{} selected. Click bottom \"▶ Start Check\"'},
    'check_filter_all':   {'zh': '全部',                      'en': 'All'},
    'check_filter_fail':  {'zh': '仅看不符',                  'en': 'Failed Only'},
    'check_filter_review':{'zh': '仅看待确认',                'en': 'Review Needed'},
    'check_col_filter':   {'zh': '筛选：',                    'en': 'Filter: '},
    # 重命名模块
    'rename_mode_label':  {'zh': '方式：',                    'en': 'Mode: '},
    'rename_mode_inplace':{'zh': '原位重命名',                'en': 'Rename In-place'},
    'rename_mode_copy':   {'zh': '复制为新文件',              'en': 'Copy as New'},
    # 消息框
    'msg_error':          {'zh': '错误',                      'en': 'Error'},
    'msg_warning':        {'zh': '提示',                      'en': 'Notice'},
    'msg_done':           {'zh': '完成',                      'en': 'Done'},
    'msg_processing_failed':{'zh': '处理过程中发生意外错误。','en': 'An unexpected error occurred.'},
    # PDF 标题
    'pdf_train_title':    {'zh': '火车票行程报销单',          'en': 'Train Ticket Itinerary'},
    'pdf_train_subtitle': {'zh': 'Train Ticket Itinerary',    'en': '火车票行程报销单'},
    'pdf_hotel_title':    {'zh': '住宿行程报销单',            'en': 'Hotel Accommodation Itinerary'},
    'pdf_hotel_subtitle': {'zh': 'Hotel Accommodation Itinerary','en': '住宿行程报销单'},
    'pdf_check_title':    {'zh': '发票信息核对报告',          'en': 'Invoice Check Report'},
    'pdf_check_subtitle': {'zh': 'Invoice Information Check Report','en': '发票信息核对报告'},
    # 对话框
    'dlg_no_file':        {'zh': '请先选择要合并的发票文件',  'en': 'Please select invoice files first'},
    'dlg_no_folder':      {'zh': '请选择发票文件夹',          'en': 'Please select invoice folder'},
    'dlg_no_files':       {'zh': '请选择发票文件',            'en': 'Please select invoice files'},
    'dlg_no_preset':      {'zh': '请先设置核对信息（点击顶部「发票核对预设」按钮）',
                                                           'en': 'Please set check info first (click "Check Presets" at top)'},
    'dlg_merge_done':     {'zh': '合并完成!\n\n文件: {}\n页数: {} 页\n\n保存至:\n{}',
                                                           'en': 'Merge complete!\n\nFile: {}\nPages: {}\n\nSaved to:\n{}'},
    'dlg_train_done':     {'zh': '生成完成！\n\n发票数量: {} 张\n总金额: ￥{:.2f} 元',
                                                           'en': 'Done!\n\nInvoices: {}\nTotal: ￥{:.2f}'},
    'dlg_train_skipped':  {'zh': '\n已去重跳过 {} 张',        'en': '\n{} duplicates skipped'},
    'dlg_train_saved':    {'zh': '\n\n文件已保存到:\n{}',     'en': '\n\nSaved to:\n{}'},
    'dlg_hotel_done':     {'zh': '生成完成！\n\n发票数量: {} 张\n总金额: ￥{:.2f} 元',
                                                           'en': 'Done!\n\nInvoices: {}\nTotal: ￥{:.2f}'},
    'dlg_error':          {'zh': '错误',                      'en': 'Error'},
    'dlg_done':           {'zh': '完成',                      'en': 'Done'},
    'dlg_warning':        {'zh': '提示',                      'en': 'Notice'},
    # 表单 / 操作类提示（原硬编码中文，现已 i18n）
    'warn_preset_name':   {'zh': '请输入预设名称',            'en': 'Please enter a preset name'},
    'warn_preset_name_tax':{'zh': '请至少填写名称或税号',     'en': 'Please enter name or tax ID'},
    'warn_preset_select': {'zh': '请先在列表中选中一个预设',  'en': 'Please select a preset first'},
    'warn_select_files':  {'zh': '请先选择发票文件',          'en': 'Please select invoice files first'},
    'warn_output_type':   {'zh': '请至少勾选一种输出文件',    'en': 'Please select at least one output type'},
    'warn_run_check':     {'zh': '请先执行核对',              'en': 'Please run check first'},
    'dlg_export_csv':     {'zh': '导出完成',                  'en': 'Export Complete'},
    'dlg_confirm_merge_title':{'zh': '确认合并',              'en': 'Confirm Merge'},
    'dlg_confirm_rename_title':{'zh': '确认重命名',           'en': 'Confirm Rename'},
    'lang_toggle':        {'zh': '中 / EN',                   'en': '中 / EN'},
}
TR['check_not_set_name'] = TR['check_not_set']
TR['check_not_set_tax'] = TR['check_tax_not_set']
TR['hotel_preview_empty2'] = TR['hotel_preview_empty']
TR['hotel_preview_found2'] = TR['hotel_preview_found']
TR['hotel_preview_sel2'] = TR['hotel_preview_sel']


# ── 工具函数 ─────────────────────────────────────────


def project_root():
    """项目根目录：开发态由 core/ 上溯定位；冻结态即 PyInstaller 解包目录。

    * 桌面版：``src/fapiao_tool/core`` → 上溯三级即项目根；
    * Web 版：``server/core`` → 上溯两级即项目根。

    两种目录结构深度不同，这里用「同级是否已有 assets/ 目录」来判定，
    因此同一份 core 代码在桌面版与 Web 版都能正确定位资源（assets/ 内）
    与产物目录。若都找不到，退回原桌面版的上溯三级行为。
    """
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return meipass
    here = os.path.dirname(os.path.abspath(__file__))
    for levels in (3, 2):
        root = os.path.abspath(os.path.join(here, *([".."] * levels)))
        if os.path.isdir(os.path.join(root, "assets")):
            return root
    return os.path.abspath(os.path.join(here, '..', '..', '..'))


def asset_path(*parts):
    """定位随包内置的数据资产（如 assets/trainmap/stations.json）。

    与 ui/app.py 的 resource_path 等价，但位于 core 层 —— core 不得 import ui。
    新增 assets/ 子目录无需改 .spec：spec 里 ('assets','assets') 是整目录打包。
    """
    return os.path.join(project_root(), *parts)
