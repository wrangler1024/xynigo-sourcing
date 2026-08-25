from __future__ import annotations

import json
import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from test_business_logs import add_member_session, authenticated_admin
from xynigo_auth.main import utcnow
from xynigo_auth.models import SystemLogEvent, Tenant
from xynigo_auth.purchase_service import PurchaseOrderService
from xynigo_auth.system_log import (
    SYSTEM_ERROR_CATEGORY,
    SYSTEM_RUNTIME_CATEGORY,
    SystemLogService,
    should_capture_http,
)


def test_system_log_service_redacts_and_retention_is_bounded(tmp_path) -> None:
    _client, database, _headers = authenticated_admin(tmp_path)
    now = utcnow()
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        assert tenant is not None
        session.execute(delete(SystemLogEvent))
        service = SystemLogService(session)
        expired = service.append(
            tenant_id=tenant.id,
            actor_user_id=None,
            actor_name=None,
            category=SYSTEM_ERROR_CATEGORY,
            level="error",
            service="auth_service",
            component="http_api",
            environment="test",
            event_type="http.request.failed",
            message="token=must-never-appear",
            request_id="request-expired-00000001",
            trace_id="trace-expired-0000000001",
            retention_days=1,
            route="/v1/example?token=must-never-appear",
            status_code=500,
            exception_type="SyntheticError",
            error_code="synthetic_failure",
            details={
                "password": "must-never-appear",
                "address": "100 Example Street",
                "safeCount": 2,
            },
            operation_time=now - timedelta(days=2),
        )
        current_events = []
        for index in range(3):
            current_events.append(
                service.append(
                    tenant_id=tenant.id,
                    actor_user_id=None,
                    actor_name=None,
                    category=SYSTEM_RUNTIME_CATEGORY,
                    level="info",
                    service="auth_service",
                    component="worker",
                    environment="test",
                    event_type="worker.heartbeat",
                    message="Worker heartbeat",
                    request_id=f"request-current-000000{index}",
                    trace_id=f"trace-current-00000000{index}",
                    retention_days=30,
                    operation_time=now - timedelta(minutes=3 - index),
                )
            )
        session.flush()
        rendered = json.dumps(
            SystemLogService.serialize(expired, include_details=True),
            ensure_ascii=False,
        )
        assert "must-never-appear" not in rendered
        assert "100 Example Street" not in rendered
        assert expired.route == "/v1/example"

        deleted = service.enforce_retention(
            retention_days=30,
            max_rows_per_tenant=2,
            now=now,
        )
        session.commit()
        assert deleted == {"expired": 1, "capacity": 1}
        assert session.scalar(
            select(func.count(SystemLogEvent.id)).where(
                SystemLogEvent.tenant_id == tenant.id
            )
        ) == 2
        assert session.get(SystemLogEvent, current_events[0].id) is None


def test_http_sampling_keeps_writes_and_failures_and_excludes_log_queries() -> None:
    common = {
        "route": "/v1/procurement/orders",
        "trace_id": "trace-sampling-00000001",
        "runtime_sample_rate": 0.0,
    }
    assert not should_capture_http(method="GET", status_code=200, **common)
    assert should_capture_http(method="POST", status_code=200, **common)
    assert should_capture_http(method="GET", status_code=404, **common)
    assert not should_capture_http(
        method="GET",
        route="/v1/system-logs",
        status_code=500,
        trace_id="trace-sampling-00000001",
        runtime_sample_rate=1.0,
    )


