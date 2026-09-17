# -*- coding: utf-8 -*-
"""采购售后导出：② 可申请清单、③ 提交结果（批次）、④ 退款跟踪。

列与工作台表格一一对应，运营可直接用 Excel 筛选：
- 退款跟踪＝环境序号/订单号/商品图/退款单号/退款信用卡/退款金额/阶段/剩余倒计时/最近检查/备注
- 提交结果＝环境序号/订单号/商品图/售后类型/送达时间/退款单号/退款路径/退款信用卡/状态/操作时间/备注

商品图以内嵌单元格图片导出，多件订单合为同格图片网格；源链接保留在批注中。
读取失败保留原链接并在备注说明，不因个别图片失败丢失整份订单数据。
空值统一留空单元格（不写「—」占位符），Excel 里能直接筛选求和。
"""
import io
import threading
from functools import wraps
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter

from .after_sale_export_images import collect_thumbnails, product_image_grid, product_image_urls
from .after_sale_presentation import display_refund_path
from .procurement_import_xlsx import embed_cell_images

HEADERS = (
    "环境序号", "订单号", "商品图", "退款单号", "退款信用卡",
    "退款金额", "阶段", "剩余倒计时", "最近检查", "备注",
)
COLUMN_WIDTHS = (12, 22, 34, 24, 20, 14, 30, 16, 20, 52)
# 与执行器 after_sale_claim.PHASE_LABELS、Web AS_TL_LABEL 保持同一套中文
PHASE_LABELS = {
    "submitted": "已受理", "reviewing": "审核中", "processing": "SHEIN处理中",
    "shein_refunded":"SHEIN退款成功", "bank_processed":"金融机构已处理",
    "review_failed":"审核未通过", "evidence_required":"审核未通过 · 待补充凭证",
    "refunded":"历史退款状态 · 待回访", "rejected":"历史拒绝状态 · 待回访", "overdue":"超期未出结果",
}

CLAIM_HEADERS = (
    "环境序号", "订单号", "商品图", "售后类型", "送达时间", "退款单号",
    "退款路径", "退款信用卡", "状态", "操作时间", "备注", "订单件数", "商品明细",
)
CLAIM_COLUMN_WIDTHS = (12, 22, 34, 12, 20, 22, 26, 16, 20, 20, 32, 12, 60)
# 与 Web AS_CLAIM_PILL 同一套中文文案：导出与页面不能各说各话
CLAIM_STATUS_LABELS = {
    "ok": "已受理", "blocked": "不可申请", "uncertain": "待核对 · 请勿补提", "verifying": "提交核验中",
    "skip": "跳过", "empty": "无订单", "fail": "失败",
    "login": "失败 · 未登录", "inuse": "失败 · 环境占用",
    "stopped": "已停止", "queued": "等待", "running": "提交中",
}
AFTER_SALE_TYPE_LABEL = "丢件退款"

MIME_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

_EXPORT_SLOT = threading.BoundedSemaphore(1)


class AfterSaleExportBusy(RuntimeError):
    pass


def _one_export_at_a_time(build):
    @wraps(build)
    def guarded(*args, **kwargs):
        if not _EXPORT_SLOT.acquire(blocking=False):
            raise AfterSaleExportBusy('售后表格正在生成，请稍后重试导出')
        try:
            return build(*args, **kwargs)
        finally:
            _EXPORT_SLOT.release()
    return guarded


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
    label = PHASE_LABELS.get(phase, label or phase)
    if row.get('status') in ('fail','login','inuse','blocked','skip','empty'):
        return '本次状态未确认' + ('；上次：'+label if label else '')
    return label


