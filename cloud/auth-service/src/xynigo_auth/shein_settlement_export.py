# -*- coding: utf-8 -*-
"""结算看板导出：服务端生成 xlsx（沿用巡检 store_finance_export.py 的样式惯例）。

与接口 JSON 的一个关键差别：**xlsx 里的金额写成真正的数值 + `#,##0.00` 显示格式**，
不是字符串。财务要能在 Excel 里直接求和、透视；接口那边用字符串是为了避开
JS 浮点，两边诉求不同，所以不共用一套序列化。

文件结构（单 sheet，块状排布，与前端导出的列序一致）：
1. 店铺明细（含状态与说明，失败行金额留空且不参与汇总）
2. 汇总（按币种分列；末行为折人民币合计）——**值取自服务端口径层，不在本文件重算**
3. 结算排期（按预计打款日逐批）
4. 页脚：数据时间 / 覆盖店铺数 / 币种范围 / 汇率说明
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .shein_settlement_service import (
    IN_TRANSIT,
    NEAREST_PAYOUT,
    SETTLED_CUMULATIVE,
    UNSETTLED,
    SettlementSummary,
)

MONEY_FORMAT = "#,##0.00"

DETAIL_HEADERS = (
    "店铺", "类型", "币种", "在途资金", "待结算资金", "下次结算",
    "预计打款日", "已结算（历史累计）", "数据时间", "状态", "说明",
)
# 明细里的金额列（1-based），用于设置数值格式
DETAIL_MONEY_COLUMNS = (4, 5, 6, 8)
DETAIL_COLUMN_WIDTHS = (26, 14, 8, 14, 14, 14, 20, 18, 18, 12, 40)

MODE_LABELS = {"self": "SHEIN 自营", "semi": "SHEIN 半托管"}
STATUS_LABELS = {"ok": "正常", "warn": "有差异", "fail": "同步失败"}


def _money(value):
    """转成 xlsx 里的数值；None 留空——「没同步」与「确实是 0」必须分得开。"""
    return None if value is None else float(value)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _write_header(sheet, row: int, headers, *, width_from=None) -> None:
    font = Font(bold=True)
    fill = PatternFill("solid", fgColor="F2F2F2")
    for col, name in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=col, value=name)
        cell.font = font
        cell.fill = fill
        if width_from:
            sheet.column_dimensions[get_column_letter(col)].width = width_from[col - 1]


def build_settlement_export(summary: SettlementSummary):
    """返回 (content, filename, mime)。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "结算看板"

    _write_header(sheet, 1, DETAIL_HEADERS, width_from=DETAIL_COLUMN_WIDTHS)

    row_index = 2
    for store in summary.stores:
        # 失败行一律留空：即使聚合层已经剥掉批次，这里再按 status 兜一次，
        # 避免"先成功再失败"的店把上一轮的过期待结算带进财务手里的文件。
        settled_ok = store.status == "ok"
        nearest = store.nearest_pay_date if settled_ok else None
        nearest_amount = None
        if nearest is not None:
            nearest_amount = sum(
                (batch.amount for batch in store.payout_batches
                 if batch.pay_date == nearest), start=0)
        unsettled = (sum((batch.amount for batch in store.payout_batches), start=0)
                     if settled_ok and store.payout_batches else None)
        values = (
            store.store_name,
            MODE_LABELS.get(store.mode, store.mode),
            store.currency,
            # 失败行的**所有**金额列一律留空（不只待结算/下次结算）：在途与已结算
            # 目前虽由聚合层清空，导出侧再按状态兜一次，避免以后有人改聚合时漏掉这里。
            _money(store.in_transit_amount) if settled_ok else None,
            _money(unsettled) if settled_ok else None,
            _money(nearest_amount) if settled_ok else None,
            " / ".join(sorted({b.pay_date.isoformat()
                               for b in store.payout_batches})) or None,
            _money(store.settled_cumulative_amount) if settled_ok else None,
            store.synced_at.strftime("%Y-%m-%d %H:%M") if store.synced_at else None,
            STATUS_LABELS.get(store.status, store.status),
            store.error_summary or None,
        )
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=col, value=value)
            if col in DETAIL_MONEY_COLUMNS:
                cell.number_format = MONEY_FORMAT
        row_index += 1

    row_index += 1
    sheet.cell(row=row_index, column=1, value="汇总（值取自服务端口径层，非本文件计算）").font = \
        Font(bold=True)
    row_index += 1
    _write_header(
        sheet, row_index,
        ("", "", "币种", "在途资金", "待结算资金", "下次结算", "",
         "已结算（历史累计）"), width_from=(0, 0, 0, 0, 0, 0, 0, 0))
    row_index += 1

    currencies: list[str] = []
    for key in (IN_TRANSIT, UNSETTLED, SETTLED_CUMULATIVE):
        for group in summary.cards[key].groups:
            if group.currency not in currencies:
                currencies.append(group.currency)
    for currency in sorted(currencies):
        def pick(key):
            group = summary.cards[key].group(currency)
            return _money(group.total) if group else None
        values = ("", "", currency, pick(IN_TRANSIT), pick(UNSETTLED),
                  pick(NEAREST_PAYOUT), "", pick(SETTLED_CUMULATIVE))
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=col, value=value)
            if col in DETAIL_MONEY_COLUMNS:
                cell.number_format = MONEY_FORMAT
        row_index += 1

    cny_values = ("", "", "折人民币",
                  _money(summary.cards[IN_TRANSIT].cny_total),
                  _money(summary.cards[UNSETTLED].cny_total),
                  _money(summary.cards[NEAREST_PAYOUT].cny_total), "",
                  _money(summary.cards[SETTLED_CUMULATIVE].cny_total))
    for col, value in enumerate(cny_values, start=1):
        cell = sheet.cell(row=row_index, column=col, value=value)
        if col in DETAIL_MONEY_COLUMNS:
            cell.number_format = MONEY_FORMAT
    row_index += 2

    sheet.cell(row=row_index, column=1,
               value="结算排期（按预计打款日，取自对账单 estimatePayTime）").font = \
        Font(bold=True)
    row_index += 1
    _write_header(sheet, row_index, ("", "", "打款日", "折人民币", "该批币种:原币金额"),
                  width_from=(0, 0, 0, 0, 0))
    row_index += 1
    for batch in summary.schedule:
        native = " ".join(f"{group.currency}:{group.total:.2f}"
                          for group in batch.groups)
        sheet.cell(row=row_index, column=3, value=batch.pay_date.isoformat())
        cny_cell = sheet.cell(row=row_index, column=4, value=_money(batch.cny))
        cny_cell.number_format = MONEY_FORMAT
        sheet.cell(row=row_index, column=5, value=native)
        row_index += 1

    row_index += 1
    sheet.cell(row=row_index, column=1,
               value=f"数据时间：{_latest(summary)}")
    row_index += 1
    sheet.cell(row=row_index, column=1,
               value=f"店铺：成功 {summary.store_ok} / 共 {summary.store_total} 家")
    row_index += 1
    sheet.cell(row=row_index, column=1,
               value="汇率口径：中国银行当日汇率（随每日快照固化，历史值不重算）")
    row_index += 1
    missing = sorted({
        group.currency
        for card in summary.cards.values() for group in card.groups
        if group.cny is None
    })
    if missing:
        sheet.cell(
            row=row_index, column=1,
            value=f"缺少 {' / '.join(missing)} 汇率，对应人民币合计为空（不按 1:1 计入）")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return (buffer.getvalue(), f"结算看板_{_stamp()}.xlsx",
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet")


def _latest(summary: SettlementSummary) -> str:
    stamps = [store.synced_at for store in summary.stores if store.synced_at]
    if not stamps:
        return "—"
    return max(stamps).strftime("%Y-%m-%d %H:%M")
