from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth_flow import build_test_app, start_login
from test_executor_channel import login

from xynigo_auth.models import (
    Permission,
    ProcurementImportJob,
    ProcurementImportPlan,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)


def _plan(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    filename: str,
    order_count: int,
    detail_count: int,
) -> ProcurementImportPlan:
    now = datetime.now(UTC)
    return ProcurementImportPlan(
        tenant_id=tenant_id,
        created_by_user_id=user_id,
        filename=filename,
        import_batch=f"IMP-{uuid.uuid4().hex[:8].upper()}",
        payload_hash="a" * 64,
        status="expired",
        source_row_count=detail_count,
        order_count=order_count,
        detail_count=detail_count,
        image_count=detail_count,
        expires_at=now - timedelta(minutes=1),
        encrypted_payload=None,
    )


def _job(
    *,
    tenant_id: uuid.UUID,
    plan_id: uuid.UUID,
    user_id: uuid.UUID,
    state: str,
    progress: dict,
    created_at: datetime,
    duration_sec: int | None = None,
) -> ProcurementImportJob:
    started = created_at
    finished = (
        created_at + timedelta(seconds=duration_sec)
        if duration_sec is not None and state in {"completed", "partial", "failed"}
        else None
    )
    return ProcurementImportJob(
        tenant_id=tenant_id,
        plan_id=plan_id,
        created_by_user_id=user_id,
        target_key_hash=("b" * 32 + uuid.uuid4().hex[:32]),
        state=state,
        progress={
            "state": state,
            "targetName": "采购执行协作区",
            "error": "部分订单商品图片未补齐" if state == "partial" else "",
            **progress,
        },
        started_at=started,
        finished_at=finished,
        created_at=created_at,
    )


def _seed_member(session, tenant_id: uuid.UUID, open_id: str, name: str) -> User:
    member = User(
        tenant_id=tenant_id,
        feishu_open_id=open_id,
        display_name=name,
        status="active",
    )
    session.add(member)
    session.commit()
    return member


