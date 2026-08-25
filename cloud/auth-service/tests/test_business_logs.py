from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app, start_local_login
from test_purchase_api import sample_draft
from xynigo_auth.business_log import (
    BUSINESS_EVENT_CATALOG,
    BusinessLogService,
    sanitize_log_value,
)
from xynigo_auth.main import utcnow
from xynigo_auth.models import (
    AuditEvent,
    Role,
    SessionRecord,
    Tenant,
    User,
    UserRole,
)
from xynigo_auth.security import hash_token
from xynigo_auth.purchase_service import PurchaseOrderService, PurchaseServiceError


def authenticated_admin(tmp_path):
    app, database, _oauth = build_test_app(tmp_path)
    client = TestClient(app)
    state, poll_token = start_local_login(client)
    callback = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "business-log-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    exchange = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
    assert exchange.status_code == 200
    token = exchange.json()["sessionToken"]
    return client, database, {"Authorization": f"Bearer {token}"}


def add_member_session(database) -> tuple[User, dict[str, str]]:
    raw_token = "m" * 64
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        assert tenant is not None
        member_role = session.scalar(
            select(Role).where(Role.tenant_id == tenant.id, Role.code == "member")
        )
        assert member_role is not None
        member = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_synthetic_member",
            display_name="合成采购员",
            status="active",
        )
        session.add(member)
        session.flush()
        session.add(UserRole(user_id=member.id, role_id=member_role.id))
        session.add(
            SessionRecord(
                user_id=member.id,
                token_hash=hash_token(raw_token),
                last_seen_at=utcnow(),
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        session.commit()
        session.refresh(member)
        return member, {"Authorization": f"Bearer {raw_token}"}


def test_procurement_p0_event_taxonomy_is_centralized() -> None:
    expected = {
        "purchase_order.submit",
        "purchase_order.lines.claim",
        "purchase_order.lines.return",
        "purchase_order.lines.reassign",
        "purchase_order.workspace.detail.read",
        "purchase_order.resource.bind",
        "purchase_order.environment.open",
        "purchase_order.order_plan.confirm",
        "purchase_order.split_plan.save",
        "purchase_order.order_plan.abandon",
        "purchase_order.payment.success",
        "purchase_order.payment.failure",
        "purchase_order.batch.create",
        "purchase_order.buyer_account.release",
        "purchase_order.environment.delete_abandoned",
        "purchase_order.paid_environment.order_lookup.open",
        "purchase_order.follow_up.update",
        "purchase_order.exception.handle",
        "purchase_order.platform_order_no.fill",
    }
    assert expected <= BUSINESS_EVENT_CATALOG.keys()
    assert all(
        BUSINESS_EVENT_CATALOG[action].module == "procurement" for action in expected
    )


def test_modules_do_not_construct_audit_events_directly() -> None:
    source_dir = Path(__file__).resolve().parents[1] / "src" / "xynigo_auth"
    offenders = []
    for path in source_dir.glob("*.py"):
        if path.name in {"business_log.py", "models.py"}:
            continue
        if "AuditEvent(" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


def test_log_sanitizer_redacts_credentials_contact_and_address_fields() -> None:
    value = sanitize_log_value(
        {
            "password": "never-store-this",
            "cookie": "session=never-store-this",
            "apiKey": "never-store-this",
            "recipientPhone": "+1 555 0100",
            "addressLine1": "100 Example Street",
            "buyer": "buyer@example.test",
            "purchaseLink": "https://example.test/item?token=never-store-this",
            "safe": "kept",
        }
    )
    rendered = json.dumps(value, ensure_ascii=False)
    assert "never-store-this" not in rendered
    assert "+1 555" not in rendered
    assert "100 Example" not in rendered
    assert "buyer@example.test" not in rendered
    assert "?token=" not in rendered
    assert value["safe"] == "kept"


def test_submit_records_structured_log_and_supports_list_detail_filters(tmp_path) -> None:
    client, database, headers = authenticated_admin(tmp_path)
    headers = {
        **headers,
        "X-Request-ID": "request-business-log-000000000001",
        "X-Trace-ID": "trace-business-log-0000000000001",
        "X-Xynigo-Source": "local_workspace",
        "X-Xynigo-Client-Version": "0.12.0-test",
    }
    response = client.post(
        "/v1/purchase-orders/submit",
        json=sample_draft(),
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == headers["X-Request-ID"]
    assert response.headers["X-Trace-ID"] == headers["X-Trace-ID"]

    listing = client.get(
        "/v1/business-logs",
        params={
            "module": "procurement",
            "result": "success",
            "businessNo": "GSHDEMO",
            "operationType": "purchase_order.submit",
            "requestId": "request-business-log",
        },
        headers=headers,
    )
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert data["scope"] == "tenant"
    assert data["total"] == 1
    item = data["items"][0]
    assert item["tenantId"]
    assert item["operator"]["name"] == "合成测试用户"
    assert item["operator"]["roles"] == ["super_admin"]
    assert item["module"] == "procurement"
    assert item["operationType"] == "purchase_order.submit"
    assert item["businessObject"]["number"] == "GSHDEMO20260825"
    assert item["source"] == "local_workspace"
    assert item["clientVersion"] == "0.12.0-test"
    assert item["requestId"] == headers["X-Request-ID"]
    assert item["traceId"] == headers["X-Trace-ID"]

    detail = client.get(
        f"/v1/business-logs/{item['businessLogId']}", headers=headers
    )
    assert detail.status_code == 200
    detail_text = detail.text.casefold()
    assert "+1 555 0100" not in detail_text
    assert "100 example street" not in detail_text
    assert "cookie" not in detail_text
    client.close()


def test_business_log_scope_is_self_for_member_and_tenant_for_auditor(tmp_path) -> None:
    client, database, admin_headers = authenticated_admin(tmp_path)
    member, member_headers = add_member_session(database)
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        admin = session.scalar(select(User).where(User.display_name == "合成测试用户"))
        assert tenant is not None and admin is not None
        other_tenant = Tenant(
            feishu_tenant_key="tenant-log-other", name="Other", status="active"
        )
        session.add(other_tenant)
        session.flush()
        service = BusinessLogService(session)
        admin_event = service.append(
            tenant_id=tenant.id,
            actor_user_id=admin.id,
            action="purchase_order.submit",
            result="success",
            request_id="request-admin-00000001",
            business_object_no="GSH-ADMIN",
        )
        service.append(
            tenant_id=tenant.id,
            actor_user_id=member.id,
            action="purchase_order.lines.claim",
            result="success",
            request_id="request-member-0000001",
            business_object_no="GSH-MEMBER",
        )
        service.append(
            tenant_id=other_tenant.id,
            actor_user_id=None,
            action="purchase_order.submit",
            result="success",
            request_id="request-other-00000001",
            business_object_no="GSH-OTHER",
        )
        session.commit()
        admin_event_id = admin_event.id

    member_listing = client.get("/v1/business-logs", headers=member_headers)
    assert member_listing.status_code == 200
    member_data = member_listing.json()["data"]
    assert member_data["scope"] == "self"
    assert [item["businessObject"]["number"] for item in member_data["items"]] == [
        "GSH-MEMBER"
    ]
    hidden = client.get(
        f"/v1/business-logs/{admin_event_id}", headers=member_headers
    )
    assert hidden.status_code == 404

    admin_listing = client.get("/v1/business-logs", headers=admin_headers)
    assert admin_listing.status_code == 200
    numbers = {
        item["businessObject"]["number"]
        for item in admin_listing.json()["data"]["items"]
    }
    assert numbers == {"GSH-ADMIN", "GSH-MEMBER"}
    assert "GSH-OTHER" not in numbers
    client.close()


def test_validation_and_permission_denial_have_explicit_outcomes(tmp_path) -> None:
    client, database, admin_headers = authenticated_admin(tmp_path)
    _member, member_headers = add_member_session(database)
    invalid = sample_draft()
    invalid["password"] = "must-never-appear-in-log"
    validation = client.post(
        "/v1/purchase-orders/submit", json=invalid, headers=admin_headers
    )
    assert validation.status_code == 422
    assert validation.json()["code"] == "validation_failed"
    assert "must-never-appear-in-log" not in validation.text

    denied = client.post(
        "/v1/purchase-orders/submit",
        json=sample_draft(),
        headers=member_headers,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"

    with database.session_factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "purchase_order.submit")
                .order_by(AuditEvent.created_at)
            )
        )
        outcomes = {event.outcome for event in events}
        assert "validation_failed" in outcomes
        assert "permission_denied" in outcomes
        rendered = json.dumps(
            [event.details for event in events], ensure_ascii=False
        )
        assert "must-never-appear-in-log" not in rendered
        denied_event = next(event for event in events if event.outcome == "permission_denied")
        assert denied_event.failure_reason == "permission_denied"
        assert denied_event.actor_name == "合成采购员"
    client.close()


def test_external_service_failure_has_separate_outcome(tmp_path, monkeypatch) -> None:
    client, database, headers = authenticated_admin(tmp_path)

    def fail_submit(service, *_args, **_kwargs):
        service.session.add(
            Tenant(
                feishu_tenant_key="must-rollback-with-business-failure",
                name="Must Roll Back",
                status="active",
            )
        )
        service.session.flush()
        raise PurchaseServiceError(
            "purchase_external_service_unavailable",
            "合成外部服务失败",
            502,
        )

    monkeypatch.setattr(PurchaseOrderService, "submit", fail_submit)
    response = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert response.status_code == 502
    with database.session_factory() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "purchase_order.submit",
                AuditEvent.outcome == "external_service_failed",
            )
        )
        assert event is not None
        assert event.result == "failure"
        partial_business_write = session.scalar(
            select(Tenant).where(
                Tenant.feishu_tenant_key == "must-rollback-with-business-failure"
            )
        )
        assert partial_business_write is None
        assert event.failure_reason == "purchase_external_service_unavailable"
    client.close()
