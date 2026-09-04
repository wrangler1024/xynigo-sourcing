from __future__ import annotations

from copy import deepcopy
import hashlib
import uuid

import pytest
from sqlalchemy import func, select

from test_purchase_api import authenticated_client
from xynigo_auth.feishu_operation_sync import FeishuOperationSyncWorker
from xynigo_auth.models import (
    AuditEvent,
    BuyerAccount,
    EnvironmentAccountRunGuard,
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    EnvironmentNameSequence,
    HubEnvironmentInventory,
    LogisticsQueryResult,
    LogisticsQueryRun,
    OperationalSyncOutbox,
)
from xynigo_auth.operation_service import OperationRunService
from xynigo_auth.purchase_service import PurchaseServiceError


def environment_payload() -> dict[str, object]:
    return {
        "source": "local_executor",
        "runKey": "env_batch-synthetic0001",
        "site": "US",
        "purchaseDate": "20260826",
        "environmentGroup": "Synthetic-US-Purchase",
        "startedAt": "2026-08-26T09:00:00+08:00",
        "completedAt": "2026-08-26T09:03:00+08:00",
        "results": [
            {
                "accountRef": "sha256-synthetic-buyer-0001",
                "accountLabel": "sy***01@example.test",
                "purchaserLabel": "合成采购员甲",
                "environmentName": "SYN-US-0826-001",
                "environmentRef": "hub-synthetic-us-0001",
                "environmentSerial": "9001",
                "status": "success",
                "bindingAt": "2026-08-26T09:02:00+08:00",
                "recoveredExisting": False,
                "createdInRun": True,
                "cleanupStatus": "not_required",
            },
            {
                "accountRef": "sha256-synthetic-buyer-0002",
                "accountLabel": "sy***02@example.test",
                "purchaserLabel": "合成采购员乙",
                "environmentName": "SYN-US-0826-002",
                "status": "failed",
                "errorStep": "account_bound",
                "errorSummary": "合成绑定失败",
                "recoveredExisting": False,
            },
        ],
        "ipChecks": [
            {
                "environmentName": "SYN-US-0826-001",
                "ipAddress": "192.0.2.10",
                "country": "United States",
                "city": "Example City",
                "isp": "Synthetic ISP",
                "ok": True,
                "errorCode": "",
            }
        ],
    }


