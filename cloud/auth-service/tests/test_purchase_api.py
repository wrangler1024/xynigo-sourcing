from __future__ import annotations

import uuid
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_auth_flow import build_test_app, start_local_login
from xynigo_auth.models import (
    AuditEvent,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseSplit,
    PurchaseSplitLine,
    PurchaseSyncOutbox,
    Role,
    Tenant,
    User,
    UserRole,
)
from xynigo_auth.purchase_service import PurchaseOrderService, PurchaseServiceError


def authenticated_client(tmp_path):
    app, database, _oauth = build_test_app(tmp_path)
    client = TestClient(app)
    state, poll_token = start_local_login(client)
    callback = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "purchase-api-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    exchange = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
    assert exchange.status_code == 200
    token = exchange.json()["sessionToken"]
    return client, database, {"Authorization": f"Bearer {token}"}


def sample_draft() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "mode": "xynigo-extension",
        "orderKey": "test store|GSHDEMO20260825|XMWUDEMO20260825",
        "packageId": "XMWUDEMO20260825",
        "platformOrderNo": "GSHDEMO20260825",
        "storeName": "Test Store",
        "site": "US",
        "salesCurrency": "USD",
        "salesAmount": 50.0,
        "dianxiaomiOrderTime": "2026-08-25 10:00:00",
        "recipientName": "Synthetic Recipient",
        "recipientPhone": "+1 555 0100",
        "addressLine1": "100 Example Street",
        "addressLine2": "Unit 2",
        "city": "Example City",
        "stateProvince": "Example State",
        "postalCode": "00001-0001",
        "items": [
            {
                "lineNo": 1,
                "sellerSku": "DEMO-12345678",
                "variant": "Black / M",
                "productImageUrl": "https://img.ltwebstatic.com/images3_pi/demo.jpg",
                "mainSpec": "Black",
                "subSpec": "M",
                "originalPrice": 20.0,
                "couponType": "无优惠券",
                "guidePrice": 18.0,
                "purchaseCurrency": "USD",
                "salesQty": 1,
                "purchaseQty": 1,
                "source": "dianxiaomi-order",
                "purchaseLink": (
                    "https://us.shein.com/demo-p-123.html"
                    "?goods_id=12345678&skucode=SKU123"
                ),
                "goodsId": "12345678",
                "skuCode": "SKU123",
                "mainAttr": "Black",
                "mallCode": "US",
            }
        ],
        "guideTotalsByCurrency": {"USD": 18.0},
        "estimatedMetrics": None,
        "remarkText": "",
        "remarkStatus": "not-generated",
        "purchaseStatus": "draft-local",
        "submissionStatus": "draft",
        "createdAt": "2026-08-25T10:00:00+08:00",
        "updatedAt": "2026-08-25T10:00:00+08:00",
    }