def test_import_history_lists_scopes_filters_paginates_and_details(tmp_path) -> None:
    app, database, _oauth = build_test_app(
        tmp_path, procurement_import_enabled=True
    )
    now = datetime.now(UTC)

    with TestClient(app) as client:
        login(client)
        with database.session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.feishu_tenant_key == "tenant_allowed")
            )
            admin = session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id,
                    User.feishu_open_id == "ou_admin",
                )
            )
            colleague = _seed_member(session, tenant.id, "ou_colleague", "合成同事")
            completed_plan = _plan(
                tenant_id=tenant.id, user_id=admin.id,
                filename="店小秘导出_0909墨西哥补货.xlsx",
                order_count=18, detail_count=23,
            )
            partial_plan = _plan(
                tenant_id=tenant.id, user_id=admin.id,
                filename="店小秘导出_0908补货.xlsx",
                order_count=12, detail_count=15,
            )
            colleague_plan = _plan(
                tenant_id=tenant.id, user_id=colleague.id,
                filename="店小秘导出_0907美国批.xlsx",
                order_count=9, detail_count=11,
            )
            session.add_all([completed_plan, partial_plan, colleague_plan])
            session.flush()
            completed = _job(
                tenant_id=tenant.id, plan_id=completed_plan.id, user_id=admin.id,
                state="completed",
                progress={"rowsTotal": 23, "rowsWritten": 23, "rowsExisting": 0,
                          "written": 22, "failed": 0,
                          "targetUrl": "https://tenant.feishu.cn/sheets/SheetToken123"},
                created_at=now - timedelta(hours=1),
                duration_sec=102,
            )
            partial = _job(
                tenant_id=tenant.id, plan_id=partial_plan.id, user_id=admin.id,
                state="partial",
                progress={"rowsTotal": 15, "rowsWritten": 11, "rowsExisting": 2,
                          "written": 9, "failed": 2},
                created_at=now - timedelta(minutes=30),
                duration_sec=76,
            )
            running = _job(
                tenant_id=tenant.id, plan_id=colleague_plan.id,
                user_id=colleague.id, state="writing_images",
                progress={"rowsTotal": 11, "rowsWritten": 11, "rowsExisting": 0,
                          "written": 3, "failed": 0, "total": 11, "processed": 3},
                created_at=now - timedelta(minutes=5),
            )
            session.add_all([completed, partial, running])
            session.commit()
            admin_id, colleague_id = str(admin.id), str(colleague.id)
            completed_id, partial_id = str(completed.id), str(partial.id)

        history = client.get("/v1/assistant/procurement-import/history")
        assert history.status_code == 200, history.text
        payload = history.json()["data"]
        items = payload["items"]
        # Admin sees the whole tenant, newest first.
        assert [item["state"] for item in items] == [
            "writing_images", "partial", "completed",
        ]
        assert items[2]["jobId"] == completed_id
        assert items[2]["filename"] == "店小秘导出_0909墨西哥补货.xlsx"
        assert items[2]["orderCount"] == 18
        assert items[2]["detailCount"] == 23
        assert items[2]["rowsWritten"] == 23
        assert items[2]["rowsExisting"] == 0
        assert items[2]["failed"] == 0
        assert items[2]["targetName"] == "采购执行协作区"
        assert items[2]["targetUrl"] == "https://tenant.feishu.cn/sheets/SheetToken123"
        assert items[2]["actorDisplayName"] == "合成测试用户"
        assert items[2]["durationSec"] == 102
        assert items[0]["actorDisplayName"] == "合成同事"
        # Running jobs surface their raw machine state and image counters.
        assert items[0]["state"] == "writing_images"
        assert items[0]["total"] == 11
        assert items[0]["processed"] == 3
        assert {actor["userId"] for actor in payload["actors"]} == {
            admin_id, colleague_id,
        }

        own = client.get(
            "/v1/assistant/procurement-import/history",
            params={"userId": admin_id},
        )
        assert own.status_code == 200, own.text
        assert [item["jobId"] for item in own.json()["data"]["items"]] == [
            partial_id, completed_id,
        ]

        running_only = client.get(
            "/v1/assistant/procurement-import/history", params={"status": "running"}
        )
        assert running_only.status_code == 200, running_only.text
        assert [item["state"] for item in running_only.json()["data"]["items"]] == [
            "writing_images",
        ]

        completed_only = client.get(
            "/v1/assistant/procurement-import/history",
            params={"status": "completed"},
        )
        assert completed_only.status_code == 200, completed_only.text
        assert [item["jobId"] for item in completed_only.json()["data"]["items"]] == [
            completed_id,
        ]

        page_one = client.get(
            "/v1/assistant/procurement-import/history", params={"limit": 1}
        )
        assert page_one.status_code == 200, page_one.text
        assert page_one.json()["data"]["hasMore"] is True
        cursor = page_one.json()["data"]["nextCursor"]
        assert cursor
        page_two = client.get(
            "/v1/assistant/procurement-import/history",
            params={"limit": 1, "cursor": cursor},
        )
        assert page_two.status_code == 200, page_two.text
        assert page_two.json()["data"]["items"][0]["jobId"] == partial_id

        invalid_cursor = client.get(
            "/v1/assistant/procurement-import/history",
            params={"cursor": str(uuid.uuid4())},
        )
        assert invalid_cursor.status_code == 422, invalid_cursor.text
        assert (
            invalid_cursor.json()["detail"]["code"]
            == "procurement_import_history_cursor_invalid"
        )

        detail = client.get(
            f"/v1/assistant/procurement-import/history/{partial_id}"
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["jobId"] == partial_id
        assert data["state"] == "partial"
        assert data["error"] == "部分订单商品图片未补齐"
        assert data["durationSec"] == 76
        assert data["own"] is True

        missing = client.get(
            f"/v1/assistant/procurement-import/history/{uuid.uuid4()}"
        )
        assert missing.status_code == 404, missing.text
        assert (
            missing.json()["detail"]["code"]
            == "procurement_import_history_job_not_found"
        )


def test_import_history_hides_other_users_and_blocks_cross_user_filters(tmp_path) -> None:
    app, database, _oauth = build_test_app(
        tmp_path,
        open_id="ou_worker",
        bootstrap="ou_admin",
        procurement_import_enabled=True,
    )
    now = datetime.now(UTC)

    with TestClient(app) as client:
        # First login attempt creates the tenant and a pending membership.
        state, _challenge = start_login(client)
        pending = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert pending.status_code == 403, pending.text
        with database.session_factory() as session:
            worker = session.scalar(
                select(User).where(User.feishu_open_id == "ou_worker")
            )
            worker.status = "active"
            tenant_id = worker.tenant_id
            role = Role(tenant_id=tenant_id, code="synthetic_importer", name="合成导入角色")
            session.add(role)
            session.flush()
            permission = session.scalar(
                select(Permission).where(Permission.code == "assistant.access")
            )
            if permission is None:
                # The catalog is seeded on first successful login, which has
                # not happened yet at this point in this test.
                permission = Permission(code="assistant.access", name="小犀助手访问")
                session.add(permission)
                session.flush()
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
            session.add(UserRole(user_id=worker.id, role_id=role.id))
            session.commit()
        login(client)

        with database.session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.feishu_tenant_key == "tenant_allowed")
            )
            # Bootstrap accounts only materialize on their own login; for the
            # cross-user checks a plain active member row is enough.
            admin = _seed_member(session, tenant.id, "ou_admin", "合成管理员")
            worker = session.scalar(
                select(User).where(
                    User.tenant_id == tenant.id,
                    User.feishu_open_id == "ou_worker",
                )
            )
            admin_plan = _plan(
                tenant_id=tenant.id, user_id=admin.id,
                filename="管理员文件.xlsx", order_count=3, detail_count=4,
            )
            worker_plan = _plan(
                tenant_id=tenant.id, user_id=worker.id,
                filename="采购同事文件.xlsx", order_count=1, detail_count=2,
            )
            session.add_all([admin_plan, worker_plan])
            session.flush()
            admin_job = _job(
                tenant_id=tenant.id, plan_id=admin_plan.id, user_id=admin.id,
                state="completed",
                progress={"rowsTotal": 4, "rowsWritten": 4, "rowsExisting": 0,
                          "written": 4, "failed": 0},
                created_at=now - timedelta(hours=2),
                duration_sec=30,
            )
            worker_job = _job(
                tenant_id=tenant.id, plan_id=worker_plan.id, user_id=worker.id,
                state="completed",
                progress={"rowsTotal": 2, "rowsWritten": 2, "rowsExisting": 0,
                          "written": 2, "failed": 0},
                created_at=now - timedelta(hours=1),
                duration_sec=20,
            )
            session.add_all([admin_job, worker_job])
            session.commit()
            admin_id = str(admin.id)
            admin_job_id = str(admin_job.id)
            worker_job_id = str(worker_job.id)

        history = client.get("/v1/assistant/procurement-import/history")
        assert history.status_code == 200, history.text
        data = history.json()["data"]
        assert [item["jobId"] for item in data["items"]] == [worker_job_id]
        assert data["actors"] == []

        # A foreign job id must not work as a cursor either: the probe would
        # otherwise reveal which UUIDs exist inside the tenant.
        foreign_cursor = client.get(
            "/v1/assistant/procurement-import/history",
            params={"cursor": admin_job_id},
        )
        assert foreign_cursor.status_code == 422, foreign_cursor.text
        assert (
            foreign_cursor.json()["detail"]["code"]
            == "procurement_import_history_cursor_invalid"
        )

        forbidden_filter = client.get(
            "/v1/assistant/procurement-import/history",
            params={"userId": admin_id},
        )
        assert forbidden_filter.status_code == 403, forbidden_filter.text
        assert (
            forbidden_filter.json()["detail"]["code"]
            == "procurement_import_history_user_filter_forbidden"
        )

        forbidden_detail = client.get(
            f"/v1/assistant/procurement-import/history/{admin_job_id}"
        )
        assert forbidden_detail.status_code == 403, forbidden_detail.text
        assert (
            forbidden_detail.json()["detail"]["code"]
            == "procurement_import_history_forbidden"
        )


def test_cloud_web_workspace_ships_import_history_ui() -> None:
    """The cloud serves its own HTML copy; it must be synced before release."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    canonical = root / "src" / "purchase_tool" / "web" / "index.html"
    deployed = (
        root / "cloud" / "auth-service" / "src" / "xynigo_auth" / "web" / "index.html"
    )
    if not canonical.exists() or not deployed.exists():
        import pytest

        pytest.skip("web workspace sources are not available")
    for marker in ("importHistoryMask", "btnProcurementImportHistory"):
        assert marker in canonical.read_text(encoding="utf-8"), (
            f"{marker} missing from canonical web UI"
        )
        assert marker in deployed.read_text(encoding="utf-8"), (
            f"{marker} missing from cloud web copy; "
            "run cloud/auth-service/deploy/sync_web_workspace.py"
        )