def test_cloud_inventory_allocates_monotonic_names_and_blocks_cross_device_duplicates(
    tmp_path,
) -> None:
    client, database, _headers = authenticated_client(tmp_path)
    with database.session_factory() as session:
        previous = session.scalar(select(EnvironmentCreationRun))
        if previous is None:
            from xynigo_auth.models import Tenant, User
            tenant = session.scalar(select(Tenant))
            user = session.scalar(select(User))
            assert tenant is not None and user is not None
            tenant_id, user_id = tenant.id, user.id
        else:
            tenant_id, user_id = previous.tenant_id, previous.actor_user_id

        def make_run(key: str) -> EnvironmentCreationRun:
            run = EnvironmentCreationRun(
                id=uuid.uuid4(), tenant_id=tenant_id, actor_user_id=user_id,
                source_run_key=key, payload_hash=hashlib.sha256(
                    key.encode("utf-8")
                ).hexdigest(), run_mode="bound", site="MX",
                purchase_date="20260903", environment_group="MX采购",
                status="created", phase="created", progress_completed=0,
                progress_total=2, total_count=2, success_count=0,
                failed_count=0, ip_ok_count=0, ip_total_count=0,
                request_summary={}, source="cloud_web",
            )
            session.add(run)
            session.flush()
            return run

        accounts = [{
            "email": "inventory1@example.test", "orderNo": "a0000001",
        }, {
            "email": "inventory2@example.test", "orderNo": "a0000002",
        }]
        service = OperationRunService(session)
        first = make_run("inventory-run-0001")
        planned = service.reserve_environment_names(
            run=first,
            plan_accounts=accounts,
            assignments=[{"purchaserLabel": "新刚", "count": 2}],
        )
        assert [row["environmentName"] for row in planned] == [
            "XG-MX-0903-001", "XG-MX-0903-002",
        ]
        inventory = list(session.scalars(select(HubEnvironmentInventory)))
        assert len(inventory) == 2
        assert {row.state for row in inventory} == {"reserved"}

        account_ref = planned[0]["accountRef"]
        session.add(EnvironmentCreationResult(
            id=uuid.uuid4(), run_id=first.id, tenant_id=tenant_id,
            account_ref=account_ref, account_label="in***01@example.test",
            purchaser_label="新刚", environment_name="XG-MX-0903-001",
            environment_ref="hub-0001", environment_serial="9001",
            status="success", completed_steps=["done"],
            recovered_existing=False, created_in_run=True,
            cleanup_status="not_required", feishu_sync_status="pending",
        ))
        session.flush()
        service.finalize_environment_inventory(
            run=first, status="partial_failure"
        )
        states = {
            row.account_ref: row.state
            for row in session.scalars(select(HubEnvironmentInventory))
        }
        assert states[account_ref] == "active"
        assert set(states.values()) == {"active", "deleted"}

        second = make_run("inventory-run-0002")
        with pytest.raises(PurchaseServiceError) as duplicate:
            service.reserve_environment_names(
                run=second,
                plan_accounts=[accounts[0], {
                    "email": "inventory3@example.test", "orderNo": "a0000003",
                }],
                assignments=[{"purchaserLabel": "新刚", "count": 2}],
            )
        assert duplicate.value.code == "environment_account_already_bound"

        third = make_run("inventory-run-0003")
        next_names = service.reserve_environment_names(
            run=third,
            plan_accounts=[{
                "email": "inventory3@example.test", "orderNo": "a0000003",
            }, {
                "email": "inventory4@example.test", "orderNo": "a0000004",
            }],
            assignments=[{"purchaserLabel": "新刚", "count": 2}],
        )
        assert [row["environmentName"] for row in next_names] == [
            "XG-MX-0903-003", "XG-MX-0903-004",
        ]
        sequence = session.scalar(select(EnvironmentNameSequence))
        assert sequence is not None and sequence.last_value == 4
        third.error_code = "operation_task_failed"
        service.finalize_environment_inventory(run=third, status="failed")
        uncertain = list(session.scalars(
            select(HubEnvironmentInventory).where(
                HubEnvironmentInventory.source_run_id == third.id
            )
        ))
        assert len(uncertain) == 2
        assert {row.state for row in uncertain} == {"uncertain"}
    client.close()


def logistics_payload() -> dict[str, object]:
    return {
        "source": "local_executor",
        "runKey": "query-synthetic0001",
        "queryMode": "initial",
        "site": "US",
        "startedAt": "2026-08-26T10:00:00+08:00",
        "completedAt": "2026-08-26T10:04:00+08:00",
        "results": [
            {
                "environmentSerial": "9001",
                "environmentName": "SYN-US-0826-001",
                "status": "ok",
                "platformOrderNo": "SYNTHETIC-ORDER-0001",
                "orderTime": "2026-08-25 20:00:00",
                "amount": "USD 12.34",
                "platformStatus": "Shipped",
                "statusLabel": "已发货",
                "fulfillmentStage": "shipping",
                "trackingNumbers": ["SYNTHETIC-TRACK-0001"],
                "packageNumbers": ["SYNTHETIC-PKG-0001"],
                "carrier": "Synthetic Carrier",
                "firstTrackingAt": "2026-08-25T22:00:00-05:00",
                "firstTrackingTime": "2026-08-25 22:00:00",
                "firstTrackingSummary": "Carrier received package",
                "firstTrackingLeadMinutes": 120,
                "cancelled": False,
                "riskOrder": False,
                "ipAddress": "192.0.2.10",
                "timeZone": "America/Chicago",
                "utcOffsetMinutes": -300,
                "queriedAt": "2026-08-25T21:03:00-05:00",
                "screenshotStatus": "ok",
            },
            {
                "environmentSerial": "9002",
                "environmentName": "SYN-US-0826-002",
                "status": "login",
                "errorSummary": "合成登录失效",
                "queriedAt": "2026-08-25T21:04:00-05:00",
                "screenshotStatus": "none",
            },
        ],
    }


class FakeBaseClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def upsert(
        self,
        *,
        table_id: str,
        sync_key: str,
        fields: dict[str, object],
        key_field: str = "同步键",
    ) -> str:
        self.calls.append((table_id, sync_key, {"keyField": key_field, **fields}))
        return f"rec-synthetic-{len(self.calls)}"


def test_real_operation_results_are_idempotent_durable_and_feishu_queued(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    headers = {**headers, "X-Xynigo-Client-Version": "0.12.0"}

    environment = client.put(
        "/v1/operations/environment-creation-runs",
        json=environment_payload(),
        headers=headers,
    )
    assert environment.status_code == 200
    assert environment.json()["data"] == {
        **environment.json()["data"],
        "status": "partial_failure",
        "totalCount": 2,
        "successCount": 1,
        "failedCount": 1,
        "ipOkCount": 1,
        "ipTotalCount": 1,
        "syncStatus": "pending",
        "unchanged": False,
        "resourceConflictCount": 0,
    }
    repeated = client.put(
        "/v1/operations/environment-creation-runs",
        json=environment_payload(),
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["unchanged"] is True

    changed = deepcopy(environment_payload())
    changed["results"][1]["errorSummary"] = "不同的合成结果"  # type: ignore[index]
    conflict = client.put(
        "/v1/operations/environment-creation-runs", json=changed, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "operation_run_idempotency_conflict"

    logistics = client.put(
        "/v1/operations/logistics-query-runs",
        json=logistics_payload(),
        headers=headers,
    )
    assert logistics.status_code == 200
    assert logistics.json()["data"]["status"] == "partial_failure"

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EnvironmentCreationRun)) == 1
        assert session.scalar(select(func.count()).select_from(EnvironmentCreationResult)) == 2
        assert session.scalar(select(func.count()).select_from(LogisticsQueryRun)) == 1
        assert session.scalar(select(func.count()).select_from(LogisticsQueryResult)) == 2
        tracked = session.scalar(
            select(LogisticsQueryResult).where(
                LogisticsQueryResult.environment_serial == "9001"
            )
        )
        assert tracked is not None
        assert tracked.first_tracking_time_text == "2026-08-25 22:00:00"
        assert tracked.first_tracking_summary == "Carrier received package"
        assert tracked.first_tracking_lead_minutes == 120
        assert session.scalar(select(func.count()).select_from(OperationalSyncOutbox)) == 5
        buyer = session.scalar(select(BuyerAccount))
        assert buyer is not None
        assert buyer.account_ref == "sha256-synthetic-buyer-0001"
        assert buyer.credential_status == "ready"
        created = session.scalar(
            select(EnvironmentCreationResult).where(
                EnvironmentCreationResult.account_ref
                == "sha256-synthetic-buyer-0001"
            )
        )
        assert created is not None
        assert created.created_in_run is True
        assert created.cleanup_status == "not_required"
        assert buyer.hub_environment_ref == "hub-synthetic-us-0001"
        assert buyer.source == "environment_creation"

        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        (
                            "resource.environment.result_ingest",
                            "fulfillment.logistics.result_ingest",
                        )
                    )
                )
            )
        )
        assert len(audits) == 4
        rendered = str([(item.details, item.change_summary) for item in audits])
        assert "SYNTHETIC-ORDER-0001" not in rendered
        assert "SYNTHETIC-TRACK-0001" not in rendered
        assert "sha256-synthetic-buyer-0001" not in rendered

    fake = FakeBaseClient()
    worker = FeishuOperationSyncWorker(
        session_factory=database.session_factory,
        client=fake,  # type: ignore[arg-type]
        buyer_account_table_id="tbl_buyer_synthetic",
        environment_table_id="tbl_environment_synthetic",
        logistics_table_id="tbl_logistics_synthetic",
        interval_seconds=15,
    )
    assert worker.run_once(limit=10) == 5
    assert len(fake.calls) == 5
    assert {call[0] for call in fake.calls} == {
        "tbl_buyer_synthetic",
        "tbl_environment_synthetic",
        "tbl_logistics_synthetic",
    }
    assert all(
        call[2].get("飞书同步时间") or call[2].get("最近同步时间")
        for call in fake.calls
    )
    assert next(
        call for call in fake.calls if call[0] == "tbl_buyer_synthetic"
    )[2]["keyField"] == "账号引用"
    serialized_fields = str([call[2] for call in fake.calls]).casefold()
    assert "synthetic-password" not in serialized_fields
    assert "synthetic-cookie" not in serialized_fields

    with database.session_factory() as session:
        assert set(session.scalars(select(OperationalSyncOutbox.status))) == {"completed"}
        assert set(session.scalars(select(EnvironmentCreationResult.feishu_sync_status))) == {
            "completed"
        }
        assert set(session.scalars(select(LogisticsQueryResult.feishu_sync_status))) == {
            "completed"
        }
        assert set(session.scalars(select(BuyerAccount.feishu_sync_status))) == {
            "completed"
        }
    client.close()


