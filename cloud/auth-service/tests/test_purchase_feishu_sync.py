from __future__ import annotations

from copy import deepcopy
import json

import httpx
import pytest
from sqlalchemy import select

from test_purchase_api import authenticated_client, sample_draft
from xynigo_auth.feishu_operation_sync import FeishuBaseSyncError
from xynigo_auth.feishu_purchase_sync import (
    LINE_FIELD_TYPES,
    MASTER_FIELD_TYPES,
    REQUIRED_SELECT_OPTIONS,
    FeishuPurchaseBaseClient,
    FeishuPurchaseSyncWorker,
)
from xynigo_auth.models import PurchaseOrder, PurchaseOrderLine, PurchaseSyncOutbox


class FakePurchaseBaseClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.master_fields: dict[str, object] = {}
        self.line_fields: dict[str, dict[str, object]] = {}
        self.failures: list[FeishuBaseSyncError] = []
        self.preflight_error: FeishuBaseSyncError | None = None

    def validate_schema(self) -> dict[str, int]:
        if self.preflight_error is not None:
            raise self.preflight_error
        return {"master_fields": 38, "line_fields": 59}

    def _maybe_fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def upsert_master(
        self,
        *,
        order_key: str,
        fields: dict[str, object],
        verify_fields: dict[str, object],
        expected_record_id: str | None = None,
    ) -> str:
        self._maybe_fail()
        record_id = "rec-master-synthetic"
        if expected_record_id not in (None, record_id):
            raise FeishuBaseSyncError("record_mapping_conflict", retryable=False)
        self.master_fields.update(fields)
        assert all(self.master_fields.get(key) == value for key, value in verify_fields.items())
        self.calls.append(("master", order_key, dict(fields)))
        return record_id

    def upsert_line(
        self,
        *,
        line_key: str,
        fields: dict[str, object],
        verify_fields: dict[str, object],
        expected_record_id: str | None = None,
    ) -> str:
        self._maybe_fail()
        record_id = "rec-line-synthetic"
        if expected_record_id not in (None, record_id):
            raise FeishuBaseSyncError("record_mapping_conflict", retryable=False)
        stored = self.line_fields.setdefault(line_key, {})
        stored.update(fields)
        assert all(stored.get(key) == value for key, value in verify_fields.items())
        self.calls.append(("line", line_key, dict(fields)))
        return record_id


