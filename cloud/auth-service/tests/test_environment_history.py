from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_executor_channel import login
from xynigo_auth.models import (
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    Tenant,
    User,
)


def _run(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    created_at: datetime,
    mode: str,
    total: int,
    status: str,
    parent: EnvironmentCreationRun | None = None,
) -> EnvironmentCreationRun:
    run_id = uuid.uuid4()
    root_id = (parent.root_run_id or parent.id) if parent else run_id
    return EnvironmentCreationRun(
        id=run_id,
        tenant_id=tenant_id,
        actor_user_id=user_id,
        source_run_key=f"environment-history-{run_id}",
        payload_hash="a" * 64,
        executor_id=None,
        parent_run_id=parent.id if parent else None,
        root_run_id=root_id,
        run_mode=mode,
        site="MX",
        purchase_date="20260904",
        environment_group="合成测试组",
        status=status,
        phase=status,
        attempt=1,
        progress_completed=total,
        progress_total=total,
        stop_requested=False,
        total_count=total,
        success_count=total if status == "completed" else 0,
        failed_count=0 if status == "completed" else total,
        ip_ok_count=0,
        ip_total_count=0,
        request_summary=(
            {"accountRefs": ["account-ref-0002"]} if parent else {}
        ),
        source="cloud_web",
        started_at=created_at,
        completed_at=created_at + timedelta(seconds=30),
        last_heartbeat_at=created_at + timedelta(seconds=30),
        created_at=created_at,
        updated_at=created_at + timedelta(seconds=30),
    )


def _result(
    run: EnvironmentCreationRun,
    account_ref: str,
    *,
    status: str,
    recovered: bool = False,
    serial: str | None = None,
) -> EnvironmentCreationResult:
    return EnvironmentCreationResult(
        id=uuid.uuid4(),
        run_id=run.id,
        tenant_id=run.tenant_id,
        account_ref=account_ref,
        account_label=f"bu***{account_ref[-2:]}@example.test",
        purchaser_label="合成采购员",
        environment_name=f"SYN-MX-{account_ref[-4:]}",
        environment_ref=f"container-{account_ref[-4:]}" if serial else None,
        environment_serial=serial,
        status=status,
        current_step="done" if status == "success" else "account_binding",
        completed_steps=["env_created"],
        error_summary="合成失败" if status == "failed" else None,
        recovered_existing=recovered,
        created_in_run=status == "success" and not recovered,
        cleanup_status="not_required",
        feishu_sync_status="pending",
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def test_environment_history_groups_retries_uses_stable_id_and_sorts_by_update(
    tmp_path,
) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    now = datetime.now(UTC)

    with TestClient(app) as client:
        login(client)
        with database.session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.feishu_tenant_key == "tenant_allowed")
            )
            user = session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id,
                    User.feishu_open_id == "ou_admin",
                )
            )
            root = _run(
                tenant_id=tenant.id,
                user_id=user.id,
                created_at=now - timedelta(days=2),
                mode="bound",
                total=2,
                status="partial_failure",
            )
            root.completed_at = root.started_at + timedelta(seconds=70)
            root.updated_at = root.completed_at
            session.add(root)
            session.flush()
            session.add_all([
                _result(
                    root, "account-ref-0001", status="success",
                    recovered=True, serial="7001",
                ),
                _result(root, "account-ref-0002", status="failed"),
            ])
            newer = _run(
                tenant_id=tenant.id,
                user_id=user.id,
                created_at=now - timedelta(days=1),
                mode="dry_run",
                total=1,
                status="completed",
            )
            session.add(newer)
            session.flush()
            session.add(_result(
                newer, "preview-ref-0001", status="success", serial="8001"
            ))
            retry = _run(
                tenant_id=tenant.id,
                user_id=user.id,
                created_at=now - timedelta(minutes=1),
                mode="retry_failed",
                total=1,
                status="completed",
                parent=root,
            )
            retry.completed_at = retry.started_at + timedelta(seconds=20)
            retry.updated_at = now
            session.add(retry)
            session.flush()
            session.add(_result(
                retry, "account-ref-0002", status="success", serial="7002"
            ))
            session.commit()
            root_id = str(root.id)
            retry_id = str(retry.id)

        history = client.get("/v1/operation-runs/environment-creation/history")
        assert history.status_code == 200, history.text
        items = history.json()["data"]["items"]
        assert items[0]["taskId"] == root_id
        assert items[0]["latestRunId"] == retry_id
        assert items[0]["taskType"] == "bound"
        assert items[0]["latestTaskType"] == "retry_failed"
        assert items[0]["plannedCount"] == 2
        assert items[0]["successCount"] == 2
        assert items[0]["recoveredCount"] == 1
        assert items[0]["failedCount"] == 0
        assert items[0]["retryCount"] == 1
        assert items[0]["durationSec"] == 90
        assert items[0]["createdAt"] != items[0]["updatedAt"]

        detail = client.get(
            f"/v1/operation-runs/environment-creation/history/{root_id}"
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["taskId"] == root_id
        assert data["latestRunId"] == retry_id
        assert {
            row["accountRef"]: row["status"] for row in data["rows"]
        } == {
            "account-ref-0001": "success",
            "account-ref-0002": "success",
        }
        assert all("*" in row["accountLabel"] for row in data["rows"])


def test_environment_history_marks_unreported_resume_rows_pending(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    now = datetime.now(UTC)
    with TestClient(app) as client:
        login(client)
        with database.session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.feishu_tenant_key == "tenant_allowed")
            )
            user = session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id,
                    User.feishu_open_id == "ou_admin",
                )
            )
            root = _run(
                tenant_id=tenant.id,
                user_id=user.id,
                created_at=now - timedelta(hours=1),
                mode="bound",
                total=2,
                status="partial_failure",
            )
            session.add(root)
            session.flush()
            session.add_all([
                _result(root, "account-ref-0001", status="success", serial="7001"),
                _result(root, "account-ref-0002", status="failed"),
            ])
            retry = _run(
                tenant_id=tenant.id,
                user_id=user.id,
                created_at=now,
                mode="retry_failed",
                total=1,
                status="running",
                parent=root,
            )
            retry.completed_at = None
            retry.progress_completed = 0
            retry.updated_at = now
            session.add(retry)
            session.commit()
            root_id = str(root.id)

        response = client.get(
            f"/v1/operation-runs/environment-creation/history/{root_id}"
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "running"
        assert data["successCount"] == 1
        assert data["pendingCount"] == 1
        assert data["failedCount"] == 0
        assert {
            row["accountRef"]: row["status"] for row in data["rows"]
        } == {
            "account-ref-0001": "success",
            "account-ref-0002": "queued",
        }