def _operation_time_text(value):
    """Keep seconds and an explicit offset; never export a misleading naive time."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return moment.isoformat(sep=" ", timespec="seconds") if moment.tzinfo else ""


def _row_values(row):
    """退款跟踪一行。列序必须与 HEADERS 一一对应（测试钉列序）。"""
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        "\n".join(product_image_urls(row)),
        row.get("refundBillId") or "",
        row.get("refundAccount") or "",
        row.get("amount") or "",
        _phase_text(row),
        (row.get("countdown") or "") if row.get("status") in (None,"ok") else "",
        _timestamp_text(row.get("checkedAt")),
        '\n'.join(filter(None, [row.get("note") or "",
            '本次未确认：'+row['errorSummary'] if row.get('errorSummary') else '',
            '历史状态，尚未按当前节点重新核验' if row.get('phase') and not row.get('phaseEvidence') else ''])),
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
    phases = list(dict.fromkeys(str(r.get("phaseLabel") or "") for r in refunds if r.get("phaseLabel")))
    if phases:
        label = ("已受理" if status == "ok" else label) + " · 平台：" + " / ".join(phases)
    for key, prefix in (("submissionError", "提交响应"), ("recoveryError", "补查")):
        if row.get(key):
            note += "\n" + prefix + "：" + row[key]
    if status == "blocked" and any(r.get("source") == "existing" for r in refunds):
        label = "不可申请 · 已有退款申请"
    if status == "uncertain" and "不可直接补提" not in note and "请勿" not in note:
        note += "；提交结果待核对，请勿直接补提"
    evidence = [" · ".join(str(r.get(k) or "") for k in ("refundBillId", "phaseLabel", "applicationTimeText", "timeZone", "source", "detailsNote"))
                for r in row.get("refunds") or [] if r.get("source")]
    if evidence:
        note += "\n" + "\n".join(evidence)
    account_checks = ['账户回访补全：%s，最近回访 %s' %
                      (r.get('refundBillId') or '', _operation_time_text(r.get('refundAccountCheckedAt')))
                      for r in refunds if r.get('refundAccountSource') == 'tracking']
    if account_checks:
        note += '\n' + '\n'.join(account_checks)
    return [
        row.get("environmentSerial") or "",
        row.get("orderNo") or "",
        "\n".join(product_image_urls(row)),
        AFTER_SALE_TYPE_LABEL,
        row.get("deliveredAt") or "",
        "\n".join(str(r.get("refundBillId") or "") for r in refunds),
        "\n".join(display_refund_path(r.get("refundPath")) for r in refunds),
        "\n".join(str(r.get("refundAccount") or "") for r in refunds),
        label,
        _operation_time_text(row.get("operationCompletedAt")),
        note,
        row.get("itemCount"),
        "\n".join(" · ".join([str(p.get("name") or "商品名称未取得"), str(p.get("specification") or "规格待核对"),
                              str(p["quantity"])+" 件" if p.get("quantity") is not None else "数量待核对"])
                    for p in row.get("goodsItems") or []),
    ]


def _build_workbook(sheet_title, headers, widths, values, *, literal_strings=False,
                    rows=(), image_column=3, image_fetcher=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    side = Side(style="thin", color="D8E2EA")
    border = Border(left=side, right=side, top=side, bottom=side)
    for column, name in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=column, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="123B63")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
        sheet.column_dimensions[get_column_letter(column)].width = (
            widths[column - 1]
        )
    for index, row_values in enumerate(values, start=2):
        for column, value in enumerate(row_values, start=1):
            cell = sheet.cell(row=index, column=column, value=value)
            cell.border = border
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            if literal_strings and isinstance(value, str):
                cell.data_type = "s"
    thumbnails = collect_thumbnails(rows, image_fetcher)
    images = []
    note_column = headers.index('备注') + 1
    for index, row in enumerate(rows, start=2):
        urls = product_image_urls(row)
        if not urls:
            continue
        cell = sheet.cell(index, image_column)
        cell.comment = Comment('商品图片原始链接（按图序）：\n'+'\n'.join(urls), 'Xynigo')
        grid = product_image_grid(urls, thumbnails)
        if grid:
            images.append((cell.coordinate, grid[0]))
            sheet.row_dimensions[index].height = grid[1]
        missing = sum(not thumbnails.get(url) for url in urls)
        if missing:
            note = sheet.cell(index, note_column)
            note.value = '\n'.join(filter(None, [str(note.value or ''),
                '商品图片 %d/%d 张未取得（读取失败或超出本次导出预算），原链接见商品图单元格及批注' % (missing,len(urls))]))
            note.data_type = 's'
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(headers))}{max(1, len(values) + 1)}"
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return embed_cell_images(buffer.getvalue(), images)


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


@_one_export_at_a_time
def build_after_sale_track_export(rows, *, image_fetcher=None):
    """④ 退款跟踪导出。返回 (content, filename, mime)。"""
    content = _build_workbook(
        "退款跟踪", HEADERS, COLUMN_WIDTHS,
        [_row_values(row) for row in rows or []], literal_strings=True,
        rows=rows or [], image_fetcher=image_fetcher,
    )
    return content, f"退款跟踪结果_{_stamp()}.xlsx", MIME_XLSX


@_one_export_at_a_time
def build_after_sale_claim_export(rows, *, image_fetcher=None):
    """③ 提交结果（批次）导出。返回 (content, filename, mime)。"""
    content = _build_workbook(
        "提交结果", CLAIM_HEADERS, CLAIM_COLUMN_WIDTHS,
        [_claim_values(row) for row in rows or []], literal_strings=True,
        rows=rows or [], image_fetcher=image_fetcher,
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


@_one_export_at_a_time
def build_after_sale_scan_export(rows, *, image_fetcher=None):
    """导出当前任务全部扫描行，保持快照顺序，不按勾选筛选、不触发扫描。"""
    values = [[
        str(row.get("environmentSerial") or ""), row.get("storeName") or "",
        str(row.get("orderNo") or ""), "\n".join(product_image_urls(row)),
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
        values, literal_strings=True, rows=rows or [], image_column=4, image_fetcher=image_fetcher,
    )
    return content, f"售后可申请清单_{_stamp()}.xlsx", MIME_XLSX
