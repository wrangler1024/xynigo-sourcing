# -*- coding: utf-8 -*-
"""采购售后·退款跟踪导出：把 ④ 回访读到的行导成 xlsx。

列与工作台 ④ 表格一一对应（环境序号/订单号/商品图/退款单号/退款信用卡/
退款金额/阶段/剩余倒计时/最近检查/备注），运营可直接用 Excel 筛选。

商品图列写的是 CDN 链接而不是内嵌图片：导出过程不依赖外网取图，
链接在 Excel 里可直接点开；要内嵌图片另说（需服务端下载，多一层失败面）。
"""
import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADERS = (
    "环境序号", "订单号", "商品图", "退款单号", "退款信用卡",
    "退款金额", "阶段", "剩余倒计时", "最近检查", "备注",
)
COLUMN_WIDTHS = (12, 22, 34, 24, 20, 14, 14, 16, 20, 40)
# 与执行器 after_sale_claim.PHASE_LABELS、Web AS_TL_LABEL 保持同一套中文
PHASE_LABELS = {
    "submitted": "已受理", "reviewing": "审核中", "processing": "处理中",
    "refunded": "已退款", "rejected": "已拒绝", "overdue": "超期未出结果",
}
MIME_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _checked_at_text(value):
    """ISO 时间 → `YYYY-MM-DD HH:MM`；解析不了就原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.strftime("%Y-%m-%d %H:%M")


def _phase_text(row):
    phase = str(row.get("phase") or "").strip()
    label = str(row.get("phaseLabel") or "").strip()
    return label or PHASE_LABELS.get(phase, phase)


def _row_values(row):
    """一行导出值。列序必须与 HEADERS 一一对应（测试钉列序）。"""
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        row.get("goodsImg") or "",
        row.get("refundBillId") or "",
        row.get("refundAccount") or "",
        row.get("amount") or "",
        _phase_text(row),
        row.get("countdown") or "",
        _checked_at_text(row.get("checkedAt")),
        row.get("note") or row.get("errorSummary") or "",
    ]


def build_after_sale_track_export(rows):
    """生成导出文件。返回 (content, filename, mime)。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    values = [_row_values(row) for row in rows or []]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "退款跟踪"
    for column, name in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=1, column=column, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="123B63")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[get_column_letter(column)].width = (
            COLUMN_WIDTHS[column - 1]
        )
    for index, row_values in enumerate(values, start=2):
        for column, value in enumerate(row_values, start=1):
            sheet.cell(row=index, column=column, value=value)
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = f"A1:J{max(1, len(values) + 1)}"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), f"退款跟踪结果_{stamp}.xlsx", MIME_XLSX
