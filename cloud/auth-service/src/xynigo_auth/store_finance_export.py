# -*- coding: utf-8 -*-
"""店铺结算查询导出：生成《店铺结算汇总表》xlsx/csv。

标准导出与运营手工表六列格式一致（店铺中文名/在途订单金额/
累计未结算金额/下次结算金额/已完成结算收入/不可提现金额），
财务可直接沿用现有整理流程。
"""
import csv
import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

STANDARD_HEADERS = (
    "店铺中文名", "在途订单金额", "累计未结算金额", "下次结算金额",
    "已完成结算收入", "不可提现金额",
)
FULL_EXTRA_HEADERS = (
    "GS 编号", "下次结算日", "最近打款金额", "可提现", "提现中",
    "待结算金额限制", "状态", "登录方式", "采集时间 (UTC)", "异常说明",
)
MONEY_COLUMNS_STD = (2, 3, 4, 5, 6)


def _cell_value(row, key):
    value = row.get(key)
    return "" if value is None else float(value)


def _std_rows(rows):
    # 标准导出（发财务的六列表）只含成功采集的店铺；失败行见完整导出
    rows = [row for row in rows if row.get("status") == "ok"]
    for row in rows:
        yield [
            row.get("storeName") or row.get("environmentSerial") or "",
            _cell_value(row, "inTransitAmount"),
            _cell_value(row, "unsettledAmount"),
            _cell_value(row, "nextSettlementAmount"),
            _cell_value(row, "completedSettlementAmount"),
            _cell_value(row, "nonWithdrawableAmount"),
        ]


def _fmt(value):
    return "" if value is None else f"{float(value):.2f}"


def build_store_finance_export(
    rows, *, variant="standard",
):
    """生成导出文件。返回 (content, filename, mime)。

    rows 为快照行（已按店铺名排序）；variant=standard 六列 / full 全列。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "full" if variant == "full" else ""
    base = f"店铺结算汇总表_{stamp}{suffix}"
    rows = sorted(
        rows, key=lambda r: str(r.get("storeName")
                                or r.get("environmentSerial") or ""))

    if variant == "full":
        headers = STANDARD_HEADERS + FULL_EXTRA_HEADERS
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([
                row.get("storeName") or row.get("environmentSerial") or "",
                _fmt(row.get("inTransitAmount")),
                _fmt(row.get("unsettledAmount")),
                _fmt(row.get("nextSettlementAmount")),
                _fmt(row.get("completedSettlementAmount")),
                _fmt(row.get("nonWithdrawableAmount")),
                row.get("gsCode") or "",
                row.get("nextSettlementDate") or "",
                _fmt(row.get("lastPayoutAmount")),
                _fmt(row.get("withdrawableAmount")),
                _fmt(row.get("payoutInProgressAmount")),
                _fmt(row.get("pendingSettleLimitAmount")),
                row.get("status") or "",
                row.get("loginMode") or "",
                row.get("collectedAt") or "",
                row.get("errorSummary") or "",
            ])
        content = buffer.getvalue().encode("utf-8-sig")
        return content, f"{base}_full.csv", "text/csv; charset=utf-8"

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    header_font = Font(bold=True)
    for col, name in enumerate(STANDARD_HEADERS, start=1):
        cell = sheet.cell(row=1, column=col, value=name)
        cell.font = header_font
        cell.fill = PatternFill("solid", fgColor="F2F2F2")
        sheet.column_dimensions[get_column_letter(col)].width = 16
    sheet.column_dimensions["A"].width = 22
    for r, values in enumerate(_std_rows(rows), start=2):
        for c, value in enumerate(values, start=1):
            sheet.cell(row=r, column=c, value=value)
    last = len(rows) + 1
    if rows:
        for col in MONEY_COLUMNS_STD:
            letter = get_column_letter(col)
            sheet.cell(
                row=last + 1, column=col,
                value=f"=SUM({letter}2:{letter}{last})")
        sheet.cell(row=last + 1, column=1, value="合计")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), f"{base}.xlsx", (
        "application/vnd.openxmlformats-officedocument"
        ".spreadsheetml.sheet")
