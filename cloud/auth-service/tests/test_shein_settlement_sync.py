# -*- coding: utf-8 -*-
"""结算同步编排：取数落库、幂等、按店隔离失败（全部合成数据）。

这些用例覆盖的是「装配」层最容易出错的地方——不是算法（在
test_shein_settlement_service 里测），而是：
- 单店失败会不会拖垮整轮 / 会不会把已成功的店一起回滚
- 重复跑会不会产生重复数据
- 订单详情是不是只对在途单拉（否则 60 天回溯会打出几百次无用调用）
- 收支轧差、分批、分页累计的组装对不对
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from xynigo_auth.database import Database
from xynigo_auth.models import (
    Base,
    SheinAuthorizedStore,
    SheinFxRate,
    SheinPayoutBatch,
    SheinSettlementOrder,
    SheinSettlementSnapshot,
    SheinSettlementSyncRun,
    Tenant,
    User,
)
from xynigo_auth.shein_openapi_client import SheinOpenApiClientError
from xynigo_auth.shein_settlement_service import UNSETTLED
from xynigo_auth.shein_settlement_sync import (
    FxRateQuote,
    SheinSettlementSyncService,
    _as_platform_tz,
    parse_frankfurter_quote,
    upsert_fx_rate,
)
from xynigo_auth.shein_settlement_windows import PLATFORM_TZ
from xynigo_auth.shein_store_auth_crypto import SheinStoreSecretCipher

FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
SECRET_PLAIN = "C" * 32
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=PLATFORM_TZ)


class FakeClient:
    """脚本化的假网关：记录调用，按配置返回；可指定某店抛错。"""

    def __init__(self, *, fail_open_key_ids: set[str] | None = None):
        self.fail_open_key_ids = fail_open_key_ids or set()
        self.order_calls: list[dict] = []
        self.detail_calls: list[list[str]] = []
        self.check_calls: list[dict] = []
        self.report_calls: list[dict] = []
        self.site_calls: int = 0

    # -- 数据源（测试可覆写这些属性）--
    orders: list[dict] = []
    details: dict[str, dict] = {}
    check_orders: list[dict] = []
    report_orders: list[dict] = []
    site_currency: str = "MXN"

    def _guard(self, open_key_id: str) -> None:
        if open_key_id in self.fail_open_key_ids:
            raise SheinOpenApiClientError("openapi00001", "签名错误:生成的签名不正确")

    def query_orders(self, *, open_key_id, secret_key, query_type, start_time,
                     end_time, page=1, page_size=30, order_status=None):
        """按 orderUpdateTime 过滤，模拟平台的 48h 窗（queryType=2）。

        过滤不能省：增量窗口只有 2 小时重叠，真实平台在没人下单/改单时就是返回空，
        而"返回空"正是最容易把在途写空的路径。假网关若不按窗过滤，这条永远测不到。
        """
        self._guard(open_key_id)
        self.order_calls.append({"page": page, "start": start_time})
        matched = [item for item in self.orders
                   if start_time <= str(item.get("orderUpdateTime") or "") < end_time]
        start_index = (page - 1) * page_size
        return {"count": len(matched),
                "orderList": matched[start_index:start_index + page_size]}

    def query_order_details(self, *, open_key_id, secret_key, order_nos):
        self._guard(open_key_id)
        self.detail_calls.append(list(order_nos))
        # 真机（20260919）：order-detail 的 info 直接是明细数组，无包裹键。
        return [
            {"orderNo": no, **self.details.get(no, {})} for no in order_nos
            if no in self.details]

    def query_check_orders(self, *, open_key_id, secret_key, start_add_time,
                           end_add_time, page=1, page_size=30,
                           check_status=None, bz_order_nos=None,
                           report_order_nos=None):
        """按 startAddTime/endAddTime（对账单**生成时间**）过滤，模拟平台行为。

        过滤不能省：生产上分片是按生成时间切的，每张对账单只应落在一个窗口里；
        假客户端若忽略窗口，同一批数据会被每个窗口各算一次（实测就是这样把
        945.16 算成了 2835.48）。
        """
        self._guard(open_key_id)
        self.check_calls.append({"page": page, "status": check_status,
                                 "start": start_add_time, "end": end_add_time})
        matched = [
            item for item in self.check_orders
            if start_add_time <= str(item.get("addTime") or "") < end_add_time
        ]
        # 真分页：一次只回一页。若像早先那样第 1 页就回全部，翻页上限这条路径
        # 永远走不到（截断保护也就测不出来）。
        start_index = (page - 1) * page_size
        return {"count": len(matched),
                "list": matched[start_index:start_index + page_size]}

    def query_report_orders(self, *, open_key_id, secret_key, page=1,
                            page_size=30, report_status=None, **kwargs):
        self._guard(open_key_id)
        self.report_calls.append({"page": page, "status": report_status})
        # 真分页（与订单/对账单一致）：第 1 页回全部会让 200 页上限这条路径走不到
        start_index = (page - 1) * page_size
        return {"count": len(self.report_orders),
                "list": self.report_orders[start_index:start_index + page_size]}

    def query_site_list(self, *, open_key_id, secret_key):
        self._guard(open_key_id)
        self.site_calls += 1
        return {"data": [{"sub_site_list": [{"currency": self.site_currency}]}]}


@pytest.fixture()
def env(tmp_path):
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'sync.sqlite3'}")
    Base.metadata.create_all(database.engine)
    cipher = SheinStoreSecretCipher(FERNET_KEY)
    with database.session_factory() as session:
        tenant = Tenant(feishu_tenant_key="t1", name="合成组织")
        session.add(tenant)
        session.flush()
        user = User(tenant_id=tenant.id, feishu_open_id="ou_a",
                    display_name="合成管理员", status="active")
        session.add(user)
        session.flush()
        session.commit()
        yield {"db": database, "cipher": cipher, "tenant_id": tenant.id,
               "user_id": user.id}


def add_store(env, name: str, *, open_key_id: str | None = None,
              status: str = "ok") -> uuid.UUID:
    with env["db"].session_factory() as session:
        store = SheinAuthorizedStore(
            tenant_id=env["tenant_id"], merchant_id="1", store_name=name,
            open_key_id=open_key_id or uuid.uuid4().hex[:32],
            secret_ciphertext=env["cipher"].encrypt_secret(SECRET_PLAIN),
            app_id="APP", mode="self",
            first_authorized_at=NOW, latest_authorized_at=NOW,
            status=status, store_info={},
        )
        session.add(store)
        session.commit()
        return store.id


def build_service(env, client) -> SheinSettlementSyncService:
    return SheinSettlementSyncService(
        client=client, cipher=env["cipher"], order_lookback_days=4,
        # 待结算要回溯到"连续 4 个空窗"才算见底，故测试用的回溯上限要够长，
        # 否则会以"未见底"判为不完整而整店失败（生产默认 180 天）
        check_order_lookback_days=60,
    )


# ---- 基本取数落库 ----

def test_sync_writes_ledger_batches_and_snapshot(env):
    client = FakeClient()
    client.orders = [
        {"orderNo": "O1", "orderStatus": 4,
         "orderCreateTime": "2026-09-10 10:00:00",
         "orderUpdateTime": "2026-09-16 10:00:00"},
        {"orderNo": "O2", "orderStatus": 5,
         "orderCreateTime": "2026-09-11 10:00:00",
         "orderUpdateTime": "2026-09-16 11:00:00"},
    ]
    client.details = {"O1": {"estimatedGrossIncome": 100.5, "orderCurrency": "MXN"}}
    client.check_orders = [
        {"checkOrderNo": "B-s1", "addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 600.00,
         "incomeExpenditureType": 1},
        {"checkOrderNo": "B-s2", "addTime": "2026-09-16 10:00:00", "estimatePayTime": "2026-09-28 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 400.00,
         "incomeExpenditureType": 1},
    ]
    client.report_orders = [
        {"income": 1124.76, "currencyCode": "MXN", "reportStatus": 2,
         "paymentMethod": 2, "rxAcct": "acct-1"},
    ]
    add_store(env, "甲店")

    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()

        assert outcome.status == "succeeded"
        assert outcome.store_ok == 1
        store_outcome = outcome.stores[0]
        assert store_outcome.status == "ok"
        assert store_outcome.in_transit_amount == Decimal("100.50")
        assert store_outcome.settled_cumulative_amount == Decimal("1124.76")
        assert store_outcome.currency == "MXN"
        assert store_outcome.payment_method == 2

        ledger = session.execute(select(SheinSettlementOrder)).scalars().all()
        assert {row.order_no: row.order_status for row in ledger} == {"O1": 4, "O2": 5}
        batches = session.execute(
            select(SheinPayoutBatch).order_by(SheinPayoutBatch.pay_date)
        ).scalars().all()
        assert [Decimal(b.amount) for b in batches] == [
            Decimal("600.00"), Decimal("400.00")]
        snap = session.execute(select(SheinSettlementSnapshot)).scalars().one()
        assert snap.status == "ok"
        assert Decimal(snap.unsettled_amount) == Decimal("1000.00")
        assert Decimal(snap.nearest_pay_amount) == Decimal("600.00")
        assert snap.receiver_account == "acct-1"


def test_order_details_only_fetched_for_in_transit_orders(env):
    """台账记录全部订单状态，但金额只对在途单调——否则 60 天回溯会打几百次。"""
    client = FakeClient()
    client.orders = [
        {"orderNo": f"O{i}", "orderStatus": 5 if i % 2 else 4,
         "orderCreateTime": "2026-09-10 10:00:00",
         "orderUpdateTime": "2026-09-16 10:00:00"}
        for i in range(6)
    ]
    client.details = {f"O{i}": {"estimatedGrossIncome": 10, "orderCurrency": "MXN"}
                      for i in range(6)}
    add_store(env, "甲店")

    with env["db"].session_factory() as session:
        build_service(env, client).run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()

    fetched = [no for batch in client.detail_calls for no in batch]
    assert sorted(fetched) == ["O0", "O2", "O4"]


def test_payout_batches_net_income_against_expense(env):
    """对账单按收支轧差；支出行取负（同店同日同币种合并）。"""
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B-s1", "addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 1059.32,
         "incomeExpenditureType": 1},
        {"checkOrderNo": "B-exp", "addTime": "2026-09-15 11:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 114.16,
         "incomeExpenditureType": 2},
    ]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].batches[0].amount == Decimal("945.16")


def test_sync_is_idempotent(env):
    """重复跑不产生重复数据（看板会定时跑，重复是常态不是异常）。"""
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 50, "orderCurrency": "MXN"}}
    client.check_orders = [
        {"checkOrderNo": "B-s1", "addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 100,
         "incomeExpenditureType": 1}]
    add_store(env, "甲店")
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        service.run(session, tenant_id=env["tenant_id"],
                    now=NOW + timedelta(hours=6))
        session.commit()

        assert len(session.execute(
            select(SheinSettlementOrder)).scalars().all()) == 1
        assert len(session.execute(
            select(SheinPayoutBatch)).scalars().all()) == 1


def test_order_detail_supports_legacy_wrapped_payload(env):
    """兼容旧假设的包裹形态（{"orderList":[…]}）与旧币种字段 currencyCode。"""
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 50, "currencyCode": "MXN"}}

    def wrapped_details(*, open_key_id, secret_key, order_nos):
        client.detail_calls.append(list(order_nos))
        return {"orderList": [
            {"orderNo": no, **client.details.get(no, {})} for no in order_nos
            if no in client.details]}

    client.query_order_details = wrapped_details
    client.check_orders = []
    add_store(env, "甲店")

    with env["db"].session_factory() as session:
        build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        row = session.execute(
            select(SheinSettlementOrder)).scalars().one()
        assert row.estimated_gross_income == Decimal("50")
        assert row.currency == "MXN"


def test_one_store_failure_does_not_affect_others(env):
    """一家店挂了不能拖垮整轮——这是看板可信度的前提。"""
    ok_store = add_store(env, "正常店", open_key_id="A" * 32)
    bad_store = add_store(env, "故障店", open_key_id="B" * 32)
    client = FakeClient(fail_open_key_ids={"B" * 32})
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 77, "orderCurrency": "MXN"}}

    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()

        assert outcome.status == "partial"
        assert outcome.store_ok == 1 and outcome.store_failed == 1
        by_name = {item.store_name: item for item in outcome.stores}
        assert by_name["正常店"].status == "ok"
        assert by_name["故障店"].status == "fail"
        assert "openapi00001" in by_name["故障店"].error_summary \
            or "签名" in by_name["故障店"].error_summary

        # 运行记录必须存活（曾因整会话 rollback 被一起撤掉）
        runs = session.execute(select(SheinSettlementSyncRun)).scalars().all()
        assert len(runs) == 1 and runs[0].status == "partial"

        # 失败店也留快照，前端才能显示「同步失败」并带原因
        snaps = session.execute(select(SheinSettlementSnapshot)).scalars().all()
        assert {s.store_id: s.status for s in snaps}[bad_store] == "fail"
        assert {s.store_id: s.status for s in snaps}[ok_store] == "ok"


def test_summary_excludes_failed_store_and_shows_alert(env):
    add_store(env, "正常店", open_key_id="A" * 32)
    add_store(env, "故障店", open_key_id="B" * 32)
    client = FakeClient(fail_open_key_ids={"B" * 32})
    client.report_orders = [{"income": 100, "currencyCode": "MXN"}]
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        summary = service.build_summary(session, tenant_id=env["tenant_id"])

        assert summary.store_total == 2 and summary.store_ok == 1
        assert summary.store_failed == 1
        fails = [a for a in summary.alerts if a.kind == "sync_fail"]
        assert [a.store_name for a in fails] == ["故障店"]


def test_missing_fx_rate_keeps_cny_none(env):
    """没有汇率时人民币合计是 None（前端显示「—」），不按 1:1 计入。"""
    client = FakeClient()
    client.report_orders = [{"income": 100, "currencyCode": "MXN"}]
    add_store(env, "甲店")
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        summary = service.build_summary(session, tenant_id=env["tenant_id"])
        card = summary.cards["settled_cumulative"]
        assert card.group("MXN").total == Decimal("100.00")
        assert card.group("MXN").cny is None
        assert card.cny_total is None


def test_fx_rate_applies_and_is_snapshotted(env):
    client = FakeClient()
    client.report_orders = [{"income": 100, "currencyCode": "MXN"}]
    add_store(env, "甲店")
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        upsert_fx_rate(session, tenant_id=env["tenant_id"], currency="MXN",
                       rate=Decimal("0.3915"),
                       effective_date=NOW.date(), source="boc")
        session.commit()
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        summary = service.build_summary(session, tenant_id=env["tenant_id"])
        assert summary.cards["settled_cumulative"].cny_total == Decimal("39.15")
        snap = session.execute(select(SheinSettlementSnapshot)).scalars().one()
        assert snap.fx_rate_snapshot.get("MXN") == "0.39150000"


def test_expired_store_is_skipped(env):
    add_store(env, "已失效店", status="expired")
    with env["db"].session_factory() as session:
        outcome = build_service(env, FakeClient()).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.store_total == 0


def test_store_without_any_sync_reports_not_synced(env):
    """没同步过的店铺在看板上是「尚未同步」，不是"0 元"。"""
    add_store(env, "新店")
    with env["db"].session_factory() as session:
        summary = build_service(env, FakeClient()).build_summary(
            session, tenant_id=env["tenant_id"])
        assert summary.store_failed == 1
        assert summary.stores[0].error_summary == "尚未同步"
        assert summary.cards["settled_cumulative"].groups == ()


# ---- 时区往返（只在 SQLite 环境暴露的坑）----

def test_naive_db_timestamp_is_read_as_platform_tz():
    """从库读回的 naive 时间要按平台时区解释，按 UTC 解释会推后 8 小时。

    后果不是报错而是静默：`start(上次同步) >= end(现在)` → 增量窗口为空 →
    整轮同步"没有数据可拉"，在途金额被写成空。生产用 Postgres（返回带时区）
    不会触发，所以只在 SQLite 测试里能发现。
    """
    naive = datetime(2026, 9, 17, 20, 0, 0)          # 库里存的上海墙上时间
    aware = _as_platform_tz(naive)
    assert aware.tzinfo is not None
    assert aware.utcoffset().total_seconds() == 8 * 3600
    # 已经是带时区的值不做改动（Postgres 路径）
    passthrough = datetime(2026, 9, 17, 12, 0, tzinfo=PLATFORM_TZ)
    assert _as_platform_tz(passthrough) is passthrough


def test_second_sync_keeps_in_transit_amount(env):
    """回归位：第二次同步走增量路径，在途金额必须保持（曾因窗口为空被写空）。"""
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 88.8, "orderCurrency": "MXN"}}
    add_store(env, "甲店")
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        service.run(session, tenant_id=env["tenant_id"],
                    now=NOW + timedelta(minutes=30))
        session.commit()
        summary = service.build_summary(session, tenant_id=env["tenant_id"])
        store = summary.stores[0]
        assert store.status == "ok", store.error_summary
        assert store.in_transit_amount == Decimal("88.80")


# ---- 评审必须改项的回归位 ----

def test_incremental_empty_window_keeps_in_transit(env):
    """增量窗口**返回空**时在途必须保持（评审必须改 #1）。

    真实平台在没人下单/改单时，2 小时重叠窗就是返回空——这是常态而不是异常。
    早期实现遇到空结果直接 `return 0, None`，于是每 6 小时的定时同步都会把
    看板上的在途整段清空，而台账里 order_status=4 的行一直还在。
    """
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 88.8, "orderCurrency": "MXN"}}
    add_store(env, "甲店")
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert service.build_summary(
            session, tenant_id=env["tenant_id"]
        ).stores[0].in_transit_amount == Decimal("88.80")

        # 第二次：增量窗 [NOW-2h, NOW] 不含 09-16 的更新 → 平台返回空
        outcome = service.run(session, tenant_id=env["tenant_id"],
                              now=NOW + timedelta(hours=6))
        session.commit()
        assert outcome.stores[0].status == "ok"
        summary = service.build_summary(session, tenant_id=env["tenant_id"])
        assert summary.stores[0].in_transit_amount == Decimal("88.80"), \
            "增量窗口为空时在途被写成空——看板会整段消失"


def test_failed_after_success_does_not_show_stale_batches(env):
    """先成功再失败：明细/导出不得显示上一轮的过期待结算（评审必须改 #4）。

    单店失败走 SAVEPOINT，上一轮写入的批次会留在库里。若照挂不误，卡片剔除了
    该店、表格与 xlsx 却仍有金额——两边对不上，还像是"这家店还在正常出数"。
    """
    store_id = add_store(env, "甲店", open_key_id="A" * 32)
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B-s1", "addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 500.00,
         "incomeExpenditureType": 1}]
    client.report_orders = [{"income": 100, "currencyCode": "MXN"}]
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        store = service.build_summary(
            session, tenant_id=env["tenant_id"]).stores[0]
        assert sum(b.amount for b in store.payout_batches) == Decimal("500.00")

    # 第二次让接口整体失败
    client.fail_open_key_ids = {"A" * 32}
    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"],
                    now=NOW + timedelta(hours=6))
        session.commit()
        store = service.build_summary(
            session, tenant_id=env["tenant_id"]).stores[0]
        assert store.status == "fail"
        assert store.payout_batches == (), "失败店仍挂着上一轮的过期批次"
        assert store.in_transit_amount is None
        assert store.settled_cumulative_amount is None
        # 卡片同样不含
        assert store.currency not in {
            g.currency for c in service.build_summary(
                session, tenant_id=env["tenant_id"]).cards.values()
            for g in c.groups}


def test_check_order_duplicate_rows_are_deduped(env):
    """同一条对账单被相邻窗口各返回一次时，不能重复计入（评审必须改 #3）。

    分片是左闭右开的，但平台对 startAddTime/endAddTime 的开闭语义未在真机确认；
    按对账单号去重后，两种语义都不会让金额翻倍。
    """
    client = FakeClient()
    duplicate = {"checkOrderNo": "B1",
                 "addTime": "2026-09-15 10:00:00",
                 "estimatePayTime": "2026-09-21 10:00:00",
                 "currencyCode": "MXN", "estimateIncomeMoneyTotal": 500.00,
                 "incomeExpenditureType": 1}
    client.check_orders = [duplicate, dict(duplicate)]   # 同一单号两次
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "ok"
        assert outcome.stores[0].batches[0].amount == Decimal("500.00")


def test_older_pending_bill_beyond_naive_window_is_still_picked_up(env):
    """生成时间较早但仍是 checkStatus=1 的账单必须被算进来（评审必须改 #2）。

    旧实现只拉最近 21 天，更早的真实待结算永远不会进桶；而且全删重建会把
    上一轮已经算对的金额抹掉。
    """
    client = FakeClient()
    client.check_orders = [
        # 生成于 40 天前（远超旧的 21 天窗），至今仍未结算
        {"checkOrderNo": "B-old", "addTime": "2026-08-08 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 777.00, "incomeExpenditureType": 1}]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "ok"
        assert sum(b.amount for b in outcome.stores[0].batches) == Decimal("777.00")


def test_check_order_missing_field_fails_store_instead_of_silently_dropping(env):
    """关键字段缺失时整店失败，不静默丢一条真实的待结算金额。"""
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B-bad", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 300.00, "incomeExpenditureType": 1}]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "关键字段" in outcome.stores[0].error_summary


def test_incomplete_backwalk_fails_instead_of_truncating(env):
    """回溯到上限仍未见底 → 显式失败，不静默少算。

    构造：把回溯上限设得极短（7 天），而最后一窗仍有待结算 → 判为不完整。
    """
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B1", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 100.00, "incomeExpenditureType": 1}]
    add_store(env, "甲店")
    service = SheinSettlementSyncService(
        client=client, cipher=env["cipher"], order_lookback_days=4,
        check_order_lookback_days=7,   # 只有一个窗口，无法形成连续空窗
    )
    with env["db"].session_factory() as session:
        outcome = service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "未见底" in outcome.stores[0].error_summary


# ---- 复评新增必须改项的回归位 ----

def test_check_order_without_order_no_fails_store(env):
    """缺对账单号必须整店失败（复评必须改 #1）。

    单号是去重键：没有它就无法判断边界秒被相邻两片各返回一次的情况，金额会翻倍。
    原实现 `if order_no:` 把缺号行直接放过——既不去重、又照常入桶。
    """
    client = FakeClient()
    client.check_orders = [
        {"addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 100.00, "incomeExpenditureType": 1}]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "缺少关键字段" in outcome.stores[0].error_summary


def test_same_order_income_and_expense_rows_are_both_kept(env):
    """同一单号的收入行与支出行都要保留（复评提醒点）。

    去重键是 (对账单号, 收支类型)：真机是否会出现同号两行尚未确认，
    但若只按单号去重，成对的支出行会被吃掉 → 轧差算错。
    """
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B-pair", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 100.00, "incomeExpenditureType": 1},
        {"checkOrderNo": "B-pair", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 30.00, "incomeExpenditureType": 2}]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "ok"
        assert outcome.stores[0].batches[0].amount == Decimal("70.00")


def test_check_order_page_cap_fails_instead_of_truncating(env):
    """对账单翻页超限必须整店失败（复评必须改 #2）。

    原先是静默 break：截断结果仍会被当成"见底"，随后全删重建 → 「看着完整、
    实际少算」。与订单列表/报账单的口径对齐。
    """
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": f"B{i}", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 1.00, "incomeExpenditureType": 1}
        for i in range(4000)          # 超过 100 页 × 30
    ]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "翻页超过" in outcome.stores[0].error_summary


def test_dirty_in_transit_row_fails_instead_of_under_counting(env):
    """台账里已存在「在途但金额为空」的脏行时必须失败（复评建议点）。

    增量空窗不会重拉这些详情的金额，而 SQL SUM 会跳过 NULL → 静默算少。
    """
    store_id = add_store(env, "甲店")
    with env["db"].session_factory() as session:
        session.add(SheinSettlementOrder(
            tenant_id=env["tenant_id"], store_id=store_id, order_no="DIRTY",
            order_status=4, currency="MXN",
            estimated_gross_income=None, last_synced_at=NOW))
        session.commit()
    client = FakeClient()
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "缺少预计收入" in outcome.stores[0].error_summary


# ---- 三轮评审必须改的回归位 ----

def test_failed_round_does_not_advance_order_watermark(env):
    """失败快照不得作为订单水位（三轮必须改）。

    时序（时间必须这样取，否则测不到）：
      T1 = NOW        成功；台账 O1=已发货，成功快照 synced_at=T1
      T2 = NOW+5h     失败；O1 已在 NOW+2h 签收。本轮**在第一次 query_orders
                      就抛异常**（`fail_open_key_ids`），所以台账根本没被写过——
                      设这个用例模拟的是"失败轮没能把该窗的变更落库"这件事本身，
                      不是"写了又被回滚"（评审指出描述过头，已改正）。失败快照
                      仍会写 synced_at=T2。
      T3 = NOW+11h    恢复

    水位若取到失败快照的 T2，增量窗只剩 [T2−2h, T3] = [NOW+3h, …]，
    **NOW+2h 的签收再也不会被读到** → 在途长期虚高。

    注意：失败轮距上次成功必须**大于 2 小时重叠**，否则更新落在重叠窗内，
    缺陷被掩盖、用例会假绿（第一版就是这么写的，实测修不修都是绿的）。
    """
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 88.8, "orderCurrency": "MXN"}}
    add_store(env, "甲店", open_key_id="A" * 32)
    service = build_service(env, client)

    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert service.build_summary(
            session, tenant_id=env["tenant_id"]
        ).stores[0].in_transit_amount == Decimal("88.80")

    # T2：O1 在失败当轮窗口内（NOW+2h）被签收
    client.orders = [{"orderNo": "O1", "orderStatus": 5,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-17 14:00:00"}]
    client.fail_open_key_ids = {"A" * 32}
    with env["db"].session_factory() as session:
        outcome = service.run(session, tenant_id=env["tenant_id"],
                              now=NOW + timedelta(hours=5))
        session.commit()
        assert outcome.stores[0].status == "fail"

    # T3：恢复。水位只认成功快照 → 窗口仍覆盖 NOW+2h → 读到签收
    client.fail_open_key_ids = set()
    with env["db"].session_factory() as session:
        outcome = service.run(session, tenant_id=env["tenant_id"],
                              now=NOW + timedelta(hours=11))
        session.commit()
        assert outcome.stores[0].status == "ok"
        assert outcome.stores[0].in_transit_amount == Decimal("0.00"), \
            "失败轮被跳窗：O1 的签收没读到，在途虚高"


def test_check_order_invalid_expenditure_type_fails_store(env):
    """收支类型缺失/非法必须整店失败，不能默认成收入（三轮建议点）。

    原 `int(x or 1)` 会把 None 与 0 都当收入 → 一笔真实支出被算成正数，轧差即错。
    """
    client = FakeClient()
    client.check_orders = [
        {"checkOrderNo": "B1", "addTime": "2026-09-15 10:00:00",
         "estimatePayTime": "2026-09-21 10:00:00", "currencyCode": "MXN",
         "estimateIncomeMoneyTotal": 100.00, "incomeExpenditureType": 0}]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "收支类型非法" in outcome.stores[0].error_summary


def test_report_order_page_cap_fails_instead_of_truncating(env):
    """报账单翻页超限也须整店失败（三轮指出的盲区：原先假网关第 1 页回全部）。"""
    client = FakeClient()
    client.report_orders = [
        {"reportOrderNo": f"R{i}", "income": 1.00, "currencyCode": "MXN"}
        for i in range(30 * 201)          # 超过 200 页 × 30
    ]
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = build_service(env, client).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.stores[0].status == "fail"
        assert "未取完" in outcome.stores[0].error_summary


# ---- frankfurter 汇率解析与 run() 接线 ----

FRANKFURTER_PAYLOAD = {
    "amount": 1.0,
    "base": "USD",
    "date": "2026-09-18",
    "rates": {"CNY": 6.6976, "MXN": 17.1776, "BRL": 5.1359, "EUR": 0.8726},
}


def test_parse_frankfurter_cross_rates():
    """1 MXN = CNY_per_USD / MXN_per_USD；CNY 恒 1、USD 直接取 CNY 行。"""
    quote = parse_frankfurter_quote(FRANKFURTER_PAYLOAD)
    assert quote.rate_date.isoformat() == "2026-09-18"
    assert quote.rates["CNY"] == Decimal("1")
    assert quote.rates["USD"] == Decimal("6.6976")
    # 6.6976 / 17.1776 = 0.38991…，quantize 到 1e-5
    assert quote.rates["MXN"] == (Decimal("6.6976") / Decimal("17.1776")
                                  ).quantize(Decimal("0.00001"))


def test_parse_frankfurter_missing_cny_returns_empty():
    """缺 CNY 基准价时宁缺毋错：返回空 rates，看板显示「—」而不是错算。"""
    payload = {"date": "2026-09-18", "rates": {"MXN": 17.1776}}
    assert parse_frankfurter_quote(payload).rates == {}


def test_parse_frankfurter_missing_rates_key_returns_empty():
    assert parse_frankfurter_quote({"date": "2026-09-18"}).rates == {}
    assert parse_frankfurter_quote({"rates": "not-a-dict"}).rates == {}


def test_parse_frankfurter_skips_zero_and_invalid_entries():
    payload = {
        "date": "2026-09-18",
        "rates": {"CNY": 6.6976, "MXN": 0, "BRL": "n/a", "EUR": None},
    }
    rates = parse_frankfurter_quote(payload).rates
    assert set(rates) == {"CNY", "USD"}


def test_parse_frankfurter_bad_date_falls_back_to_today():
    import datetime as _dt
    quote = parse_frankfurter_quote({"rates": {"CNY": 6.6976}})
    assert quote.rate_date == _dt.date.today()


def _fx_quote() -> FxRateQuote:
    return parse_frankfurter_quote(FRANKFURTER_PAYLOAD)


def _service_with_fx(env, client, fetcher) -> SheinSettlementSyncService:
    return SheinSettlementSyncService(
        client=client, cipher=env["cipher"], order_lookback_days=4,
        check_order_lookback_days=60, fx_fetcher=fetcher)


def test_run_fetches_fx_rates_once_and_upserts(env):
    """run() 开头整轮取一次汇率并落库（source=frankfurter）。"""
    client = FakeClient()
    client.orders = []
    client.check_orders = []
    client.report_orders = []
    calls: list[int] = []

    def fetcher() -> FxRateQuote:
        calls.append(1)
        return _fx_quote()

    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        _service_with_fx(env, client, fetcher).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        rows = session.execute(select(SheinFxRate).order_by(
            SheinFxRate.currency)).scalars().all()
    assert len(calls) == 1
    assert {row.currency for row in rows} == {"CNY", "USD", "MXN", "BRL", "EUR"}
    assert all(row.source == "frankfurter" for row in rows)
    mxn = next(row for row in rows if row.currency == "MXN")
    assert mxn.effective_date.isoformat() == "2026-09-18"


def test_run_fx_fetch_failure_does_not_block_sync(env):
    """frankfurter 挂了：店铺同步照常成功，失败原因记入 run.detail["fx"]。"""
    client = FakeClient()
    client.orders = []
    client.check_orders = []
    client.report_orders = []

    def fetcher() -> FxRateQuote:
        raise RuntimeError("network down")

    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        outcome = _service_with_fx(env, client, fetcher).run(
            session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        assert outcome.status == "succeeded"
        run = session.execute(select(
            SheinSettlementSyncRun).order_by(
            SheinSettlementSyncRun.id.desc())).scalars().first()
    assert run.detail["fx"]["status"] == "failed"
    assert "RuntimeError" in run.detail["fx"]["error"]


def test_run_fx_fetch_failure_stale_rates_still_serve_summary(env):
    """取数失败后沿用库内旧汇率：人民币合计仍按上一有效汇率折算。"""
    client = FakeClient()
    client.orders = []
    client.check_orders = [{
        "checkOrderNo": "CK1", "addTime": "2026-09-16 10:00:00",
        "estimatePayTime": "2026-09-21 10:00:00",
        "currencyCode": "MXN", "estimateIncomeMoneyTotal": 100.00,
        "incomeExpenditureType": 1,
    }]
    client.report_orders = []

    def broken_fetcher() -> FxRateQuote:
        raise RuntimeError("network down")

    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        # 上一轮成功取到汇率
        upsert_fx_rate(session, tenant_id=env["tenant_id"], currency="MXN",
                       rate=Decimal("0.39"), effective_date=NOW.date(),
                       source="frankfurter")
        session.commit()
        service = _service_with_fx(env, client, broken_fetcher)
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        summary = service.build_summary(session, tenant_id=env["tenant_id"])
    unsettled_card = summary.cards[UNSETTLED]
    assert unsettled_card.cny_total == Decimal("39.00")
    assert unsettled_card.group("MXN").cny == Decimal("39.00")