def test_draft_is_tenant_scoped_idempotent_and_queued_for_sync(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()

    first = client.post("/v1/purchase-orders/draft", json=draft, headers=headers)
    assert first.status_code == 200
    first_data = first.json()["data"]
    assert first_data["submissionStatus"] == "draft"
    assert first_data["draftRevision"] == 1
    assert first_data["syncStatus"] == "pending"
    assert first_data["unchanged"] is False

    retry = deepcopy(draft)
    retry["updatedAt"] = "2026-08-25T10:05:00+08:00"
    second = client.post("/v1/purchase-orders/draft", json=retry, headers=headers)
    assert second.status_code == 200
    assert second.json()["data"]["draftRevision"] == 1
    assert second.json()["data"]["unchanged"] is True

    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        assert order is not None
        assert order.order_key == draft["orderKey"]
        assert order.submission_status == "draft"
        assert session.scalar(select(func.count(PurchaseOrderLine.id))) == 1
        assert session.scalar(select(func.count(PurchaseSyncOutbox.id))) == 1
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert [event.action for event in events].count("purchase_order.draft.save") == 2
        assert all("recipient" not in str(event.details).casefold() for event in events)
        other_tenant = Tenant(
            feishu_tenant_key="tenant-other",
            name="Other Tenant",
            status="active",
        )
        session.add(other_tenant)
        session.flush()
        with pytest.raises(PurchaseServiceError) as caught:
            PurchaseOrderService(session).get(
                tenant_id=other_tenant.id,
                order_key=str(draft["orderKey"]),
            )
        assert caught.value.code == "purchase_order_not_found"
    client.close()


def test_submit_sets_authenticated_actor_and_is_idempotent(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()

    submitted = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert submitted.status_code == 200
    data = submitted.json()["data"]
    assert data["submissionStatus"] == "submitted"
    assert data["submittedAt"]
    assert data["submittedBy"]["name"] == "合成测试用户"
    assert "open_id" not in submitted.text.casefold()
    assert "ou_admin" not in submitted.text

    retry = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert retry.status_code == 200
    assert retry.json()["data"]["unchanged"] is True

    changed = deepcopy(draft)
    changed["items"][0]["guidePrice"] = 19.0  # type: ignore[index]
    changed["guideTotalsByCurrency"] = {"USD": 19.0}
    conflict = client.post("/v1/purchase-orders/submit", json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "purchase_order_locked"

    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        assert order is not None
        user = session.get(User, order.submitted_by_user_id)
        assert user is not None
        assert user.display_name == "合成测试用户"
        line = session.scalar(select(PurchaseOrderLine))
        assert line is not None
        assert line.workflow_status == "unclaimed"
        assert session.scalar(select(func.count(PurchaseSyncOutbox.id))) == 1
    client.close()


def test_procurement_workspace_overview_list_and_detail(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    submitted_draft = sample_draft()
    submitted = client.post(
        "/v1/purchase-orders/submit",
        json=submitted_draft,
        headers=headers,
    )
    assert submitted.status_code == 200
    submitted_id = submitted.json()["data"]["purchaseOrderId"]

    saved_draft = deepcopy(submitted_draft)
    saved_draft.update(
        {
            "orderKey": "draft store|GSHDRAFT20260825|XMWUDRAFT20260825",
            "packageId": "XMWUDRAFT20260825",
            "platformOrderNo": "GSHDRAFT20260825",
            "storeName": "Draft Store",
            "recipientName": "Private Draft Recipient",
        }
    )
    saved = client.post(
        "/v1/purchase-orders/draft",
        json=saved_draft,
        headers=headers,
    )
    assert saved.status_code == 200

    overview = client.get("/v1/procurement/overview", headers=headers)
    assert overview.status_code == 200
    overview_data = overview.json()["data"]
    assert overview_data["orders"]["total"] == 2
    assert overview_data["orders"]["bySubmissionStatus"] == {
        "draft": 1,
        "submitted": 1,
    }
    assert overview_data["orders"]["bySyncStatus"]["pending"] == 2
    assert overview_data["lines"]["total"] == 2
    assert overview_data["lines"]["byWorkflowStatus"]["draft"] == 1
    assert overview_data["lines"]["byWorkflowStatus"]["unclaimed"] == 1

    listed = client.get(
        "/v1/procurement/orders",
        params={
            "submissionStatus": "submitted",
            "workflowStatus": "unclaimed",
            "syncStatus": "pending",
            "page": 1,
            "pageSize": 1,
        },
        headers=headers,
    )
    assert listed.status_code == 200
    list_data = listed.json()["data"]
    assert list_data["page"] == 1
    assert list_data["pageSize"] == 1
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    summary = list_data["items"][0]
    assert summary["purchaseOrderId"] == submitted_id
    assert summary["packageId"] == "XMWUDEMO20260825"
    assert summary["itemCount"] == 1
    assert summary["previewImages"] == [
        "https://img.ltwebstatic.com/images3_pi/demo.jpg"
    ]
    assert summary["workflowCounts"]["unclaimed"] == 1
    assert "recipientName" not in listed.text
    assert "Synthetic Recipient" not in listed.text
    assert "recipientPhone" not in listed.text
    assert "100 Example Street" not in listed.text

    detail = client.get(
        f"/v1/procurement/orders/{submitted_id}",
        headers=headers,
    )
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert detail_data["draft"]["recipientName"] == "Synthetic Recipient"
    assert "items" not in detail_data["draft"]
    assert detail_data["lines"][0]["workflowStatus"] == "unclaimed"
    assert detail_data["lines"][0]["payload"]["sellerSku"] == "DEMO-12345678"
    assert detail_data["lines"][0]["purchaseOrderLineId"]
    with database.session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "purchase_order.workspace.detail.read"
            )
        )
        assert audit is not None
        assert audit.details == {"purchaseOrderId": submitted_id}
        assert "recipient" not in str(audit.details).casefold()
    client.close()


def test_procurement_workspace_validates_filters_and_hides_cross_tenant_orders(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    created = client.post(
        "/v1/purchase-orders/submit",
        json=sample_draft(),
        headers=headers,
    )
    assert created.status_code == 200
    purchase_order_id = created.json()["data"]["purchaseOrderId"]

    invalid_status = client.get(
        "/v1/procurement/orders",
        params={"workflowStatus": "unknown"},
        headers=headers,
    )
    assert invalid_status.status_code == 422
    invalid_page_size = client.get(
        "/v1/procurement/orders",
        params={"pageSize": 301},
        headers=headers,
    )
    assert invalid_page_size.status_code == 422
    maximum_page_size = client.get(
        "/v1/procurement/orders",
        params={"pageSize": 300},
        headers=headers,
    )
    assert maximum_page_size.status_code == 200
    assert maximum_page_size.json()["data"]["pageSize"] == 300

    with database.session_factory() as session:
        other_tenant = Tenant(
            feishu_tenant_key="tenant-workspace-other",
            name="Workspace Other Tenant",
            status="active",
        )
        session.add(other_tenant)
        session.flush()
        service = PurchaseOrderService(session)
        assert service.workspace_overview(tenant_id=other_tenant.id)["orders"]["total"] == 0
        assert service.workspace_list(tenant_id=other_tenant.id)["total"] == 0
        with pytest.raises(PurchaseServiceError) as caught:
            service.workspace_detail(
                tenant_id=other_tenant.id,
                purchase_order_id=uuid.UUID(purchase_order_id),
            )
        assert caught.value.code == "purchase_order_not_found"
    client.close()


def test_procurement_claim_split_and_execution_queue_use_persisted_test_data(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()
    draft["items"][0]["salesQty"] = 3  # type: ignore[index]
    draft["items"][0]["purchaseQty"] = 3  # type: ignore[index]
    draft["guideTotalsByCurrency"] = {"USD": 54.0}
    submitted = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert submitted.status_code == 200
    purchase_order_id = submitted.json()["data"]["purchaseOrderId"]

    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    line_id = detail["lines"][0]["purchaseOrderLineId"]
    assert detail["executionRevision"] == 0
    assert detail["lines"][0]["claimedBy"] is None

    claimed = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderLineIds": [line_id]},
        headers=headers,
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["claimedCount"] == 1
    assert claimed.json()["data"]["claimant"]["name"] == "合成测试用户"
    retry = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderLineIds": [line_id]},
        headers=headers,
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["unchangedCount"] == 1

    plan = {
        "expectedRevision": 0,
        "groups": [
            {
                "clientKey": "group-a",
                "resource": {
                    "hubEnvironmentRef": "synthetic-us-1001",
                    "hubEnvironmentName": "US-PUR-1001",
                    "buyerAccountRef": "synthetic-buyer-01",
                    "buyerAccountLabel": "美国买家号 ····01",
                    "site": "US",
                },
                "note": "合成测试资源",
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
            },
            {
                "clientKey": "group-b",
                "resource": None,
                "note": "等待资源绑定",
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 2}],
            },
        ],
    }
    saved = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/splits",
        json=plan,
        headers=headers,
    )
    assert saved.status_code == 200
    saved_data = saved.json()["data"]
    assert saved_data["executionRevision"] == 1
    assert saved_data["splitCount"] == 2
    assert {item["status"] for item in saved_data["items"]} == {
        "waiting_binding",
        "waiting_order",
    }
    assert sum(
        line["qty"]
        for item in saved_data["items"]
        for line in item["lines"]
    ) == 3

    queue = client.get(
        "/v1/procurement/execution/splits",
        params={"keyword": "GSHDEMO20260825", "pageSize": 100},
        headers=headers,
    )
    assert queue.status_code == 200
    queue_data = queue.json()["data"]
    assert queue_data["total"] == 2
    assert all(item["salesOrderNo"] == "GSHDEMO20260825" for item in queue_data["items"])
    assert all("@" not in str(item.get("buyer") or "") for item in queue_data["items"])
    bound = client.get(
        "/v1/procurement/execution/splits",
        params={"binding": "bound"},
        headers=headers,
    )
    assert bound.status_code == 200
    assert bound.json()["data"]["total"] == 1
    assert bound.json()["data"]["items"][0]["hub"] == "US-PUR-1001"

    stale = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/splits",
        json=plan,
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "purchase_execution_revision_conflict"

    refreshed_detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    assert refreshed_detail["executionRevision"] == 1
    assert refreshed_detail["lines"][0]["claimedBy"]["name"] == "合成测试用户"
    with database.session_factory() as session:
        assert session.scalar(select(func.count(PurchaseSplit.id))) == 2
        assert session.scalar(select(func.count(PurchaseSplitLine.id))) == 2
        audits = list(session.scalars(select(AuditEvent)))
        assert any(event.action == "purchase_order.lines.claim" for event in audits)
        assert any(event.action == "purchase_order.split_plan.save" for event in audits)
        assert all("buyerAccountRef" not in str(event.details) for event in audits)
    client.close()


def test_procurement_split_plan_rejects_wrong_quantity_and_unsafe_resource_label(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    submitted = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    purchase_order_id = submitted.json()["data"]["purchaseOrderId"]
    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    line_id = detail["lines"][0]["purchaseOrderLineId"]
    assert client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderIds": [purchase_order_id]},
        headers=headers,
    ).status_code == 200

    invalid_quantity = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/splits",
        json={
            "expectedRevision": 0,
            "groups": [{
                "clientKey": "only",
                "resource": None,
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 2}],
            }],
        },
        headers=headers,
    )
    assert invalid_quantity.status_code == 422
    assert invalid_quantity.json()["detail"]["code"] == "purchase_split_quantity_mismatch"

    unsafe_label = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/splits",
        json={
            "expectedRevision": 0,
            "groups": [{
                "clientKey": "only",
                "resource": {
                    "hubEnvironmentRef": "synthetic-us-1001",
                    "hubEnvironmentName": "US-PUR-1001",
                    "buyerAccountRef": "synthetic-buyer-01",
                    "buyerAccountLabel": "buyer@example.test",
                    "site": "US",
                },
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
            }],
        },
        headers=headers,
    )
    assert unsafe_label.status_code == 422
    with database.session_factory() as session:
        assert session.scalar(select(PurchaseSplit)) is None
    client.close()