def test_operation_contract_rejects_credentials_without_echoing_values(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    payload = environment_payload()
    payload["results"][0]["password"] = "synthetic-secret-must-not-echo"  # type: ignore[index]
    response = client.put(
        "/v1/operations/environment-creation-runs", json=payload, headers=headers
    )
    assert response.status_code == 422
    assert "synthetic-secret-must-not-echo" not in response.text
    with database.session_factory() as session:
        assert session.scalar(select(EnvironmentCreationRun)) is None
    client.close()


def test_cleanup_failed_account_refs_are_tenant_scoped_rerun_guards(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    payload = environment_payload()
    payload["runKey"] = "env_batch-cleanup-failed-0001"
    payload["results"] = [{
        "accountRef": hashlib.sha256(
            "buyer1@example.test".encode("utf-8")
        ).hexdigest(),
        "accountLabel": "bu***01@example.test",
        "purchaserLabel": "合成采购员甲",
        "environmentName": "SYN-US-0826-001",
        "environmentRef": "132725138",
        "environmentSerial": "9001",
        "status": "stopped",
        "createdInRun": True,
        "cleanupStatus": "failed",
        "cleanupErrorCode": "hubstudio_local_api_timeout",
        "cleanupErrorSummary": "HubStudio Local API 请求超时",
    }]
    payload["ipChecks"] = []
    response = client.put(
        "/v1/operations/environment-creation-runs",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200, response.text

    account_ref = payload["results"][0]["accountRef"]
    with database.session_factory() as session:
        old_run = session.scalar(select(EnvironmentCreationRun))
        assert old_run is not None
        session.add(EnvironmentAccountRunGuard(
            id=uuid.uuid4(), tenant_id=old_run.tenant_id,
            account_ref=account_ref, run_id=old_run.id,
            state="cleanup_failed",
        ))
        new_run = EnvironmentCreationRun(
            id=uuid.uuid4(), tenant_id=old_run.tenant_id,
            actor_user_id=old_run.actor_user_id,
            source_run_key="env-guard-transfer-0001",
            payload_hash="b" * 64, run_mode="bound", site="US",
            purchase_date="20260827",
            environment_group="Synthetic-US-Purchase",
            status="created", phase="created", progress_completed=0,
            progress_total=1, total_count=1, success_count=0,
            failed_count=0, ip_ok_count=0, ip_total_count=0,
            request_summary={}, source="cloud_web",
        )
        session.add(new_run)
        session.flush()
        service = OperationRunService(session)
        inherited = service.acquire_environment_account_guards(
            run=new_run,
            account_refs={account_ref},
        )
        assert inherited == {account_ref}
        guard = session.scalar(select(EnvironmentAccountRunGuard))
        assert guard is not None
        assert guard.run_id == new_run.id
        assert guard.state == "active"

        competing = EnvironmentCreationRun(
            id=uuid.uuid4(), tenant_id=old_run.tenant_id,
            actor_user_id=old_run.actor_user_id,
            source_run_key="env-guard-conflict-0001",
            payload_hash="c" * 64, run_mode="bound", site="US",
            purchase_date="20260827",
            environment_group="Synthetic-US-Purchase",
            status="created", phase="created", progress_completed=0,
            progress_total=1, total_count=1, success_count=0,
            failed_count=0, ip_ok_count=0, ip_total_count=0,
            request_summary={}, source="cloud_web",
        )
        session.add(competing)
        session.flush()
        try:
            service.acquire_environment_account_guards(
                run=competing,
                account_refs={account_ref},
            )
        except PurchaseServiceError as exc:
            assert exc.code == "environment_cleanup_in_progress"
        else:
            raise AssertionError("concurrent environment Run was not blocked")
    client.close()


def test_cleanup_pending_guard_blocks_immediate_rerun_until_delete_finishes(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    payload = environment_payload()
    payload["runKey"] = "env_batch-cleanup-pending-0001"
    response = client.put(
        "/v1/operations/environment-creation-runs",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200, response.text

    account_ref = payload["results"][0]["accountRef"]
    with database.session_factory() as session:
        previous = session.scalar(select(EnvironmentCreationRun))
        assert previous is not None
        service = OperationRunService(session)
        service.acquire_environment_account_guards(
            run=previous,
            account_refs={account_ref},
        )
        service.mark_environment_guards_cleanup_pending(run_id=previous.id)
        session.flush()
        guard = session.scalar(select(EnvironmentAccountRunGuard))
        assert guard is not None
        assert guard.state == "cleanup_pending"

        rerun = EnvironmentCreationRun(
            id=uuid.uuid4(), tenant_id=previous.tenant_id,
            actor_user_id=previous.actor_user_id,
            source_run_key="env-guard-after-cleanup-0001",
            payload_hash="d" * 64, run_mode="bound", site="US",
            purchase_date="20260827",
            environment_group="Synthetic-US-Purchase",
            status="created", phase="created", progress_completed=0,
            progress_total=1, total_count=1, success_count=0,
            failed_count=0, ip_ok_count=0, ip_total_count=0,
            request_summary={}, source="cloud_web",
        )
        session.add(rerun)
        session.flush()
        with pytest.raises(PurchaseServiceError) as blocked:
            service.acquire_environment_account_guards(
                run=rerun,
                account_refs={account_ref},
            )
        assert blocked.value.code == "environment_cleanup_in_progress"

        service.finalize_environment_account_guards(
            run=previous,
            status="cancelled",
            summary={
                "cleanupTotal": 1,
                "cleanupDone": 1,
                "cleanupFailed": 0,
            },
        )
        session.flush()
        assert session.scalar(select(EnvironmentAccountRunGuard)) is None

        inherited = service.acquire_environment_account_guards(
            run=rerun,
            account_refs={account_ref},
        )
        assert inherited == set()
        guard = session.scalar(select(EnvironmentAccountRunGuard))
        assert guard is not None
        assert guard.run_id == rerun.id
        assert guard.state == "active"

        rerun.started_at = previous.started_at
        service.finalize_environment_account_guards(
            run=rerun,
            status="cancelled",
            summary={
                "cleanupTotal": 1,
                "cleanupDone": 0,
                "cleanupFailed": 1,
            },
        )
        session.flush()
        guard = session.scalar(select(EnvironmentAccountRunGuard))
        assert guard is not None
        assert guard.run_id == rerun.id
        assert guard.state == "cleanup_failed"
    client.close()
