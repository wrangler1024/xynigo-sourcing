from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .models import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseSplit,
    PurchaseSplitLine,
    PurchaseSyncOutbox,
    User,
)
from .purchase_contract import (
    PurchaseDraft,
    canonical_draft_dict,
    draft_content_hash,
    line_content_hash,
    line_key,
    validate_formal_submit,
)


class PurchaseServiceError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PurchaseOrderService:
    WORKFLOW_STATUSES = (
        "draft",
        "unclaimed",
        "claimed",
        "purchasing",
        "ordered",
        "logistics_filled",
        "completed",
        "returned",
        "exception",
    )
    SUBMISSION_STATUSES = ("draft", "submitted")
    SYNC_STATUSES = ("pending", "synced", "failed", "conflict")

    def __init__(self, session: Session, clock=_utcnow) -> None:  # type: ignore[no-untyped-def]
        self.session = session
        self.clock = clock

    def save_draft(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        draft: PurchaseDraft,
    ) -> dict[str, object]:
        now = self.clock()
        record = self._find_order(tenant_id, draft.orderKey, lock=True)
        target_hash = draft_content_hash(draft)
        if record is not None and record.submission_status == "submitted":
            if record.content_hash != target_hash:
                raise PurchaseServiceError(
                    "purchase_order_locked",
                    "采购单已正式提交，不能用草稿覆盖",
                    409,
                )
            return self._payload(record, unchanged=True)

        changed = record is None or record.content_hash != target_hash
        if record is None:
            record = PurchaseOrder(
                tenant_id=tenant_id,
                order_key=draft.orderKey,
                schema_version=draft.schemaVersion,
                draft_payload=canonical_draft_dict(draft),
                content_hash=target_hash,
                draft_revision=1,
                submission_status="draft",
                sync_status="pending",
                created_by_user_id=actor_user_id,
                last_edited_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            self.session.add(record)
            self.session.flush()
        elif changed:
            record.schema_version = draft.schemaVersion
            record.draft_payload = canonical_draft_dict(draft)
            record.content_hash = target_hash
            record.draft_revision += 1
            record.sync_status = "pending"
            record.last_edited_by_user_id = actor_user_id
            record.updated_at = now

        if changed:
            self._reconcile_lines(record, draft, workflow_status="draft", now=now)
            self._enqueue(record, "draft.saved", now)
        self.session.flush()
        return self._payload(record, unchanged=not changed)

    def submit(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        draft: PurchaseDraft,
    ) -> dict[str, object]:
        try:
            validate_formal_submit(draft)
        except ValueError as exc:
            raise PurchaseServiceError("purchase_submit_invalid", str(exc), 422) from None

        now = self.clock()
        target_hash = draft_content_hash(draft)
        record = self._find_order(tenant_id, draft.orderKey, lock=True)
        if record is not None and record.submission_status == "submitted":
            if record.content_hash != target_hash:
                raise PurchaseServiceError(
                    "purchase_order_locked",
                    "采购单已正式提交，内容变化必须走退回重提流程",
                    409,
                )
            return self._payload(record, unchanged=True)

        changed = record is None or record.content_hash != target_hash
        if record is None:
            record = PurchaseOrder(
                tenant_id=tenant_id,
                order_key=draft.orderKey,
                schema_version=draft.schemaVersion,
                draft_payload=canonical_draft_dict(draft),
                content_hash=target_hash,
                draft_revision=1,
                submission_status="draft",
                sync_status="pending",
                created_by_user_id=actor_user_id,
                last_edited_by_user_id=actor_user_id,
                created_at=now,
                updated_at=now,
            )
            self.session.add(record)
            self.session.flush()
        elif changed:
            record.schema_version = draft.schemaVersion
            record.draft_payload = canonical_draft_dict(draft)
            record.content_hash = target_hash
            record.draft_revision += 1
            record.last_edited_by_user_id = actor_user_id

        record.submission_status = "submitted"
        record.sync_status = "pending"
        record.submitted_by_user_id = actor_user_id
        record.submitted_at = now
        record.updated_at = now
        self._reconcile_lines(record, draft, workflow_status="unclaimed", now=now)
        self._enqueue(record, "order.submitted", now)
        self.session.flush()
        return self._payload(record, unchanged=False)

    def get(self, *, tenant_id: uuid.UUID, order_key: str) -> dict[str, object]:
        normalized = str(order_key or "").strip()
        if not normalized or len(normalized) > 800:
            raise PurchaseServiceError("purchase_order_key_invalid", "采购单标识无效", 422)
        record = self._find_order(tenant_id, normalized, lock=False)
        if record is None:
            raise PurchaseServiceError("purchase_order_not_found", "采购单不存在", 404)
        return self._payload(record, unchanged=True)

    def workspace_overview(self, *, tenant_id: uuid.UUID) -> dict[str, object]:
        submission_counts = dict(
            self.session.execute(
                select(PurchaseOrder.submission_status, func.count(PurchaseOrder.id))
                .where(PurchaseOrder.tenant_id == tenant_id)
                .group_by(PurchaseOrder.submission_status)
            ).all()
        )
        sync_counts = dict(
            self.session.execute(
                select(PurchaseOrder.sync_status, func.count(PurchaseOrder.id))
                .where(PurchaseOrder.tenant_id == tenant_id)
                .group_by(PurchaseOrder.sync_status)
            ).all()
        )
        workflow_counts = dict(
            self.session.execute(
                select(PurchaseOrderLine.workflow_status, func.count(PurchaseOrderLine.id))
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
                .where(
                    PurchaseOrder.tenant_id == tenant_id,
                    PurchaseOrderLine.is_active.is_(True),
                )
                .group_by(PurchaseOrderLine.workflow_status)
            ).all()
        )
        normalized_submission = {
            status: int(submission_counts.get(status, 0))
            for status in self.SUBMISSION_STATUSES
        }
        normalized_sync = {
            status: int(sync_counts.get(status, 0))
            for status in self.SYNC_STATUSES
        }
        normalized_workflow = {
            status: int(workflow_counts.get(status, 0))
            for status in self.WORKFLOW_STATUSES
        }
        return {
            "orders": {
                "total": sum(normalized_submission.values()),
                "bySubmissionStatus": normalized_submission,
                "bySyncStatus": normalized_sync,
            },
            "lines": {
                "total": sum(normalized_workflow.values()),
                "byWorkflowStatus": normalized_workflow,
            },
        }

    def workspace_list(
        self,
        *,
        tenant_id: uuid.UUID,
        submission_status: str | None = None,
        sync_status: str | None = None,
        workflow_status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, object]:
        filters = [PurchaseOrder.tenant_id == tenant_id]
        if submission_status is not None:
            filters.append(PurchaseOrder.submission_status == submission_status)
        if sync_status is not None:
            filters.append(PurchaseOrder.sync_status == sync_status)
        if workflow_status is not None:
            filters.append(
                select(PurchaseOrderLine.id)
                .where(
                    PurchaseOrderLine.purchase_order_id == PurchaseOrder.id,
                    PurchaseOrderLine.is_active.is_(True),
                    PurchaseOrderLine.workflow_status == workflow_status,
                )
                .exists()
            )

        total = int(
            self.session.scalar(
                select(func.count(PurchaseOrder.id)).where(*filters)
            )
            or 0
        )
        records = list(
            self.session.scalars(
                select(PurchaseOrder)
                .where(*filters)
                .order_by(PurchaseOrder.updated_at.desc(), PurchaseOrder.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        lines_by_order = self._active_lines_by_order([record.id for record in records])
        users_by_id = self._users_by_id(
            [
                record.submitted_by_user_id
                for record in records
                if record.submitted_by_user_id is not None
            ]
        )
        return {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "items": [
                self._workspace_summary(
                    record,
                    lines_by_order.get(record.id, []),
                    users_by_id,
                )
                for record in records
            ],
        }

    def workspace_detail(
        self,
        *,
        tenant_id: uuid.UUID,
        purchase_order_id: uuid.UUID,
    ) -> dict[str, object]:
        record = self.session.scalar(
            select(PurchaseOrder).where(
                PurchaseOrder.id == purchase_order_id,
                PurchaseOrder.tenant_id == tenant_id,
            )
        )
        if record is None:
            raise PurchaseServiceError("purchase_order_not_found", "采购单不存在", 404)
        lines = self._active_lines_by_order([record.id]).get(record.id, [])
        users_by_id = self._users_by_id(
            [
                user_id
                for user_id in [
                    record.submitted_by_user_id,
                    *(line.claimed_by_user_id for line in lines),
                ]
                if user_id is not None
            ]
        )
        draft_header = dict(record.draft_payload)
        draft_header.pop("items", None)
        return {
            **self._workspace_summary(record, lines, users_by_id),
            "schemaVersion": record.schema_version,
            "contentHash": record.content_hash,
            "executionRevision": record.execution_revision,
            "draft": draft_header,
            "lines": [
                {
                    "purchaseOrderLineId": str(line.id),
                    "lineKey": line.line_key,
                    "lineNo": line.line_no,
                    "workflowStatus": line.workflow_status,
                    "claimedBy": self._public_user(
                        users_by_id.get(line.claimed_by_user_id)
                        if line.claimed_by_user_id
                        else None
                    ),
                    "claimedAt": line.claimed_at.isoformat() if line.claimed_at else None,
                    "updatedAt": line.updated_at.isoformat(),
                    "payload": line.payload,
                }
                for line in lines
            ],
        }

    def claim(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        purchase_order_ids: list[uuid.UUID],
        purchase_order_line_ids: list[uuid.UUID],
    ) -> dict[str, object]:
        order_ids = list(dict.fromkeys(purchase_order_ids))
        line_ids = list(dict.fromkeys(purchase_order_line_ids))
        if not order_ids and not line_ids:
            raise PurchaseServiceError(
                "purchase_claim_selection_required",
                "请至少选择一张采购单或一条采购明细",
                422,
            )

        if order_ids:
            found_order_ids = set(
                self.session.scalars(
                    select(PurchaseOrder.id).where(
                        PurchaseOrder.tenant_id == tenant_id,
                        PurchaseOrder.id.in_(order_ids),
                        PurchaseOrder.submission_status == "submitted",
                    )
                )
            )
            if found_order_ids != set(order_ids):
                raise PurchaseServiceError(
                    "purchase_order_not_found",
                    "采购单不存在或尚未正式提交",
                    404,
                )

        selectors = []
        if order_ids:
            selectors.append(PurchaseOrderLine.purchase_order_id.in_(order_ids))
        if line_ids:
            selectors.append(PurchaseOrderLine.id.in_(line_ids))
        lines = list(
            self.session.scalars(
                select(PurchaseOrderLine)
                .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
                .where(
                    PurchaseOrder.tenant_id == tenant_id,
                    PurchaseOrder.submission_status == "submitted",
                    PurchaseOrderLine.is_active.is_(True),
                    or_(*selectors),
                )
                .order_by(PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.line_no)
                .with_for_update()
            )
        )
        found_line_ids = {line.id for line in lines}
        if line_ids and not set(line_ids).issubset(found_line_ids):
            raise PurchaseServiceError(
                "purchase_line_not_found",
                "采购明细不存在或不属于当前组织",
                404,
            )
        if not lines:
            raise PurchaseServiceError(
                "purchase_claim_empty",
                "所选采购单没有可认领的有效明细",
                409,
            )

        conflicts = [
            line
            for line in lines
            if not (
                line.workflow_status == "unclaimed"
                or (
                    line.workflow_status == "claimed"
                    and line.claimed_by_user_id == actor_user_id
                )
            )
        ]
        if conflicts:
            raise PurchaseServiceError(
                "purchase_line_claim_conflict",
                "部分采购明细已被其他采购员认领或已进入采购流程，请刷新后重试",
                409,
            )

        now = self.clock()
        newly_claimed = 0
        for line in lines:
            if line.workflow_status == "claimed":
                continue
            line.workflow_status = "claimed"
            line.claimed_by_user_id = actor_user_id
            line.claimed_at = now
            line.updated_at = now
            newly_claimed += 1
        actor = self.session.get(User, actor_user_id)
        self.session.flush()
        return {
            "purchaseOrderIds": sorted({str(line.purchase_order_id) for line in lines}),
            "purchaseOrderLineIds": [str(line.id) for line in lines],
            "lineCount": len(lines),
            "claimedCount": newly_claimed,
            "unchangedCount": len(lines) - newly_claimed,
            "claimant": self._public_user(actor),
            "claimedAt": now.isoformat(),
        }

    def save_split_plan(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        purchase_order_id: uuid.UUID,
        expected_revision: int,
        groups: list[dict[str, object]],
    ) -> dict[str, object]:
        order = self.session.scalar(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.id == purchase_order_id,
                PurchaseOrder.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if order is None or order.submission_status != "submitted":
            raise PurchaseServiceError(
                "purchase_order_not_found",
                "采购单不存在或尚未正式提交",
                404,
            )
        if order.execution_revision != expected_revision:
            raise PurchaseServiceError(
                "purchase_execution_revision_conflict",
                "采购分单已被更新，请刷新采购单详情后重试",
                409,
            )

        existing_splits = list(
            self.session.scalars(
                select(PurchaseSplit)
                .where(
                    PurchaseSplit.purchase_order_id == order.id,
                    PurchaseSplit.purchaser_user_id == actor_user_id,
                )
                .with_for_update()
            )
        )
        if any(split.status not in ("waiting_binding", "waiting_order") for split in existing_splits):
            raise PurchaseServiceError(
                "purchase_split_started",
                "采购分单已进入执行流程，不能整体重建",
                409,
            )

        claimed_lines = list(
            self.session.scalars(
                select(PurchaseOrderLine)
                .where(
                    PurchaseOrderLine.purchase_order_id == order.id,
                    PurchaseOrderLine.is_active.is_(True),
                    PurchaseOrderLine.workflow_status == "claimed",
                    PurchaseOrderLine.claimed_by_user_id == actor_user_id,
                )
                .order_by(PurchaseOrderLine.line_no)
                .with_for_update()
            )
        )
        if not claimed_lines:
            raise PurchaseServiceError(
                "purchase_split_no_claimed_lines",
                "请先认领采购明细，再创建采购分单",
                409,
            )
        lines_by_id = {line.id: line for line in claimed_lines}
        allocated_totals: dict[uuid.UUID, int] = defaultdict(int)
        resource_pairs: set[tuple[str, str]] = set()
        normalized_groups: list[dict[str, object]] = []
        order_site = str(order.draft_payload.get("site") or "").upper()
        for group in groups:
            normalized_lines: list[tuple[PurchaseOrderLine, int]] = []
            for allocation in group["lines"]:  # type: ignore[index]
                line_id = uuid.UUID(str(allocation["purchaseOrderLineId"]))
                line = lines_by_id.get(line_id)
                if line is None:
                    raise PurchaseServiceError(
                        "purchase_split_line_unavailable",
                        "采购分单包含未由当前采购员认领的明细",
                        409,
                    )
                quantity = int(allocation["quantity"])
                allocated_totals[line_id] += quantity
                normalized_lines.append((line, quantity))

            resource = group.get("resource")
            if resource is not None:
                resource = dict(resource)  # type: ignore[arg-type]
                if str(resource["site"]).upper() != order_site:
                    raise PurchaseServiceError(
                        "purchase_split_resource_site_mismatch",
                        "Hub 环境、买家号与采购单站点不一致",
                        422,
                    )
                pair = (
                    str(resource["hubEnvironmentRef"]),
                    str(resource["buyerAccountRef"]),
                )
                if pair in resource_pairs:
                    raise PurchaseServiceError(
                        "purchase_split_resource_duplicate",
                        "同一采购计划不能重复占用相同的 Hub 环境与买家号组合",
                        409,
                    )
                resource_pairs.add(pair)
            normalized_groups.append(
                {
                    "resource": resource,
                    "note": str(group.get("note") or "").strip() or None,
                    "lines": normalized_lines,
                }
            )

        if set(allocated_totals) != set(lines_by_id):
            raise PurchaseServiceError(
                "purchase_split_allocation_incomplete",
                "当前采购员已认领的明细必须全部分配到采购分单",
                422,
            )
        for line_id, line in lines_by_id.items():
            expected_qty = int(line.payload.get("purchaseQty") or 0)
            if allocated_totals[line_id] != expected_qty:
                raise PurchaseServiceError(
                    "purchase_split_quantity_mismatch",
                    f"第 {line.line_no} 行分单数量合计必须等于采购数量 {expected_qty}",
                    422,
                )

        existing_ids = [split.id for split in existing_splits]
        if existing_ids:
            self.session.execute(
                delete(PurchaseSplitLine).where(
                    PurchaseSplitLine.purchase_split_id.in_(existing_ids)
                )
            )
            self.session.execute(delete(PurchaseSplit).where(PurchaseSplit.id.in_(existing_ids)))
            self.session.flush()

        now = self.clock()
        actor_fragment = str(actor_user_id).replace("-", "")[:12].upper()
        order_fragment = str(order.id).replace("-", "")[:8].upper()
        created: list[PurchaseSplit] = []
        split_lines: dict[uuid.UUID, list[PurchaseSplitLine]] = {}
        for index, group in enumerate(normalized_groups, start=1):
            resource = group["resource"]
            split = PurchaseSplit(
                tenant_id=tenant_id,
                purchase_order_id=order.id,
                split_no=f"CGFD-{order_fragment}-{actor_fragment}-{index:02d}",
                status="waiting_order" if resource else "waiting_binding",
                purchaser_user_id=actor_user_id,
                site=order_site,
                hub_environment_ref=(
                    str(resource["hubEnvironmentRef"]) if resource else None
                ),
                hub_environment_name=(
                    str(resource["hubEnvironmentName"]) if resource else None
                ),
                buyer_account_ref=(str(resource["buyerAccountRef"]) if resource else None),
                buyer_account_label=(
                    str(resource["buyerAccountLabel"]) if resource else None
                ),
                note=group["note"],
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.session.add(split)
            self.session.flush()
            created.append(split)
            split_lines[split.id] = []
            for line, quantity in group["lines"]:  # type: ignore[assignment]
                split_line = PurchaseSplitLine(
                    purchase_split_id=split.id,
                    purchase_order_line_id=line.id,
                    allocated_qty=quantity,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(split_line)
                split_lines[split.id].append(split_line)

        order.execution_revision += 1
        order.updated_at = now
        actor = self.session.get(User, actor_user_id)
        self.session.flush()
        return {
            "purchaseOrderId": str(order.id),
            "executionRevision": order.execution_revision,
            "splitCount": len(created),
            "lineCount": len(claimed_lines),
            "items": [
                self._execution_item(split, order, split_lines[split.id], lines_by_id, actor)
                for split in created
            ],
        }

    def execution_list(
        self,
        *,
        tenant_id: uuid.UUID,
        status: str | None = None,
        site: str | None = None,
        binding: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, object]:
        filters = [PurchaseSplit.tenant_id == tenant_id]
        if status:
            filters.append(PurchaseSplit.status == status)
        if site:
            filters.append(PurchaseSplit.site == site)
        splits = list(
            self.session.scalars(
                select(PurchaseSplit)
                .where(*filters)
                .order_by(PurchaseSplit.updated_at.desc(), PurchaseSplit.id.desc())
            )
        )
        order_ids = {split.purchase_order_id for split in splits}
        orders = {
            order.id: order
            for order in self.session.scalars(
                select(PurchaseOrder).where(PurchaseOrder.id.in_(order_ids))
            )
        } if order_ids else {}
        purchaser_ids = {split.purchaser_user_id for split in splits}
        purchasers = self._users_by_id(list(purchaser_ids))
        split_ids = [split.id for split in splits]
        stored_split_lines = list(
            self.session.scalars(
                select(PurchaseSplitLine)
                .where(PurchaseSplitLine.purchase_split_id.in_(split_ids))
                .order_by(PurchaseSplitLine.created_at, PurchaseSplitLine.id)
            )
        ) if split_ids else []
        source_line_ids = {item.purchase_order_line_id for item in stored_split_lines}
        source_lines = {
            line.id: line
            for line in self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.id.in_(source_line_ids))
            )
        } if source_line_ids else {}
        lines_by_split: dict[uuid.UUID, list[PurchaseSplitLine]] = defaultdict(list)
        for item in stored_split_lines:
            lines_by_split[item.purchase_split_id].append(item)

        items = [
            self._execution_item(
                split,
                orders[split.purchase_order_id],
                lines_by_split.get(split.id, []),
                source_lines,
                purchasers.get(split.purchaser_user_id),
            )
            for split in splits
            if split.purchase_order_id in orders
        ]
        if binding == "bound":
            items = [item for item in items if item["resourceBound"]]
        elif binding == "unbound":
            items = [item for item in items if not item["resourceBound"]]
        normalized_keyword = str(keyword or "").strip().casefold()
        if normalized_keyword:
            items = [
                item
                for item in items
                if normalized_keyword
                in " ".join(
                    [
                        str(item.get("id") or ""),
                        str(item.get("salesOrderNo") or ""),
                        str(item.get("sourceOrder") or ""),
                        str(item.get("store") or ""),
                        str(item.get("purchaser") or ""),
                        *[
                            f"{line.get('sku', '')} {line.get('spec', '')}"
                            for line in item["lines"]  # type: ignore[index]
                        ],
                    ]
                ).casefold()
            ]
        total = len(items)
        start = (page - 1) * page_size
        return {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "items": items[start : start + page_size],
        }

    @staticmethod
    def _public_user(user: User | None) -> dict[str, str] | None:
        if user is None:
            return None
        return {"id": str(user.id), "name": user.display_name}

    def _execution_item(
        self,
        split: PurchaseSplit,
        order: PurchaseOrder,
        split_lines: list[PurchaseSplitLine],
        source_lines: dict[uuid.UUID, PurchaseOrderLine],
        purchaser: User | None,
    ) -> dict[str, object]:
        lines: list[dict[str, object]] = []
        totals_by_currency: dict[str, Decimal] = defaultdict(Decimal)
        for item in split_lines:
            source = source_lines.get(item.purchase_order_line_id)
            if source is None:
                continue
            payload = source.payload
            currency = str(payload.get("purchaseCurrency") or "").upper()
            price = Decimal(str(payload.get("guidePrice") or 0))
            if currency:
                totals_by_currency[currency] += price * item.allocated_qty
            lines.append(
                {
                    "purchaseOrderLineId": str(source.id),
                    "lineNo": source.line_no,
                    "sku": payload.get("sellerSku") or "",
                    "spec": " / ".join(
                        str(value)
                        for value in (payload.get("mainSpec"), payload.get("subSpec"))
                        if value
                    ),
                    "qty": item.allocated_qty,
                    "image": payload.get("productImageUrl") or "",
                }
            )
        currencies = sorted(totals_by_currency)
        currency = currencies[0] if len(currencies) == 1 else ("MIXED" if currencies else "")
        amount = (
            float(totals_by_currency[currency])
            if currency and currency != "MIXED"
            else float(sum(totals_by_currency.values(), Decimal("0")))
        )
        draft = order.draft_payload
        resource_bound = bool(split.hub_environment_ref and split.buyer_account_ref)
        return {
            "purchaseSplitId": str(split.id),
            "id": split.split_no,
            "purchaseOrderId": str(order.id),
            "sourceOrder": draft.get("packageId") or order.order_key,
            "salesOrderNo": draft.get("platformOrderNo") or "",
            "site": split.site,
            "store": draft.get("storeName") or "",
            "purchaser": purchaser.display_name if purchaser else "—",
            "purchaserUserId": str(split.purchaser_user_id),
            "status": split.status,
            "amount": amount,
            "currency": currency,
            "guideTotalsByCurrency": {
                code: float(value) for code, value in sorted(totals_by_currency.items())
            },
            "resourceBound": resource_bound,
            "hub": split.hub_environment_name or "",
            "buyer": split.buyer_account_label or "",
            "resource": {
                "hubEnvironmentRef": split.hub_environment_ref,
                "hubEnvironmentName": split.hub_environment_name,
                "buyerAccountRef": split.buyer_account_ref,
                "buyerAccountLabel": split.buyer_account_label,
                "site": split.site,
            } if resource_bound else None,
            "platformOrderNo": split.platform_order_no or "",
            "updatedAt": split.updated_at.isoformat(),
            "note": split.note or "",
            "version": split.version,
            "lines": lines,
        }

    def _find_order(
        self,
        tenant_id: uuid.UUID,
        order_key_value: str,
        *,
        lock: bool,
    ) -> PurchaseOrder | None:
        statement = select(PurchaseOrder).where(
            PurchaseOrder.tenant_id == tenant_id,
            PurchaseOrder.order_key == order_key_value,
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _active_lines_by_order(
        self,
        order_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[PurchaseOrderLine]]:
        result: dict[uuid.UUID, list[PurchaseOrderLine]] = {}
        if not order_ids:
            return result
        for line in self.session.scalars(
            select(PurchaseOrderLine)
            .where(
                PurchaseOrderLine.purchase_order_id.in_(order_ids),
                PurchaseOrderLine.is_active.is_(True),
            )
            .order_by(PurchaseOrderLine.purchase_order_id, PurchaseOrderLine.line_no)
        ):
            result.setdefault(line.purchase_order_id, []).append(line)
        return result

    def _users_by_id(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, User]:
        if not user_ids:
            return {}
        return {
            user.id: user
            for user in self.session.scalars(
                select(User).where(User.id.in_(set(user_ids)))
            )
        }

    def _workspace_summary(
        self,
        record: PurchaseOrder,
        lines: list[PurchaseOrderLine],
        users_by_id: dict[uuid.UUID, User],
    ) -> dict[str, object]:
        draft = record.draft_payload
        workflow_counts = {status: 0 for status in self.WORKFLOW_STATUSES}
        preview_images: list[str] = []
        for line in lines:
            workflow_counts[line.workflow_status] = (
                workflow_counts.get(line.workflow_status, 0) + 1
            )
            image_url = str(line.payload.get("productImageUrl") or "").strip()
            if image_url and image_url not in preview_images:
                preview_images.append(image_url)
        submitted_by = None
        if record.submitted_by_user_id is not None:
            user = users_by_id.get(record.submitted_by_user_id)
            if user is not None:
                submitted_by = {"id": str(user.id), "name": user.display_name}
        return {
            "purchaseOrderId": str(record.id),
            "orderKey": record.order_key,
            "packageId": draft.get("packageId"),
            "platformOrderNo": draft.get("platformOrderNo"),
            "storeName": draft.get("storeName"),
            "site": draft.get("site"),
            "salesCurrency": draft.get("salesCurrency"),
            "salesAmount": draft.get("salesAmount"),
            "dianxiaomiOrderTime": draft.get("dianxiaomiOrderTime"),
            "guideTotalsByCurrency": draft.get("guideTotalsByCurrency") or {},
            "submissionStatus": record.submission_status,
            "syncStatus": record.sync_status,
            "draftRevision": record.draft_revision,
            "itemCount": len(lines),
            "previewImages": preview_images[:3],
            "workflowCounts": workflow_counts,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "submittedAt": record.submitted_at.isoformat() if record.submitted_at else None,
            "submittedBy": submitted_by,
        }

    def _reconcile_lines(
        self,
        record: PurchaseOrder,
        draft: PurchaseDraft,
        *,
        workflow_status: str,
        now: datetime,
    ) -> None:
        existing = {
            item.line_key: item
            for item in self.session.scalars(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.purchase_order_id == record.id
                )
            )
        }
        active_keys: set[str] = set()
        for item in draft.items:
            item_key = line_key(draft.orderKey, item.lineNo)
            active_keys.add(item_key)
            payload = item.model_dump(mode="json")
            target_hash = line_content_hash(item)
            stored = existing.get(item_key)
            if stored is None:
                self.session.add(
                    PurchaseOrderLine(
                        purchase_order_id=record.id,
                        line_key=item_key,
                        line_no=item.lineNo,
                        payload=payload,
                        content_hash=target_hash,
                        is_active=True,
                        workflow_status=workflow_status,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            stored.line_no = item.lineNo
            stored.payload = payload
            stored.content_hash = target_hash
            stored.is_active = True
            if stored.workflow_status == "draft" or workflow_status == "draft":
                stored.workflow_status = workflow_status
            stored.updated_at = now
        for item_key, stored in existing.items():
            if item_key not in active_keys and stored.is_active:
                stored.is_active = False
                stored.updated_at = now

    def _enqueue(self, record: PurchaseOrder, event_type: str, now: datetime) -> None:
        dedupe_key = f"{record.id}:{event_type}:{record.draft_revision}"
        existing = self.session.scalar(
            select(PurchaseSyncOutbox.id).where(PurchaseSyncOutbox.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return
        self.session.add(
            PurchaseSyncOutbox(
                tenant_id=record.tenant_id,
                purchase_order_id=record.id,
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload={
                    "purchaseOrderId": str(record.id),
                    "draftRevision": record.draft_revision,
                },
                status="pending",
                attempt_count=0,
                available_at=now,
            )
        )

    def _payload(self, record: PurchaseOrder, *, unchanged: bool) -> dict[str, object]:
        submitted_by = None
        if record.submitted_by_user_id is not None:
            user = self.session.get(User, record.submitted_by_user_id)
            if user is not None:
                submitted_by = {"id": str(user.id), "name": user.display_name}
        return {
            "purchaseOrderId": str(record.id),
            "orderKey": record.order_key,
            "submissionStatus": record.submission_status,
            "syncStatus": record.sync_status,
            "draftRevision": record.draft_revision,
            "contentHash": record.content_hash,
            "unchanged": unchanged,
            "savedAt": record.updated_at.isoformat(),
            "submittedAt": record.submitted_at.isoformat() if record.submitted_at else None,
            "submittedBy": submitted_by,
            "draft": record.draft_payload,
        }
