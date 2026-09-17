# -*- coding: utf-8 -*-
"""取数窗口分片回归。

两条约束的性质不同，用例也分两类：
- 对账单：**超界是硬报错**（gsfs99401），所以要保证每个窗口都真的 ≤7 天整，
  尤其是带毫秒的边界。
- 订单：**漏单是静默的**，所以要保证窗口覆盖连续无空洞、且增量带重叠。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from xynigo_auth.shein_settlement_windows import (
    CHECK_ORDER_MAX_SPAN,
    ORDER_MAX_SPAN,
    PLATFORM_TZ,
    build_check_order_windows,
    build_fetch_plan,
    build_order_backfill_windows,
    build_order_incremental_windows,
    format_platform_time,
    slice_time_windows,
)

TZ = ZoneInfo("Asia/Shanghai")


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


# ---- 对账单：窗口绝不能超界 ----

def test_check_order_window_span_never_exceeds_seven_days():
    windows = build_check_order_windows(
        _dt(2026, 9, 1, 0, 0, 0), _dt(2026, 9, 25, 0, 0, 0))
    assert windows
    for start, end in windows:
        assert end - start <= CHECK_ORDER_MAX_SPAN


def test_millisecond_boundary_does_not_overflow_window():
    """恰好 7 天的区间若保留毫秒就变成"7 天零几毫秒"，平台直接报 gsfs99401。"""
    start = datetime(2026, 9, 1, 0, 0, 0, 500000, tzinfo=TZ)
    end = datetime(2026, 9, 8, 0, 0, 0, 900000, tzinfo=TZ)
    windows = build_check_order_windows(start, end)
    assert len(windows) == 1
    for window_start, window_end in windows:
        assert window_start.microsecond == 0
        assert window_end.microsecond == 0
        assert window_end - window_start <= CHECK_ORDER_MAX_SPAN


def test_check_order_windows_are_contiguous_and_non_overlapping():
    """重叠会让同一张账单被算两次，而待结算是求和口径——重复即翻倍。"""
    windows = build_check_order_windows(
        _dt(2026, 9, 1), _dt(2026, 9, 25))
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert prev_end == next_start


def test_check_order_window_boundaries_align_to_whole_days():
    windows = build_check_order_windows(
        _dt(2026, 9, 1, 8, 30), _dt(2026, 9, 16, 8, 30))
    assert all((end - start) == CHECK_ORDER_MAX_SPAN for start, end in windows[:-1])
    assert windows[0][0] == _dt(2026, 9, 1, 8, 30)


def test_slice_rejects_non_positive_span():
    with pytest.raises(ValueError):
        slice_time_windows(_dt(2026, 9, 1), _dt(2026, 9, 2), timedelta(0))


def test_empty_range_yields_no_windows():
    assert build_check_order_windows(_dt(2026, 9, 2), _dt(2026, 9, 1)) == []


# ---- 订单：窗口必须覆盖连续，不能有空洞 ----

def test_order_backfill_is_ordered_oldest_first():
    """从早到晚 upsert，最后落库的才是最新状态；倒序会把台账写乱。"""
    windows = build_order_backfill_windows(
        _dt(2026, 9, 17, 12, 0), lookback_days=5)
    assert windows == sorted(windows)
    assert windows[-1][1] == _dt(2026, 9, 17, 12, 0)
    assert windows[0][0] == _dt(2026, 9, 12, 12, 0)


def test_order_backfill_windows_are_contiguous():
    windows = build_order_backfill_windows(
        _dt(2026, 9, 17, 12, 0), lookback_days=10)
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert prev_end == next_start
    assert all(end - start <= ORDER_MAX_SPAN for start, end in windows)


def test_order_backfill_span_never_exceeds_48h():
    windows = build_order_backfill_windows(
        _dt(2026, 9, 17, 0, 0), lookback_days=60)
    assert len(windows) == 30  # 60 天 / 48h
    assert all(end - start <= ORDER_MAX_SPAN for start, end in windows)


def test_incremental_windows_overlap_to_avoid_boundary_gap():
    """平台写入与本地读取有时间差，边界上的订单会被两边都漏掉；
    重叠只会重复 upsert（幂等），漏掉则是永久少一笔在途。"""
    last = _dt(2026, 9, 17, 6, 0)
    now = _dt(2026, 9, 17, 9, 0)
    windows = build_order_incremental_windows(last, now)
    assert windows
    assert windows[0][0] == last - timedelta(hours=2)
    assert windows[-1][1] == now


def test_incremental_without_previous_sync_falls_back_to_backfill():
    now = _dt(2026, 9, 17, 9, 0)
    windows = build_order_incremental_windows(None, now, )
    assert windows
    assert windows[-1][1] == now
    assert len(windows) > 1


def test_same_instant_sync_still_covers_the_overlap_window():
    """重叠是有意为之：即使距上次同步只有一瞬间，也会重覆盖重叠窗。

    重复拉到只是再 upsert 一次（幂等），漏掉则是永久少一笔在途——
    所以这里断言"仍有一个窗口"，而不是"没有窗口"。
    """
    now = _dt(2026, 9, 17, 9, 0)
    windows = build_order_incremental_windows(now, now)
    assert windows == [(now - timedelta(hours=2), now)]


# ---- 时间格式 ----

def test_platform_time_format_is_utc8_whole_seconds():
    value = datetime(2026, 9, 17, 1, 2, 3, 456789, tzinfo=ZoneInfo("UTC"))
    assert format_platform_time(value) == "2026-09-17 09:02:03"


def test_platform_time_format_converts_naive_utc_to_utc8():
    naive_utc = datetime(2026, 9, 17, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert format_platform_time(naive_utc) == "2026-09-17 08:00:00"
    assert PLATFORM_TZ.key == "Asia/Shanghai"


# ---- 取数计划 ----

def test_plan_marks_full_backfill_on_first_sync():
    plan = build_fetch_plan(
        now=_dt(2026, 9, 17, 12, 0),
        last_order_synced_at=None,
        check_order_start=_dt(2026, 9, 10),
        check_order_end=_dt(2026, 9, 17),
        order_lookback_days=4,
    )
    assert plan.full_backfill is True
    assert plan.order_window_count == 2      # 4 天 / 48h
    assert plan.check_order_window_count == 1  # 7 天整


def test_plan_uses_incremental_after_first_sync():
    plan = build_fetch_plan(
        now=_dt(2026, 9, 17, 12, 0),
        last_order_synced_at=_dt(2026, 9, 17, 6, 0),
        check_order_start=_dt(2026, 9, 14),
        check_order_end=_dt(2026, 9, 17),
    )
    assert plan.full_backfill is False
    assert plan.order_window_count == 1
    assert plan.order_windows[0][0] == _dt(2026, 9, 17, 4, 0)
