from __future__ import annotations

import uuid
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_auth_flow import build_test_app, start_local_login
from xynigo_auth.models import (
    AuditEvent,
    BuyerAccount,
    CheckoutAttempt,
    PurchaseBatch,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseSplit,
    PurchaseSplitLine,
    PurchaseSyncOutbox,
    Role,
    SupplierShipment,
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
        "schemaVersion": 2,
        "mode": "xynigo-extension",
        "orderKey": "蓝天-周远超（一组）|GSHDEMO20260825|XMWUDEMO20260825",
        "packageId": "XMWUDEMO20260825",
        "platformOrderNo": "GSHDEMO20260825",
        "storeName": "蓝天-周远超（一组）",
        "storeBaseName": "蓝天",
        "operatorName": "周远超",
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
        "estimatedMetrics": {
            "ok": True,
            "currency": "MXN",
            "salesAmount": 216.71,
            "guideTotal": 100.0,
            "estimatedCost": 100.0,
            "estimatedProfit": 116.71,
            "profitMargin": 53.86,
            "roi": 116.71,
            "minimumApplied": False,
            "costBasis": "synthetic-test",
        },
        "remarkText": "",
        "remarkStatus": "not-generated",
        "purchaseStatus": "draft-local",
        "submissionStatus": "draft",
        "createdAt": "2026-08-25T10:00:00+08:00",
        "updatedAt": "2026-08-25T10:00:00+08:00",
    }


def claimed_purchase_order(
    client: TestClient,
    headers: dict[str, str],
    *,
    quantity: int,
    suffix: str = "",
) -> tuple[str, str]:
    draft = sample_draft()
    if suffix:
        normalized = suffix.upper()
        draft.update({
            "orderKey": f"蓝天-周远超（一组）|GSH{normalized}|XMWU{normalized}",
            "packageId": f"XMWU{normalized}",
            "platformOrderNo": f"GSH{normalized}",
        })
    draft["items"][0]["salesQty"] = quantity  # type: ignore[index]
    draft["items"][0]["purchaseQty"] = quantity  # type: ignore[index]
    draft["guideTotalsByCurrency"] = {"USD": 18.0 * quantity}
    submitted = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert submitted.status_code == 200
    purchase_order_id = submitted.json()["data"]["purchaseOrderId"]
    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    )
    assert detail.status_code == 200
    line_id = detail.json()["data"]["lines"][0]["purchaseOrderLineId"]
    claimed = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderLineIds": [line_id]},
        headers=headers,
    )
    assert claimed.status_code == 200
    return purchase_order_id, line_id


