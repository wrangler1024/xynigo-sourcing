"""Duplicate-guard guidance for environment creation.

A single already-bound row used to reject the whole batch with a message that
named no rows, so operators re-uploaded edited files and burned the shared
short-lived plan quota.  These cases pin the replacement behaviour: the refusal
lists the rows (masked), and an explicit retry can drop exactly those rows.
"""
from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_executor_channel import (
    CSRF,
    create_pairing_code,
    environment_workbook,
    heartbeat,
    login,
    pair,
)
from xynigo_auth.models import (
    EnvironmentAccountPlan,
    EnvironmentCreationRun,
    HubEnvironmentInventory,
    HubEnvironmentObservation,
)
from xynigo_auth.operation_service import OperationRunService


CAPABILITIES = [
    "environment.cloud-plan.v1",
    "environment.cloud-inventory.v1",
    "environment.create-bound.v1",
]
GROUP = "合成MX采购"


def create_executor(web: TestClient, device: TestClient) -> tuple[str, str]:
    paired = pair(device, create_pairing_code(web), capabilities=CAPABILITIES)
    credential = str(paired["deviceCredential"])
    heartbeat(device, credential, capabilities=CAPABILITIES)
    return str(paired["executorId"]), credential


def parse_plan(
    web: TestClient,
    *,
    key: str,
    count: int = 3,
    group: str = GROUP,
) -> dict:
    response = web.post(
        "/v1/environment-plans/parse",
        json={
            "idempotencyKey": key,
            "filename": "synthetic-buyers.xlsx",
            "contentBase64": environment_workbook(count),
            "site": "MX",
            "environmentGroup": group,
        },
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_body(
    executor_id: str,
    plan: dict,
    *,
    key: str,
    count: int = 3,
    group: str = GROUP,
    **extra: object,
) -> dict:
    return {
        "idempotencyKey": key,
        "executorId": executor_id,
        "mode": "bound",
        "site": "MX",
        "purchaseDate": "20260901",
        "environmentGroup": group,
        "cloudPlanId": plan["cloudPlanId"],
        "totalCount": count,
        "verifySampleCount": 0,
        "assignments": [{"purchaserLabel": "新刚", "count": count}],
        **extra,
    }


def plan_context(app, database, cloud_plan_id: str) -> tuple[uuid.UUID, list[dict]]:
    """Tenant plus per-row identity, derived from the server's own helpers."""
    with database.session_factory() as session:
        record = session.scalar(
            select(EnvironmentAccountPlan).where(
                EnvironmentAccountPlan.id == uuid.UUID(cloud_plan_id)
            )
        )
        assert record is not None
        tenant_id = record.tenant_id
        accounts = app.state.environment_plan_service._accounts(record)
    rows = [
        {
            "email": account.email,
            "orderNo": account.order_no,
            "accountRef": OperationRunService._account_ref({"email": account.email}),
            "orderRef": OperationRunService._source_order_ref(
                {"orderNo": account.order_no}
            ),
        }
        for account in accounts
    ]
    return tenant_id, rows


def bind_account(database, *, tenant_id, account: dict, name: str) -> None:
    with database.session_factory() as session:
        session.add(HubEnvironmentInventory(
            tenant_id=tenant_id,
            account_ref=account["accountRef"],
            source_order_ref=account["orderRef"],
            environment_name=name,
            environment_ref="ref-" + name,
            environment_group=GROUP,
            site="MX",
            purchaser_label="新刚",
            state="active",
        ))
        session.commit()


def observe_order(database, *, tenant_id, account: dict, name: str) -> None:
    with database.session_factory() as session:
        session.add(HubEnvironmentObservation(
            tenant_id=tenant_id,
            environment_key=("key-" + name)[:71],
            environment_name=name,
            environment_group=GROUP,
            site="MX",
            source_order_ref=account["orderRef"],
            snapshot_revision="f" * 64,
            last_observed_at=datetime.now(UTC),
        ))
        session.commit()


def test_bound_submit_lists_conflicting_rows_in_diagnostics(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        executor_id, _credential = create_executor(web, device)
        plan = parse_plan(web, key="environment-conflict-0001")
        tenant_id, accounts = plan_context(app, database, plan["cloudPlanId"])
        bind_account(
            database,
            tenant_id=tenant_id,
            account=accounts[1],
            name="XG-MX-260901-001-ABCD",
        )
        response = web.post(
            "/v1/operation-runs/environment-creation",
            json=create_body(executor_id, plan, key="environment-run-conflict-0001"),
            headers=CSRF,
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "environment_account_already_bound"
        diagnostics = detail["diagnostics"]
        assert diagnostics["totalRows"] == 3
        assert diagnostics["conflictCount"] == 1
        assert diagnostics["skippableRows"] == 1
        assert diagnostics["truncated"] is False
        rows = diagnostics["rows"]
        assert len(rows) == 1
        assert rows[0]["rowNumber"] == 2
        assert rows[0]["emailMasked"] == "bu***@example.test"
        assert accounts[1]["email"] not in str(rows[0])
        assert rows[0]["environmentName"] == "XG-MX-260901-001-ABCD"
        assert rows[0]["reasons"] == ["environment_bound"]
        # A refused submit must leave no run behind.
        with database.session_factory() as session:
            assert session.scalar(
                select(EnvironmentCreationRun).where(
                    EnvironmentCreationRun.source_run_key
                    == "environment-run-conflict-0001"
                )
            ) is None


def test_bound_submit_can_skip_the_conflicting_rows(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        executor_id, credential = create_executor(web, device)
        plan = parse_plan(web, key="environment-skip-0001")
        tenant_id, accounts = plan_context(app, database, plan["cloudPlanId"])
        bind_account(
            database,
            tenant_id=tenant_id,
            account=accounts[1],
            name="XG-MX-260901-002-EFGH",
        )
        response = web.post(
            "/v1/operation-runs/environment-creation",
            json=create_body(
                executor_id,
                plan,
                key="environment-run-skip-0001",
                skipConflictingRows=True,
            ),
            headers=CSRF,
        )
        assert response.status_code == 202, response.text
        snapshot = response.json()["data"]
        assert snapshot["totalCount"] == 2
        assert snapshot["progressTotal"] == 2
        assert snapshot["skippedConflictCount"] == 1
        assert snapshot["skippedConflictRows"] == [{
            "rowNumber": 2,
            "emailMasked": "bu***@example.test",
            "orderMasked": rows_order_mask(accounts[1]),
            "environmentName": "XG-MX-260901-002-EFGH",
            "batchLabel": None,
        }]
        with database.session_factory() as session:
            run = session.get(EnvironmentCreationRun, uuid.UUID(snapshot["runId"]))
            assert run is not None
            assert run.total_count == 2
            assert run.progress_total == 2
            summary = run.request_summary or {}
            assert summary["skippedConflictCount"] == 1
            assert len(summary["skippedConflictRows"]) == 1
            # Only the surviving rows keep their purchaser assignment.
            assert summary["assignments"] == [
                {"purchaserLabel": "新刚", "count": 2}
            ]
            # The probe sample was frozen against 3 rows; it must not exceed 2.
            assert summary["verifySampleCount"] == 2
        leased = heartbeat(device, credential, capabilities=CAPABILITIES)["task"]
        assert leased["type"] == "environment.create-bound.v1"
        assert leased["payload"]["totalCount"] == 2
        assert len(leased["payload"]["planAccounts"]) == 2
        assert leased["payload"]["assignments"] == [
            {"purchaserLabel": "新刚", "count": 2}
        ]


def test_bound_submit_without_skip_still_blocks_the_whole_batch(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        executor_id, _credential = create_executor(web, device)
        plan = parse_plan(web, key="environment-block-0001")
        tenant_id, accounts = plan_context(app, database, plan["cloudPlanId"])
        for index, account in enumerate(accounts):
            bind_account(
                database,
                tenant_id=tenant_id,
                account=account,
                name=f"XG-MX-260901-00{index + 1}-IJKL",
            )
        blocked = web.post(
            "/v1/operation-runs/environment-creation",
            json=create_body(executor_id, plan, key="environment-run-block-0001"),
            headers=CSRF,
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["diagnostics"]["conflictCount"] == 3
        skipped = web.post(
            "/v1/operation-runs/environment-creation",
            json=create_body(
                executor_id,
                plan,
                key="environment-run-block-0002",
                skipConflictingRows=True,
            ),
            headers=CSRF,
        )
        assert skipped.status_code == 409, skipped.text
        assert (
            skipped.json()["detail"]["code"]
            == "environment_plan_all_conflicting"
        )
        assert skipped.json()["detail"]["diagnostics"]["conflictCount"] == 3


def test_observation_hits_are_reported_as_conflicts(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        executor_id, _credential = create_executor(web, device)
        plan = parse_plan(web, key="environment-observed-0001")
        tenant_id, accounts = plan_context(app, database, plan["cloudPlanId"])
        observe_order(
            database,
            tenant_id=tenant_id,
            account=accounts[0],
            name="ZH-MX-260901-007-MNOP",
        )
        response = web.post(
            "/v1/operation-runs/environment-creation",
            json=create_body(executor_id, plan, key="environment-run-observed-0001"),
            headers=CSRF,
        )
        assert response.status_code == 409, response.text
        rows = response.json()["detail"]["diagnostics"]["rows"]
        assert len(rows) == 1
        assert rows[0]["rowNumber"] == 1
        assert rows[0]["reasons"] == ["environment_observed"]
        assert rows[0]["environmentName"] == "ZH-MX-260901-007-MNOP"


def rows_order_mask(account: dict) -> str:
    return OperationRunService._mask_order(account["orderNo"])