def test_middleware_records_tenant_scoped_runtime_log_and_api_filters(tmp_path) -> None:
    client, database, headers = authenticated_admin(tmp_path)
    request_headers = {
        **headers,
        "X-Request-ID": "request-system-log-00000000001",
        "X-Trace-ID": "trace-system-log-000000000001",
        "X-Xynigo-Source": "local_workspace",
        "X-Xynigo-Client-Version": "0.11.1-test",
    }
    missing_order_id = uuid.uuid4()
    response = client.get(
        f"/v1/procurement/orders/{missing_order_id}",
        headers=request_headers,
    )
    assert response.status_code == 404

    listing = client.get(
        "/v1/system-logs",
        params={
            "category": "system_runtime",
            "level": "warning",
            "service": "auth_service",
            "component": "http_api",
            "eventType": "http.request.completed",
            "statusCode": 404,
            "requestId": "trace-system-log",
            "keyword": "http_404",
        },
        headers=headers,
    )
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert data["scope"] == "tenant"
    assert data["retentionDays"] == 30
    assert data["maxRowsPerTenant"] == 100_000
    assert data["total"] == 1
    item = data["items"][0]
    assert item["category"] == "system_runtime"
    assert item["level"] == "warning"
    assert item["http"] == {
        "method": "GET",
        "route": "/v1/procurement/orders/{purchase_order_id}",
        "statusCode": 404,
        "durationMs": item["http"]["durationMs"],
    }
    assert item["actor"]["name"] == "合成测试用户"
    assert item["source"] == "local_workspace"
    assert item["clientVersion"] == "0.11.1-test"
    assert item["requestId"] == request_headers["X-Request-ID"]
    assert item["traceId"] == request_headers["X-Trace-ID"]

    detail = client.get(
        f"/v1/system-logs/{item['systemLogId']}", headers=headers
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["details"] == {"handled": True}
    client.close()


def test_system_log_read_requires_permission_and_never_crosses_tenant(tmp_path) -> None:
    client, database, admin_headers = authenticated_admin(tmp_path)
    _member, member_headers = add_member_session(database)
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        assert tenant is not None
        other_tenant = Tenant(
            feishu_tenant_key="tenant-system-log-other",
            name="Other",
            status="active",
        )
        session.add(other_tenant)
        session.flush()
        event = SystemLogService(session).append(
            tenant_id=other_tenant.id,
            actor_user_id=None,
            actor_name=None,
            category=SYSTEM_ERROR_CATEGORY,
            level="error",
            service="auth_service",
            component="worker",
            environment="test",
            event_type="worker.failed",
            message="Worker failed",
            request_id="request-other-system-00001",
            trace_id="trace-other-system-0000001",
            retention_days=30,
        )
        session.commit()
        other_event_id = event.id

    denied = client.get("/v1/system-logs", headers=member_headers)
    assert denied.status_code == 403
    listing = client.get("/v1/system-logs", headers=admin_headers)
    assert listing.status_code == 200
    assert all(
        item["tenantId"] != str(other_tenant.id)
        for item in listing.json()["data"]["items"]
    )
    hidden = client.get(
        f"/v1/system-logs/{other_event_id}", headers=admin_headers
    )
    assert hidden.status_code == 404
    client.close()


def test_unhandled_exception_records_safe_error_without_stack_or_message(
    tmp_path, monkeypatch
) -> None:
    client, database, headers = authenticated_admin(tmp_path)

    def explode(_service, **_kwargs):
        raise RuntimeError("password=must-never-appear")

    monkeypatch.setattr(PurchaseOrderService, "workspace_overview", explode)
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    response = failing_client.get("/v1/procurement/overview", headers=headers)
    assert response.status_code == 500
    with database.session_factory() as session:
        event = session.scalar(
            select(SystemLogEvent).where(
                SystemLogEvent.category == SYSTEM_ERROR_CATEGORY,
                SystemLogEvent.event_type == "http.request.failed",
            )
        )
        assert event is not None
        serialized = json.dumps(
            SystemLogService.serialize(event, include_details=True),
            ensure_ascii=False,
        )
        assert "must-never-appear" not in serialized
        assert "password=" not in serialized
        assert "traceback" not in serialized.casefold()
        assert event.error_code == "unhandled_exception"
        assert event.status_code == 500
        assert event.tenant_id is not None
    failing_client.close()
    client.close()
