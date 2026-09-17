# -*- coding: utf-8 -*-
"""结算取数的时间窗分片与回溯计划。

这是同步层最容易被低估的一块，两条接口的窗口约束性质完全不同：

- **对账单**（`get-check-order-list`）：窗口按"对账单生成时间"算，文档写"不可超过
  7 天"，但**实测"恰好 7 天整"仍会被拒**（gsfs99401）。所以分片要取 7 天**减 1 秒**，
  且边界清零微秒——带毫秒会让"恰好 7 天"变成 7 天零几毫秒而报错。
- **订单列表**（`order-list`）：窗口 ≤48h，同样按减 1 秒处理。它的问题不是报错
  而是**漏单**：一张 5 天前发货、至今未签收的订单，若 48h 内没有更新时间就不会
  出现在窗口里 → 直接查会把在途算少。故必须按窗口分片向前回溯建本地台账，
  之后只拉增量。

本模块只做"该取哪些窗口"的计算，纯函数、可单测；真正的调用与落库在
同步编排里（后续增量）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# 平台时间戳按 UTC+8 解释（订单列表 startTime/endTime 文档口径）。
PLATFORM_TZ = ZoneInfo("Asia/Shanghai")
PLATFORM_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 窗口上限。实测"恰好 7 天整"会被平台拒绝（gsfs99401），所以各减 1 秒，
# 保证生成的是"严格小于"上限的窗口。
CHECK_ORDER_MAX_SPAN = timedelta(days=7) - timedelta(seconds=1)
ORDER_MAX_SPAN = timedelta(hours=48) - timedelta(seconds=1)

# 订单台账首次回溯的最大天数。未签收订单不会无限累积——最终会签收/退款，
# 所以回溯到"连续若干窗口都没有在途单"即可停；这个上限是兜底，防止异常数据
# 让首次同步无限翻页。
DEFAULT_ORDER_BACKFILL_DAYS = 60


def floor_to_second(value: datetime) -> datetime:
    """边界取到整秒：窗口含毫秒会让"恰好 7 天"凭空超界而报 gsfs99401。"""
    return value.replace(microsecond=0)


def format_platform_time(value: datetime) -> str:
    """格式化为平台要求的 `yyyy-MM-dd HH:mm:ss`（UTC+8）。"""
    return floor_to_second(value).astimezone(PLATFORM_TZ).strftime(
        PLATFORM_TIME_FORMAT)


def slice_time_windows(
    start: datetime, end: datetime, max_span: timedelta,
) -> list[tuple[datetime, datetime]]:
    """把 [start, end) 切成若干长度不超过 max_span 的窗口，边界整秒、按时间升序。

    `end` 不含在窗口内（左闭右开），相邻窗口首尾相接不重叠——重叠会让同一张
    账单被算两次，而待结算是求和口径，重复即金额翻倍。
    """
    if max_span <= timedelta(0):
        raise ValueError("max_span 必须为正")
    cursor = floor_to_second(start)
    limit = floor_to_second(end)
    windows: list[tuple[datetime, datetime]] = []
    while cursor < limit:
        nxt = min(cursor + max_span, limit)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def build_check_order_windows(
    start: datetime, end: datetime,
) -> list[tuple[datetime, datetime]]:
    """对账单查询窗口（≤7 天整）。"""
    return slice_time_windows(start, end, CHECK_ORDER_MAX_SPAN)


def build_order_backfill_windows(
    now: datetime,
    *,
    lookback_days: int = DEFAULT_ORDER_BACKFILL_DAYS,
    max_span: timedelta = ORDER_MAX_SPAN,
) -> list[tuple[datetime, datetime]]:
    """订单台账首次建账：从 now 向前回溯，返回**从早到晚**的窗口。

    按更新时间倒着拉会把台账越写越乱（同订单多次更新），从早到晚顺序
    upsert 才能保证最后落库的是最新状态。
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days 必须为正")
    end = floor_to_second(now)
    start = end - timedelta(days=lookback_days)
    return slice_time_windows(start, end, max_span)


def build_order_incremental_windows(
    last_synced_at: datetime | None,
    now: datetime,
    *,
    overlap: timedelta = timedelta(hours=2),
    max_span: timedelta = ORDER_MAX_SPAN,
) -> list[tuple[datetime, datetime]]:
    """订单台账增量窗口：从上次同步时刻（回退一段重叠）拉到现在。

    故意与上次窗口**重叠**一段：平台更新时间的写入与我们的读取存在时间差，
    刚好落在边界上的订单会被两边都漏掉。重复拉到只是再 upsert 一次（幂等），
    漏掉则是永久少一笔在途。
    """
    end = floor_to_second(now)
    if last_synced_at is None:
        return build_order_backfill_windows(now)
    start = floor_to_second(last_synced_at) - overlap
    if start >= end:
        return []
    return slice_time_windows(start, end, max_span)


@dataclass(frozen=True)
class SettlementFetchPlan:
    """一次同步要打的窗口清单（只描述计划，不含网络调用）。"""

    order_windows: tuple[tuple[datetime, datetime], ...]
    check_order_windows: tuple[tuple[datetime, datetime], ...]
    full_backfill: bool

    @property
    def order_window_count(self) -> int:
        return len(self.order_windows)

    @property
    def check_order_window_count(self) -> int:
        return len(self.check_order_windows)


def build_fetch_plan(
    *,
    now: datetime,
    last_order_synced_at: datetime | None,
    check_order_start: datetime,
    check_order_end: datetime,
    order_lookback_days: int = DEFAULT_ORDER_BACKFILL_DAYS,
) -> SettlementFetchPlan:
    """组装一次同步的取数计划。

    - 订单：首次全量回溯建台账，之后只拉增量（带重叠）。
    - 对账单：按调用方给的区间分 7 天片；常规刷新只需覆盖最近一两个窗口，
      全量历史由已付款报账单累计表承担（不在这里重复翻）。
    """
    full_backfill = last_order_synced_at is None
    order_windows = (
        build_order_backfill_windows(now, lookback_days=order_lookback_days)
        if full_backfill
        else build_order_incremental_windows(last_order_synced_at, now)
    )
    return SettlementFetchPlan(
        order_windows=tuple(order_windows),
        check_order_windows=tuple(
            build_check_order_windows(check_order_start, check_order_end)),
        full_backfill=full_backfill,
    )