class FailFirstLinePurchaseBaseClient(FakePurchaseBaseClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def upsert_line(
        self,
        *,
        line_key: str,
        fields: dict[str, object],
        verify_fields: dict[str, object],
        expected_record_id: str | None = None,
    ) -> str:
        if not self.failed:
            self.failed = True
            raise FeishuBaseSyncError("feishu_1254291", retryable=True)
        return super().upsert_line(
            line_key=line_key,
            fields=fields,
            verify_fields=verify_fields,
            expected_record_id=expected_record_id,
        )


def build_worker(database, fake: FakePurchaseBaseClient) -> FeishuPurchaseSyncWorker:
    return FeishuPurchaseSyncWorker(
        session_factory=database.session_factory,
        client=fake,  # type: ignore[arg-type]
        interval_seconds=15,
    )


def _schema_fields(
    contract: dict[str, frozenset[int]], *, master_table_id: str
) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    for name, allowed_types in contract.items():
        field: dict[str, object] = {"field_name": name, "type": min(allowed_types)}
        if name in REQUIRED_SELECT_OPTIONS:
            field["property"] = {
                "options": [
                    {"name": option} for option in sorted(REQUIRED_SELECT_OPTIONS[name])
                ]
            }
        if name == "关联采购单":
            field["property"] = {"table_id": master_table_id}
        fields.append(field)
    return fields


def test_purchase_base_client_validates_exact_schema_contract() -> None:
    master_table_id = "tbl-master"
    line_table_id = "tbl-line"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        if request.url.path.endswith(f"/tables/{master_table_id}/fields"):
            items = _schema_fields(MASTER_FIELD_TYPES, master_table_id=master_table_id)
        elif request.url.path.endswith(f"/tables/{line_table_id}/fields"):
            items = _schema_fields(LINE_FIELD_TYPES, master_table_id=master_table_id)
        else:  # pragma: no cover - makes an unexpected endpoint immediately visible
            raise AssertionError(request.url)
        return httpx.Response(200, json={"code": 0, "data": {"items": items}})

    client = FeishuPurchaseBaseClient(
        app_id="app-id",
        app_secret="app-secret",
        base_token="base-token",
        master_table_id=master_table_id,
        line_table_id=line_table_id,
        transport=httpx.MockTransport(handler),
    )
    assert client.validate_schema() == {"master_fields": 38, "line_fields": 59}


def test_purchase_base_client_writes_v1_link_ids_and_accepts_v1_readback() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        if request.method == "GET" and request.url.path.endswith("/records"):
            return httpx.Response(200, json={"code": 0, "data": {"items": []}})
        if request.method == "POST" and request.url.path.endswith("/records"):
            payload = json.loads(request.content)
            assert payload["fields"]["关联采购单"] == ["rec-master"]
            return httpx.Response(
                200,
                json={"code": 0, "data": {"record": {"record_id": "rec-line"}}},
            )
        if request.method == "GET" and request.url.path.endswith("/records/rec-line"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": "rec-line",
                            "fields": {
                                "lineKey": "line-key",
                                "关联采购单": [
                                    {
                                        "record_ids": ["rec-master"],
                                        "table_id": "tbl-master",
                                        "text": "master",
                                        "type": "text",
                                    }
                                ],
                            },
                        }
                    },
                },
            )
        raise AssertionError((request.method, request.url))

    client = FeishuPurchaseBaseClient(
        app_id="app-id",
        app_secret="app-secret",
        base_token="base-token",
        master_table_id="tbl-master",
        line_table_id="tbl-line",
        transport=httpx.MockTransport(handler),
    )
    assert client.upsert_line(
        line_key="line-key",
        fields={"lineKey": "line-key", "关联采购单": ["rec-master"]},
        verify_fields={"lineKey": "line-key", "关联采购单": ["rec-master"]},
    ) == "rec-line"
    assert [(method, path.rsplit("/", 1)[-1]) for method, path in requests] == [
        ("POST", "internal"),
        ("GET", "records"),
        ("POST", "records"),
        ("GET", "rec-line"),
    ]