def test_submit_rejects_incomplete_order_before_persisting(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()
    draft["recipientPhone"] = ""
    draft["items"][0]["purchaseQty"] = ""  # type: ignore[index]

    response = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "purchase_submit_invalid"
    with database.session_factory() as session:
        assert session.scalar(select(PurchaseOrder)) is None
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "purchase_order.submit")
        )
        assert audit is not None
        assert audit.details == {"reason": "purchase_submit_invalid"}
    client.close()


def test_submit_rejects_forged_link_identifiers(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()
    draft["items"][0]["goodsId"] = "87654321"  # type: ignore[index]

    response = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "purchase_submit_invalid"
    with database.session_factory() as session:
        assert session.scalar(select(PurchaseOrder)) is None
    client.close()


def test_purchase_routes_require_explicit_permission(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    with database.session_factory() as session:
        user = session.scalar(select(User))
        member = session.scalar(select(Role).where(Role.code == "member"))
        assert user is not None and member is not None
        session.query(UserRole).filter(UserRole.user_id == user.id).delete()
        session.add(UserRole(user_id=user.id, role_id=member.id))
        session.commit()

    response = client.post("/v1/purchase-orders/draft", json=sample_draft(), headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "permission_denied"
    for path in (
        "/v1/procurement/overview",
        "/v1/procurement/orders",
        "/v1/procurement/orders/00000000-0000-0000-0000-000000000001",
    ):
        workspace_response = client.get(path, headers=headers)
        assert workspace_response.status_code == 403
        assert workspace_response.json()["detail"]["code"] == "permission_denied"
    claim_response = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderIds": ["00000000-0000-0000-0000-000000000001"]},
        headers=headers,
    )
    assert claim_response.status_code == 403
    assert claim_response.json()["detail"]["code"] == "permission_denied"
    split_response = client.post(
        "/v1/procurement/orders/00000000-0000-0000-0000-000000000001/splits",
        json={
            "expectedRevision": 0,
            "groups": [{
                "clientKey": "only",
                "resource": None,
                "lines": [{
                    "purchaseOrderLineId": "00000000-0000-0000-0000-000000000002",
                    "quantity": 1,
                }],
            }],
        },
        headers=headers,
    )
    assert split_response.status_code == 403
    assert split_response.json()["detail"]["code"] == "permission_denied"
    with database.session_factory() as session:
        assert session.scalar(select(PurchaseOrder)) is None
    client.close()
