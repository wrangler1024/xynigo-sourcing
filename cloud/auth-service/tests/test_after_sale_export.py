# -*- coding: utf-8 -*-
"""退款跟踪导出的列契约：列序、阶段文案、时间格式、空值。

盯的是「导出列与工作台 ④ 表格漂移」这类问题——只数格子个数的断言
挡不住整列错位，所以这里逐列比对表头与取值来源。
"""
from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from xynigo_auth.after_sale_export import (
    HEADERS,
    MIME_XLSX,
    build_after_sale_track_export,
)


def _row(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "refundBillId": "2390833880014851",
        "orderNo": "GSH1RV13Y00NQUV",
        "environmentSerial": "4586",
        "storeName": "合成店铺",
        "status": "ok",
        "phase": "refunded",
        "phaseLabel": "已退款",
        "countdown": "",
        "refundAccount": "****7935",
        "amount": "270.22",
        "goodsImg": "https://img.shein.com/synthetic-goods.jpg",
        "checkedAt": "2026-09-16T01:08:17+00:00",
        "note": None,
        "errorSummary": None,
    }
    row.update(overrides)
    return row


def _sheet(rows):
    content, filename, mime = build_after_sale_track_export(rows)
    workbook = load_workbook(BytesIO(content))
    return workbook, workbook.active, filename, mime


def test_export_header_order_matches_workbench_table() -> None:
    """列序 = 工作台 ④ 表头，逐字钉死（改列必须同时改 UI 与这里）。"""
    assert HEADERS == (
        "环境序号", "订单号", "商品图", "退款单号", "退款信用卡",
        "退款金额", "阶段", "剩余倒计时", "最近检查", "备注",
    )
    sheet = _sheet([])[1]
    assert [cell.value for cell in sheet[1]] == list(HEADERS)
    assert sheet.title == "退款跟踪"


def test_export_row_values_follow_header_order() -> None:
    """一行数据必须按表头顺序落格——整列错位是这块最易犯的错。"""
    _wb, sheet, _filename, mime = _sheet([_row()])
    assert mime == MIME_XLSX
    # 空列落成真正的空单元格（openpyxl 读回来是 None），不写 “—” 占位符，
    # 这样 Excel 里能直接筛选/求和
    assert [cell.value for cell in sheet[2]] == [
        "4586", "GSH1RV13Y00NQUV", "https://img.shein.com/synthetic-goods.jpg",
        "2390833880014851", "****7935", "270.22", "已退款", None,
        "2026-09-16 01:08", None,
    ]
    assert sheet.max_row == 2


def test_export_phase_falls_back_to_chinese_label() -> None:
    """执行器没带 phaseLabel 时按中文表补齐；未知阶段原样导出不吞掉。"""
    _wb, sheet, _f, _m = _sheet([
        _row(phase="reviewing", phaseLabel=""),
        _row(phase="rejected", phaseLabel=""),
        _row(phase="submitted", phaseLabel=""),
        _row(phase="overdue", phaseLabel=""),
        _row(phase="weird_new_phase", phaseLabel=""),
        _row(phase="refunded", phaseLabel="平台已退款"),
    ])
    assert [sheet.cell(row=index, column=7).value
            for index in range(2, 8)] == [
        "审核中", "已拒绝", "已受理", "超期未出结果",
        "weird_new_phase", "平台已退款",
    ]


def test_export_checked_at_and_note_fallbacks() -> None:
    """最近检查统一 `YYYY-MM-DD HH:MM`；备注缺 note 时取 errorSummary。"""
    _wb, sheet, _f, _m = _sheet([
        _row(checkedAt="2026-09-16T01:08:17+00:00"),
        _row(checkedAt=""),
        _row(checkedAt="不是时间"),
        _row(note="拒绝理由：地址不可达"),
        _row(note=None, errorSummary="回访失败：环境占用"),
    ])
    assert [sheet.cell(row=index, column=9).value
            for index in range(2, 5)] == [
        "2026-09-16 01:08", None, "不是时间",
    ]
    assert [sheet.cell(row=index, column=10).value
            for index in range(5, 7)] == [
        "拒绝理由：地址不可达", "回访失败：环境占用",
    ]


def test_export_filename_carries_utc_stamp() -> None:
    content, filename, _mime = build_after_sale_track_export([])
    assert filename.startswith("退款跟踪结果_")
    assert filename.endswith(".xlsx")
    assert len(filename) == len("退款跟踪结果_") + 8 + len(".xlsx")
    assert filename[len("退款跟踪结果_"):len("退款跟踪结果_") + 8].isdigit()
    assert content[:2] == b"PK"  # xlsx 就是 zip 容器


def test_export_without_rows_still_yields_readable_sheet() -> None:
    """没有可回访行时也要给出可用文件（只有表头），不能让页面拿到半截下载。"""
    workbook, sheet, _f, _m = _sheet([])
    assert [cell.value for cell in sheet[1]] == list(HEADERS)
    assert sheet.max_row == 1
    assert workbook.sheetnames == ["退款跟踪"]
