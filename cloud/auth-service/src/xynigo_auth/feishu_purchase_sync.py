"""Durable PostgreSQL-to-Feishu Base mirror for purchase tasks."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .feishu_operation_sync import FeishuBaseSyncError, FeishuOperationBaseClient
from .models import PurchaseOrder, PurchaseOrderLine, PurchaseSyncOutbox, User


SHANGHAI = ZoneInfo("Asia/Shanghai")

TEXT = frozenset({1})
NUMBER = frozenset({2})
SELECT = frozenset({3})
DATETIME = frozenset({5})
CHECKBOX = frozenset({7})
USER = frozenset({11})
ATTACHMENT = frozenset({17})
LINK = frozenset({18, 21})
CREATED_AT = frozenset({1001})
UPDATED_AT = frozenset({1002})

MASTER_FIELD_TYPES: dict[str, frozenset[int]] = {
    "采购单标题": TEXT,
    "orderKey": TEXT,
    "Schema版本": NUMBER,
    "店铺": TEXT,
    "站点": TEXT,
    "销售订单号": TEXT,
    "包裹号": TEXT,
    "店小秘下单时间": DATETIME,
    "收件人姓名": TEXT,
    "收货地址1": TEXT,
    "收货地址2": TEXT,
    "城市": TEXT,
    "州/省": TEXT,
    "邮编": TEXT,
    "收件人电话": TEXT,
    "销售币种": TEXT,
    "包裹总金额": NUMBER,
    "指导采购总额JSON": TEXT,
    "预估指标JSON": TEXT,
    "客户端模式": TEXT,
    "客户端采购状态": TEXT,
    "提交状态": SELECT,
    "运营提交人": USER,
    "运营提交时间": DATETIME,
    "草稿同步状态": SELECT,
    "草稿版本": NUMBER,
    "内容哈希": TEXT,
    "待写内容哈希": TEXT,
    "有效明细数": NUMBER,
    "草稿JSON": TEXT,
    "客户端创建时间": DATETIME,
    "客户端更新时间": DATETIME,
    "最近同步时间": DATETIME,
    "最近错误代码": TEXT,
    "最近错误摘要": TEXT,
    "创建时间": CREATED_AT,
    "更新时间": UPDATED_AT,
    "采购明细": LINK,
}

LINE_FIELD_TYPES: dict[str, frozenset[int]] = {
    "明细标题": TEXT,
    "lineKey": TEXT,
    "orderKey": TEXT,
    "关联采购单": LINK,
    "行号": NUMBER,
    "是否有效": CHECKBOX,
    "失效时间": DATETIME,
    "商品图片": ATTACHMENT,
    "卖家SKU": TEXT,
    "店小秘规格": TEXT,
    "销售订单号": TEXT,
    "店铺": TEXT,
    "站点": TEXT,
    "收件人姓名": TEXT,
    "收件人电话": TEXT,
    "收货地址1": TEXT,
    "收货地址2": TEXT,
    "城市": TEXT,
    "州/省": TEXT,
    "邮编": TEXT,
    "采购主规格": TEXT,
    "采购次规格": TEXT,
    "原价": NUMBER,
    "优惠券类型": TEXT,
    "指导价": NUMBER,
    "采购币种": TEXT,
    "销售数量": NUMBER,
    "采购数量": NUMBER,
    "来源": TEXT,
    "精确采购链接": TEXT,
    "goods_id": TEXT,
    "skucode": TEXT,
    "main_attr": TEXT,
    "mallCode": TEXT,
    "行内容哈希": TEXT,
    "明细状态": SELECT,
    "采购员": USER,
    "认领时间": DATETIME,
    "实际下单金额": NUMBER,
    "实付币种": TEXT,
    "采购截图": ATTACHMENT,
    "Hub环境序号": TEXT,
    "下单账号标识": TEXT,
    "付款卡标识": TEXT,
    "采购平台订单号": TEXT,
    "下单时间": DATETIME,
    "物流承运商": TEXT,
    "物流单号": TEXT,
    "物流回填时间": DATETIME,
    "物流截图": ATTACHMENT,
    "异常备注": TEXT,
    "退回原因": TEXT,
    "退回人": USER,
    "退回时间": DATETIME,
    "重新提交时间": DATETIME,
    "客户端更新时间": DATETIME,
    "最近同步时间": DATETIME,
    "创建时间": CREATED_AT,
    "更新时间": UPDATED_AT,
}

REQUIRED_SELECT_OPTIONS = {
    "提交状态": {"draft", "submitted"},
    "草稿同步状态": {
        "saving-draft",
        "draft-synced",
        "draft-save-failed",
        "sync-conflict",
        "remote-missing",
    },
    "明细状态": {
        "待认领",
        "待采购",
        "采购中",
        "已下单",
        "物流已回填",
        "已完成",
        "退回修改",
        "采购异常",
    },
}

WORKFLOW_STATUS = {
    "draft": "待认领",
    "unclaimed": "待认领",
    "claimed": "待采购",
    "purchasing": "采购中",
    "ordered": "已下单",
    "logistics_filled": "物流已回填",
    "completed": "已完成",
    "returned": "退回修改",
    "exception": "采购异常",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _timestamp(value: Any, *, naive_zone: ZoneInfo | timezone = timezone.utc) -> int | None:
    if value in (None, ""):
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FeishuBaseSyncError("invalid_source_datetime", retryable=False) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_zone)
    return int(parsed.timestamp() * 1000)


def _cell_scalar(value: Any) -> Any:
    if isinstance(value, list) and len(value) == 1:
        item = value[0]
        if isinstance(item, dict):
            for key in ("text", "name", "value"):
                if key in item:
                    return item[key]
        return item
    return value


def _reference_ids(value: Any) -> set[str] | None:
    if not isinstance(value, list):
        return None
    result: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            record_ids = item.get("record_ids")
            if isinstance(record_ids, list):
                result.update(str(record_id) for record_id in record_ids if record_id)
                continue
            identifier = item.get("id") or item.get("record_id")
        else:
            identifier = item
        if identifier:
            result.add(str(identifier))
    return result


def _cell_matches(actual: Any, expected: Any) -> bool:
    if expected in (None, "", []):
        return actual in (None, "", [])
    expected_refs = _reference_ids(expected)
    if expected_refs is not None:
        return _reference_ids(actual) == expected_refs
    actual = _cell_scalar(actual)
    expected = _cell_scalar(expected)
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(actual) == str(expected)


class FeishuPurchaseBaseClient(FeishuOperationBaseClient):
    """Purchase-specific Base client with schema and write-back verification."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_token: str,
        master_table_id: str,
        line_table_id: str,
        timeout_seconds: float = 15.0,
        transport: Any | None = None,
    ) -> None:
        super().__init__(
            app_id=app_id,
            app_secret=app_secret,
            base_token=base_token,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self.master_table_id = master_table_id
        self.line_table_id = line_table_id

    def _fields_path(self, table_id: str) -> str:
        return "/open-apis/bitable/v1/apps/%s/tables/%s/fields" % (
            quote(self.base_token, safe=""),
            quote(table_id, safe=""),
        )

    def _list_fields(self, table_id: str) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            payload = self._request("GET", self._fields_path(table_id), params=params)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            items = data.get("items") if isinstance(data.get("items"), list) else []
            fields.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                return fields
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise FeishuBaseSyncError("field_pagination_stalled", retryable=True)
            page_token = next_token

    @staticmethod
    def _validate_field_contract(
        fields: Sequence[dict[str, Any]],
        expected: Mapping[str, frozenset[int]],
        *,
        table_label: str,
    ) -> dict[str, dict[str, Any]]:
        indexed = {
            str(item.get("field_name") or item.get("name") or ""): item
            for item in fields
        }
        if len(fields) != len(expected) or set(indexed) != set(expected):
            raise FeishuBaseSyncError(
                f"{table_label}_schema_field_set", retryable=False
            )
        for name, allowed_types in expected.items():
            try:
                field_type = int(indexed[name].get("type"))
            except (TypeError, ValueError):
                field_type = -1
            if field_type not in allowed_types:
                raise FeishuBaseSyncError(
                    f"{table_label}_schema_field_type", retryable=False
                )
        return indexed

    @staticmethod
    def _validate_options(field: dict[str, Any], required: set[str]) -> None:
        property_value = (
            field.get("property") if isinstance(field.get("property"), dict) else {}
        )
        options = (
            property_value.get("options")
            if isinstance(property_value.get("options"), list)
            else field.get("options")
            if isinstance(field.get("options"), list)
            else []
        )
        actual = {
            str(item.get("name") or "")
            for item in options
            if isinstance(item, dict)
        }
        if not required.issubset(actual):
            raise FeishuBaseSyncError("purchase_schema_select_options", retryable=False)

    def validate_schema(self) -> dict[str, int]:
        master_fields = self._list_fields(self.master_table_id)
        line_fields = self._list_fields(self.line_table_id)
        master = self._validate_field_contract(
            master_fields, MASTER_FIELD_TYPES, table_label="purchase_master"
        )
        lines = self._validate_field_contract(
            line_fields, LINE_FIELD_TYPES, table_label="purchase_line"
        )
        self._validate_options(master["提交状态"], REQUIRED_SELECT_OPTIONS["提交状态"])
        self._validate_options(
            master["草稿同步状态"], REQUIRED_SELECT_OPTIONS["草稿同步状态"]
        )
        self._validate_options(lines["明细状态"], REQUIRED_SELECT_OPTIONS["明细状态"])
        link_property = (
            lines["关联采购单"].get("property")
            if isinstance(lines["关联采购单"].get("property"), dict)
            else {}
        )
        linked_table = str(link_property.get("table_id") or "")
        if linked_table != self.master_table_id:
            raise FeishuBaseSyncError("purchase_schema_link_target", retryable=False)
        return {"master_fields": len(master_fields), "line_fields": len(line_fields)}

    def _find_by_key(
        self, table_id: str, key_field: str, sync_key: str
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        page_token = ""
        escaped_key = sync_key.replace("\\", "\\\\").replace('"', '\\"')
        while True:
            params: dict[str, Any] = {
                "page_size": 100,
                "filter": f'CurrentValue.[{key_field}]="{escaped_key}"',
            }
            if page_token:
                params["page_token"] = page_token
            payload = self._request("GET", self._records_path(table_id), params=params)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            items = data.get("items") if isinstance(data.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                if _cell_matches(fields.get(key_field), sync_key):
                    matches.append(item)
            if not data.get("has_more"):
                return matches
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise FeishuBaseSyncError("record_pagination_stalled", retryable=True)
            page_token = next_token

    def _verified_upsert(
        self,
        *,
        table_id: str,
        key_field: str,
        sync_key: str,
        fields: dict[str, Any],
        verify_fields: Mapping[str, Any],
        expected_record_id: str | None,
        duplicate_code: str,
    ) -> str:
        matches = self._find_by_key(table_id, key_field, sync_key)
        if len(matches) > 1:
            raise FeishuBaseSyncError(duplicate_code, retryable=False)
        if matches:
            record_id = str(matches[0].get("record_id") or "")
            if not record_id:
                raise FeishuBaseSyncError("record_id_missing", retryable=False)
            if expected_record_id and expected_record_id != record_id:
                raise FeishuBaseSyncError("record_mapping_conflict", retryable=False)
            self._request(
                "PUT",
                self._records_path(table_id) + "/" + quote(record_id, safe=""),
                payload={"fields": fields},
            )
        else:
            if expected_record_id:
                raise FeishuBaseSyncError("remote_record_missing", retryable=False)
            payload = self._request(
                "POST", self._records_path(table_id), payload={"fields": fields}
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            record = data.get("record") if isinstance(data.get("record"), dict) else {}
            record_id = str(record.get("record_id") or "")
            if not record_id:
                raise FeishuBaseSyncError("record_create_response", retryable=True)
        readback = self._request(
            "GET", self._records_path(table_id) + "/" + quote(record_id, safe="")
        )
        data = readback.get("data") if isinstance(readback.get("data"), dict) else {}
        record = data.get("record") if isinstance(data.get("record"), dict) else {}
        actual = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        if any(
            not _cell_matches(actual.get(field_name), expected)
            for field_name, expected in verify_fields.items()
        ):
            raise FeishuBaseSyncError("record_readback_mismatch", retryable=True)
        return record_id

    def upsert_master(
        self,
        *,
        order_key: str,
        fields: dict[str, Any],
        verify_fields: Mapping[str, Any],
        expected_record_id: str | None = None,
    ) -> str:
        return self._verified_upsert(
            table_id=self.master_table_id,
            key_field="orderKey",
            sync_key=order_key,
            fields=fields,
            verify_fields=verify_fields,
            expected_record_id=expected_record_id,
            duplicate_code="duplicate_order_key",
        )

    def upsert_line(
        self,
        *,
        line_key: str,
        fields: dict[str, Any],
        verify_fields: Mapping[str, Any],
        expected_record_id: str | None = None,
    ) -> str:
        return self._verified_upsert(
            table_id=self.line_table_id,
            key_field="lineKey",
            sync_key=line_key,
            fields=fields,
            verify_fields=verify_fields,
            expected_record_id=expected_record_id,
            duplicate_code="duplicate_line_key",
        )


@dataclass(frozen=True)
class PurchaseMirrorSnapshot:
    order: PurchaseOrder
    lines: tuple[PurchaseOrderLine, ...]
    submitter_open_id: str | None
    claimant_open_ids: Mapping[uuid.UUID, str]
    draft_revision: int
    execution_revision: int


def _master_common_fields(
    snapshot: PurchaseMirrorSnapshot, *, sync_time: datetime
) -> dict[str, Any]:
    order = snapshot.order
    draft = dict(order.draft_payload or {})
    submitter = (
        [{"id": snapshot.submitter_open_id}] if snapshot.submitter_open_id else []
    )
    return {
        "采购单标题": "%s｜%s｜%s"
        % (
            draft.get("storeName") or order.store_name,
            draft.get("platformOrderNo") or "",
            draft.get("packageId") or "",
        ),
        "orderKey": order.system_order_key,
        "Schema版本": order.schema_version,
        "店铺": draft.get("storeName") or order.store_name,
        "站点": draft.get("site") or "",
        "销售订单号": draft.get("platformOrderNo") or "",
        "包裹号": draft.get("packageId") or "",
        "店小秘下单时间": _timestamp(
            draft.get("dianxiaomiOrderTime"), naive_zone=SHANGHAI
        ),
        "收件人姓名": draft.get("recipientName") or "",
        "收货地址1": draft.get("addressLine1") or "",
        "收货地址2": draft.get("addressLine2") or "",
        "城市": draft.get("city") or "",
        "州/省": draft.get("stateProvince") or "",
        "邮编": draft.get("postalCode") or "",
        "收件人电话": draft.get("recipientPhone") or "",
        "销售币种": draft.get("salesCurrency") or "",
        "包裹总金额": draft.get("salesAmount"),
        "指导采购总额JSON": canonical_json(draft.get("guideTotalsByCurrency") or {}),
        "预估指标JSON": canonical_json(draft.get("estimatedMetrics"))
        if draft.get("estimatedMetrics") is not None
        else "",
        "客户端模式": draft.get("mode") or "",
        "客户端采购状态": draft.get("purchaseStatus") or "",
        "提交状态": order.submission_status,
        "运营提交人": submitter,
        "运营提交时间": _timestamp(order.submitted_at),
        "客户端创建时间": _timestamp(draft.get("createdAt")),
        "客户端更新时间": _timestamp(draft.get("updatedAt")),
        "最近同步时间": _timestamp(sync_time),
        "最近错误代码": None,
        "最近错误摘要": None,
    }


def _line_fields(
    snapshot: PurchaseMirrorSnapshot,
    line: PurchaseOrderLine,
    *,
    master_record_id: str,
    sync_time: datetime,
) -> dict[str, Any]:
    order = snapshot.order
    draft = dict(order.draft_payload or {})
    item = dict(line.payload or {})
    claimant_open_id = snapshot.claimant_open_ids.get(line.id)
    return {
        "明细标题": "%s #%02d" % (draft.get("platformOrderNo") or "", line.line_no),
        "lineKey": line.line_key,
        "orderKey": order.system_order_key,
        "关联采购单": [master_record_id],
        "行号": line.line_no,
        "是否有效": bool(line.is_active),
        "失效时间": None if line.is_active else _timestamp(line.updated_at),
        "卖家SKU": item.get("sellerSku") or "",
        "店小秘规格": item.get("variant") or "",
        "销售订单号": draft.get("platformOrderNo") or "",
        "店铺": draft.get("storeName") or order.store_name,
        "站点": draft.get("site") or "",
        "收件人姓名": draft.get("recipientName") or "",
        "收件人电话": draft.get("recipientPhone") or "",
        "收货地址1": draft.get("addressLine1") or "",
        "收货地址2": draft.get("addressLine2") or "",
        "城市": draft.get("city") or "",
        "州/省": draft.get("stateProvince") or "",
        "邮编": draft.get("postalCode") or "",
        "采购主规格": item.get("mainSpec") or "",
        "采购次规格": item.get("subSpec") or "",
        "原价": item.get("originalPrice"),
        "优惠券类型": item.get("couponType") or "",
        "指导价": item.get("guidePrice"),
        "采购币种": item.get("purchaseCurrency") or "",
        "销售数量": item.get("salesQty"),
        "采购数量": item.get("purchaseQty"),
        "来源": item.get("source") or "",
        "精确采购链接": item.get("purchaseLink") or "",
        "goods_id": item.get("goodsId") or "",
        "skucode": item.get("skuCode") or "",
        "main_attr": item.get("mainAttr") or "",
        "mallCode": item.get("mallCode") or "",
        "行内容哈希": line.content_hash,
        "明细状态": WORKFLOW_STATUS[line.workflow_status],
        "采购员": [{"id": claimant_open_id}] if claimant_open_id else [],
        "认领时间": _timestamp(line.claimed_at),
        "客户端更新时间": _timestamp(draft.get("updatedAt")),
        "最近同步时间": _timestamp(sync_time),
    }


class FeishuPurchaseSyncWorker:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client: FeishuPurchaseBaseClient,
        interval_seconds: int = 15,
        max_attempts: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.interval_seconds = max(5, int(interval_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._preflight_lock = threading.Lock()
        self._preflight_complete = False

    def _ensure_preflight(self) -> None:
        if self._preflight_complete:
            return
        with self._preflight_lock:
            if not self._preflight_complete:
                self.client.validate_schema()
                self._preflight_complete = True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ensure_preflight()
        self.recover_stale()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            processed = self.run_once(limit=20)
            if processed == 0:
                self._stop.wait(self.interval_seconds)

    def recover_stale(self) -> None:
        now = utcnow()
        cutoff = now - timedelta(minutes=10)
        with self.session_factory() as session:
            stale_order_ids = list(
                session.scalars(
                    select(PurchaseSyncOutbox.purchase_order_id).where(
                        PurchaseSyncOutbox.status == "processing",
                        PurchaseSyncOutbox.updated_at < cutoff,
                    )
                )
            )
            session.execute(
                update(PurchaseSyncOutbox)
                .where(
                    PurchaseSyncOutbox.status == "processing",
                    PurchaseSyncOutbox.updated_at < cutoff,
                )
                .values(
                    status="pending",
                    available_at=now,
                    last_error_code="worker_recovered",
                    updated_at=now,
                )
            )
            if stale_order_ids:
                session.execute(
                    update(PurchaseOrder)
                    .where(PurchaseOrder.id.in_(stale_order_ids))
                    .values(sync_status="pending", sync_error_code="worker_recovered")
                )
            session.commit()

    def run_once(self, *, limit: int = 20) -> int:
        self._ensure_preflight()
        processed = 0
        for _index in range(max(1, int(limit))):
            claimed = self._claim_one()
            if claimed is None:
                break
            processed += 1
            outbox_id, order_id, event_type, payload, attempt_count = claimed
            snapshot = self._snapshot(order_id)
            if snapshot is None:
                self._mark_obsolete(outbox_id, "aggregate_missing")
                continue
            if self._draft_event_is_stale(event_type, payload, snapshot):
                self._mark_obsolete(outbox_id, "superseded")
                continue
            try:
                record_ids = self._sync_snapshot(snapshot)
            except FeishuBaseSyncError as exc:
                self._mark_failure(
                    outbox_id=outbox_id,
                    order_id=order_id,
                    attempt_count=attempt_count,
                    code=exc.code,
                    retryable=exc.retryable,
                )
            except Exception:
                self._mark_failure(
                    outbox_id=outbox_id,
                    order_id=order_id,
                    attempt_count=attempt_count,
                    code="unexpected_sync_failure",
                    retryable=True,
                )
            else:
                self._mark_success(
                    outbox_id=outbox_id,
                    snapshot=snapshot,
                    master_record_id=record_ids[0],
                    line_record_ids=record_ids[1],
                )
        return processed

    def _claim_one(
        self,
    ) -> tuple[uuid.UUID, uuid.UUID, str, dict[str, Any], int] | None:
        with self.session_factory() as session:
            event = session.scalar(
                select(PurchaseSyncOutbox)
                .where(
                    PurchaseSyncOutbox.status == "pending",
                    PurchaseSyncOutbox.available_at <= utcnow(),
                )
                .order_by(PurchaseSyncOutbox.created_at, PurchaseSyncOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            event.status = "processing"
            event.attempt_count += 1
            event.updated_at = utcnow()
            result = (
                event.id,
                event.purchase_order_id,
                event.event_type,
                dict(event.payload or {}),
                event.attempt_count,
            )
            session.commit()
            return result

    def _snapshot(self, order_id: uuid.UUID) -> PurchaseMirrorSnapshot | None:
        with self.session_factory() as session:
            order = session.get(PurchaseOrder, order_id)
            if order is None:
                return None
            lines = tuple(
                session.scalars(
                    select(PurchaseOrderLine)
                    .where(PurchaseOrderLine.purchase_order_id == order.id)
                    .order_by(PurchaseOrderLine.line_no)
                )
            )
            user_ids = {
                value
                for value in (
                    order.submitted_by_user_id,
                    *(line.claimed_by_user_id for line in lines),
                )
                if value is not None
            }
            users = {
                user.id: user.feishu_open_id
                for user in session.scalars(select(User).where(User.id.in_(user_ids)))
            }
            session.expunge(order)
            for line in lines:
                session.expunge(line)
            return PurchaseMirrorSnapshot(
                order=order,
                lines=lines,
                submitter_open_id=users.get(order.submitted_by_user_id),
                claimant_open_ids={
                    line.id: users[line.claimed_by_user_id]
                    for line in lines
                    if line.claimed_by_user_id in users
                },
                draft_revision=order.draft_revision,
                execution_revision=order.execution_revision,
            )

    @staticmethod
    def _draft_event_is_stale(
        event_type: str,
        payload: Mapping[str, Any],
        snapshot: PurchaseMirrorSnapshot,
    ) -> bool:
        if event_type not in {"draft.saved", "order.submitted"}:
            return False
        try:
            event_revision = int(payload.get("draftRevision") or 0)
        except (TypeError, ValueError):
            event_revision = 0
        return event_revision < snapshot.draft_revision

    def _sync_snapshot(
        self, snapshot: PurchaseMirrorSnapshot
    ) -> tuple[str, dict[uuid.UUID, str]]:
        now = utcnow()
        order = snapshot.order
        master_saving = _master_common_fields(snapshot, sync_time=now)
        master_saving.update(
            {
                "草稿同步状态": "saving-draft",
                "待写内容哈希": order.content_hash,
            }
        )
        master_record_id = self.client.upsert_master(
            order_key=order.system_order_key,
            fields=master_saving,
            verify_fields={
                "orderKey": order.system_order_key,
                "草稿同步状态": "saving-draft",
                "待写内容哈希": order.content_hash,
            },
            expected_record_id=order.feishu_record_id,
        )
        try:
            return self._sync_lines_and_finalize(
                snapshot,
                master_record_id=master_record_id,
                sync_time=now,
            )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, FeishuBaseSyncError)
                else "unexpected_sync_failure"
            )
            self._mark_remote_failure_best_effort(
                order_key=order.system_order_key,
                master_record_id=master_record_id,
                code=code,
                sync_time=now,
            )
            raise

    def _sync_lines_and_finalize(
        self,
        snapshot: PurchaseMirrorSnapshot,
        *,
        master_record_id: str,
        sync_time: datetime,
    ) -> tuple[str, dict[uuid.UUID, str]]:
        order = snapshot.order
        line_record_ids: dict[uuid.UUID, str] = {}
        for line in snapshot.lines:
            fields = _line_fields(
                snapshot,
                line,
                master_record_id=master_record_id,
                sync_time=sync_time,
            )
            record_id = self.client.upsert_line(
                line_key=line.line_key,
                fields=fields,
                verify_fields={
                    "lineKey": line.line_key,
                    "orderKey": order.system_order_key,
                    "关联采购单": [master_record_id],
                    "行内容哈希": line.content_hash,
                    "明细状态": WORKFLOW_STATUS[line.workflow_status],
                },
                expected_record_id=line.feishu_record_id,
            )
            line_record_ids[line.id] = record_id
        final_fields = {
            "草稿同步状态": "draft-synced",
            "草稿版本": order.draft_revision,
            "内容哈希": order.content_hash,
            "待写内容哈希": None,
            "有效明细数": sum(1 for line in snapshot.lines if line.is_active),
            "草稿JSON": canonical_json(order.draft_payload),
            "最近同步时间": _timestamp(sync_time),
            "最近错误代码": None,
            "最近错误摘要": None,
        }
        confirmed_master_id = self.client.upsert_master(
            order_key=order.system_order_key,
            fields=final_fields,
            verify_fields={
                "orderKey": order.system_order_key,
                "草稿同步状态": "draft-synced",
                "草稿版本": order.draft_revision,
                "内容哈希": order.content_hash,
                "待写内容哈希": None,
            },
            expected_record_id=master_record_id,
        )
        return confirmed_master_id, line_record_ids

    def _mark_remote_failure_best_effort(
        self,
        *,
        order_key: str,
        master_record_id: str,
        code: str,
        sync_time: datetime,
    ) -> None:
        if code in {
            "duplicate_order_key",
            "duplicate_line_key",
            "record_mapping_conflict",
        }:
            sync_status = "sync-conflict"
        elif code == "remote_record_missing":
            sync_status = "remote-missing"
        else:
            sync_status = "draft-save-failed"
        safe_code = str(code or "feishu_sync_failed")[:128]
        try:
            self.client.upsert_master(
                order_key=order_key,
                fields={
                    "草稿同步状态": sync_status,
                    "最近同步时间": _timestamp(sync_time),
                    "最近错误代码": safe_code,
                    "最近错误摘要": "采购任务镜像未完整完成，可安全重试",
                },
                verify_fields={
                    "orderKey": order_key,
                    "草稿同步状态": sync_status,
                    "最近错误代码": safe_code,
                },
                expected_record_id=master_record_id,
            )
        except Exception:
            # Failure marking is secondary and must never hide the original error.
            return

    def _mark_success(
        self,
        *,
        outbox_id: uuid.UUID,
        snapshot: PurchaseMirrorSnapshot,
        master_record_id: str,
        line_record_ids: Mapping[uuid.UUID, str],
    ) -> None:
        now = utcnow()
        with self.session_factory() as session:
            event = session.get(PurchaseSyncOutbox, outbox_id)
            order = session.get(PurchaseOrder, snapshot.order.id)
            if event is None or order is None:
                return
            event.status = "completed"
            event.last_error_code = None
            event.processed_at = now
            event.updated_at = now
            order.feishu_record_id = master_record_id
            order.feishu_synced_at = now
            order.sync_error_code = None
            pending_newer = session.scalar(
                select(PurchaseSyncOutbox.id).where(
                    PurchaseSyncOutbox.purchase_order_id == order.id,
                    PurchaseSyncOutbox.id != event.id,
                    PurchaseSyncOutbox.status == "pending",
                )
            )
            revisions_unchanged = (
                order.draft_revision == snapshot.draft_revision
                and order.execution_revision == snapshot.execution_revision
            )
            order.sync_status = (
                "synced" if pending_newer is None and revisions_unchanged else "pending"
            )
            for line_id, record_id in line_record_ids.items():
                line = session.get(PurchaseOrderLine, line_id)
                if line is not None:
                    line.feishu_record_id = record_id
                    line.feishu_synced_at = now
            session.commit()

    def _mark_failure(
        self,
        *,
        outbox_id: uuid.UUID,
        order_id: uuid.UUID,
        attempt_count: int,
        code: str,
        retryable: bool,
    ) -> None:
        terminal = not retryable or attempt_count >= self.max_attempts
        safe_code = str(code or "feishu_sync_failed")[:128]
        with self.session_factory() as session:
            event = session.get(PurchaseSyncOutbox, outbox_id)
            order = session.get(PurchaseOrder, order_id)
            if event is None:
                return
            event.status = "failed" if terminal else "pending"
            event.available_at = utcnow() + timedelta(
                seconds=min(900, 15 * (2 ** max(0, attempt_count - 1)))
            )
            event.last_error_code = safe_code
            event.updated_at = utcnow()
            if order is not None:
                pending_newer = session.scalar(
                    select(PurchaseSyncOutbox.id).where(
                        PurchaseSyncOutbox.purchase_order_id == order.id,
                        PurchaseSyncOutbox.id != event.id,
                        PurchaseSyncOutbox.status == "pending",
                    )
                )
                if pending_newer is not None or not terminal:
                    order.sync_status = "pending"
                elif safe_code in {
                    "duplicate_order_key",
                    "duplicate_line_key",
                    "record_mapping_conflict",
                    "remote_record_missing",
                }:
                    order.sync_status = "conflict"
                else:
                    order.sync_status = "failed"
                order.sync_error_code = safe_code
            session.commit()

    def _mark_obsolete(self, outbox_id: uuid.UUID, code: str) -> None:
        now = utcnow()
        with self.session_factory() as session:
            event = session.get(PurchaseSyncOutbox, outbox_id)
            if event is None:
                return
            event.status = "completed"
            event.last_error_code = code[:128]
            event.processed_at = now
            event.updated_at = now
            session.commit()
