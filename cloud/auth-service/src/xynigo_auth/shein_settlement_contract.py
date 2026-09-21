# -*- coding: utf-8 -*-
"""结算看板接口契约：把聚合结果序列化成前端可用的载荷。

金额一律序列化为**两位小数字符串**（如 `"945.16"`），不用 JSON number：
- 金额已是 Decimal 且落到分，转 float 会在 JS 侧引入二进制浮点误差；
- 前端本来就要格式化展示，字符串既精确又不需要再转一次。

缺汇率的币种 `cny` 为 `null`，前端显示「—」——**不要**把 null 当 0 处理。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .shein_settlement_service import (
    MetricCard,
    MetricGroup,
    PayoutScheduleRow,
    SettlementSummary,
    StoreSettlementInput,
    next_pay_date,
)
from .shein_settlement_windows import PLATFORM_TZ


def _today(value: date | None) -> date:
    return value if value is not None else datetime.now(PLATFORM_TZ).date()


def _money(value) -> str | None:
    return None if value is None else f"{value:.2f}"


def _group(group: MetricGroup) -> dict[str, Any]:
    return {
        "currency": group.currency,
        "total": _money(group.total),
        "cny": _money(group.cny),
    }


def _card(card: MetricCard) -> dict[str, Any]:
    return {
        "key": card.key,
        "title": card.title,
        "hint": card.hint,
        "groups": [_group(item) for item in card.groups],
        "cnyTotal": _money(card.cny_total),
        "nearestPayDate": (
            card.nearest_pay_date.isoformat() if card.nearest_pay_date else None
        ),
    }


def _schedule_row(row: PayoutScheduleRow) -> dict[str, Any]:
    return {
        "payDate": row.pay_date.isoformat(),
        "groups": [_group(item) for item in row.groups],
        "cny": _money(row.cny),
    }


def _store(store: StoreSettlementInput, today: date) -> dict[str, Any]:
    # 店铺列与卡片同口径：未来最近一批；只有逾期批次时该列为空。
    nearest = next_pay_date(store.payout_batches, today=today)
    nearest_amount = None
    if nearest is not None:
        total = sum(
            (batch.amount for batch in store.payout_batches
             if batch.pay_date == nearest), start=0)
        nearest_amount = _money(total)
    return {
        "storeId": store.store_id,
        "storeName": store.store_name,
        "mode": store.mode,
        "currency": store.currency,
        "status": store.status,
        "error": store.error_summary,
        # 收款方式：1 直接打款 / 2 钱包充值（本期不在看板呈现，先留给后续口径）
        "paymentMethod": store.payment_method,
        "inTransitAmount": _money(store.in_transit_amount),
        "unsettledAmount": _money(sum(
            (batch.amount for batch in store.payout_batches), start=0))
        if store.payout_batches else None,
        "nearestPayoutAmount": nearest_amount,
        "nearestPayDate": nearest.isoformat() if nearest else None,
        "payDates": sorted({batch.pay_date.isoformat()
                            for batch in store.payout_batches}),
        "settledCumulativeAmount": _money(store.settled_cumulative_amount),
        "syncedAt": store.synced_at.isoformat() if store.synced_at else None,
    }


def _latest_synced_at(summary: SettlementSummary) -> str | None:
    """整体数据时间=各店最近同步时间的最大值（展示用，不参与口径）。"""
    stamps = [store.synced_at for store in summary.stores if store.synced_at]
    return max(stamps).isoformat() if stamps else None


def settlement_summary_payload(
    summary: SettlementSummary, *, today: date | None = None
) -> dict[str, Any]:
    current = _today(today)
    return {
        "cards": {key: _card(card) for key, card in summary.cards.items()},
        "schedule": [_schedule_row(row) for row in summary.schedule],
        "stores": [_store(store, current) for store in summary.stores],
        "alerts": [
            {"kind": alert.kind, "storeName": alert.store_name,
             "message": alert.message}
            for alert in summary.alerts
        ],
        "currencies": list(summary.currencies),
        "syncedAt": _latest_synced_at(summary),
        "storeTotal": summary.store_total,
        "storeOk": summary.store_ok,
        "storeFailed": summary.store_failed,
    }
