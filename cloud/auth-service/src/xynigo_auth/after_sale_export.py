# -*- coding: utf-8 -*-
"""采购售后的两条导出：④ 退款跟踪、③ 提交结果（批次）。

列与工作台表格一一对应，运营可直接用 Excel 筛选：
- 退款跟踪＝环境序号/订单号/商品图/退款单号/退款信用卡/退款金额/阶段/剩余倒计时/最近检查/备注
- 提交结果＝环境序号/订单号/商品图/售后类型/送达时间/退款单号/退款路径/退款信用卡/状态/操作时间/备注

商品图列写的是 CDN 链接而不是内嵌图片：导出过程不依赖外网取图，
链接在 Excel 里可直接点开；要内嵌图片另说（需服务端下载，多一层失败面）。
空值统一留空单元格（不写「—」占位符），Excel 里能直接筛选求和。
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

CLAIM_HEADERS = (
    "环境序号", "订单号", "商品图", "售后类型", "送达时间", "退款单号",
    "退款路径", "退款信用卡", "状态", "操作时间", "备注",
)
CLAIM_COLUMN_WIDTHS = (12, 22, 34, 12, 20, 22, 26, 16, 20, 20, 32)
# 与 Web AS_CLAIM_PILL 同一套中文文案：导出与页面不能各说各话
CLAIM_STATUS_LABELS = {
    "ok": "已受理 · 退款审核中", "blocked": "不可申请 · 已提交过",
    "skip": "跳过", "empty": "无订单", "fail": "失败",
    "login": "失败 · 未登录", "inuse": "失败 · 环境占用",
    "stopped": "已停止", "queued": "等待", "running": "提交中",
}
AFTER_SALE_TYPE_LABEL = "丢件退款"

MIME_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _timestamp_text(value):
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
    """退款跟踪一行。列序必须与 HEADERS 一一对应（测试钉列序）。"""
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        row.get("goodsImg") or "",
        row.get("refundBillId") or "",
        row.get("refundAccount") or "",
        row.get("amount") or "",
        _phase_text(row),
        row.get("countdown") or "",
        _timestamp_text(row.get("checkedAt")),
        row.get("note") or row.get("errorSummary") or "",
    ]


def _claim_values(row):
    """提交结果一行。列序必须与 CLAIM_HEADERS 一一对应（测试钉列序）。"""
    status = str(row.get("status") or "").strip()
    refunds = row.get("refunds") or [row]
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        row.get("goodsImg") or "",
        AFTER_SALE_TYPE_LABEL,
        row.get("deliveredAt") or "",
        "\n".join(str(r.get("refundBillId") or "") for r in refunds),
        "\n".join(str(r.get("refundPath") or "") for r in refunds),
        "\n".join(str(r.get("refundAccount") or "") for r in refunds),
        (CLAIM_STATUS_LABELS.get(status, status) + (" · 部分包裹已受理" if status != "ok" and row.get("refunds") else "")),
        _timestamp_text(row.get("submittedAt")),
        row.get("note") or row.get("errorSummary") or "",
    ]


def _build_workbook(sheet_title, headers, widths, values):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    for column, name in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="123B63")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[get_column_letter(column)].width = (
            widths[column - 1]
        )
    for index, row_values in enumerate(values, start=2):
        for column, value in enumerate(row_values, start=1):
            sheet.cell(row=index, column=column, value=value)
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(headers))}{max(1, len(values) + 1)}"
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def build_after_sale_track_export(rows):
    """④ 退款跟踪导出。返回 (content, filename, mime)。"""
    content = _build_workbook(
        "退款跟踪", HEADERS, COLUMN_WIDTHS,
        [_row_values(row) for row in rows or []],
    )
    return content, f"退款跟踪结果_{_stamp()}.xlsx", MIME_XLSX


def build_after_sale_claim_export(rows):
    """③ 提交结果（批次）导出。返回 (content, filename, mime)。"""
    content = _build_workbook(
        "提交结果", CLAIM_HEADERS, CLAIM_COLUMN_WIDTHS,
        [_claim_values(row) for row in rows or []],
    )
    return content, f"售后提交结果_{_stamp()}.xlsx", MIME_XLSX
