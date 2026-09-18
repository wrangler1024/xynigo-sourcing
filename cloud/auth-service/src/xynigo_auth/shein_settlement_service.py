# -*- coding: utf-8 -*-
"""结算看板聚合：把店铺级取数结果汇总成看板口径。

纯函数、无副作用、不碰数据库——口径集中在这里，便于评审与回归。
口径依据 docs/20260917_需求_财务中心结算看板.md（20260917 真机实测 + 拍板）：

1. 在途资金   = Σ estimatedGrossIncome（orderStatus=4 已发货未签收，订单级）
2. 待结算资金 = Σ 对账单 checkStatus=1 按收支轧差净额（**全部**）
3. 下次结算   = 同上净额中**仅最近一个打款日那一批**
   — 两者都取全量就会是同一个数，必须按打款日切开。
4. 已结算资金 = Σ 报账单 reportStatus=2（**历史全量累计**，非本期/本年度）
5. 人民币合计 = Σ(原币 × 当日维护汇率)；**任一币种缺汇率即返回 None**，
   绝不按 1:1 静默计入（比索当人民币算会让总数错得离谱还不报错）。
6. 失败店铺不计入任何汇总，但必须能在结果里看到。
7. 金额一律以 Decimal 计算并落到分；导出与接口不出现浮点脏数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

IN_TRANSIT = "in_transit"
UNSETTLED = "unsettled"
NEAREST_PAYOUT = "nearest_payout"
SETTLED_CUMULATIVE = "settled_cumulative"

METRIC_KEYS = (IN_TRANSIT, UNSETTLED, NEAREST_PAYOUT, SETTLED_CUMULATIVE)

CARD_TITLES = {
    IN_TRANSIT: "在途资金",
    UNSETTLED: "待结算资金",
    NEAREST_PAYOUT: "下次结算金额",
    SETTLED_CUMULATIVE: "已结算资金",
}

# 口径说明文案（前端卡片 hint，与原型一致）
CARD_HINTS = {
    IN_TRANSIT: "已发货未签收（订单级）",
    UNSETTLED: "对账单待结算按收支轧差（全部）",
    NEAREST_PAYOUT: "仅最近一批",
    SETTLED_CUMULATIVE: "报账单已付款历史累计",
}


def to_cent(amount: Decimal | None) -> Decimal | None:
    """金额落到分。求和是 Decimal 累加，这里再统一舍入，避免下游出现长尾。"""
    if amount is None:
        return None
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PayoutBatch:
    """一条待结算批次：某个预计打款日、某个币种下的净额。"""

    pay_date: date
    currency: str
    amount: Decimal


@dataclass(frozen=True)
class StoreSettlementInput:
    """一家店铺某次同步后的取数结果。"""

    store_id: str
    store_name: str
    mode: str
    currency: str
    status: str  # ok | fail
    error_summary: str = ""
    in_transit_amount: Decimal | None = None
    settled_cumulative_amount: Decimal | None = None
    payout_batches: tuple[PayoutBatch, ...] = ()
    # 收款方式：1 直接打款 / 2 钱包充值。钱包充值店的「已结算」只代表钱进
    # SHEIN 平台钱包、未进公司账户，现金可用性口径不同。
    payment_method: int | None = None
    # 该店最近一次同步时间（展示用；不参与任何汇总口径）
    synced_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def nearest_pay_date(self) -> date | None:
        dates = sorted(batch.pay_date for batch in self.payout_batches)
        return dates[0] if dates else None


@dataclass(frozen=True)
class MetricGroup:
    """某指标在某币种下的合计（含折人民币，缺汇率为 None）。"""

    currency: str
    total: Decimal
    cny: Decimal | None


@dataclass(frozen=True)
class MetricCard:
    key: str
    title: str
    hint: str
    groups: tuple[MetricGroup, ...]
    cny_total: Decimal | None
    nearest_pay_date: date | None = None

    def group(self, currency: str) -> MetricGroup | None:
        for item in self.groups:
            if item.currency == currency:
                return item
        return None


@dataclass(frozen=True)
class PayoutScheduleRow:
    """一个打款日下的分批金额（批内再按币种拆分）。"""

    pay_date: date
    groups: tuple[MetricGroup, ...]
    cny: Decimal | None


@dataclass(frozen=True)
class SettlementAlert:
    kind: str  # diff | sync_fail
    store_name: str
    message: str


@dataclass(frozen=True)
class SettlementSummary:
    cards: dict[str, MetricCard]
    schedule: tuple[PayoutScheduleRow, ...]
    stores: tuple[StoreSettlementInput, ...]
    alerts: tuple[SettlementAlert, ...] = ()
    currencies: tuple[str, ...] = ()
    store_total: int = 0
    store_ok: int = 0
    store_failed: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def nearest_payout(self) -> PayoutScheduleRow | None:
        return self.schedule[0] if self.schedule else None


def _sum_by_currency(
    entries: list[tuple[str, Decimal]],
    fx_rates: dict[str, Decimal],
) -> tuple[MetricGroup, ...]:
    """按币种汇总并折人民币。任一币种缺汇率则整组 cny 置 None。

    故意不做"缺汇率就跳过该币种"的处理：那会让人民币合计看起来正常、
    实则少算了一整个币种，属于最难发现的错。宁可整体显示"—"。
    """
    # 人民币不需要折算：汇率表里通常不会有 CNY 自身，若不兜底，一个人民币店
    # 就会让整张卡的人民币合计变成「—」（缺汇率），把其他币种一起拖下水。
    rates = dict(fx_rates)
    rates.setdefault("CNY", Decimal("1"))
    rates.setdefault("RMB", Decimal("1"))

    totals: dict[str, Decimal] = {}
    for currency, amount in entries:
        totals[currency] = totals.get(currency, Decimal("0")) + amount

    groups: list[MetricGroup] = []
    for currency in sorted(totals):
        total = to_cent(totals[currency])
        rate = rates.get(currency)
        groups.append(MetricGroup(
            currency=currency,
            total=total,
            cny=to_cent(total * rate) if rate is not None else None,
        ))
    return tuple(groups)


def _cny_total(groups: tuple[MetricGroup, ...]) -> Decimal | None:
    if not groups:
        return None
    total = Decimal("0")
    for group in groups:
        if group.cny is None:
            return None
        total += group.cny
    return to_cent(total)


def build_payout_schedule(
    stores: list[StoreSettlementInput],
    fx_rates: dict[str, Decimal],
) -> tuple[PayoutScheduleRow, ...]:
    """按预计打款日把待结算金额分批。

    各店打款日不一致（同店自身也会有多批），所以"把所有待结算金额加起来
    配一个最早打款日"是错的——那等于说钱都在那天到账。这里按日期切开，
    调用方再决定呈现多少（看板只呈现最近一批）。
    """
    buckets: dict[date, list[tuple[str, Decimal]]] = {}
    for store in stores:
        if not store.ok:
            continue
        for batch in store.payout_batches:
            buckets.setdefault(batch.pay_date, []).append(
                (batch.currency, batch.amount))

    rows: list[PayoutScheduleRow] = []
    for pay_date in sorted(buckets):
        groups = _sum_by_currency(buckets[pay_date], fx_rates)
        rows.append(PayoutScheduleRow(
            pay_date=pay_date, groups=groups, cny=_cny_total(groups)))
    return tuple(rows)


def build_settlement_summary(
    stores: list[StoreSettlementInput],
    fx_rates: dict[str, Decimal],
    *,
    diff_alerts: list[SettlementAlert] | None = None,
) -> SettlementSummary:
    """汇总成看板所需的四张卡片 + 排期 + 告警。"""
    ok_stores = [store for store in stores if store.ok]

    in_transit_entries: list[tuple[str, Decimal]] = []
    unsettled_entries: list[tuple[str, Decimal]] = []
    settled_entries: list[tuple[str, Decimal]] = []
    for store in ok_stores:
        if store.in_transit_amount is not None:
            in_transit_entries.append((store.currency, store.in_transit_amount))
        if store.settled_cumulative_amount is not None:
            settled_entries.append(
                (store.currency, store.settled_cumulative_amount))
        for batch in store.payout_batches:
            unsettled_entries.append((batch.currency, batch.amount))

    schedule = build_payout_schedule(stores, fx_rates)
    nearest = schedule[0] if schedule else None

    groups_by_key = {
        IN_TRANSIT: _sum_by_currency(in_transit_entries, fx_rates),
        UNSETTLED: _sum_by_currency(unsettled_entries, fx_rates),
        NEAREST_PAYOUT: nearest.groups if nearest else (),
        SETTLED_CUMULATIVE: _sum_by_currency(settled_entries, fx_rates),
    }
    cards: dict[str, MetricCard] = {}
    for key in METRIC_KEYS:
        groups = groups_by_key[key]
        cards[key] = MetricCard(
            key=key,
            title=CARD_TITLES[key],
            hint=CARD_HINTS[key],
            groups=groups,
            cny_total=_cny_total(groups),
            # 打款日只挂在「下次结算」上：四张卡都带会让每张卡头都渲染
            # 「预计 MM-DD」，与本题无关（在途/已结算都没有"打款日"概念）。
            nearest_pay_date=(
                nearest.pay_date if nearest and key == NEAREST_PAYOUT else None
            ),
        )

    alerts = list(diff_alerts or [])
    for store in stores:
        if not store.ok:
            alerts.append(SettlementAlert(
                kind="sync_fail",
                store_name=store.store_name,
                message=store.error_summary or "同步失败",
            ))

    currencies: list[str] = []
    for store in ok_stores:
        if store.currency not in currencies:
            currencies.append(store.currency)

    return SettlementSummary(
        cards=cards,
        schedule=schedule,
        stores=tuple(stores),
        alerts=tuple(alerts),
        currencies=tuple(sorted(currencies)),
        store_total=len(stores),
        store_ok=len(ok_stores),
        store_failed=len(stores) - len(ok_stores),
    )


def nearest_payout_amount(store: StoreSettlementInput) -> Decimal | None:
    """店铺级「下次结算」：最近一个打款日那一批的该店金额。

    对账单接口自身的实现（agent 侧同步）可复用此口径；缺少批次时回退 None，
    不要回退成全部未结算——那会让该店在"下次结算"列虚高。
    """
    nearest = store.nearest_pay_date
    if nearest is None:
        return None
    total = Decimal("0")
    hit = False
    for batch in store.payout_batches:
        if batch.pay_date == nearest:
            total += batch.amount
            hit = True
    return to_cent(total) if hit else None
