# -*- coding: utf-8 -*-
"""定时同步与互斥闸门（全部合成数据，不启真实线程）。

覆盖三件容易做错的事：
1. **同店互斥**——手动刷新与定时任务撞车时，后来的那路要被挡住，而不是
   两边各取一遍（轻则浪费配额，重则同一家店的数据一半新一半旧）。
2. **到期才跑**——容器重启不该触发重复同步；多进程/重复调度也不该多打平台。
3. **中断恢复**——进程被杀会留下 running 记录，不清理就会永久占住闸门。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from xynigo_auth.database import Database
from xynigo_auth.models import (
    Base,
    SheinAuthorizedStore,
    SheinSettlementSyncRun,
    Tenant,
    User,
)
from xynigo_auth.shein_settlement_sync import (
    SheinSettlementSyncBusy,
    SheinSettlementSyncService,
    SheinSettlementSyncWorker,
)
from xynigo_auth.shein_store_auth_crypto import SheinStoreSecretCipher

from test_shein_settlement_sync import (  # noqa: E402 - 复用同目录的假网关与夹具
    NOW,
    FakeClient,
    add_store,
    build_service,
    env,
)

INTERVAL = 6 * 60 * 60
STALE = 2 * 60 * 60


def build_worker(env, client, **kwargs):
    return SheinSettlementSyncWorker(
        session_factory=env["db"].session_factory,
        service=build_service(env, client),
        interval_seconds=kwargs.pop("interval_seconds", INTERVAL),
        stale_after_seconds=kwargs.pop("stale_after_seconds", STALE),
        initial_delay_seconds=kwargs.pop("initial_delay_seconds", 0),
        **kwargs,
    )


def _ok_client() -> FakeClient:
    client = FakeClient()
    client.orders = [{"orderNo": "O1", "orderStatus": 4,
                      "orderCreateTime": "2026-09-10 10:00:00",
                      "orderUpdateTime": "2026-09-16 10:00:00"}]
    client.details = {"O1": {"estimatedGrossIncome": 50, "currencyCode": "MXN"}}
    return client


# ---- 同店互斥 ----

def test_second_run_is_rejected_while_first_is_running(env):
    """已有 running 记录时，第二轮必须被挡住（手动与定时共用这个闸门）。"""
    add_store(env, "甲店")
    service = build_service(env, _ok_client())
    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        # 人为把该轮改回 running，模拟"正在跑"
        run = session.execute(select(SheinSettlementSyncRun)).scalars().one()
        run.status = "running"
        run.finished_at = None
        session.commit()

        with pytest.raises(SheinSettlementSyncBusy) as excinfo:
            service.run(session, tenant_id=env["tenant_id"],
                        now=NOW + timedelta(minutes=5))
        assert excinfo.value.status_code == 409


def test_stale_running_record_does_not_block_forever(env):
    """超过 stale 阈值的 running 视为异常中断，不再阻塞新同步。"""
    add_store(env, "甲店")
    service = build_service(env, _ok_client())
    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        run = session.execute(select(SheinSettlementSyncRun)).scalars().one()
        run.status = "running"
        run.finished_at = None
        session.commit()
        # 已超过 2 小时阈值 → 允许新一轮
        outcome = service.run(
            session, tenant_id=env["tenant_id"],
            now=NOW + timedelta(hours=3), stale_after_seconds=STALE)
        session.commit()
        assert outcome.status == "succeeded"


def test_recover_stale_marks_interrupted_run_failed(env):
    add_store(env, "甲店")
    service = build_service(env, _ok_client())
    with env["db"].session_factory() as session:
        run = SheinSettlementSyncRun(
            tenant_id=env["tenant_id"], trigger="schedule", status="running",
            started_at=NOW - timedelta(hours=5), store_total=1)
        session.add(run)
        session.commit()
        recovered = service.recover_stale_runs(
            session, tenant_id=env["tenant_id"], stale_after_seconds=STALE,
            now=NOW)
        session.commit()
        assert recovered == 1
        row = session.execute(select(SheinSettlementSyncRun)).scalars().one()
        assert row.status == "failed"
        assert "recovered" in (row.detail or {})


# ---- 到期判定 ----

def test_worker_runs_when_tenant_never_synced(env):
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        due = build_worker(env, _ok_client()).due_tenants(session, now=NOW)
    assert due == [env["tenant_id"]]


def test_worker_skips_tenant_synced_within_interval(env):
    """容器重启不该重复同步——这是"到期才跑"的核心价值。"""
    add_store(env, "甲店")
    service = build_service(env, _ok_client())
    with env["db"].session_factory() as session:
        service.run(session, tenant_id=env["tenant_id"], now=NOW)
        session.commit()
        worker = build_worker(env, _ok_client())
        assert worker.due_tenants(session, now=NOW + timedelta(hours=1)) == []
        # 超过间隔后重新到期
        assert worker.due_tenants(
            session, now=NOW + timedelta(hours=7)) == [env["tenant_id"]]


def test_worker_ignores_tenants_without_active_stores(env):
    add_store(env, "已失效店", status="expired")
    with env["db"].session_factory() as session:
        assert build_worker(env, _ok_client()).due_tenants(session, now=NOW) == []


# ---- worker 循环行为 ----

def test_run_once_executes_and_records_scheduled_run(env):
    add_store(env, "甲店")
    worker = build_worker(env, _ok_client())
    executed = worker.run_once(now=NOW)
    assert executed == 1
    with env["db"].session_factory() as session:
        run = session.execute(select(SheinSettlementSyncRun)).scalars().one()
        assert run.trigger == "schedule"
        assert run.status == "succeeded"


def test_run_once_is_noop_when_not_due(env):
    add_store(env, "甲店")
    worker = build_worker(env, _ok_client())
    assert worker.run_once(now=NOW) == 1
    # 一小时后仍未到期 → 不执行、不产生第二条记录
    assert worker.run_once(now=NOW + timedelta(hours=1)) == 0
    with env["db"].session_factory() as session:
        assert len(session.execute(select(SheinSettlementSyncRun)).scalars().all()) == 1


def test_run_once_survives_tenant_failure(env):
    """单租户异常不能中断整轮，也不能让 worker 线程退出。"""
    add_store(env, "故障店", open_key_id="B" * 32)
    client = _ok_client()
    client.fail_open_key_ids = {"B" * 32}
    worker = build_worker(env, client)
    # 单店接口失败会被 service 收敛成 fail（不算租户级异常），故本轮仍算执行
    assert worker.run_once(now=NOW) == 1
    with env["db"].session_factory() as session:
        run = session.execute(select(SheinSettlementSyncRun)).scalars().one()
        assert run.status == "failed"
        assert run.store_failed == 1


def test_run_once_skips_quietly_when_manual_sync_is_running(env):
    """手动刷新正在跑时，定时任务安静跳过——不记失败、不影响下个周期。

    构造方式：一条 7 小时前成功的记录（让租户到期）+ 一条刚开始的 running
    记录（未超 2 小时 stale 阈值，代表"手动刷新正在跑"）。
    """
    add_store(env, "甲店")
    with env["db"].session_factory() as session:
        session.add(SheinSettlementSyncRun(
            tenant_id=env["tenant_id"], trigger="manual", status="succeeded",
            started_at=NOW - timedelta(hours=7),
            finished_at=NOW - timedelta(hours=7), store_total=1))
        session.add(SheinSettlementSyncRun(
            tenant_id=env["tenant_id"], trigger="manual", status="running",
            started_at=NOW, store_total=1))
        session.commit()

    worker = build_worker(env, _ok_client())
    with env["db"].session_factory() as session:
        assert worker.due_tenants(session, now=NOW) == [env["tenant_id"]]
    assert worker.run_once(now=NOW) == 0
    with env["db"].session_factory() as session:
        rows = session.execute(select(SheinSettlementSyncRun)).scalars().all()
        # 只有原有的两条，没有新增（既没记失败也没起新的一轮）
        assert len(rows) == 2


def test_worker_does_not_start_when_no_stores(env):
    assert build_worker(env, _ok_client()).run_once(now=NOW) == 0