def synthetic_resource(
    client: TestClient,
    headers: dict[str, str],
    *,
    suffix: str = "2001",
) -> dict[str, str]:
    synced = client.put(
        "/v1/resources/buyer-accounts/snapshot",
            json={
                "source": "legacy_feishu_migration",
                "snapshotKey": f"synthetic-snapshot-{suffix}",
            "accounts": [
                {
                    "accountRef": f"synthetic-buyer-{suffix}",
                    "displayLabel": f"美国买家号 ····{suffix}",
                    "site": "US",
                    "availabilityStatus": "available",
                    "credentialStatus": "ready",
                    "sourceStatus": "合成可用",
                }
            ],
        },
        headers=headers,
    )
    assert synced.status_code == 200
    listed = client.get(
        "/v1/resources/buyer-accounts",
        params={"keyword": f"synthetic-buyer-{suffix}"},
        headers=headers,
    )
    assert listed.status_code == 200
    account = listed.json()["data"]["rows"][0]
    return {
        "hubEnvironmentRef": f"synthetic-us-{suffix}",
        "hubEnvironmentName": f"US-PUR-{suffix}",
        "buyerAccountId": account["accountId"],
        "site": "US",
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
        assert order.store_name == "蓝天-周远超（一组）"
        assert order.store_base_name == "蓝天"
        assert order.operator_name == "周远超"
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


def test_schema_v1_remains_compatible_without_operator_fields(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    draft = sample_draft()
    draft.update(
        {
            "schemaVersion": 1,
            "orderKey": "legacy store|GSHLEGACY20260825|XMWULEGACY20260825",
            "packageId": "XMWULEGACY20260825",
            "platformOrderNo": "GSHLEGACY20260825",
            "storeName": "Legacy Store",
        }
    )
    draft.pop("storeBaseName")
    draft.pop("operatorName")

    submitted = client.post("/v1/purchase-orders/submit", json=draft, headers=headers)
    assert submitted.status_code == 200
    assert "storeBaseName" not in submitted.json()["data"]["draft"]
    assert "operatorName" not in submitted.json()["data"]["draft"]

    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        assert order is not None
        assert order.store_name == "Legacy Store"
        assert order.store_base_name == "Legacy Store"
        assert order.operator_name is None
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
    revised = client.post("/v1/purchase-orders/submit", json=changed, headers=headers)
    assert revised.status_code == 200
    assert revised.json()["data"]["unchanged"] is False
    assert revised.json()["data"]["revised"] is True
    assert revised.json()["data"]["draftRevision"] == 2

    revised_retry = client.post(
        "/v1/purchase-orders/submit", json=changed, headers=headers
    )
    assert revised_retry.status_code == 200
    assert revised_retry.json()["data"]["unchanged"] is True
    assert revised_retry.json()["data"]["revised"] is False

    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        assert order is not None
        user = session.get(User, order.submitted_by_user_id)
        assert user is not None
        assert user.display_name == "合成测试用户"
        line = session.scalar(select(PurchaseOrderLine))
        assert line is not None
        assert line.workflow_status == "unclaimed"
        assert line.payload["guidePrice"] == 19.0
        assert session.scalar(select(func.count(PurchaseSyncOutbox.id))) == 2
    client.close()


def test_submit_rejects_revision_after_procurement_claim(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    purchase_order_id, _line_id = claimed_purchase_order(
        client, headers, quantity=1
    )
    changed = sample_draft()
    changed["items"][0]["guidePrice"] = 19.0  # type: ignore[index]
    changed["guideTotalsByCurrency"] = {"USD": 19.0}

    conflict = client.post(
        "/v1/purchase-orders/submit", json=changed, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "purchase_order_in_progress"

    with database.session_factory() as session:
        order = session.get(PurchaseOrder, uuid.UUID(purchase_order_id))
        assert order is not None
        assert order.draft_revision == 1
        line = session.scalar(
            select(PurchaseOrderLine).where(
                PurchaseOrderLine.purchase_order_id == order.id,
                PurchaseOrderLine.is_active.is_(True),
            )
        )
        assert line is not None
        assert line.workflow_status == "claimed"
        assert line.payload["guidePrice"] == 18.0
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
            "orderKey": "草原-运营乙（二组）|GSHDRAFT20260825|XMWUDRAFT20260825",
            "packageId": "XMWUDRAFT20260825",
            "platformOrderNo": "GSHDRAFT20260825",
            "storeName": "草原-运营乙（二组）",
            "storeBaseName": "草原",
            "operatorName": "运营乙",
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
    assert overview_data["orders"]["byTaskScope"] == {
        "unclaimed": 1,
        "processing": 0,
        "ordered": 0,
        "abnormal": 0,
    }
    assert overview_data["lines"]["total"] == 2
    assert overview_data["lines"]["byWorkflowStatus"]["draft"] == 1
    assert overview_data["lines"]["byWorkflowStatus"]["unclaimed"] == 1
    assert overview_data["fieldVisibility"] == {
        "store": True,
        "operator": True,
        "salesAmount": True,
        "profit": True,
        "profitMargin": True,
    }
    assert set(overview_data["filters"]["stores"]) == {"蓝天", "草原"}
    assert set(overview_data["filters"]["operators"]) == {"周远超", "运营乙"}

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
    assert summary["recipientName"] == "Synthetic Recipient"
    assert summary["recipientCountry"] == "US"
    assert summary["storeName"] == "蓝天-周远超（一组）"
    assert summary["storeBaseName"] == "蓝天"
    assert summary["operator"]["name"] == "周远超"
    assert "team" not in summary["operator"]
    assert summary["requiredQty"] == 1
    assert summary["purchasedQty"] == 0
    assert summary["purchasers"] == []
    assert summary["profitCurrency"] == "MXN"
    assert summary["estimatedProfit"] == 116.71
    assert summary["profitMargin"] == 53.86
    assert list_data["fieldVisibility"] == overview_data["fieldVisibility"]
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
    assert detail_data["fieldVisibility"] == overview_data["fieldVisibility"]
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
    invalid_scope = client.get(
        "/v1/procurement/orders",
        params={"taskScope": "unknown"},
        headers=headers,
    )
    assert invalid_scope.status_code == 422
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
    searched = client.get(
        "/v1/procurement/orders",
        params={"site": "US", "keyword": "周远超"},
        headers=headers,
    )
    assert searched.status_code == 200
    assert searched.json()["data"]["total"] == 1
    exact_store_operator = client.get(
        "/v1/procurement/orders",
        params={"store": "蓝天", "operator": "周远超"},
        headers=headers,
    )
    assert exact_store_operator.status_code == 200
    assert exact_store_operator.json()["data"]["total"] == 1
    wrong_operator = client.get(
        "/v1/procurement/orders",
        params={"store": "蓝天", "operator": "不存在的运营"},
        headers=headers,
    )
    assert wrong_operator.status_code == 200
    assert wrong_operator.json()["data"]["total"] == 0
    wrong_site = client.get(
        "/v1/procurement/orders",
        params={"site": "MX"},
        headers=headers,
    )
    assert wrong_site.status_code == 200
    assert wrong_site.json()["data"]["total"] == 0

    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        assert tenant is not None
        hidden = {
            "store": False,
            "operator": False,
            "salesAmount": False,
            "profit": False,
            "profitMargin": False,
        }
        hidden_list = PurchaseOrderService(session).workspace_list(
            tenant_id=tenant.id,
            field_visibility=hidden,
        )
        hidden_summary = hidden_list["items"][0]
        for key in (
            "storeName",
            "storeBaseName",
            "operator",
            "salesAmount",
            "salesCurrency",
            "estimatedProfit",
            "profitCurrency",
            "profitMargin",
            "orderKey",
        ):
            assert key not in hidden_summary
        hidden_detail = PurchaseOrderService(session).workspace_detail(
            tenant_id=tenant.id,
            purchase_order_id=uuid.UUID(purchase_order_id),
            field_visibility=hidden,
        )
        assert "storeName" not in hidden_detail["draft"]
        assert "orderKey" not in hidden_detail["draft"]
        assert "operatorName" not in hidden_detail["draft"]
        assert "salesAmount" not in hidden_detail["draft"]
        assert "estimatedProfit" not in hidden_detail["draft"]["estimatedMetrics"]
        assert "estimatedCost" not in hidden_detail["draft"]["estimatedMetrics"]
        assert "profitMargin" not in hidden_detail["draft"]["estimatedMetrics"]
        store_only = PurchaseOrderService(session).workspace_list(
            tenant_id=tenant.id,
            field_visibility={"operator": False},
        )["items"][0]
        assert store_only["storeName"] == "蓝天"
        assert store_only["storeBaseName"] == "蓝天"
        assert "operator" not in store_only
        assert "orderKey" not in store_only
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
    before_claim = client.get(
        "/v1/procurement/orders",
        params={"claimedByMe": "true"},
        headers=headers,
    )
    assert before_claim.status_code == 200
    assert before_claim.json()["data"]["total"] == 0

    claimed = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderLineIds": [line_id]},
        headers=headers,
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["claimedCount"] == 1
    assert claimed.json()["data"]["claimant"]["name"] == "合成测试用户"
    my_execution = client.get(
        "/v1/procurement/orders",
        params={"claimedByMe": "true", "taskScope": "processing"},
        headers=headers,
    )
    assert my_execution.status_code == 200
    assert my_execution.json()["data"]["total"] == 1
    execution_summary = my_execution.json()["data"]["items"][0]
    assert execution_summary["requiredQty"] == 3
    assert execution_summary["purchasers"][0]["name"] == "合成测试用户"
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
    assert len(refreshed_detail["executionBatches"]) == 2
    assert refreshed_detail["purchaseBatchCount"] == 2
    with database.session_factory() as session:
        assert session.scalar(select(func.count(PurchaseSplit.id))) == 2
        assert session.scalar(select(func.count(PurchaseSplitLine.id))) == 2
        audits = list(session.scalars(select(AuditEvent)))
        assert any(event.action == "purchase_order.lines.claim" for event in audits)
        assert any(event.action == "purchase_order.split_plan.save" for event in audits)
        assert all("buyerAccountRef" not in str(event.details) for event in audits)
    client.close()


def test_procurement_return_releases_unstarted_claim_and_rejects_split_plan(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    submitted = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert submitted.status_code == 200
    purchase_order_id = submitted.json()["data"]["purchaseOrderId"]
    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    line_id = detail["lines"][0]["purchaseOrderLineId"]

    claimed = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderIds": [purchase_order_id]},
        headers=headers,
    )
    assert claimed.status_code == 200
    returned = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/return",
        json={"reason": "采购员排班调整，退回公共任务"},
        headers=headers,
    )
    assert returned.status_code == 200
    returned_data = returned.json()["data"]
    assert returned_data["returnedCount"] == 1
    assert returned_data["executionRevision"] == 1

    mine = client.get(
        "/v1/procurement/orders",
        params={"claimedByMe": "true"},
        headers=headers,
    )
    assert mine.status_code == 200
    assert mine.json()["data"]["total"] == 0
    public_tasks = client.get(
        "/v1/procurement/orders",
        params={"taskScope": "unclaimed"},
        headers=headers,
    )
    assert public_tasks.status_code == 200
    assert public_tasks.json()["data"]["total"] == 1

    assert client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderIds": [purchase_order_id]},
        headers=headers,
    ).status_code == 200
    planned = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/splits",
        json={
            "expectedRevision": 1,
            "groups": [{
                "clientKey": "planned",
                "resource": None,
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
            }],
        },
        headers=headers,
    )
    assert planned.status_code == 200
    blocked = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/return",
        json={"reason": "已经分单后尝试退回"},
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "purchase_return_split_exists"

    with database.session_factory() as session:
        line = session.get(PurchaseOrderLine, uuid.UUID(line_id))
        assert line is not None
        assert line.workflow_status == "claimed"
        assert line.claimed_by_user_id is not None
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "purchase_order.lines.return"
                )
            )
        )
        assert any(event.result == "success" for event in audits)
        assert any(event.result == "denied" for event in audits)
        success = next(event for event in audits if event.result == "success")
        assert success.details["reason"] == "采购员排班调整，退回公共任务"
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
    return_response = client.post(
        "/v1/procurement/orders/00000000-0000-0000-0000-000000000001/return",
        json={"reason": "权限测试退回"},
        headers=headers,
    )
    assert return_response.status_code == 403
    assert return_response.json()["detail"]["code"] == "permission_denied"
    checkout_response = client.post(
        "/v1/procurement/orders/00000000-0000-0000-0000-000000000001/checkout-attempts",
        json={
            "idempotencyKey": "permission-checkout-001",
            "expectedExecutionRevision": 0,
            "lines": [{
                "purchaseOrderLineId": "00000000-0000-0000-0000-000000000002",
                "quantity": 1,
            }],
        },
        headers=headers,
    )
    assert checkout_response.status_code == 403
    assert checkout_response.json()["detail"]["code"] == "permission_denied"
    with database.session_factory() as session:
        assert session.scalar(select(PurchaseOrder)) is None
    client.close()


