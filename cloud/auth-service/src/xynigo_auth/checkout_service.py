"""采购下单闭环 P1：数量占用、付款批次、失败补偿和物流状态机。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .buyer_account_sync import enqueue_buyer_account_mirror
from .buyer_account_service import BuyerAccountService
from .models import (
    BuyerAccount,
    CheckoutAttempt,
    CheckoutAttemptLine,
    PurchaseBatch,
    PurchaseBatchLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseSplit,
    PurchaseSyncOutbox,
    SupplierShipment,
    User,
)
from .purchase_service import PurchaseServiceError


ACTIVE_ATTEMPT_STATUSES = frozenset(
    {"planning", "ready", "checkout", "cleanup_pending", "manual_review"}
)
EDITABLE_ATTEMPT_STATUSES = frozenset({"planning", "ready"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _request_hash(payload: dict[str, Any]) -> str:
    resource = payload.get("resource")
    normalized_resource = None
    if isinstance(resource, dict):
        normalized_resource = {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in resource.items()
        }
    normalized = {
        "resource": normalized_resource,
        "note": str(payload.get("note") or "").strip(),
        "lines": sorted(
            [
                {
                    "purchaseOrderLineId": str(item["purchaseOrderLineId"]),
                    "quantity": int(item["quantity"]),
                }
                for item in payload.get("lines", [])
            ],
            key=lambda item: item["purchaseOrderLineId"],
        ),
    }
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _comparable_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ProcurementCheckoutService:
    """纯数据库状态机；不直接调用 HubStudio、SHEIN 或飞书。"""

    def __init__(self, session: Session, clock=_utcnow) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.clock = clock

    def create_attempt(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        purchase_order_id: uuid.UUID,
        idempotency_key: str,
        expected_execution_revision: int,
        plan: dict[str, Any],
    ) -> dict[str, object]:
        target_hash = _request_hash(plan)
        existing = self.session.scalar(
            select(CheckoutAttempt)
            .where(
                CheckoutAttempt.tenant_id == tenant_id,
                CheckoutAttempt.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.purchase_order_id != purchase_order_id
                or existing.purchaser_user_id != actor_user_id
                or existing.request_hash != target_hash
            ):
                raise PurchaseServiceError(
                    "checkout_idempotency_conflict",
                    "下单尝试幂等键已被不同请求使用",
                    409,
                )
            return self._attempt_payload(existing, unchanged=True)

        order = self._order(tenant_id, purchase_order_id, lock=True)
        # The order lock serializes competing creates. Recheck after waiting so
        # two equal idempotency keys cannot race into the unique constraint.
        existing = self.session.scalar(
            select(CheckoutAttempt).where(
                CheckoutAttempt.tenant_id == tenant_id,
                CheckoutAttempt.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.purchase_order_id != purchase_order_id
                or existing.purchaser_user_id != actor_user_id
                or existing.request_hash != target_hash
            ):
                raise PurchaseServiceError(
                    "checkout_idempotency_conflict",
                    "下单尝试幂等键已被不同请求使用",
                    409,
                )
            return self._attempt_payload(existing, unchanged=True)
        self._expected_execution_revision(order, expected_execution_revision)
        if self.session.scalar(
            select(PurchaseSplit.id).where(
                PurchaseSplit.purchase_order_id == order.id,
                PurchaseSplit.purchaser_user_id == actor_user_id,
            )
        ) is not None:
            raise PurchaseServiceError(
                "checkout_legacy_split_exists",
                "采购单仍有旧测试分单，请先人工确认迁移后再开始真实下单",
                409,
            )
        allocations = self._validate_allocations(
            order=order,
            actor_user_id=actor_user_id,
            raw_lines=plan["lines"],
        )
        resource, buyer_account = self._validate_resource(
            tenant_id=tenant_id,
            order=order,
            resource=plan.get("resource"),
        )
        self._ensure_resource_available(
            tenant_id=tenant_id,
            resource=resource,
        )
        now = self.clock()
        order_fragment = str(order.id).replace("-", "")[:10].upper()
        attempt = CheckoutAttempt(
            tenant_id=tenant_id,
            purchase_order_id=order.id,
            purchaser_user_id=actor_user_id,
            attempt_no=f"CGXS-{order_fragment}-{order.execution_revision + 1:04d}",
            idempotency_key=idempotency_key,
            request_hash=target_hash,
            status="ready" if resource else "planning",
            site=str(order.draft_payload.get("site") or "").upper(),
            resource_status="reserved" if resource else "unbound",
            note=str(plan.get("note") or "").strip() or None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._apply_resource(attempt, resource)
        self.session.add(attempt)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise PurchaseServiceError(
                "checkout_resource_conflict",
                "Hub 环境或买家号已被其他下单尝试占用",
                409,
            ) from exc
        self._set_buyer_account_state(
            buyer_account,
            attempt=attempt,
            status="reserved",
            keep_ownership=True,
            now=now,
        )
        for line, quantity in allocations.values():
            self.session.add(
                CheckoutAttemptLine(
                    checkout_attempt_id=attempt.id,
                    purchase_order_line_id=line.id,
                    reserved_qty=quantity,
                    created_at=now,
                    updated_at=now,
                )
            )
            line.workflow_status = "purchasing"
            line.updated_at = now
        self._touch_order(order, now)
        self._enqueue(order, "checkout.attempted", str(attempt.id), attempt.version, now)
        self.session.flush()
        return self._attempt_payload(attempt, unchanged=False)

    def revise_attempt(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_version: int,
        expected_execution_revision: int,
        plan: dict[str, Any],
    ) -> dict[str, object]:
        attempt = self._attempt(tenant_id, actor_user_id, attempt_id, lock=True)
        target_hash = _request_hash(plan)
        if attempt.request_hash == target_hash:
            return self._attempt_payload(attempt, unchanged=True)
        if attempt.status not in EDITABLE_ATTEMPT_STATUSES:
            raise PurchaseServiceError(
                "checkout_attempt_not_editable",
                "下单尝试已开始结算，不能再修改组合",
                409,
            )
        self._expected_version(attempt, expected_version)
        order = self._order(tenant_id, attempt.purchase_order_id, lock=True)
        self._expected_execution_revision(order, expected_execution_revision)
        old_line_ids = list(
            self.session.scalars(
                select(CheckoutAttemptLine.purchase_order_line_id).where(
                    CheckoutAttemptLine.checkout_attempt_id == attempt.id
                )
            )
        )
        allocations = self._validate_allocations(
            order=order,
            actor_user_id=actor_user_id,
            raw_lines=plan["lines"],
            exclude_attempt_id=attempt.id,
        )
        old_buyer_account = self._buyer_account_for_attempt(attempt, lock=True)
        resource, buyer_account = self._validate_resource(
            tenant_id=tenant_id,
            order=order,
            resource=plan.get("resource"),
            current_attempt_id=attempt.id,
        )
        self._ensure_resource_available(
            tenant_id=tenant_id,
            resource=resource,
            exclude_attempt_id=attempt.id,
        )
        now = self.clock()
        self.session.execute(
            delete(CheckoutAttemptLine).where(
                CheckoutAttemptLine.checkout_attempt_id == attempt.id
            )
        )
        for line, quantity in allocations.values():
            self.session.add(
                CheckoutAttemptLine(
                    checkout_attempt_id=attempt.id,
                    purchase_order_line_id=line.id,
                    reserved_qty=quantity,
                    created_at=now,
                    updated_at=now,
                )
            )
        attempt.request_hash = target_hash
        attempt.note = str(plan.get("note") or "").strip() or None
        attempt.status = "ready" if resource else "planning"
        attempt.resource_status = "reserved" if resource else "unbound"
        attempt.version += 1
        attempt.updated_at = now
        self._apply_resource(attempt, resource)
        self._touch_order(order, now)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise PurchaseServiceError(
                "checkout_resource_conflict",
                "Hub 环境或买家号已被其他下单尝试占用",
                409,
            ) from exc
        if old_buyer_account is not None and (
            buyer_account is None or old_buyer_account.id != buyer_account.id
        ):
            self._set_buyer_account_state(
                old_buyer_account,
                attempt=attempt,
                status="available",
                keep_ownership=False,
                now=now,
            )
        self._set_buyer_account_state(
            buyer_account,
            attempt=attempt,
            status="reserved",
            keep_ownership=True,
            now=now,
        )
        self._recompute_line_statuses(
            order,
            list(set(old_line_ids) | set(allocations)),
            now,
        )
        self._enqueue(order, "checkout.updated", str(attempt.id), attempt.version, now)
        self.session.flush()
        return self._attempt_payload(attempt, unchanged=False)

    def begin_attempt(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_version: int,
    ) -> dict[str, object]:
        attempt = self._attempt(tenant_id, actor_user_id, attempt_id, lock=True)
        if attempt.status == "checkout":
            return self._attempt_payload(attempt, unchanged=True)
        if attempt.status != "ready":
            raise PurchaseServiceError(
                "checkout_attempt_not_ready",
                "请先完整绑定同站点 Hub 环境和买家号",
                409,
            )
        self._expected_version(attempt, expected_version)
        order = self._order(tenant_id, attempt.purchase_order_id, lock=True)
        now = self.clock()
        buyer_account = self._buyer_account_for_attempt(attempt, lock=True)
        if (
            buyer_account is None
            or buyer_account.current_checkout_attempt_id != attempt.id
            or buyer_account.status != "reserved"
        ):
            raise PurchaseServiceError(
                "buyer_account_unavailable", "买家号预占状态已变化，请刷新后重试", 409
            )
        if buyer_account.source_availability_status != "available":
            raise PurchaseServiceError(
                "buyer_account_unavailable", "买家号源台账当前不可用于下单", 409
            )
        if buyer_account.credential_status != "ready":
            raise PurchaseServiceError(
                "buyer_account_credential_unavailable",
                "买家号凭证尚未验证或已经失效",
                409,
            )
        attempt.status = "checkout"
        attempt.resource_status = "active"
        attempt.started_at = now
        attempt.version += 1
        attempt.updated_at = now
        self._set_buyer_environment(buyer_account, attempt, bound=True)
        self._set_buyer_account_state(
            buyer_account,
            attempt=attempt,
            status="in_use",
            keep_ownership=True,
            now=now,
        )
        self._touch_order(order, now)
        self._enqueue(order, "checkout.started", str(attempt.id), attempt.version, now)
        self.session.flush()
        return self._attempt_payload(attempt, unchanged=False)

    def abandon_attempt(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_version: int,
        reason: str,
    ) -> dict[str, object]:
        attempt = self._attempt(tenant_id, actor_user_id, attempt_id, lock=True)
        if attempt.status == "abandoned":
            return self._attempt_payload(attempt, unchanged=True)
        if attempt.status not in {"planning", "ready", "checkout"}:
            raise PurchaseServiceError(
                "checkout_attempt_cannot_abandon",
                "当前下单尝试不能直接放弃，请先处理付款或资源异常",
                409,
            )
        self._expected_version(attempt, expected_version)
        order = self._order(tenant_id, attempt.purchase_order_id, lock=True)
        now = self.clock()
        buyer_account = self._buyer_account_for_attempt(attempt, lock=True)
        attempt.terminal_reason = reason
        attempt.version += 1
        attempt.updated_at = now
        if attempt.status == "checkout":
            attempt.status = "cleanup_pending"
            attempt.resource_status = "cleanup_pending"
            attempt.pending_terminal_status = "abandoned"
            self._set_buyer_environment(buyer_account, attempt, bound=True)
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status="cleanup_pending",
                keep_ownership=True,
                now=now,
            )
        else:
            attempt.status = "abandoned"
            attempt.resource_status = "released"
            attempt.terminal_at = now
            self.session.flush()
            self._recompute_attempt_lines(order, attempt, now)
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status="available",
                keep_ownership=False,
                now=now,
            )
        self._touch_order(order, now)
        self._enqueue(order, "checkout.abandoned", str(attempt.id), attempt.version, now)
        self.session.flush()
        return self._attempt_payload(attempt, unchanged=False)

    def record_payment_result(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_version: int,
        result: dict[str, Any],
    ) -> dict[str, object]:
        attempt = self._attempt(tenant_id, actor_user_id, attempt_id, lock=True)
        if attempt.status == "paid":
            batch = self.session.scalar(
                select(PurchaseBatch).where(PurchaseBatch.checkout_attempt_id == attempt.id)
            )
            if (
                result.get("outcome") == "paid"
                and batch is not None
                and batch.platform_order_no == result.get("platformOrderNo")
            ):
                return {
                    **self._attempt_payload(attempt, unchanged=True),
                    "purchaseBatch": self._batch_payload(batch),
                }
            raise PurchaseServiceError(
                "checkout_payment_conflict",
                "该下单尝试已记录不同的付款成功结果",
                409,
            )
        if attempt.status not in {"checkout", "manual_review"}:
            raise PurchaseServiceError(
                "checkout_payment_state_invalid",
                "当前下单尝试不能记录付款结果",
                409,
            )
        self._expected_version(attempt, expected_version)
        order = self._order(tenant_id, attempt.purchase_order_id, lock=True)
        now = self.clock()
        buyer_account = self._buyer_account_for_attempt(attempt, lock=True)
        outcome = str(result["outcome"])
        batch: PurchaseBatch | None = None
        attempt.version += 1
        attempt.payment_recorded_at = now
        attempt.updated_at = now
        if outcome == "paid":
            if (
                not attempt.hub_environment_ref
                or not attempt.buyer_account_id
                or not attempt.buyer_account_ref
                or buyer_account is None
            ):
                raise PurchaseServiceError(
                    "checkout_resource_missing",
                    "付款成功前必须绑定 Hub 环境和买家号",
                    409,
                )
            self._set_buyer_environment(buyer_account, attempt, bound=True)
            duplicate = self.session.scalar(
                select(PurchaseBatch).where(
                    PurchaseBatch.tenant_id == tenant_id,
                    PurchaseBatch.platform == result["platform"],
                    PurchaseBatch.platform_order_no == result["platformOrderNo"],
                )
            )
            if duplicate is not None:
                raise PurchaseServiceError(
                    "purchase_batch_platform_order_conflict",
                    "采购平台订单号已绑定其他采购批次",
                    409,
                )
            attempt_lines = self._attempt_lines(attempt.id)
            batch_fragment = str(attempt.id).replace("-", "")[:12].upper()
            batch = PurchaseBatch(
                tenant_id=tenant_id,
                purchase_order_id=order.id,
                checkout_attempt_id=attempt.id,
                purchaser_user_id=actor_user_id,
                batch_no=f"CGPC-{batch_fragment}",
                platform=str(result["platform"]),
                platform_order_no=str(result["platformOrderNo"]),
                site=attempt.site,
                actual_amount=Decimal(str(result["actualAmount"])),
                currency=str(result["currency"]),
                discount_amount=(
                    Decimal(str(result["discountAmount"]))
                    if result.get("discountAmount") is not None
                    else None
                ),
                coupon_summary=str(result.get("couponSummary") or "").strip() or None,
                status="paid",
                hub_environment_ref=str(attempt.hub_environment_ref),
                hub_environment_name=str(attempt.hub_environment_name or ""),
                buyer_account_id=attempt.buyer_account_id,
                buyer_account_ref=str(attempt.buyer_account_ref),
                buyer_account_label=str(attempt.buyer_account_label or ""),
                paid_at=result["paidAt"],
                created_at=now,
                updated_at=now,
            )
            self.session.add(batch)
            self.session.flush()
            for item in attempt_lines:
                self.session.add(
                    PurchaseBatchLine(
                        purchase_batch_id=batch.id,
                        purchase_order_line_id=item.purchase_order_line_id,
                        purchased_qty=item.reserved_qty,
                        created_at=now,
                        updated_at=now,
                    )
                )
            attempt.status = "paid"
            attempt.resource_status = "retained"
            attempt.pending_terminal_status = None
            attempt.terminal_at = now
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status="post_payment_hold",
                keep_ownership=False,
                now=now,
            )
            self.session.flush()
            self._recompute_line_statuses(
                order,
                [item.purchase_order_line_id for item in attempt_lines],
                now,
            )
            event_type = "purchase.paid"
        elif outcome == "uncertain":
            attempt.status = "manual_review"
            attempt.resource_status = "manual_review"
            attempt.pending_terminal_status = None
            attempt.terminal_reason = str(result.get("reason") or "")
            if bool(result.get("environmentLoggedIn")):
                self._set_buyer_environment(buyer_account, attempt, bound=True)
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status="manual_review",
                keep_ownership=True,
                now=now,
            )
            event_type = "checkout.failed"
        else:
            attempt.terminal_reason = str(result.get("reason") or "")
            if bool(result.get("environmentLoggedIn")):
                attempt.status = "cleanup_pending"
                attempt.resource_status = "cleanup_pending"
                attempt.pending_terminal_status = "failed"
                self._set_buyer_environment(buyer_account, attempt, bound=True)
                self._set_buyer_account_state(
                    buyer_account,
                    attempt=attempt,
                    status="cleanup_pending",
                    keep_ownership=True,
                    now=now,
                )
            else:
                attempt.status = "failed"
                attempt.resource_status = "released"
                attempt.pending_terminal_status = None
                attempt.terminal_at = now
                self._set_buyer_environment(buyer_account, attempt, bound=False)
                self.session.flush()
                self._recompute_attempt_lines(order, attempt, now)
                self._set_buyer_account_state(
                    buyer_account,
                    attempt=attempt,
                    status="available",
                    keep_ownership=False,
                    now=now,
                )
            event_type = "checkout.failed"
        self._touch_order(order, now)
        self._enqueue(order, event_type, str(attempt.id), attempt.version, now)
        self.session.flush()
        payload: dict[str, object] = self._attempt_payload(attempt, unchanged=False)
        if batch is not None:
            payload["purchaseBatch"] = self._batch_payload(batch)
        return payload

    def record_cleanup_result(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        expected_version: int,
        environment_result: str,
        buyer_result: str,
        reason: str,
    ) -> dict[str, object]:
        attempt = self._attempt(tenant_id, actor_user_id, attempt_id, lock=True)
        if attempt.pending_terminal_status is None or attempt.status not in {
            "cleanup_pending",
            "manual_review",
        }:
            raise PurchaseServiceError(
                "checkout_cleanup_state_invalid",
                "当前下单尝试没有待确认的资源清理动作",
                409,
            )
        self._expected_version(attempt, expected_version)
        order = self._order(tenant_id, attempt.purchase_order_id, lock=True)
        now = self.clock()
        buyer_account = self._buyer_account_for_attempt(attempt, lock=True)
        attempt.version += 1
        attempt.updated_at = now
        attempt.terminal_reason = reason
        if environment_result == "delete_failed":
            attempt.status = "manual_review"
            attempt.resource_status = "manual_review"
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status="manual_review",
                keep_ownership=True,
                now=now,
            )
        else:
            terminal_status = str(attempt.pending_terminal_status)
            attempt.status = terminal_status
            attempt.resource_status = (
                "released" if buyer_result == "reusable" else "manual_review"
            )
            attempt.pending_terminal_status = None
            attempt.terminal_at = now
            self._set_buyer_environment(buyer_account, attempt, bound=False)
            self.session.flush()
            self._recompute_attempt_lines(order, attempt, now)
            self._set_buyer_account_state(
                buyer_account,
                attempt=attempt,
                status=("available" if buyer_result == "reusable" else "manual_review"),
                keep_ownership=False,
                now=now,
            )
        self._touch_order(order, now)
        event_type = (
            "checkout.abandoned"
            if attempt.pending_terminal_status == "abandoned" or attempt.status == "abandoned"
            else "checkout.failed"
        )
        self._enqueue(order, event_type, str(attempt.id), attempt.version, now)
        self.session.flush()
        return self._attempt_payload(attempt, unchanged=False)

    def upsert_shipment(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        purchase_batch_id: uuid.UUID,
        shipment: dict[str, Any],
    ) -> dict[str, object]:
        batch = self.session.scalar(
            select(PurchaseBatch)
            .where(
                PurchaseBatch.id == purchase_batch_id,
                PurchaseBatch.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if batch is None:
            raise PurchaseServiceError("purchase_batch_not_found", "采购批次不存在", 404)
        if batch.purchaser_user_id != actor_user_id:
            raise PurchaseServiceError(
                "purchase_batch_not_owned",
                "只能回填本人采购批次的物流信息",
                403,
            )
        stored = self.session.scalar(
            select(SupplierShipment)
            .where(
                SupplierShipment.purchase_batch_id == batch.id,
                SupplierShipment.shipment_key == shipment["shipmentKey"],
            )
            .with_for_update()
        )
        if stored is not None and self._shipment_matches(stored, shipment):
            return {
                "purchaseBatchId": str(batch.id),
                "batchStatus": batch.status,
                "shipment": self._shipment_payload(stored),
                "unchanged": True,
            }
        expected_version = int(shipment["expectedVersion"])
        now = self.clock()
        duplicate_tracking = self.session.scalar(
            select(SupplierShipment).where(
                SupplierShipment.purchase_batch_id == batch.id,
                SupplierShipment.tracking_no == shipment["trackingNo"],
                SupplierShipment.shipment_key != shipment["shipmentKey"],
            )
        )
        if duplicate_tracking is not None:
            raise PurchaseServiceError(
                "shipment_tracking_conflict",
                "物流单号已绑定同一采购批次的其他包裹",
                409,
            )
        if stored is None:
            if expected_version != 0:
                raise PurchaseServiceError(
                    "shipment_version_conflict",
                    "物流包裹尚不存在，请刷新后重试",
                    409,
                )
            stored = SupplierShipment(
                tenant_id=tenant_id,
                purchase_batch_id=batch.id,
                shipment_key=str(shipment["shipmentKey"]),
                package_no=str(shipment.get("packageNo") or "") or None,
                carrier_code=str(shipment.get("carrierCode") or "") or None,
                carrier_name=str(shipment["carrierName"]),
                tracking_no=str(shipment["trackingNo"]),
                status=str(shipment["status"]),
                version=1,
                shipped_at=shipment.get("shippedAt"),
                delivered_at=shipment.get("deliveredAt"),
                created_at=now,
                updated_at=now,
            )
            self.session.add(stored)
        else:
            if stored.version != expected_version:
                raise PurchaseServiceError(
                    "shipment_version_conflict",
                    "物流包裹已被更新，请刷新后重试",
                    409,
                )
            stored.package_no = str(shipment.get("packageNo") or "") or None
            stored.carrier_code = str(shipment.get("carrierCode") or "") or None
            stored.carrier_name = str(shipment["carrierName"])
            stored.tracking_no = str(shipment["trackingNo"])
            stored.status = str(shipment["status"])
            stored.shipped_at = shipment.get("shippedAt")
            stored.delivered_at = shipment.get("deliveredAt")
            stored.version += 1
            stored.updated_at = now
        self.session.flush()
        all_shipments = list(
            self.session.scalars(
                select(SupplierShipment).where(
                    SupplierShipment.purchase_batch_id == batch.id
                )
            )
        )
        if any(item.status == "exception" for item in all_shipments):
            batch.status = "exception"
        elif all_shipments and all(item.status == "delivered" for item in all_shipments):
            batch.status = "completed"
        else:
            batch.status = "tracking"
        batch.updated_at = now
        self.session.flush()
        order = self._order(tenant_id, batch.purchase_order_id, lock=True)
        line_ids = list(
            self.session.scalars(
                select(PurchaseBatchLine.purchase_order_line_id).where(
                    PurchaseBatchLine.purchase_batch_id == batch.id
                )
            )
        )
        self._recompute_line_statuses(order, line_ids, now)
        self._touch_order(order, now)
        self._enqueue(order, "shipment.updated", str(stored.id), stored.version, now)
        self.session.flush()
        return {
            "purchaseBatchId": str(batch.id),
            "batchStatus": batch.status,
            "shipment": self._shipment_payload(stored),
            "unchanged": False,
        }

    def order_snapshot(
        self,
        *,
        tenant_id: uuid.UUID,
        purchase_order_id: uuid.UUID,
    ) -> dict[str, object]:
        attempts = list(
            self.session.scalars(
                select(CheckoutAttempt)
                .where(
                    CheckoutAttempt.tenant_id == tenant_id,
                    CheckoutAttempt.purchase_order_id == purchase_order_id,
                )
                .order_by(CheckoutAttempt.created_at, CheckoutAttempt.id)
            )
        )
        batches = list(
            self.session.scalars(
                select(PurchaseBatch)
                .where(
                    PurchaseBatch.tenant_id == tenant_id,
                    PurchaseBatch.purchase_order_id == purchase_order_id,
                )
                .order_by(PurchaseBatch.paid_at, PurchaseBatch.id)
            )
        )
        return {
            "checkoutAttempts": [self._attempt_payload(item) for item in attempts],
            "purchaseBatches": [self._batch_payload(item) for item in batches],
        }

    def purchased_quantities(
        self,
        order_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        if not order_ids:
            return {}
        rows = self.session.execute(
            select(
                PurchaseBatchLine.purchase_order_line_id,
                func.sum(PurchaseBatchLine.purchased_qty),
            )
            .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchLine.purchase_batch_id)
            .where(PurchaseBatch.purchase_order_id.in_(order_ids))
            .group_by(PurchaseBatchLine.purchase_order_line_id)
        ).all()
        return {line_id: int(quantity or 0) for line_id, quantity in rows}

    def _validate_allocations(
        self,
        *,
        order: PurchaseOrder,
        actor_user_id: uuid.UUID,
        raw_lines: list[dict[str, Any]],
        exclude_attempt_id: uuid.UUID | None = None,
    ) -> dict[uuid.UUID, tuple[PurchaseOrderLine, int]]:
        requested = {
            uuid.UUID(str(item["purchaseOrderLineId"])): int(item["quantity"])
            for item in raw_lines
        }
        lines = list(
            self.session.scalars(
                select(PurchaseOrderLine)
                .where(
                    PurchaseOrderLine.purchase_order_id == order.id,
                    PurchaseOrderLine.id.in_(requested),
                    PurchaseOrderLine.is_active.is_(True),
                )
                .with_for_update()
            )
        )
        if {line.id for line in lines} != set(requested):
            raise PurchaseServiceError(
                "checkout_line_not_found",
                "下单尝试包含不存在或已失效的采购明细",
                404,
            )
        for line in lines:
            if line.claimed_by_user_id != actor_user_id or line.workflow_status not in {
                "claimed",
                "purchasing",
            }:
                raise PurchaseServiceError(
                    "checkout_line_not_owned",
                    "只能为本人已认领且未完成的采购明细创建下单尝试",
                    409,
                )
        line_ids = list(requested)
        reserved = defaultdict(int)
        reserve_statement = (
            select(
                CheckoutAttemptLine.purchase_order_line_id,
                func.sum(CheckoutAttemptLine.reserved_qty),
            )
            .join(
                CheckoutAttempt,
                CheckoutAttempt.id == CheckoutAttemptLine.checkout_attempt_id,
            )
            .where(
                CheckoutAttemptLine.purchase_order_line_id.in_(line_ids),
                CheckoutAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
        )
        if exclude_attempt_id is not None:
            reserve_statement = reserve_statement.where(
                CheckoutAttempt.id != exclude_attempt_id
            )
        for line_id, quantity in self.session.execute(
            reserve_statement.group_by(CheckoutAttemptLine.purchase_order_line_id)
        ).all():
            reserved[line_id] = int(quantity or 0)
        purchased = self._purchased_for_lines(line_ids)
        result: dict[uuid.UUID, tuple[PurchaseOrderLine, int]] = {}
        for line in lines:
            required = int(line.payload.get("purchaseQty") or line.payload.get("salesQty") or 0)
            quantity = requested[line.id]
            available = required - purchased.get(line.id, 0) - reserved.get(line.id, 0)
            if quantity > available:
                raise PurchaseServiceError(
                    "checkout_quantity_unavailable",
                    f"第 {line.line_no} 行剩余可下单数量为 {max(0, available)}",
                    409,
                )
            result[line.id] = (line, quantity)
        return result

    def _validate_resource(
        self,
        *,
        tenant_id: uuid.UUID,
        order: PurchaseOrder,
        resource: Any,
        current_attempt_id: uuid.UUID | None = None,
    ) -> tuple[dict[str, Any] | None, BuyerAccount | None]:
        if resource is None:
            return None, None
        normalized = dict(resource)
        order_site = str(order.draft_payload.get("site") or "").upper()
        if str(normalized.get("site") or "").upper() != order_site:
            raise PurchaseServiceError(
                "checkout_resource_site_mismatch",
                "Hub 环境、买家号与采购单站点不一致",
                422,
            )
        buyer_account = BuyerAccountService(self.session).checkout_candidate(
            tenant_id=tenant_id,
            account_id=uuid.UUID(str(normalized["buyerAccountId"])),
            site=order_site,
            current_attempt_id=current_attempt_id,
        )
        if buyer_account.hub_environment_ref is not None and (
            buyer_account.hub_environment_ref
            != str(normalized["hubEnvironmentRef"])
            or buyer_account.hub_environment_name
            != str(normalized["hubEnvironmentName"])
        ):
            raise PurchaseServiceError(
                "checkout_resource_binding_mismatch",
                "买家号已绑定其他 Hub 环境，请刷新资源后重试",
                409,
            )
        normalized.update(
            {
                "buyerAccountId": buyer_account.id,
                "buyerAccountRef": buyer_account.account_ref,
                "buyerAccountLabel": buyer_account.display_label,
            }
        )
        return normalized, buyer_account

    @staticmethod
    def _apply_resource(
        attempt: CheckoutAttempt,
        resource: dict[str, Any] | None,
    ) -> None:
        attempt.hub_environment_ref = (
            str(resource["hubEnvironmentRef"]) if resource else None
        )
        attempt.hub_environment_name = (
            str(resource["hubEnvironmentName"]) if resource else None
        )
        attempt.buyer_account_id = resource["buyerAccountId"] if resource else None
        attempt.buyer_account_ref = str(resource["buyerAccountRef"]) if resource else None
        attempt.buyer_account_label = (
            str(resource["buyerAccountLabel"]) if resource else None
        )

    def _ensure_resource_available(
        self,
        *,
        tenant_id: uuid.UUID,
        resource: dict[str, Any] | None,
        exclude_attempt_id: uuid.UUID | None = None,
    ) -> None:
        if resource is None:
            return
        retained = self.session.scalar(
            select(PurchaseBatch.id).where(
                PurchaseBatch.tenant_id == tenant_id,
                or_(
                    PurchaseBatch.hub_environment_ref
                    == str(resource["hubEnvironmentRef"]),
                    PurchaseBatch.buyer_account_ref
                    == str(resource["buyerAccountRef"]),
                ),
            )
        )
        if retained is not None:
            raise PurchaseServiceError(
                "checkout_resource_retained",
                "Hub 环境或买家号已绑定成功采购批次，当前不可复用",
                409,
            )
        statement = select(CheckoutAttempt.id).where(
            CheckoutAttempt.tenant_id == tenant_id,
            CheckoutAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            or_(
                CheckoutAttempt.hub_environment_ref
                == str(resource["hubEnvironmentRef"]),
                CheckoutAttempt.buyer_account_ref == str(resource["buyerAccountRef"]),
            ),
        )
        if exclude_attempt_id is not None:
            statement = statement.where(CheckoutAttempt.id != exclude_attempt_id)
        if self.session.scalar(statement.with_for_update()) is not None:
            raise PurchaseServiceError(
                "checkout_resource_conflict",
                "Hub 环境或买家号已被其他下单尝试占用",
                409,
            )

    def _buyer_account_for_attempt(
        self,
        attempt: CheckoutAttempt,
        *,
        lock: bool,
    ) -> BuyerAccount | None:
        if attempt.buyer_account_id is None:
            return None
        statement = select(BuyerAccount).where(
            BuyerAccount.id == attempt.buyer_account_id,
            BuyerAccount.tenant_id == attempt.tenant_id,
        )
        if lock:
            statement = statement.with_for_update()
        account = self.session.scalar(statement)
        if account is None:
            raise PurchaseServiceError(
                "buyer_account_not_found", "下单尝试绑定的买家号不存在", 409
            )
        return account

    def _set_buyer_account_state(
        self,
        account: BuyerAccount | None,
        *,
        attempt: CheckoutAttempt,
        status: str,
        keep_ownership: bool,
        now: datetime,
    ) -> None:
        if account is None:
            return
        if account.current_checkout_attempt_id not in {None, attempt.id}:
            raise PurchaseServiceError(
                "buyer_account_unavailable", "买家号已被其他下单尝试占用", 409
            )
        if status == "available":
            if account.source_availability_status == "disabled":
                status = "disabled"
            elif (
                account.source_availability_status != "available"
                or account.credential_status != "ready"
            ):
                status = "manual_review"
        target_owner = attempt.id if keep_ownership else None
        if (
            account.status == status
            and account.current_checkout_attempt_id == target_owner
        ):
            return
        account.status = status
        account.current_checkout_attempt_id = target_owner
        account.version += 1
        account.updated_at = now
        enqueue_buyer_account_mirror(self.session, account, available_at=now)

    @staticmethod
    def _set_buyer_environment(
        account: BuyerAccount | None,
        attempt: CheckoutAttempt,
        *,
        bound: bool,
    ) -> None:
        if account is None:
            return
        account.hub_environment_ref = attempt.hub_environment_ref if bound else None
        account.hub_environment_name = attempt.hub_environment_name if bound else None

    def _order(
        self,
        tenant_id: uuid.UUID,
        purchase_order_id: uuid.UUID,
        *,
        lock: bool,
    ) -> PurchaseOrder:
        statement = select(PurchaseOrder).where(
            PurchaseOrder.id == purchase_order_id,
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.submission_status == "submitted",
        )
        if lock:
            statement = statement.with_for_update()
        order = self.session.scalar(statement)
        if order is None:
            raise PurchaseServiceError(
                "purchase_order_not_found",
                "采购单不存在或尚未正式提交",
                404,
            )
        return order

    def _attempt(
        self,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        attempt_id: uuid.UUID,
        *,
        lock: bool,
    ) -> CheckoutAttempt:
        statement = select(CheckoutAttempt).where(
            CheckoutAttempt.id == attempt_id,
            CheckoutAttempt.tenant_id == tenant_id,
        )
        if lock:
            statement = statement.with_for_update()
        attempt = self.session.scalar(statement)
        if attempt is None:
            raise PurchaseServiceError(
                "checkout_attempt_not_found",
                "下单尝试不存在",
                404,
            )
        if attempt.purchaser_user_id != actor_user_id:
            raise PurchaseServiceError(
                "checkout_attempt_not_owned",
                "只能操作本人创建的下单尝试",
                403,
            )
        return attempt

    @staticmethod
    def _expected_version(attempt: CheckoutAttempt, expected_version: int) -> None:
        if attempt.version != expected_version:
            raise PurchaseServiceError(
                "checkout_attempt_version_conflict",
                "下单尝试已被更新，请刷新后重试",
                409,
            )

    @staticmethod
    def _expected_execution_revision(order: PurchaseOrder, expected: int) -> None:
        if order.execution_revision != expected:
            raise PurchaseServiceError(
                "purchase_execution_revision_conflict",
                "采购执行状态已更新，请刷新采购单后重试",
                409,
            )

    @staticmethod
    def _touch_order(order: PurchaseOrder, now: datetime) -> None:
        order.execution_revision += 1
        order.updated_at = now

    def _attempt_lines(self, attempt_id: uuid.UUID) -> list[CheckoutAttemptLine]:
        return list(
            self.session.scalars(
                select(CheckoutAttemptLine)
                .where(CheckoutAttemptLine.checkout_attempt_id == attempt_id)
                .order_by(CheckoutAttemptLine.created_at, CheckoutAttemptLine.id)
            )
        )

    def _purchased_for_lines(
        self,
        line_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        if not line_ids:
            return {}
        rows = self.session.execute(
            select(
                PurchaseBatchLine.purchase_order_line_id,
                func.sum(PurchaseBatchLine.purchased_qty),
            )
            .where(PurchaseBatchLine.purchase_order_line_id.in_(line_ids))
            .group_by(PurchaseBatchLine.purchase_order_line_id)
        ).all()
        return {line_id: int(quantity or 0) for line_id, quantity in rows}

    def _reserved_for_lines(
        self,
        line_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        if not line_ids:
            return {}
        rows = self.session.execute(
            select(
                CheckoutAttemptLine.purchase_order_line_id,
                func.sum(CheckoutAttemptLine.reserved_qty),
            )
            .join(
                CheckoutAttempt,
                CheckoutAttempt.id == CheckoutAttemptLine.checkout_attempt_id,
            )
            .where(
                CheckoutAttemptLine.purchase_order_line_id.in_(line_ids),
                CheckoutAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
            )
            .group_by(CheckoutAttemptLine.purchase_order_line_id)
        ).all()
        return {line_id: int(quantity or 0) for line_id, quantity in rows}

    def _recompute_attempt_lines(
        self,
        order: PurchaseOrder,
        attempt: CheckoutAttempt,
        now: datetime,
    ) -> None:
        self._recompute_line_statuses(
            order,
            [item.purchase_order_line_id for item in self._attempt_lines(attempt.id)],
            now,
        )

    def _recompute_line_statuses(
        self,
        order: PurchaseOrder,
        line_ids: list[uuid.UUID],
        now: datetime,
    ) -> None:
        unique_ids = list(dict.fromkeys(line_ids))
        if not unique_ids:
            return
        purchased = self._purchased_for_lines(unique_ids)
        reserved = self._reserved_for_lines(unique_ids)
        lines = list(
            self.session.scalars(
                select(PurchaseOrderLine)
                .where(
                    PurchaseOrderLine.purchase_order_id == order.id,
                    PurchaseOrderLine.id.in_(unique_ids),
                )
                .with_for_update()
            )
        )
        batch_statuses: dict[uuid.UUID, list[str]] = defaultdict(list)
        for line_id, status in self.session.execute(
            select(PurchaseBatchLine.purchase_order_line_id, PurchaseBatch.status)
            .join(PurchaseBatch, PurchaseBatch.id == PurchaseBatchLine.purchase_batch_id)
            .where(PurchaseBatchLine.purchase_order_line_id.in_(unique_ids))
        ).all():
            batch_statuses[line_id].append(str(status))
        for line in lines:
            required = int(line.payload.get("purchaseQty") or line.payload.get("salesQty") or 0)
            bought = purchased.get(line.id, 0)
            held = reserved.get(line.id, 0)
            statuses = batch_statuses.get(line.id, [])
            if any(status == "exception" for status in statuses):
                line.workflow_status = "exception"
            elif bought >= required and statuses and all(
                status == "completed" for status in statuses
            ):
                line.workflow_status = "completed"
            elif bought >= required and statuses and all(
                status in {"tracking", "completed"} for status in statuses
            ):
                line.workflow_status = "logistics_filled"
            elif bought >= required:
                line.workflow_status = "ordered"
            elif bought > 0 or held > 0:
                line.workflow_status = "purchasing"
            else:
                line.workflow_status = "claimed"
            line.updated_at = now

    def _enqueue(
        self,
        order: PurchaseOrder,
        event_type: str,
        aggregate_id: str,
        version: int,
        now: datetime,
    ) -> None:
        order.sync_status = "pending"
        order.updated_at = now
        dedupe_key = f"{aggregate_id}:{event_type}:{version}"
        if self.session.scalar(
            select(PurchaseSyncOutbox.id).where(
                PurchaseSyncOutbox.dedupe_key == dedupe_key
            )
        ) is not None:
            return
        self.session.add(
            PurchaseSyncOutbox(
                tenant_id=order.tenant_id,
                purchase_order_id=order.id,
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload={
                    "purchaseOrderId": str(order.id),
                    "aggregateId": aggregate_id,
                    "aggregateVersion": version,
                },
                status="pending",
                attempt_count=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    def _attempt_payload(
        self,
        attempt: CheckoutAttempt,
        *,
        unchanged: bool = False,
    ) -> dict[str, object]:
        lines = self._attempt_lines(attempt.id)
        return {
            "checkoutAttemptId": str(attempt.id),
            "attemptNo": attempt.attempt_no,
            "purchaseOrderId": str(attempt.purchase_order_id),
            "executionRevision": self._attempt_execution_revision(attempt),
            "purchaserUserId": str(attempt.purchaser_user_id),
            "status": attempt.status,
            "resourceStatus": attempt.resource_status,
            "pendingTerminalStatus": attempt.pending_terminal_status,
            "site": attempt.site,
            "version": attempt.version,
            "resource": (
                {
                    "hubEnvironmentRef": attempt.hub_environment_ref,
                    "hubEnvironmentName": attempt.hub_environment_name,
                    "buyerAccountId": str(attempt.buyer_account_id),
                    "buyerAccountRef": attempt.buyer_account_ref,
                    "buyerAccountLabel": attempt.buyer_account_label,
                    "site": attempt.site,
                }
                if (
                    attempt.hub_environment_ref
                    and attempt.buyer_account_id
                    and attempt.buyer_account_ref
                )
                else None
            ),
            "note": attempt.note or "",
            "terminalReason": attempt.terminal_reason or "",
            "lines": [
                {
                    "purchaseOrderLineId": str(item.purchase_order_line_id),
                    "quantity": item.reserved_qty,
                }
                for item in lines
            ],
            "startedAt": attempt.started_at.isoformat() if attempt.started_at else None,
            "paymentRecordedAt": (
                attempt.payment_recorded_at.isoformat()
                if attempt.payment_recorded_at
                else None
            ),
            "terminalAt": attempt.terminal_at.isoformat() if attempt.terminal_at else None,
            "createdAt": attempt.created_at.isoformat(),
            "updatedAt": attempt.updated_at.isoformat(),
            "unchanged": unchanged,
        }

    def _batch_payload(self, batch: PurchaseBatch) -> dict[str, object]:
        lines = list(
            self.session.scalars(
                select(PurchaseBatchLine)
                .where(PurchaseBatchLine.purchase_batch_id == batch.id)
                .order_by(PurchaseBatchLine.created_at, PurchaseBatchLine.id)
            )
        )
        shipments = list(
            self.session.scalars(
                select(SupplierShipment)
                .where(SupplierShipment.purchase_batch_id == batch.id)
                .order_by(SupplierShipment.created_at, SupplierShipment.id)
            )
        )
        purchaser = self.session.get(User, batch.purchaser_user_id)
        return {
            "purchaseBatchId": str(batch.id),
            "batchNo": batch.batch_no,
            "purchaseOrderId": str(batch.purchase_order_id),
            "checkoutAttemptId": str(batch.checkout_attempt_id),
            "purchaser": (
                {"id": str(purchaser.id), "name": purchaser.display_name}
                if purchaser
                else None
            ),
            "platform": batch.platform,
            "platformOrderNo": batch.platform_order_no,
            "site": batch.site,
            "actualAmount": float(batch.actual_amount),
            "currency": batch.currency,
            "discountAmount": (
                float(batch.discount_amount) if batch.discount_amount is not None else None
            ),
            "couponSummary": batch.coupon_summary or "",
            "status": batch.status,
            "resource": {
                "hubEnvironmentRef": batch.hub_environment_ref,
                "hubEnvironmentName": batch.hub_environment_name,
                "buyerAccountId": (
                    str(batch.buyer_account_id) if batch.buyer_account_id else None
                ),
                "buyerAccountRef": batch.buyer_account_ref,
                "buyerAccountLabel": batch.buyer_account_label,
                "site": batch.site,
            },
            "paidAt": batch.paid_at.isoformat(),
            "lines": [
                {
                    "purchaseOrderLineId": str(item.purchase_order_line_id),
                    "quantity": item.purchased_qty,
                }
                for item in lines
            ],
            "shipments": [self._shipment_payload(item) for item in shipments],
            "createdAt": batch.created_at.isoformat(),
            "updatedAt": batch.updated_at.isoformat(),
        }

    @staticmethod
    def _shipment_matches(stored: SupplierShipment, payload: dict[str, Any]) -> bool:
        return all(
            (
                stored.package_no or "",
                stored.carrier_code or "",
                stored.carrier_name,
                stored.tracking_no,
                stored.status,
                _comparable_datetime(stored.shipped_at),
                _comparable_datetime(stored.delivered_at),
            )[index]
            == value
            for index, value in enumerate(
                (
                    str(payload.get("packageNo") or ""),
                    str(payload.get("carrierCode") or ""),
                    str(payload["carrierName"]),
                    str(payload["trackingNo"]),
                    str(payload["status"]),
                    _comparable_datetime(payload.get("shippedAt")),
                    _comparable_datetime(payload.get("deliveredAt")),
                )
            )
        )

    def _attempt_execution_revision(self, attempt: CheckoutAttempt) -> int:
        order = self.session.get(PurchaseOrder, attempt.purchase_order_id)
        return int(order.execution_revision) if order is not None else 0

    @staticmethod
    def _shipment_payload(item: SupplierShipment) -> dict[str, object]:
        return {
            "shipmentId": str(item.id),
            "shipmentKey": item.shipment_key,
            "packageNo": item.package_no or "",
            "carrierCode": item.carrier_code or "",
            "carrierName": item.carrier_name,
            "trackingNo": item.tracking_no,
            "status": item.status,
            "version": item.version,
            "shippedAt": item.shipped_at.isoformat() if item.shipped_at else None,
            "deliveredAt": item.delivered_at.isoformat() if item.delivered_at else None,
            "updatedAt": item.updated_at.isoformat(),
        }
