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
    SheinPayoutBatch,
    SheinSettlementOrder,
    SheinSettlementSnapshot,
    SheinSettlementSyncRun,
    Tenant,
    User,
)
from xynigo_auth.shein_openapi_client import SheinOpenApiClientError
from xynigo_auth.shein_settlement_sync import (
    SheinSettlementSyncService,
    _as_platform_tz,
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
        self._guard(open_key_id)
        self.order_calls.append({"page": page, "start": start_time})
        return {"count": len(self.orders), "orderList": self.orders} \
            if page == 1 else {"count": len(self.orders), "orderList": []}

    def query_order_details(self, *, open_key_id, secret_key, order_nos):
        self._guard(open_key_id)
        self.detail_calls.append(list(order_nos))
        return {"orderList": [
            {"orderNo": no, **self.details.get(no, {})} for no in order_nos
            if no in self.details]}

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
        return {"count": len(matched), "list": matched} if page == 1 \
            else {"count": len(matched), "list": []}

    def query_report_orders(self, *, open_key_id, secret_key, page=1,
                            page_size=30, report_status=None, **kwargs):
        self._guard(open_key_id)
        self.report_calls.append({"page": page, "status": report_status})
        return {"count": len(self.report_orders), "list": self.report_orders} \
            if page == 1 else {"count": len(self.report_orders), "list": []}

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
        check_order_days_back=14,
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
    client.details = {"O1": {"estimatedGrossIncome": 100.5, "currencyCode": "MXN"}}
    client.check_orders = [
        {"addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 600.00,
         "incomeExpenditureType": 1},
        {"addTime": "2026-09-16 10:00:00", "estimatePayTime": "2026-09-28 10:00:00",
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
    client.details = {f"O{i}": {"estimatedGrossIncome": 10, "currencyCode": "MXN"}
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
        {"addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
         "currencyCode": "MXN", "estimateIncomeMoneyTotal": 1059.32,
         "incomeExpenditureType": 1},
        {"addTime": "2026-09-15 11:00:00", "estimatePayTime": "2026-09-21 10:00:00",
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
    client.details = {"O1": {"estimatedGrossIncome": 50, "currencyCode": "MXN"}}
    client.check_orders = [
        {"addTime": "2026-09-15 10:00:00", "estimatePayTime": "2026-09-21 10:00:00",
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


def test_one_store_failure_does_not_affect_others(env):
    """一家店挂了不能拖垮整轮——这是看板可信度的前提。"""
    ok_store = add_store(env, "正常店", open_key_id="A" * 32)
    bad_store = add_store(env, "故障店", open_key_id="B" * 32)
    client = FakeClient(fail_open_key_ids={"B" * 32})
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 77, "currencyCode": "MXN"}}

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
    client.details = {"O1": {"estimatedGrossIncome": 88.8, "currencyCode": "MXN"}}
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