def test_checkout_attempt_reserves_quantity_idempotently_and_releases_on_abandon(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    purchase_order_id, line_id = claimed_purchase_order(
        client, headers, quantity=3
    )
    payload = {
        "idempotencyKey": "synthetic-checkout-reserve-001",
        "expectedExecutionRevision": 0,
        "resource": synthetic_resource(client, headers),
        "note": "脱敏数量占用测试",
        "lines": [{"purchaseOrderLineId": line_id, "quantity": 2}],
    }

    created = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 200
    attempt = created.json()["data"]
    assert attempt["status"] == "ready"
    assert attempt["resourceStatus"] == "reserved"
    assert attempt["version"] == 1
    assert attempt["executionRevision"] == 1
    assert attempt["unchanged"] is False
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.status == "reserved"
        assert str(account.current_checkout_attempt_id) == attempt["checkoutAttemptId"]

    invalidated = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json={
            "source": "legacy_feishu_migration",
            "snapshotKey": "synthetic-snapshot-2001-invalidated",
            "accounts": [{
                "accountRef": "synthetic-buyer-2001",
                "displayLabel": "美国买家号 ····2001",
                "site": "US",
                "availabilityStatus": "available",
                "credentialStatus": "invalid",
            }],
        },
        headers=headers,
    )
    assert invalidated.status_code == 200
    blocked_begin = client.post(
        f"/v1/procurement/checkout-attempts/{attempt['checkoutAttemptId']}/begin",
        json={"expectedVersion": 1},
        headers=headers,
    )
    assert blocked_begin.status_code == 409
    assert (
        blocked_begin.json()["detail"]["code"]
        == "buyer_account_credential_unavailable"
    )
    restored = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json={
            "source": "legacy_feishu_migration",
            "snapshotKey": "synthetic-snapshot-2001-restored",
            "accounts": [{
                "accountRef": "synthetic-buyer-2001",
                "displayLabel": "美国买家号 ····2001",
                "site": "US",
                "availabilityStatus": "available",
                "credentialStatus": "ready",
            }],
        },
        headers=headers,
    )
    assert restored.status_code == 200

    source_disabled = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json={
            "source": "legacy_feishu_migration",
            "snapshotKey": "synthetic-snapshot-2001-source-disabled",
            "accounts": [{
                "accountRef": "synthetic-buyer-2001",
                "displayLabel": "美国买家号 ····2001",
                "site": "US",
                "availabilityStatus": "disabled",
                "credentialStatus": "ready",
            }],
        },
        headers=headers,
    )
    assert source_disabled.status_code == 200
    assert source_disabled.json()["data"]["protectedCount"] == 1
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.status == "reserved"
        assert account.source_availability_status == "disabled"
        assert str(account.current_checkout_attempt_id) == attempt["checkoutAttemptId"]
    blocked_by_source = client.post(
        f"/v1/procurement/checkout-attempts/{attempt['checkoutAttemptId']}/begin",
        json={"expectedVersion": 1},
        headers=headers,
    )
    assert blocked_by_source.status_code == 409
    assert blocked_by_source.json()["detail"]["code"] == "buyer_account_unavailable"
    source_restored = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json={
            "source": "legacy_feishu_migration",
            "snapshotKey": "synthetic-snapshot-2001-source-restored",
            "accounts": [{
                "accountRef": "synthetic-buyer-2001",
                "displayLabel": "美国买家号 ····2001",
                "site": "US",
                "availabilityStatus": "available",
                "credentialStatus": "ready",
            }],
        },
        headers=headers,
    )
    assert source_restored.status_code == 200

    retry = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json=payload,
        headers=headers,
    )
    assert retry.status_code == 200
    assert retry.json()["data"]["checkoutAttemptId"] == attempt["checkoutAttemptId"]
    assert retry.json()["data"]["unchanged"] is True

    overbooked = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            **payload,
            "idempotencyKey": "synthetic-checkout-reserve-002",
            "expectedExecutionRevision": 1,
            "lines": [{"purchaseOrderLineId": line_id, "quantity": 2}],
        },
        headers=headers,
    )
    assert overbooked.status_code == 409
    assert overbooked.json()["detail"]["code"] == "checkout_quantity_unavailable"

    resource_conflict = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            **payload,
            "idempotencyKey": "synthetic-checkout-resource-001",
            "expectedExecutionRevision": 1,
            "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
        },
        headers=headers,
    )
    assert resource_conflict.status_code == 409
    assert resource_conflict.json()["detail"]["code"] == "buyer_account_unavailable"

    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    assert detail["lines"][0]["requiredQty"] == 3
    assert detail["lines"][0]["purchasedQty"] == 0
    assert detail["lines"][0]["reservedQty"] == 2
    assert detail["lines"][0]["remainingQty"] == 1

    abandoned = client.post(
        f"/v1/procurement/checkout-attempts/{attempt['checkoutAttemptId']}/abandon",
        json={"expectedVersion": 1, "reason": "合成测试主动放弃"},
        headers=headers,
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["data"]["status"] == "abandoned"
    assert abandoned.json()["data"]["resourceStatus"] == "released"
    assert abandoned.json()["data"]["executionRevision"] == 2
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.status == "available"
        assert account.current_checkout_attempt_id is None

    replacement = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            **payload,
            "idempotencyKey": "synthetic-checkout-reserve-003",
            "expectedExecutionRevision": 2,
            "lines": [{"purchaseOrderLineId": line_id, "quantity": 3}],
        },
        headers=headers,
    )
    assert replacement.status_code == 200
    assert replacement.json()["data"]["lines"][0]["quantity"] == 3
    with database.session_factory() as session:
        assert session.scalar(select(func.count(CheckoutAttempt.id))) == 2
    client.close()