def test_purchase_worker_mirrors_master_lines_and_assignment_idempotently(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    submitted = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert submitted.status_code == 200
    order_id = submitted.json()["data"]["purchaseOrderId"]

    fake = FakePurchaseBaseClient()
    worker = build_worker(database, fake)
    assert worker.run_once(limit=10) == 1
    assert [call[0] for call in fake.calls] == ["master", "line", "master"]
    assert fake.master_fields["草稿同步状态"] == "draft-synced"
    assert fake.master_fields["提交状态"] == "submitted"
    assert fake.master_fields["待写内容哈希"] is None
    line_key, line_fields = next(iter(fake.line_fields.items()))
    assert line_key.endswith("|1")
    assert line_fields["明细状态"] == "待认领"
    assert line_fields["关联采购单"] == ["rec-master-synthetic"]
    assert "商品图片" not in line_fields

    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        line = session.scalar(select(PurchaseOrderLine))
        event = session.scalar(select(PurchaseSyncOutbox))
        assert order is not None and order.sync_status == "synced"
        assert order.feishu_record_id == "rec-master-synthetic"
        assert line is not None and line.feishu_record_id == "rec-line-synthetic"
        assert event is not None and event.status == "completed"

    detail = client.get(f"/v1/procurement/orders/{order_id}", headers=headers)
    line_id = detail.json()["data"]["lines"][0]["purchaseOrderLineId"]
    claimed = client.post(
        "/v1/procurement/claims",
        json={"purchaseOrderLineIds": [line_id]},
        headers=headers,
    )
    assert claimed.status_code == 200
    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        events = list(session.scalars(select(PurchaseSyncOutbox)))
        assert order is not None and order.sync_status == "pending"
        assert [event.event_type for event in events] == [
            "order.submitted",
            "order.assignment_changed",
        ]

    assert worker.run_once(limit=10) == 1
    assert fake.line_fields[line_key]["明细状态"] == "待采购"
    assert fake.line_fields[line_key]["采购员"]
    with database.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        assert order is not None and order.sync_status == "synced"
        assert set(session.scalars(select(PurchaseSyncOutbox.status))) == {"completed"}
    client.close()


def test_purchase_worker_retries_without_creating_duplicate_business_keys(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    response = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert response.status_code == 200
    fake = FakePurchaseBaseClient()
    fake.failures.append(FeishuBaseSyncError("feishu_1254291", retryable=True))
    worker = build_worker(database, fake)

    assert worker.run_once(limit=1) == 1
    with database.session_factory() as session:
        event = session.scalar(select(PurchaseSyncOutbox))
        order = session.scalar(select(PurchaseOrder))
        assert event is not None and event.status == "pending"
        assert event.attempt_count == 1
        assert event.last_error_code == "feishu_1254291"
        assert order is not None and order.sync_status == "pending"
        event.available_at = event.created_at
        session.commit()

    assert worker.run_once(limit=1) == 1
    assert len(fake.line_fields) == 1
    assert [call[0] for call in fake.calls] == ["master", "line", "master"]
    with database.session_factory() as session:
        event = session.scalar(select(PurchaseSyncOutbox))
        assert event is not None and event.status == "completed"
        assert event.attempt_count == 2
    client.close()


def test_purchase_worker_marks_partial_remote_failure_and_recovers(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    response = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert response.status_code == 200
    fake = FailFirstLinePurchaseBaseClient()
    worker = build_worker(database, fake)

    assert worker.run_once(limit=1) == 1
    assert fake.master_fields["草稿同步状态"] == "draft-save-failed"
    assert fake.master_fields["最近错误代码"] == "feishu_1254291"
    with database.session_factory() as session:
        event = session.scalar(select(PurchaseSyncOutbox))
        assert event is not None and event.status == "pending"
        event.available_at = event.created_at
        session.commit()

    assert worker.run_once(limit=1) == 1
    assert fake.master_fields["草稿同步状态"] == "draft-synced"
    assert fake.master_fields["最近错误代码"] is None
    client.close()


def test_purchase_worker_stops_before_claiming_when_schema_preflight_fails(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    response = client.post(
        "/v1/purchase-orders/submit", json=sample_draft(), headers=headers
    )
    assert response.status_code == 200
    fake = FakePurchaseBaseClient()
    fake.preflight_error = FeishuBaseSyncError(
        "purchase_line_schema_field_set", retryable=False
    )
    worker = build_worker(database, fake)

    with pytest.raises(FeishuBaseSyncError) as caught:
        worker.run_once(limit=1)
    assert caught.value.code == "purchase_line_schema_field_set"
    assert fake.calls == []
    with database.session_factory() as session:
        event = session.scalar(select(PurchaseSyncOutbox))
        assert event is not None and event.status == "pending"
        assert event.attempt_count == 0
    client.close()


def test_purchase_worker_skips_superseded_draft_revision(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    first = client.post(
        "/v1/purchase-orders/draft", json=sample_draft(), headers=headers
    )
    assert first.status_code == 200
    changed = deepcopy(sample_draft())
    changed["items"][0]["guidePrice"] = 17.5  # type: ignore[index]
    changed["guideTotalsByCurrency"] = {"USD": 17.5}
    second = client.post(
        "/v1/purchase-orders/draft", json=changed, headers=headers
    )
    assert second.status_code == 200

    fake = FakePurchaseBaseClient()
    worker = build_worker(database, fake)
    assert worker.run_once(limit=1) == 1
    assert fake.calls == []
    with database.session_factory() as session:
        events = list(
            session.scalars(select(PurchaseSyncOutbox).order_by(PurchaseSyncOutbox.created_at))
        )
        assert events[0].status == "completed"
        assert events[0].last_error_code == "superseded"
        assert events[1].status == "pending"

    assert worker.run_once(limit=1) == 1
    assert fake.master_fields["草稿版本"] == 2
    client.close()
