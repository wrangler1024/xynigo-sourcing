# -*- coding: utf-8 -*-
"""结算看板聚合口径回归（纯合成数据，不连库、不打网络）。

这里钉住的是需求文档 §3.2 的口径规则——它们是看板正确性的全部依据，
改任何一条都必须先改文档再改这里的断言。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from xynigo_auth.shein_settlement_service import (
    IN_TRANSIT,
    NEAREST_PAYOUT,
    SETTLED_CUMULATIVE,
    UNSETTLED,
    PayoutBatch,
    SettlementAlert,
    StoreSettlementInput,
    build_payout_schedule,
    build_settlement_summary,
    nearest_payout_amount,
    to_cent,
)

MXN = Decimal("0.3915")
USD = Decimal("7.1080")
FX = {"MXN": MXN, "USD": USD}

D21 = date(2026, 9, 21)
D28 = date(2026, 9, 28)


def _store(
    name: str, currency: str, *, in_transit: str | None = None,
    settled: str | None = None, batches: tuple[tuple[date, str], ...] = (),
    status: str = "ok", error: str = "", payment_method: int | None = 1,
) -> StoreSettlementInput:
    return StoreSettlementInput(
        store_id=name, store_name=name, mode="self", currency=currency,
        status=status, error_summary=error,
        in_transit_amount=None if in_transit is None else Decimal(in_transit),
        settled_cumulative_amount=None if settled is None else Decimal(settled),
        payout_batches=tuple(
            PayoutBatch(pay_date=d, currency=currency, amount=Decimal(a))
            for d, a in batches
        ),
        payment_method=payment_method,
    )


# ---- 金额精度 ----

def test_amounts_quantize_to_cent():
    """金额一律落到分——浮点长尾会直接写进 Excel 与接口。"""
    assert to_cent(Decimal("29547.730000000003")) == Decimal("29547.73")
    assert to_cent(Decimal("0.005")) == Decimal("0.01")
    assert to_cent(None) is None


def test_sum_of_many_batches_has_no_float_tail():
    stores = [
        _store(f"s{i}", "MXN", batches=((D21, "0.07"),))
        for i in range(7)
    ]
    summary = build_settlement_summary(stores, FX)
    total = summary.cards[UNSETTLED].cny_total
    assert total == Decimal("0.19")
    assert str(total) == "0.19"


# ---- 人民币折算 ----

def test_cny_total_uses_per_currency_rates():
    stores = [
        _store("a", "MXN", settled="100.00"),
        _store("b", "USD", settled="10.00"),
    ]
    summary = build_settlement_summary(stores, FX)
    card = summary.cards[SETTLED_CUMULATIVE]
    assert card.group("MXN").cny == Decimal("39.15")
    assert card.group("USD").cny == Decimal("71.08")
    assert card.cny_total == Decimal("110.23")


def test_missing_rate_yields_none_not_parity():
    """缺汇率的币种不能按 1:1 计入——比索当人民币算会让总数错得离谱。"""
    stores = [
        _store("a", "MXN", settled="100.00"),
        _store("b", "BRL", settled="50.00"),
    ]
    summary = build_settlement_summary(stores, FX)  # 无 BRL 汇率
    card = summary.cards[SETTLED_CUMULATIVE]
    assert card.group("BRL").cny is None
    assert card.cny_total is None
    # 原币合计仍要正确给出，不能因为缺汇率整张卡变空
    assert card.group("BRL").total == Decimal("50.00")
    assert card.group("MXN").total == Decimal("100.00")


# ---- 打款日分批（本次的核心口径）----

def test_unsettled_is_all_but_nearest_is_only_first_batch():
    """待结算取全部、下次结算只取最近一批——两者都取全量就会是同一个数。"""
    stores = [
        _store("a", "MXN", batches=((D21, "600.00"), (D28, "400.00"))),
    ]
    summary = build_settlement_summary(stores, FX)

    unsettled = summary.cards[UNSETTLED]
    nearest = summary.cards[NEAREST_PAYOUT]
    assert unsettled.group("MXN").total == Decimal("1000.00")
    assert nearest.group("MXN").total == Decimal("600.00")
    assert nearest.nearest_pay_date == D21
    assert unsettled.group("MXN").total != nearest.group("MXN").total


def test_nearest_batch_never_carries_an_earliest_date_for_full_amount():
    """打款日不对齐时，不能把全部待结算金额配一个最早打款日。"""
    stores = [
        _store("a", "MXN", batches=((D21, "600.00"),)),
        _store("b", "MXN", batches=((D28, "400.00"),)),
    ]
    summary = build_settlement_summary(stores, FX)
    schedule = [(row.pay_date, row.groups[0].total) for row in summary.schedule]

    assert schedule == [(D21, Decimal("600.00")), (D28, Decimal("400.00"))]
    assert summary.cards[NEAREST_PAYOUT].nearest_pay_date == D21
    assert summary.cards[NEAREST_PAYOUT].group("MXN").total == Decimal("600.00")


def test_schedule_batches_sum_to_unsettled_total():
    """恒等式：各批次之和 ≡ 待结算总额（看板可复算的前提）。"""
    stores = [
        _store("a", "MXN", batches=((D21, "600.00"), (D28, "400.00"))),
        _store("b", "MXN", batches=((D21, "250.50"),)),
        _store("c", "USD", batches=((D28, "80.25"),)),
    ]
    summary = build_settlement_summary(stores, FX)

    schedule_total = sum(row.cny for row in summary.schedule)
    assert schedule_total == summary.cards[UNSETTLED].cny_total


def test_schedule_is_sorted_by_pay_date():
    stores = [
        _store("a", "MXN", batches=((D28, "1.00"),)),
        _store("b", "MXN", batches=((D21, "2.00"),)),
    ]
    schedule = build_payout_schedule(stores, FX)
    assert [row.pay_date for row in schedule] == [D21, D28]


def test_leapfrog_payout_across_dates_per_currency():
    """同一打款日下不同币种分别汇总，互不混淆。"""
    stores = [
        _store("a", "MXN", batches=((D21, "100.00"),)),
        _store("b", "USD", batches=((D21, "10.00"),)),
    ]
    summary = build_settlement_summary(stores, FX)
    row = summary.schedule[0]
    by_currency = {group.currency: group.total for group in row.groups}
    assert by_currency == {"MXN": Decimal("100.00"), "USD": Decimal("10.00")}
    assert row.cny == Decimal("110.23")


# ---- 单店「下次结算」口径 ----

def test_store_nearest_payout_amount_uses_only_nearest_date():
    store = _store("a", "MXN", batches=((D21, "600.00"), (D28, "400.00")))
    assert nearest_payout_amount(store) == Decimal("600.00")


def test_store_nearest_payout_amount_is_none_without_batches():
    """没有批次时返回 None，而不是回退成全部未结算（那会让该店虚高）。"""
    assert nearest_payout_amount(_store("a", "MXN")) is None


def test_store_nearest_sums_multiple_currencies_on_same_date():
    store = StoreSettlementInput(
        store_id="a", store_name="a", mode="self", currency="MXN",
        status="ok",
        payout_batches=(
            PayoutBatch(D21, "MXN", Decimal("100.00")),
            PayoutBatch(D21, "USD", Decimal("5.00")),
            PayoutBatch(D28, "MXN", Decimal("9.00")),
        ),
    )
    assert nearest_payout_amount(store) == Decimal("105.00")


# ---- 失败店铺与告警 ----

def test_failed_store_excluded_from_totals_but_visible():
    stores = [
        _store("ok店", "MXN", settled="100.00"),
        _store("失败店", "MXN", status="fail", error="店铺凭证失效"),
    ]
    summary = build_settlement_summary(stores, FX)

    assert summary.cards[SETTLED_CUMULATIVE].group("MXN").total == Decimal("100.00")
    assert summary.store_total == 2
    assert summary.store_ok == 1
    assert summary.store_failed == 1
    fails = [a for a in summary.alerts if a.kind == "sync_fail"]
    assert len(fails) == 1 and fails[0].store_name == "失败店"


def test_failed_store_never_contributes_batches():
    """失败店铺可能带着上一次的过期批次；口径是整店不计入。"""
    stores = [
        _store("失败店", "MXN", status="fail", error="窗口越界",
               batches=((D21, "999.00"),)),
    ]
    summary = build_settlement_summary(stores, FX)
    assert summary.schedule == ()
    assert summary.cards[UNSETTLED].groups == ()


def test_diff_alerts_are_passed_through():
    stores = [_store("a", "MXN", settled="1.00")]
    diff = [SettlementAlert(kind="diff", store_name="a", message="差额 0.02")]
    summary = build_settlement_summary(stores, FX, diff_alerts=diff)
    assert [a.kind for a in summary.alerts] == ["diff"]


# ---- 卡片与币种 ----

def test_four_cards_present_with_titles_and_hints():
    summary = build_settlement_summary([_store("a", "MXN")], FX)
    assert set(summary.cards) == {
        IN_TRANSIT, UNSETTLED, NEAREST_PAYOUT, SETTLED_CUMULATIVE}
    assert summary.cards[IN_TRANSIT].title == "在途资金"
    assert summary.cards[NEAREST_PAYOUT].hint == "仅最近一批"


def test_currencies_only_include_successful_stores():
    stores = [
        _store("a", "MXN"),
        _store("b", "USD", status="fail", error="x"),
    ]
    summary = build_settlement_summary(stores, FX)
    assert summary.currencies == ("MXN",)


def test_empty_input_yields_none_cny_not_zero():
    """无店铺时人民币合计是 None（显示"—"），不是 0——0 会被读成"真的没有钱"。"""
    summary = build_settlement_summary([], FX)
    for card in summary.cards.values():
        assert card.cny_total is None
        assert card.groups == ()
    assert summary.nearest_payout is None