def test_checkout_binding_rejects_unknown_wrong_site_and_unverified_buyers(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    purchase_order_id, line_id = claimed_purchase_order(
        client, headers, quantity=1
    )

    def attempt(account_id: str, key: str):
        return client.post(
            f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
            json={
                "idempotencyKey": key,
                "expectedExecutionRevision": 0,
                "resource": {
                    "hubEnvironmentRef": "synthetic-us-validation",
                    "hubEnvironmentName": "US-PUR-VALIDATION",
                    "buyerAccountId": account_id,
                    "site": "US",
                },
                "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
            },
            headers=headers,
        )

    unknown = attempt(
        "00000000-0000-0000-0000-000000000999",
        "synthetic-checkout-unknown-buyer",
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "buyer_account_not_found"

    synced = client.put(
        "/v1/resources/buyer-accounts/snapshot",
            json={
                "source": "legacy_feishu_migration",
                "snapshotKey": "synthetic-validation-buyers",
            "accounts": [
                {
                    "accountRef": "synthetic-buyer-mx-validation",
                    "displayLabel": "墨西哥买家号 ····91",
                    "site": "MX",
                    "credentialStatus": "ready",
                },
                {
                    "accountRef": "synthetic-buyer-us-unverified",
                    "displayLabel": "美国买家号 ····92",
                    "site": "US",
                    "credentialStatus": "unverified",
                },
            ],
        },
        headers=headers,
    )
    assert synced.status_code == 200
    listed = client.get(
        "/v1/resources/buyer-accounts", headers=headers
    ).json()["data"]["rows"]
    ids = {row["accountRef"]: row["accountId"] for row in listed}

    wrong_site = attempt(
        ids["synthetic-buyer-mx-validation"],
        "synthetic-checkout-wrong-site",
    )
    assert wrong_site.status_code == 422
    assert wrong_site.json()["detail"]["code"] == "checkout_resource_site_mismatch"

    unverified = attempt(
        ids["synthetic-buyer-us-unverified"],
        "synthetic-checkout-unverified",
    )
    assert unverified.status_code == 409
    assert (
        unverified.json()["detail"]["code"]
        == "buyer_account_credential_unavailable"
    )
    with database.session_factory() as session:
        assert session.scalar(select(CheckoutAttempt)) is None
    client.close()


def test_paid_checkout_creates_formal_batch_and_shipment_completes_line(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    purchase_order_id, line_id = claimed_purchase_order(
        client, headers, quantity=1
    )
    created = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            "idempotencyKey": "synthetic-paid-checkout-001",
            "expectedExecutionRevision": 0,
            "resource": synthetic_resource(client, headers),
            "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
        },
        headers=headers,
    )
    assert created.status_code == 200
    attempt_id = created.json()["data"]["checkoutAttemptId"]
    begun = client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/begin",
        json={"expectedVersion": 1},
        headers=headers,
    )
    assert begun.status_code == 200
    assert begun.json()["data"]["status"] == "checkout"

    paid = client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/payment-result",
        json={
            "expectedVersion": 2,
            "outcome": "paid",
            "environmentLoggedIn": True,
            "platform": "SHEIN",
            "platformOrderNo": "GSHBUY202608260001",
            "actualAmount": "18.00",
            "currency": "USD",
            "discountAmount": "2.00",
            "couponSummary": "synthetic coupon",
            "paidAt": "2026-08-26T12:00:00+08:00",
        },
        headers=headers,
    )
    assert paid.status_code == 200
    paid_data = paid.json()["data"]
    assert paid_data["status"] == "paid"
    assert paid_data["resourceStatus"] == "retained"
    assert paid_data["executionRevision"] == 3
    batch = paid_data["purchaseBatch"]
    assert batch["status"] == "paid"
    assert batch["actualAmount"] == 18.0
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.status == "post_payment_hold"
        assert account.current_checkout_attempt_id is None
        assert account.hub_environment_ref == "synthetic-us-2001"
        assert account.hub_environment_name == "US-PUR-2001"

    other_order_id, other_line_id = claimed_purchase_order(
        client,
        headers,
        quantity=1,
        suffix="RETAINED20260826",
    )
    retained_resource = client.post(
        f"/v1/procurement/orders/{other_order_id}/checkout-attempts",
        json={
            "idempotencyKey": "synthetic-retained-resource-001",
            "expectedExecutionRevision": 0,
            "resource": synthetic_resource(client, headers),
            "lines": [{"purchaseOrderLineId": other_line_id, "quantity": 1}],
        },
        headers=headers,
    )
    assert retained_resource.status_code == 409
    assert retained_resource.json()["detail"]["code"] == "checkout_resource_retained"
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.hub_environment_ref == "synthetic-us-2001"
        assert account.hub_environment_name == "US-PUR-2001"

    shipment_path = (
        f"/v1/procurement/purchase-batches/{batch['purchaseBatchId']}/shipments"
    )
    shipment = {
        "shipmentKey": "synthetic-package-001",
        "expectedVersion": 0,
        "packageNo": "PKG-SYNTHETIC-001",
        "carrierCode": "USPS",
        "carrierName": "Synthetic Carrier",
        "trackingNo": "SYNTHETIC/TRACK/001",
        "status": "pending_pickup",
    }
    tracking = client.post(shipment_path, json=shipment, headers=headers)
    assert tracking.status_code == 200
    assert tracking.json()["data"]["batchStatus"] == "tracking"
    assert tracking.json()["data"]["shipment"]["version"] == 1
    duplicate = client.post(shipment_path, json=shipment, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["unchanged"] is True

    delivered = client.post(
        shipment_path,
        json={
            **shipment,
            "expectedVersion": 1,
            "status": "delivered",
            "shippedAt": "2026-08-27T08:00:00+08:00",
            "deliveredAt": "2026-08-29T18:00:00+08:00",
        },
        headers=headers,
    )
    assert delivered.status_code == 200
    assert delivered.json()["data"]["batchStatus"] == "completed"
    assert delivered.json()["data"]["shipment"]["version"] == 2

    detail = client.get(
        f"/v1/procurement/orders/{purchase_order_id}", headers=headers
    ).json()["data"]
    assert detail["lines"][0]["workflowStatus"] == "completed"
    assert detail["lines"][0]["purchasedQty"] == 1
    assert detail["lines"][0]["reservedQty"] == 0
    assert detail["formalPurchaseBatchCount"] == 1
    assert detail["purchaseBatches"][0]["status"] == "completed"
    with database.session_factory() as session:
        assert session.scalar(select(func.count(PurchaseBatch.id))) == 1
        assert session.scalar(select(func.count(SupplierShipment.id))) == 1
        audits = list(session.scalars(select(AuditEvent)))
        assert all("trackingNo" not in str(event.details) for event in audits)
        assert all("buyerAccountRef" not in str(event.details) for event in audits)
        assert all("platformOrderNo" not in str(event.details) for event in audits)
    client.close()


def test_failed_checkout_keeps_lock_until_environment_cleanup_succeeds(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    purchase_order_id, line_id = claimed_purchase_order(
        client, headers, quantity=1
    )
    plan = {
        "idempotencyKey": "synthetic-failed-checkout-001",
        "expectedExecutionRevision": 0,
        "resource": synthetic_resource(client, headers),
        "lines": [{"purchaseOrderLineId": line_id, "quantity": 1}],
    }
    created = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json=plan,
        headers=headers,
    )
    attempt_id = created.json()["data"]["checkoutAttemptId"]
    assert client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/begin",
        json={"expectedVersion": 1},
        headers=headers,
    ).status_code == 200
    failed = client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/payment-result",
        json={
            "expectedVersion": 2,
            "outcome": "failed",
            "environmentLoggedIn": True,
            "reason": "合成支付拒绝",
        },
        headers=headers,
    )
    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "cleanup_pending"
    assert failed.json()["data"]["pendingTerminalStatus"] == "failed"

    blocked = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            **plan,
            "idempotencyKey": "synthetic-failed-checkout-002",
            "expectedExecutionRevision": 3,
        },
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "checkout_quantity_unavailable"

    cleanup_failed = client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/cleanup-result",
        json={
            "expectedVersion": 3,
            "environmentResult": "delete_failed",
            "buyerResult": "manual_review",
            "reason": "合成环境删除失败",
        },
        headers=headers,
    )
    assert cleanup_failed.status_code == 200
    assert cleanup_failed.json()["data"]["status"] == "manual_review"
    assert cleanup_failed.json()["data"]["pendingTerminalStatus"] == "failed"

    cleaned = client.post(
        f"/v1/procurement/checkout-attempts/{attempt_id}/cleanup-result",
        json={
            "expectedVersion": 4,
            "environmentResult": "deleted",
            "buyerResult": "reusable",
            "reason": "合成环境清理完成",
        },
        headers=headers,
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["data"]["status"] == "failed"
    assert cleaned.json()["data"]["resourceStatus"] == "released"
    assert cleaned.json()["data"]["executionRevision"] == 5
    with database.session_factory() as session:
        account = session.scalar(select(BuyerAccount))
        assert account is not None
        assert account.status == "available"
        assert account.current_checkout_attempt_id is None

    replacement = client.post(
        f"/v1/procurement/orders/{purchase_order_id}/checkout-attempts",
        json={
            **plan,
            "idempotencyKey": "synthetic-failed-checkout-003",
            "expectedExecutionRevision": 5,
        },
        headers=headers,
    )
    assert replacement.status_code == 200
    with database.session_factory() as session:
        attempts = list(session.scalars(select(CheckoutAttempt)))
        assert [item.status for item in attempts] == ["failed", "ready"]
    client.close()
