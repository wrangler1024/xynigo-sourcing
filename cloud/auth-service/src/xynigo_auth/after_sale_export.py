# -*- coding: utf-8 -*-
"""采购售后导出：② 可申请清单、③ 提交结果（批次）、④ 退款跟踪。

列与工作台表格一一对应，运营可直接用 Excel 筛选：
- 退款跟踪＝环境序号/订单号/商品图/退款单号/退款信用卡/退款金额/阶段/剩余倒计时/最近检查/备注
- 提交结果＝环境序号/订单号/商品图/售后类型/送达时间/退款单号/退款路径/退款信用卡/状态/操作时间/备注

商品图列写的是 CDN 链接而不是内嵌图片：导出过程不依赖外网取图，
链接在 Excel 里可直接点开；要内嵌图片另说（需服务端下载，多一层失败面）。
空值统一留空单元格（不写「—」占位符），Excel 里能直接筛选求和。
"""
import io
from decimal import Decimal, InvalidOperation
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
    "ok": "已受理 · 退款审核中", "blocked": "不可申请", "uncertain": "待核对 · 请勿补提", "verifying": "提交核验中",
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
    note = row.get("errorSummary") or row.get("note") or ""
    if status == "blocked" and "可能已提交过" in note:
        note = "平台未返回可申请包裹；历史记录未采集具体原因，需回访核对"
    legacy_unknown = status == "fail" and (row.get("refunds") or row.get("refundBillId") or any(
        marker in note for marker in ("提交后未跳转", "已跳转退款页", "已受理，但后续核验失败")))
    if legacy_unknown:
        status = "uncertain"
    label = CLAIM_STATUS_LABELS.get(status, status)
    if status == "blocked" and any(r.get("source") == "existing" for r in refunds):
        label = "不可申请 · 已有退款申请"
    if status == "uncertain" and "不可直接补提" not in note and "请勿" not in note:
        note += "；提交结果待核对，请勿直接补提"
    evidence = [" · ".join(str(r.get(k) or "") for k in ("refundBillId", "phaseLabel", "applicationTimeText", "timeZone", "source"))
                for r in row.get("refunds") or [] if r.get("source")]
    if evidence:
        note += "\n" + "\n".join(evidence)
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        row.get("goodsImg") or "",
        AFTER_SALE_TYPE_LABEL,
        row.get("deliveredAt") or "",
        "\n".join(str(r.get("refundBillId") or "") for r in refunds),
        "\n".join(str(r.get("refundPath") or "") for r in refunds),
        "\n".join(str(r.get("refundAccount") or "") for r in refunds),
        label,
        _timestamp_text(row.get("submittedAt")),
        note,
    ]


def _build_workbook(sheet_title, headers, widths, values, *, literal_strings=False):
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
            cell = sheet.cell(row=index, column=column, value=value)
            if literal_strings and isinstance(value, str):
                cell.data_type = "s"
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
        [_claim_values(row) for row in rows or []], literal_strings=True,
    )
    return content, f"售后提交结果_{_stamp()}.xlsx", MIME_XLSX


SCAN_HEADERS = (
    "环境序号", "买家号环境", "订单号", "商品图", "售后类型", "送达时间",
    "金额（MXN）", "可退包裹", "物流号", "状态", "备注",
    "平台订单状态原文", "资格判断来源", "最近读取时间",
)
SCAN_STATUS_LABELS = {
    "ok": "已扫描", "empty": "无订单", "skip": "暂不可申请",
    "blocked": "不可申请", "fail": "失败", "login": "未登录",
    "inuse": "环境占用", "stopped": "已停止", "queued": "排队中",
    "running": "扫描中",
}


def _scan_status(row):
    status = str(row.get("status") or "")
    if row.get("orderNo"):
        if status in {"skip", "empty"}:
            return "暂不可申请"
        if status == "ok":
            return "可申请" if row.get("claimable") else "不可申请"
    return SCAN_STATUS_LABELS.get(status, status)


def _scan_number(value):
    if value is None or value == "":
        return ""
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else str(value)
    except InvalidOperation:
        return str(value)


def build_after_sale_scan_export(rows):
    """导出当前任务全部扫描行，保持快照顺序，不按勾选筛选、不触发扫描。"""
    values = [[
        str(row.get("environmentSerial") or ""), row.get("storeName") or "",
        str(row.get("orderNo") or ""), row.get("goodsImg") or "",
        AFTER_SALE_TYPE_LABEL if row.get("orderNo") else "",
        row.get("deliveredAt") or "", _scan_number(row.get("amount")),
        _scan_number(row.get("packageCount")), str(row.get("trackingNo") or ""),
        _scan_status(row), row.get("errorSummary") or "",
        row.get("platformStatus") or "",
        {"order_list": "平台订单列表", "pre_info": "平台售后资格核验"}.get(row.get("reasonSource"), ""),
        row.get("checkedAt") or "",
    ] for row in rows or []]
    content = _build_workbook(
        "可申请清单", SCAN_HEADERS, (12, 28, 24, 34, 12, 24, 16, 12, 28, 18, 50, 45, 24, 30),
        values, literal_strings=True,
    )
    return content, f"售后可申请清单_{_stamp()}.xlsx", MIME_XLSX
