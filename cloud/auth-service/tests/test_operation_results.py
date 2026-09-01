from __future__ import annotations

from copy import deepcopy

from sqlalchemy import func, select

from test_purchase_api import authenticated_client
from xynigo_auth.feishu_operation_sync import FeishuOperationSyncWorker
from xynigo_auth.models import (
    AuditEvent,
    BuyerAccount,
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    LogisticsQueryResult,
    LogisticsQueryRun,
    OperationalSyncOutbox,
)


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
