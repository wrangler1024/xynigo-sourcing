"""Durable ingestion of real local procurement-operation results."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from .buyer_account_sync import enqueue_buyer_account_mirror
from .models import (
    BuyerAccount,
    EnvironmentAccountRunGuard,
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    EnvironmentNameSequence,
    ExecutorTask,
    HubEnvironmentInventory,
    LogisticsQueryResult,
    LogisticsQueryRun,
    LocalExecutor,
    OperationalSyncOutbox,
    Tenant,
    User,
)
from .operation_contract import (
    EnvironmentCreationRunBody,
    EnvironmentCreationRunCreateBody,
    EnvironmentRetryRunCreateBody,
    LogisticsQueryRunBody,
    LogisticsQueryRunCreateBody,
)
from .purchase_service import PurchaseServiceError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _payload_hash(
    body: (
        EnvironmentCreationRunBody
        | EnvironmentCreationRunCreateBody
        | EnvironmentRetryRunCreateBody
        | LogisticsQueryRunBody
        | LogisticsQueryRunCreateBody
    ),
) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _milliseconds(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def _nonempty(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _run_status(success_count: int, total_count: int) -> str:
    if success_count == total_count:
        return "completed"
    if success_count:
        return "partial_failure"
    return "failed"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class OperationRunService:
    """Cloud-owned lifecycle and read model for local business operations."""

    TERMINAL_STATUSES = frozenset(
        {"completed", "partial_failure", "failed", "cancelled", "uncertain"}
    )

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_environment_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        body: EnvironmentCreationRunCreateBody,
    ) -> tuple[EnvironmentCreationRun, bool]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(EnvironmentCreationRun).where(
                EnvironmentCreationRun.tenant_id == tenant_id,
                EnvironmentCreationRun.source_run_key == body.idempotencyKey,
            )
        )
        if existing is not None:
            if existing.payload_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一建环境任务标识已提交不同请求",
                    409,
                )
            return existing, True
        now = utcnow()
        run = EnvironmentCreationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.idempotencyKey,
            payload_hash=digest,
            executor_id=body.executorId,
            run_mode=body.mode,
            site=body.site,
            purchase_date=body.purchaseDate,
            environment_group=body.environmentGroup,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=body.totalCount,
            total_count=body.totalCount,
            success_count=0,
            failed_count=0,
            ip_ok_count=0,
            ip_total_count=0,
            request_summary={
                "mode": body.mode,
                "cloudPlanId": body.cloudPlanId,
                "buyerLabel": body.buyerLabel,
                "verifySampleCount": body.verifySampleCount,
                "assignments": [
                    item.model_dump(mode="json") for item in body.assignments
                ],
            },
            source="cloud_web",
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        self.session.flush()
        return run, False

    def create_logistics_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        body: LogisticsQueryRunCreateBody,
    ) -> tuple[LogisticsQueryRun, bool]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.source_run_key == body.idempotencyKey,
            )
        )
        if existing is not None:
            accepted_hash = existing.result_payload_hash or (
                existing.payload_hash if existing.source != "cloud_web" else None
            )
            if accepted_hash is not None and accepted_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一物流查询任务标识已提交不同请求",
                    409,
                )
            return existing, True
        parent: LogisticsQueryRun | None = None
        if body.parentRunId is not None:
            parent = self.session.scalar(
                select(LogisticsQueryRun).where(
                    LogisticsQueryRun.id == body.parentRunId,
                    LogisticsQueryRun.tenant_id == tenant_id,
                    LogisticsQueryRun.actor_user_id == actor_user_id,
                )
            )
            if parent is None:
                raise PurchaseServiceError(
                    "operation_parent_run_not_found", "原物流查询批次不存在", 404
                )
            if parent.executor_id != body.executorId or parent.site != body.site:
                raise PurchaseServiceError(
                    "operation_retry_context_changed",
                    "执行设备或查询站点已变化，请重新发起整批查询",
                    409,
                )
            if parent.status not in self.TERMINAL_STATUSES:
                raise PurchaseServiceError(
                    "operation_parent_run_active", "原物流查询仍在执行", 409
                )
            effective_serials = {
                row["environmentSerial"]
                for row in self._effective_logistics_rows(parent)
            }
            if not set(body.environmentSerials).issubset(effective_serials):
                raise PurchaseServiceError(
                    "operation_retry_rows_changed",
                    "待重查环境已变化，请刷新后重试",
                    409,
                )
        now = utcnow()
        run_id = uuid.uuid4()
        root_run_id = (
            (parent.root_run_id or parent.id) if parent is not None else run_id
        )
        run = LogisticsQueryRun(
            id=run_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.idempotencyKey,
            payload_hash=digest,
            executor_id=body.executorId,
            parent_run_id=parent.id if parent is not None else None,
            root_run_id=root_run_id,
            query_mode=body.queryMode,
            site=body.site,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=len(body.environmentSerials),
            total_count=len(body.environmentSerials),
            success_count=0,
            failed_count=0,
            request_summary={
                "environmentSerials": list(body.environmentSerials),
                "force": body.force,
                "browserMode": body.browserMode,
                "allowOpenEnvironment": body.allowOpenEnvironment,
                "parentRunId": str(parent.id) if parent is not None else None,
            },
            source="cloud_web",
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        self.session.flush()
        return run, False

    def create_environment_retry_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        parent: EnvironmentCreationRun,
        body: EnvironmentRetryRunCreateBody,
    ) -> tuple[EnvironmentCreationRun, bool]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(EnvironmentCreationRun).where(
                EnvironmentCreationRun.tenant_id == tenant_id,
                EnvironmentCreationRun.source_run_key == body.idempotencyKey,
            )
        )
        if existing is not None:
            if existing.payload_hash != digest or existing.parent_run_id != parent.id:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一环境重试标识已提交不同请求",
                    409,
                )
            return existing, True
        if parent.executor_id is None:
            raise PurchaseServiceError(
                "operation_run_executor_missing", "原环境任务没有可用执行器", 409
            )
        failed_refs = self._effective_environment_failed_refs(
            tenant_id=tenant_id, run=parent
        )
        requested_refs = set(body.accountRefs)
        if not requested_refs.issubset(failed_refs):
            raise PurchaseServiceError(
                "operation_retry_rows_changed",
                "待重试行已变化，请刷新任务后重新选择",
                409,
            )
        now = utcnow()
        run = EnvironmentCreationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.idempotencyKey,
            payload_hash=digest,
            executor_id=parent.executor_id,
            parent_run_id=parent.id,
            run_mode=(
                "retry_row" if body.retryMode == "single" else "retry_failed"
            ),
            site=parent.site,
            purchase_date=parent.purchase_date,
            environment_group=parent.environment_group,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=len(body.accountRefs),
            total_count=len(body.accountRefs),
            success_count=0,
            failed_count=0,
            ip_ok_count=0,
            ip_total_count=0,
            request_summary={
                "retryMode": body.retryMode,
                "accountRefs": list(body.accountRefs),
            },
            source="cloud_web",
            created_at=now,
            updated_at=now,
        )
        self.session.add(run)
        self.session.flush()
        return run, False

    _PURCHASER_CODES = {
        "新刚": "XG",
        "志恒": "ZH",
        "康德": "KD",
        "宇航": "YH",
    }

    @classmethod
    def _purchaser_code(cls, label: str) -> str:
        known = cls._PURCHASER_CODES.get(label)
        if known:
            return known
        # Legacy/test tenants may still carry a custom purchaser label. Keep
        # allocation deterministic even though the current desktop UI only
        # offers the managed roster.
        return "U" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:5].upper()

    @staticmethod
    def _account_ref(account: dict[str, Any]) -> str:
        return hashlib.sha256(
            str(account.get("email") or "").strip().casefold().encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _source_order_ref(account: dict[str, Any]) -> str:
        value = str(account.get("orderNo") or "").strip().casefold()
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def reserve_environment_names(
        self,
        *,
        run: EnvironmentCreationRun,
        plan_accounts: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Reserve monotonic names and permanent account/order identities.

        Locking the tenant row serializes first-time sequence creation as well
        as updates, so two different executors cannot both allocate the same
        suffix before either sequence row exists.
        """
        self.session.scalar(
            select(Tenant.id)
            .where(Tenant.id == run.tenant_id)
            .with_for_update()
        )
        buyers: list[str] = []
        for assignment in assignments:
            label = str(assignment.get("purchaserLabel") or "").strip()
            try:
                count = int(assignment.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if not label or count < 1:
                raise PurchaseServiceError(
                    "environment_assignment_invalid", "采购员分配无效", 422
                )
            buyers.extend([label] * count)
        if len(buyers) != len(plan_accounts):
            raise PurchaseServiceError(
                "environment_assignment_invalid", "采购员分配数量无效", 422
            )

        account_refs = [self._account_ref(item) for item in plan_accounts]
        order_refs = [self._source_order_ref(item) for item in plan_accounts]
        if (
            len(account_refs) != len(set(account_refs))
            or len(order_refs) != len(set(order_refs))
        ):
            raise PurchaseServiceError(
                "environment_plan_duplicate", "买家号或号商单号在本批次重复", 409
            )
        existing = list(
            self.session.scalars(
                select(HubEnvironmentInventory)
                .where(
                    HubEnvironmentInventory.tenant_id == run.tenant_id,
                    or_(
                        HubEnvironmentInventory.account_ref.in_(account_refs),
                        HubEnvironmentInventory.source_order_ref.in_(order_refs),
                    ),
                )
                .with_for_update()
            )
        )
        blocking = [
            row for row in existing
            if row.source_run_id != run.id and row.state != "deleted"
        ]
        if blocking:
            raise PurchaseServiceError(
                "environment_account_already_bound",
                "买家号或号商单号已存在 HubStudio 环境，请勿跨设备重复创建",
                409,
            )
        reusable = {
            row.account_ref: row for row in existing
            if row.account_ref in account_refs and row.state == "deleted"
        }

        now = utcnow()
        offsets: dict[str, int] = {}
        for buyer in buyers:
            code = self._purchaser_code(buyer)
            if code in offsets:
                continue
            sequence = self.session.scalar(
                select(EnvironmentNameSequence)
                .where(
                    EnvironmentNameSequence.tenant_id == run.tenant_id,
                    EnvironmentNameSequence.site == run.site,
                    EnvironmentNameSequence.purchase_date == run.purchase_date,
                    EnvironmentNameSequence.purchaser_code == code,
                )
                .with_for_update()
            )
            prefix = f"{code}-{run.site}-{run.purchase_date[-4:]}-"
            known_names = list(
                self.session.scalars(
                    select(HubEnvironmentInventory.environment_name).where(
                        HubEnvironmentInventory.tenant_id == run.tenant_id,
                        HubEnvironmentInventory.environment_name.like(prefix + "%"),
                    )
                )
            )
            known_max = max(
                [
                    int(match.group(1))
                    for value in known_names
                    if (
                        match := re.fullmatch(
                            re.escape(prefix) + r"(\d{3,})", value
                        )
                    )
                ] or [0]
            )
            if sequence is None:
                sequence = EnvironmentNameSequence(
                    id=uuid.uuid4(),
                    tenant_id=run.tenant_id,
                    site=run.site,
                    purchase_date=run.purchase_date,
                    purchaser_code=code,
                    last_value=known_max,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(sequence)
                self.session.flush()
            elif sequence.last_value < known_max:
                sequence.last_value = known_max
            offsets[code] = sequence.last_value

        planned: list[dict[str, str]] = []
        sequences: dict[str, EnvironmentNameSequence] = {
            row.purchaser_code: row
            for row in self.session.scalars(
                select(EnvironmentNameSequence).where(
                    EnvironmentNameSequence.tenant_id == run.tenant_id,
                    EnvironmentNameSequence.site == run.site,
                    EnvironmentNameSequence.purchase_date == run.purchase_date,
                    EnvironmentNameSequence.purchaser_code.in_(offsets),
                )
            )
        }
        for account_ref, order_ref, buyer in zip(
            account_refs, order_refs, buyers
        ):
            code = self._purchaser_code(buyer)
            offsets[code] += 1
            if offsets[code] > 999:
                raise PurchaseServiceError(
                    "environment_daily_sequence_exhausted",
                    "该采购员当日环境序号已超过 999，请调整购买日期后重试",
                    409,
                )
            env_name = (
                f"{code}-{run.site}-{run.purchase_date[-4:]}-{offsets[code]:03d}"
            )
            sequence = sequences[code]
            sequence.last_value = offsets[code]
            sequence.updated_at = now
            inventory = reusable.get(account_ref)
            if inventory is None:
                inventory = HubEnvironmentInventory(
                    id=uuid.uuid4(), tenant_id=run.tenant_id,
                    account_ref=account_ref, source_order_ref=order_ref,
                    environment_name=env_name, site=run.site,
                    environment_group=run.environment_group,
                    purchaser_label=buyer, state="reserved",
                    source_run_id=run.id, created_at=now, updated_at=now,
                )
                self.session.add(inventory)
            else:
                inventory.source_order_ref = order_ref
                inventory.environment_name = env_name
                inventory.environment_ref = None
                inventory.environment_serial = None
                inventory.site = run.site
                inventory.environment_group = run.environment_group
                inventory.purchaser_label = buyer
                inventory.state = "reserved"
                inventory.source_run_id = run.id
                inventory.last_observed_at = None
                inventory.updated_at = now
            planned.append({
                "accountRef": account_ref,
                "environmentName": env_name,
            })
        summary = dict(run.request_summary or {})
        summary["plannedEnvironmentNames"] = planned
        run.request_summary = summary
        run.updated_at = now
        self.session.flush()
        return planned

    def finalize_environment_inventory(
        self,
        *,
        run: EnvironmentCreationRun,
        status: str,
        now: datetime | None = None,
    ) -> None:
        now = now or utcnow()
        reservations = list(
            self.session.scalars(
                select(HubEnvironmentInventory)
                .where(HubEnvironmentInventory.source_run_id == run.id)
                .with_for_update()
            )
        )
        results = {
            row.account_ref: row
            for row in self.session.scalars(
                select(EnvironmentCreationResult).where(
                    EnvironmentCreationResult.run_id == run.id
                )
            )
        }
        uncertain_run = status == "uncertain" or (
            status == "failed"
            and str(run.error_code or "") in {
                "operation_task_failed",
                "environment_task_failed",
                "executor_lease_renew_failed",
                "lease_expired_after_start",
            }
        )
        for result in results.values():
            if result.status != "success":
                continue
            inventory = next(
                (row for row in reservations
                 if row.account_ref == result.account_ref),
                None,
            )
            if inventory is None:
                inventory = self.session.scalar(
                    select(HubEnvironmentInventory)
                    .where(
                        HubEnvironmentInventory.tenant_id == run.tenant_id,
                        HubEnvironmentInventory.account_ref == result.account_ref,
                    )
                    .with_for_update()
                )
            if inventory is None:
                inventory = HubEnvironmentInventory(
                    id=uuid.uuid4(), tenant_id=run.tenant_id,
                    account_ref=result.account_ref,
                    environment_name=result.environment_name,
                    site=run.site,
                    environment_group=run.environment_group,
                    purchaser_label=result.purchaser_label,
                    state="active", source_run_id=run.id,
                    created_at=now, updated_at=now,
                )
                self.session.add(inventory)
            inventory.environment_name = result.environment_name
            inventory.environment_ref = result.environment_ref
            inventory.environment_serial = result.environment_serial
            if inventory.source_order_ref is None:
                buyer_account = self.session.scalar(
                    select(BuyerAccount).where(
                        BuyerAccount.tenant_id == run.tenant_id,
                        BuyerAccount.account_ref == result.account_ref,
                    )
                )
                if buyer_account is not None:
                    inventory.source_order_ref = buyer_account.source_order_ref
            inventory.site = run.site
            inventory.environment_group = run.environment_group
            inventory.purchaser_label = result.purchaser_label
            inventory.state = "active"
            inventory.source_run_id = run.id
            inventory.last_observed_at = now
            inventory.updated_at = now
        for inventory in reservations:
            result = results.get(inventory.account_ref)
            if result is not None and result.status == "success":
                continue
            uncertain = uncertain_run or bool(
                result is not None
                and result.created_in_run
                and result.cleanup_status not in {"deleted", "not_required"}
            )
            inventory.state = "uncertain" if uncertain else "deleted"
            inventory.updated_at = now

    def acquire_environment_account_guards(
        self,
        *,
        run: EnvironmentCreationRun,
        account_refs: set[str],
        allow_cleanup_failed: bool = True,
    ) -> set[str]:
        """Atomically reserve account refs for one environment Run.

        Active and cleaning accounts fail closed.  A prior cleanup failure is
        transferred to the new Run and returned so the executor can perform a
        live HubStudio existence check before issuing any write.
        """
        normalized = {
            str(value or "").strip() for value in account_refs
            if str(value or "").strip()
        }
        if not normalized:
            raise PurchaseServiceError(
                "environment_run_accounts_missing",
                "建环境任务缺少安全账号引用",
                422,
            )
        now = utcnow()
        existing = list(
            self.session.scalars(
                select(EnvironmentAccountRunGuard)
                .where(
                    EnvironmentAccountRunGuard.tenant_id == run.tenant_id,
                    EnvironmentAccountRunGuard.account_ref.in_(normalized),
                )
                .with_for_update()
            )
        )
        blocking = [
            guard for guard in existing
            if guard.run_id != run.id
            and (
                guard.state in {"active", "cleanup_pending"}
                or (
                    guard.state == "cleanup_failed"
                    and not allow_cleanup_failed
                )
            )
        ]
        if blocking:
            raise PurchaseServiceError(
                "environment_cleanup_in_progress",
                "上一批环境任务仍在执行或撤销中，请等待清理完成后再提交",
                409,
            )
        by_ref = {guard.account_ref: guard for guard in existing}
        cleanup_blocked: set[str] = set()
        for account_ref in sorted(normalized):
            guard = by_ref.get(account_ref)
            if guard is None:
                self.session.add(EnvironmentAccountRunGuard(
                    id=uuid.uuid4(),
                    tenant_id=run.tenant_id,
                    account_ref=account_ref,
                    run_id=run.id,
                    state="active",
                    created_at=now,
                    updated_at=now,
                ))
                continue
            if guard.state == "cleanup_failed":
                cleanup_blocked.add(account_ref)
            guard.run_id = run.id
            guard.state = "active"
            guard.updated_at = now
        summary = dict(run.request_summary or {})
        summary["accountRefs"] = sorted(normalized)
        summary["cleanupBlockedAccountRefs"] = sorted(cleanup_blocked)
        run.request_summary = summary
        run.updated_at = now
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise PurchaseServiceError(
                "environment_cleanup_in_progress",
                "相同买家号已有环境任务正在提交，请稍后重试",
                409,
            ) from exc
        return cleanup_blocked

    def mark_environment_guards_cleanup_pending(
        self, *, run_id: uuid.UUID, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        for guard in self.session.scalars(
            select(EnvironmentAccountRunGuard).where(
                EnvironmentAccountRunGuard.run_id == run_id
            )
        ):
            guard.state = "cleanup_pending"
            guard.updated_at = now

    def finalize_environment_account_guards(
        self,
        *,
        run: EnvironmentCreationRun,
        status: str,
        summary: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Release successful guards or persist a fail-closed cleanup barrier."""
        now = now or utcnow()
        summary = summary or {}
        guards = list(
            self.session.scalars(
                select(EnvironmentAccountRunGuard).where(
                    EnvironmentAccountRunGuard.run_id == run.id
                )
            )
        )
        if not guards:
            return
        cleanup_total = int(summary.get("cleanupTotal") or 0)
        cleanup_done = int(summary.get("cleanupDone") or 0)
        cleanup_failed = int(summary.get("cleanupFailed") or 0)
        inherited_refs = {
            str(value or "").strip()
            for value in (
                (run.request_summary or {}).get("cleanupBlockedAccountRefs") or []
            )
            if str(value or "").strip()
        }
        retain_failed = status == "uncertain"
        if status == "cancelled":
            retain_failed = not (
                run.started_at is None
                or (
                    cleanup_failed == 0
                    and cleanup_done >= cleanup_total
                )
            )
        elif status == "failed" and inherited_refs:
            # A rerun admitted after an earlier cleanup failure performs a live
            # HubStudio existence check before any writes.  If that check still
            # fails, retain only the inherited uncertain accounts; unrelated
            # accounts in the same batch must remain retryable.
            for guard in guards:
                if guard.account_ref in inherited_refs:
                    guard.state = "cleanup_failed"
                    guard.updated_at = now
                else:
                    self.session.delete(guard)
            return
        if retain_failed:
            for guard in guards:
                guard.state = "cleanup_failed"
                guard.updated_at = now
            return
        self.session.execute(
            delete(EnvironmentAccountRunGuard).where(
                EnvironmentAccountRunGuard.run_id == run.id
            )
        )

    def _effective_environment_failed_refs(
        self,
        *,
        tenant_id: uuid.UUID,
        run: EnvironmentCreationRun,
    ) -> set[str]:
        chain = [run]
        seen = {run.id}
        parent_id = run.parent_run_id
        while parent_id is not None and len(chain) < 20:
            if parent_id in seen:
                raise PurchaseServiceError(
                    "operation_run_parent_cycle", "环境任务重试链异常", 409
                )
            parent = self.session.scalar(
                select(EnvironmentCreationRun).where(
                    EnvironmentCreationRun.id == parent_id,
                    EnvironmentCreationRun.tenant_id == tenant_id,
                )
            )
            if parent is None:
                break
            chain.append(parent)
            seen.add(parent.id)
            parent_id = parent.parent_run_id
        effective: dict[str, str] = {}
        for item in reversed(chain):
            for row in self.session.scalars(
                select(EnvironmentCreationResult).where(
                    EnvironmentCreationResult.run_id == item.id
                )
            ):
                effective[row.account_ref] = row.status
        return {
            account_ref
            for account_ref, status in effective.items()
            if status == "failed"
        }

    def get_environment_run(
        self, *, tenant_id: uuid.UUID, run_id: uuid.UUID
    ) -> EnvironmentCreationRun:
        run = self.session.scalar(
            select(EnvironmentCreationRun).where(
                EnvironmentCreationRun.id == run_id,
                EnvironmentCreationRun.tenant_id == tenant_id,
            )
        )
        if run is None:
            raise PurchaseServiceError("operation_run_not_found", "建环境任务不存在", 404)
        return run

    def get_logistics_run(
        self, *, tenant_id: uuid.UUID, run_id: uuid.UUID
    ) -> LogisticsQueryRun:
        run = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.id == run_id,
                LogisticsQueryRun.tenant_id == tenant_id,
            )
        )
        if run is None:
            raise PurchaseServiceError("operation_run_not_found", "物流任务不存在", 404)
        return run

    def latest_environment_run(
        self, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> EnvironmentCreationRun | None:
        return self.session.scalar(
            select(EnvironmentCreationRun)
            .where(
                EnvironmentCreationRun.tenant_id == tenant_id,
                EnvironmentCreationRun.actor_user_id == actor_user_id,
            )
            .order_by(EnvironmentCreationRun.updated_at.desc())
            .limit(1)
        )

    def latest_logistics_run(
        self, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID
    ) -> LogisticsQueryRun | None:
        return self.session.scalar(
            select(LogisticsQueryRun)
            .where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.actor_user_id == actor_user_id,
            )
            .order_by(LogisticsQueryRun.updated_at.desc())
            .limit(1)
        )

    def resolve_latest_logistics_history_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        root_run_id: uuid.UUID,
        allow_tenant_scope: bool = False,
    ) -> tuple[LogisticsQueryRun, LogisticsQueryRun]:
        """Resolve an allowed logical batch and its newest retry descendant."""

        requested_query = select(LogisticsQueryRun).where(
            LogisticsQueryRun.id == root_run_id,
            LogisticsQueryRun.tenant_id == tenant_id,
        )
        if not allow_tenant_scope:
            requested_query = requested_query.where(
                LogisticsQueryRun.actor_user_id == actor_user_id
            )
        requested = self.session.scalar(requested_query)
        if requested is None:
            raise PurchaseServiceError(
                "operation_run_not_found", "物流查询历史不存在", 404
            )
        resolved_root_id = requested.root_run_id
        if resolved_root_id is None:
            lineage = self._logistics_lineage(requested)
            resolved_root_id = lineage[0].id
        history_actor_user_id = requested.actor_user_id
        root = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.id == resolved_root_id,
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.actor_user_id == history_actor_user_id,
            )
        )
        if root is None:
            raise PurchaseServiceError(
                "operation_run_not_found", "物流查询历史不存在", 404
            )
        latest = self.session.scalar(
            select(LogisticsQueryRun)
            .where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.actor_user_id == history_actor_user_id,
                or_(
                    LogisticsQueryRun.id == root.id,
                    LogisticsQueryRun.root_run_id == root.id,
                ),
            )
            .order_by(
                LogisticsQueryRun.created_at.desc(),
                LogisticsQueryRun.id.desc(),
            )
            .limit(1)
        )
        return root, latest or root

    def logistics_history(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        limit: int,
        cursor: uuid.UUID | None = None,
        site: str | None = None,
        status: str | None = None,
        include_all_users: bool = False,
        filter_actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        """Return one page of logical root batches in the authorized scope."""

        root_run = aliased(LogisticsQueryRun)
        latest_run = aliased(LogisticsQueryRun)
        descendant = aliased(LogisticsQueryRun)
        latest_id = (
            select(latest_run.id)
            .where(
                latest_run.tenant_id == tenant_id,
                latest_run.actor_user_id == root_run.actor_user_id,
                or_(
                    latest_run.id == root_run.id,
                    latest_run.root_run_id == root_run.id,
                ),
            )
            .order_by(latest_run.created_at.desc(), latest_run.id.desc())
            .limit(1)
            .correlate(root_run)
            .scalar_subquery()
        )
        retry_count = (
            select(func.count(descendant.id))
            .where(
                descendant.tenant_id == tenant_id,
                descendant.actor_user_id == root_run.actor_user_id,
                descendant.root_run_id == root_run.id,
                descendant.id != root_run.id,
            )
            .correlate(root_run)
            .scalar_subquery()
        )
        statement = (
            select(root_run, latest_run, retry_count.label("retry_count"))
            .join(latest_run, latest_run.id == latest_id)
            .where(
                root_run.tenant_id == tenant_id,
                root_run.parent_run_id.is_(None),
            )
        )
        if include_all_users:
            if filter_actor_user_id is not None:
                statement = statement.where(
                    root_run.actor_user_id == filter_actor_user_id
                )
        else:
            statement = statement.where(
                root_run.actor_user_id == actor_user_id
            )
        if site is not None:
            statement = statement.where(root_run.site == site)
        if status is not None:
            statement = statement.where(latest_run.status == status)
        if cursor is not None:
            cursor_query = select(LogisticsQueryRun).where(
                LogisticsQueryRun.id == cursor,
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.parent_run_id.is_(None),
            )
            if include_all_users:
                if filter_actor_user_id is not None:
                    cursor_query = cursor_query.where(
                        LogisticsQueryRun.actor_user_id == filter_actor_user_id
                    )
            else:
                cursor_query = cursor_query.where(
                    LogisticsQueryRun.actor_user_id == actor_user_id
                )
            cursor_run = self.session.scalar(cursor_query)
            if cursor_run is None:
                raise PurchaseServiceError(
                    "logistics_history_cursor_invalid", "查询历史游标无效", 422
                )
            statement = statement.where(
                or_(
                    root_run.created_at < cursor_run.created_at,
                    and_(
                        root_run.created_at == cursor_run.created_at,
                        root_run.id < cursor_run.id,
                    ),
                )
            )
        result_rows = list(
            self.session.execute(
                statement.order_by(root_run.created_at.desc(), root_run.id.desc())
                .limit(limit + 1)
            )
        )
        has_more = len(result_rows) > limit
        page = result_rows[:limit]
        items = [
            self._logistics_history_item(root, latest, int(retries or 0))
            for root, latest, retries in page
        ]
        actors = []
        if include_all_users:
            actors = [{
                "userId": str(user_id),
                "displayName": display_name,
                "status": user_status,
            } for user_id, display_name, user_status in self.session.execute(
                select(User.id, User.display_name, User.status)
                .join(
                    LogisticsQueryRun,
                    LogisticsQueryRun.actor_user_id == User.id,
                )
                .where(
                    User.tenant_id == tenant_id,
                    LogisticsQueryRun.tenant_id == tenant_id,
                    LogisticsQueryRun.parent_run_id.is_(None),
                )
                .distinct()
                .order_by(User.display_name, User.id)
            )]
        return {
            "items": items,
            "nextCursor": str(page[-1][0].id) if has_more and page else None,
            "hasMore": has_more,
            "actors": actors,
        }

    def logistics_history_snapshot(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        root_run_id: uuid.UUID,
        allow_tenant_scope: bool = False,
    ) -> dict[str, object]:
        root, latest = self.resolve_latest_logistics_history_run(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            root_run_id=root_run_id,
            allow_tenant_scope=allow_tenant_scope,
        )
        retry_count = int(
            self.session.scalar(
                select(func.count(LogisticsQueryRun.id)).where(
                    LogisticsQueryRun.tenant_id == tenant_id,
                    LogisticsQueryRun.actor_user_id == root.actor_user_id,
                    LogisticsQueryRun.root_run_id == root.id,
                    LogisticsQueryRun.id != root.id,
                )
            )
            or 0
        )
        snapshot = self.logistics_snapshot(latest)
        actor = self.session.get(User, root.actor_user_id)
        snapshot.update(
            {
                "rootRunId": str(root.id),
                "latestRunId": str(latest.id),
                "retryCount": retry_count,
                "originalEnvironmentSerials": self._original_logistics_serials(root),
                "logicalCreatedAt": _iso(root.created_at),
                "actorUserId": str(root.actor_user_id),
                "actorDisplayName": actor.display_name if actor else "未知用户",
                "actorStatus": actor.status if actor else "unknown",
            }
        )
        snapshot.update(self._effective_logistics_counts(latest))
        return snapshot

    def _original_logistics_serials(self, root: LogisticsQueryRun) -> list[str]:
        serials = [
            str(item)
            for item in ((root.request_summary or {}).get("environmentSerials") or [])
            if str(item)
        ]
        if serials:
            return serials
        return [
            row["environmentSerial"] for row in self._effective_logistics_rows(root)
        ]

    def _effective_logistics_counts(self, run: LogisticsQueryRun) -> dict[str, int]:
        rows = self._effective_logistics_rows(run)
        success_count = sum(row.get("status") == "ok" for row in rows)
        pending_count = sum(row.get("status") in {"pending", "running"} for row in rows)
        return {
            "totalCount": len(rows),
            "successCount": success_count,
            "failedCount": len(rows) - success_count - pending_count,
            "pendingCount": pending_count,
        }

    def _logistics_history_item(
        self,
        root: LogisticsQueryRun,
        latest: LogisticsQueryRun,
        retry_count: int,
    ) -> dict[str, object]:
        counts = self._effective_logistics_counts(latest)
        executor = (
            self.session.get(LocalExecutor, latest.executor_id)
            if latest.executor_id is not None else None
        )
        if executor is not None:
            executor_display_name = executor.display_name
            executor_attribution = "verified"
        elif root.source == "local_executor" or latest.source == "local_executor":
            executor_display_name = "旧版本地任务（未记录设备）"
            executor_attribution = "legacy_unattributed"
        else:
            executor_display_name = "原执行器已移除"
            executor_attribution = "removed"
        actor = self.session.get(User, root.actor_user_id)
        duration_seconds = self._logistics_duration_seconds(latest)
        return {
            "rootRunId": str(root.id),
            "latestRunId": str(latest.id),
            "site": root.site,
            "status": latest.status,
            "phase": latest.phase,
            "terminal": latest.status in self.TERMINAL_STATUSES,
            "retryCount": retry_count,
            "originalEnvironmentSerials": self._original_logistics_serials(root),
            "executorDisplayName": executor_display_name,
            "executorAttribution": executor_attribution,
            "actorUserId": str(root.actor_user_id),
            "actorDisplayName": actor.display_name if actor else "未知用户",
            "actorStatus": actor.status if actor else "unknown",
            "durationSec": duration_seconds,
            **counts,
            "startedAt": _iso(root.started_at),
            "completedAt": _iso(latest.completed_at),
            "createdAt": _iso(root.created_at),
            "updatedAt": _iso(latest.updated_at),
        }

    def _logistics_duration_seconds(self, run: LogisticsQueryRun) -> int:
        """Sum active execution time across the initial run and every retry."""

        now = utcnow()
        return sum(
            self._logistics_attempt_duration_seconds(item, now=now)
            for item in self._logistics_lineage(run)
        )

    def _logistics_attempt_duration_seconds(
        self, run: LogisticsQueryRun, *, now: datetime | None = None
    ) -> int:
        if run.started_at is None:
            return 0
        finished_at = run.completed_at or (
            run.updated_at
            if run.status in self.TERMINAL_STATUSES
            else (now or utcnow())
        )
        started_at = run.started_at
        if started_at.tzinfo is None and finished_at.tzinfo is not None:
            finished_at = finished_at.replace(tzinfo=None)
        elif started_at.tzinfo is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=started_at.tzinfo)
        return max(0, int((finished_at - started_at).total_seconds()))

    def environment_snapshot(
        self, run: EnvironmentCreationRun, *, unchanged: bool = False
    ) -> dict[str, object]:
        rows = list(
            self.session.scalars(
                select(EnvironmentCreationResult)
                .where(EnvironmentCreationResult.run_id == run.id)
                .order_by(EnvironmentCreationResult.created_at, EnvironmentCreationResult.id)
            )
        )
        task_result_code = (
            self.session.scalar(
                select(ExecutorTask.result_code).where(
                    ExecutorTask.id == run.executor_task_id
                )
            )
            if run.executor_task_id else None
        )
        created_count = sum(row.created_in_run for row in rows)
        recovered_count = sum(row.recovered_existing for row in rows)
        cleanup_total = sum(
            row.created_in_run and row.cleanup_status != "not_required"
            for row in rows
        )
        cleanup_done = sum(row.cleanup_status == "deleted" for row in rows)
        cleanup_failed = sum(row.cleanup_status == "failed" for row in rows)
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "executorId": str(run.executor_id) if run.executor_id else None,
            "executorTaskId": str(run.executor_task_id) if run.executor_task_id else None,
            "parentRunId": str(run.parent_run_id) if run.parent_run_id else None,
            "mode": run.run_mode,
            "site": run.site,
            "purchaseDate": run.purchase_date,
            "environmentGroup": run.environment_group,
            "status": run.status,
            "phase": run.phase,
            "attempt": run.attempt,
            "progressCompleted": run.progress_completed,
            "progressTotal": run.progress_total,
            "stopRequested": run.stop_requested,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "ipOkCount": run.ip_ok_count,
            "ipTotalCount": run.ip_total_count,
            "errorCode": run.error_code,
            "errorSummary": run.error_summary,
            "createdCount": created_count,
            "recoveredCount": recovered_count,
            "cleanupTotal": cleanup_total,
            "cleanupDone": cleanup_done,
            "cleanupFailed": cleanup_failed,
            "startedAt": _iso(run.started_at),
            "completedAt": _iso(run.completed_at),
            "lastHeartbeatAt": _iso(run.last_heartbeat_at),
            "createdAt": _iso(run.created_at),
            "updatedAt": _iso(run.updated_at),
            "terminal": run.status in self.TERMINAL_STATUSES,
            "resultCode": task_result_code,
            "unchanged": unchanged,
            "rows": [
                {
                    "accountRef": row.account_ref,
                    "accountLabel": row.account_label,
                    "purchaserLabel": row.purchaser_label,
                    "environmentName": row.environment_name,
                    "environmentRef": row.environment_ref,
                    "environmentSerial": row.environment_serial,
                    "status": row.status,
                    "currentStep": row.current_step,
                    "completedSteps": list(row.completed_steps or []),
                    "errorStep": row.error_step,
                    "errorSummary": row.error_summary,
                    "recoveredExisting": row.recovered_existing,
                    "createdInRun": row.created_in_run,
                    "cleanupStatus": row.cleanup_status,
                    "cleanupErrorCode": row.cleanup_error_code,
                    "cleanupErrorSummary": row.cleanup_error_summary,
                    "ipAddress": row.ip_address,
                    "ipCountry": row.ip_country,
                    "ipVerified": row.ip_verified,
                    "ipErrorCode": row.ip_error_code,
                    "ipErrorSummary": row.ip_error_summary,
                    "updatedAt": _iso(row.updated_at),
                }
                for row in rows
            ],
        }

    def logistics_snapshot(
        self, run: LogisticsQueryRun, *, unchanged: bool = False
    ) -> dict[str, object]:
        rows = self._effective_logistics_rows(run)
        duration_seconds = self._logistics_duration_seconds(run)
        attempt_duration_seconds = self._logistics_attempt_duration_seconds(run)
        progress_completed = run.progress_completed
        progress_total = run.progress_total
        retry_progress_completed: int | None = None
        retry_progress_total: int | None = None
        if run.parent_run_id is not None:
            original_serials = self._original_logistics_serials(
                self._logistics_lineage(run)[0]
            )
            retry_serials = [
                str(value)
                for value in (
                    (run.request_summary or {}).get("environmentSerials") or []
                )
                if str(value)
            ]
            retry_progress_total = len(retry_serials)
            retry_progress_completed = min(
                retry_progress_total, max(0, run.progress_completed)
            )
            progress_total = len(original_serials)
            progress_completed = min(
                progress_total,
                max(0, progress_total - retry_progress_total)
                + retry_progress_completed,
            )

            # Until a retried row is reported for the new attempt, the merged
            # lineage still contains its terminal result from the parent run.
            # Present that row as pending so the logical batch starts at
            # (original total - retry total) instead of looking 100% complete.
            if run.status not in self.TERMINAL_STATUSES:
                reported_serials = set(
                    self.session.scalars(
                        select(LogisticsQueryResult.environment_serial).where(
                            LogisticsQueryResult.run_id == run.id
                        )
                    )
                )
                by_serial = {
                    str(row.get("environmentSerial") or ""): row for row in rows
                }
                for serial in retry_serials:
                    if serial in reported_serials:
                        continue
                    previous = dict(by_serial.get(serial) or {})
                    previous.update(
                        {
                            "environmentSerial": serial,
                            "status": "pending",
                            "currentStep": "retry_pending",
                            "errorSummary": None,
                        }
                    )
                    by_serial[serial] = previous
                rows = [
                    by_serial[serial]
                    for serial in original_serials
                    if serial in by_serial
                ]
        task_result_code = (
            self.session.scalar(
                select(ExecutorTask.result_code).where(
                    ExecutorTask.id == run.executor_task_id
                )
            )
            if run.executor_task_id else None
        )
        return {
            "runId": str(run.id),
            "rootRunId": str(run.root_run_id or run.id),
            "runKey": run.source_run_key,
            "parentRunId": (
                str(run.parent_run_id)
                if run.parent_run_id
                else (run.request_summary or {}).get("parentRunId")
            ),
            "executorId": str(run.executor_id) if run.executor_id else None,
            "executorTaskId": str(run.executor_task_id) if run.executor_task_id else None,
            "queryMode": run.query_mode,
            "browserMode": str(
                (run.request_summary or {}).get("browserMode") or "default"
            ),
            "allowOpenEnvironment": bool(
                (run.request_summary or {}).get("allowOpenEnvironment")
            ),
            "site": run.site,
            "status": run.status,
            "phase": run.phase,
            "attempt": run.attempt,
            "progressCompleted": progress_completed,
            "progressTotal": progress_total,
            "retryProgressCompleted": retry_progress_completed,
            "retryProgressTotal": retry_progress_total,
            "stopRequested": run.stop_requested,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "durationSec": duration_seconds,
            "attemptDurationSec": attempt_duration_seconds,
            "startedAt": _iso(run.started_at),
            "completedAt": _iso(run.completed_at),
            "lastHeartbeatAt": _iso(run.last_heartbeat_at),
            "createdAt": _iso(run.created_at),
            "updatedAt": _iso(run.updated_at),
            "terminal": run.status in self.TERMINAL_STATUSES,
            "resultCode": task_result_code,
            "unchanged": unchanged,
            "displayTotalCount": len(rows),
            "rows": rows,
        }

    def _logistics_lineage(self, run: LogisticsQueryRun) -> list[LogisticsQueryRun]:
        if run.root_run_id is not None:
            # A retry can be launched from any historical descendant.  Fold every
            # earlier retry in the logical root batch so a later branch never
            # discards a successful sibling retry.
            return list(
                self.session.scalars(
                    select(LogisticsQueryRun)
                    .where(
                        LogisticsQueryRun.tenant_id == run.tenant_id,
                        LogisticsQueryRun.actor_user_id == run.actor_user_id,
                        or_(
                            LogisticsQueryRun.id == run.root_run_id,
                            LogisticsQueryRun.root_run_id == run.root_run_id,
                        ),
                        or_(
                            LogisticsQueryRun.created_at < run.created_at,
                            and_(
                                LogisticsQueryRun.created_at == run.created_at,
                                LogisticsQueryRun.id <= run.id,
                            ),
                        ),
                    )
                    .order_by(
                        LogisticsQueryRun.created_at.asc(),
                        LogisticsQueryRun.id.asc(),
                    )
                )
            )
        lineage = [run]
        visited = {run.id}
        current = run
        for _ in range(20):
            raw_parent = current.parent_run_id or (
                current.request_summary or {}
            ).get("parentRunId")
            if not raw_parent:
                break
            try:
                parent_id = uuid.UUID(str(raw_parent))
            except (TypeError, ValueError):
                break
            parent = self.session.scalar(
                select(LogisticsQueryRun).where(
                    LogisticsQueryRun.id == parent_id,
                    LogisticsQueryRun.tenant_id == run.tenant_id,
                    LogisticsQueryRun.actor_user_id == run.actor_user_id,
                )
            )
            if parent is None or parent.id in visited:
                break
            lineage.append(parent)
            visited.add(parent.id)
            current = parent
        lineage.reverse()
        return lineage

    def _effective_logistics_rows(self, run: LogisticsQueryRun) -> list[dict[str, Any]]:
        lineage = self._logistics_lineage(run)
        order: list[str] = []
        by_serial: dict[str, dict[str, Any]] = {}
        for ancestor in lineage:
            requested = list((ancestor.request_summary or {}).get("environmentSerials") or [])
            for serial in requested:
                serial = str(serial)
                if serial not in order:
                    order.append(serial)
            stored = list(
                self.session.scalars(
                    select(LogisticsQueryResult).where(
                        LogisticsQueryResult.run_id == ancestor.id
                    )
                )
            )
            for row in stored:
                expires_at = row.screenshot_expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                screenshot_available = bool(
                    row.screenshot_content
                    and (expires_at is None or expires_at > utcnow())
                )
                if row.environment_serial not in order:
                    order.append(row.environment_serial)
                by_serial[row.environment_serial] = {
                    "environmentSerial": row.environment_serial,
                    "environmentName": row.environment_name,
                    "status": row.status,
                    "currentStep": row.current_step,
                    "completedSteps": list(row.completed_steps or []),
                    "platformOrderNo": row.platform_order_no,
                    "orderTime": row.order_time_text,
                    "amount": row.amount_text,
                    "platformStatus": row.platform_status,
                    "statusLabel": row.status_label,
                    "fulfillmentStage": row.fulfillment_stage,
                    "trackingNumbers": list(row.tracking_numbers or []),
                    "packageNumbers": list(row.package_numbers or []),
                    "carrier": row.carrier,
                    "firstTrackingAt": _iso(row.first_tracking_at),
                    "firstTrackingTime": row.first_tracking_time_text,
                    "firstTrackingSummary": row.first_tracking_summary,
                    "firstTrackingLeadMinutes": row.first_tracking_lead_minutes,
                    "cancelled": row.cancelled,
                    "riskOrder": row.risk_order,
                    "riskSummary": row.risk_summary,
                    "ipAddress": row.ip_address,
                    "timeZone": row.time_zone,
                    "utcOffsetMinutes": row.utc_offset_minutes,
                    "queriedAt": _iso(row.queried_at),
                    "errorSummary": row.error_summary,
                    "screenshotStatus": row.screenshot_status,
                    "screenshotAvailable": screenshot_available,
                    "screenshotSizeKb": (
                        int(round((row.screenshot_size or 0) / 1024))
                        if screenshot_available else 0
                    ),
                    "updatedAt": _iso(row.updated_at),
                }
        return [by_serial[serial] for serial in order if serial in by_serial]

    def logistics_screenshot_row(
        self, *, run: LogisticsQueryRun, environment_serial: str
    ) -> LogisticsQueryResult | None:
        for ancestor in reversed(self._logistics_lineage(run)):
            row = self.session.scalar(
                select(LogisticsQueryResult).where(
                    LogisticsQueryResult.run_id == ancestor.id,
                    LogisticsQueryResult.environment_serial == environment_serial,
                )
            )
            if row is not None and row.screenshot_content:
                return row
        return None


class OperationResultService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest_environment_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        client_version: str | None,
        body: EnvironmentCreationRunBody,
    ) -> dict[str, object]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(EnvironmentCreationRun).where(
                EnvironmentCreationRun.tenant_id == tenant_id,
                EnvironmentCreationRun.source_run_key == body.runKey,
            )
        )
        if existing is not None:
            accepted_hash = existing.result_payload_hash or (
                existing.payload_hash if existing.source != "cloud_web" else None
            )
            if accepted_hash is not None and accepted_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一建环境任务标识已提交不同结果",
                    409,
                )
            if accepted_hash is not None:
                return self._environment_result(existing, unchanged=True)
            if (
                existing.site != body.site
                or existing.purchase_date != body.purchaseDate
                or existing.environment_group != body.environmentGroup
            ):
                raise PurchaseServiceError(
                    "operation_run_result_conflict",
                    "建环境结果与已受理任务参数不一致",
                    409,
                )

        now = utcnow()
        success_count = sum(item.status == "success" for item in body.results)
        failed_count = sum(item.status == "failed" for item in body.results)
        stopped_count = sum(item.status == "stopped" for item in body.results)
        run = existing or EnvironmentCreationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.runKey,
            payload_hash=digest,
            result_payload_hash=digest,
            run_mode="bound",
            site=body.site,
            purchase_date=body.purchaseDate,
            environment_group=body.environmentGroup,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=len(body.results),
            total_count=len(body.results),
            success_count=0,
            failed_count=0,
            ip_ok_count=0,
            ip_total_count=0,
            request_summary={},
            source=body.source,
            client_version=client_version,
            created_at=now,
            updated_at=now,
        )
        if existing is None:
            self.session.add(run)
        preserve_cancelled = existing is not None and existing.status == "cancelled"
        run.result_payload_hash = digest
        run.status = (
            "cancelled"
            if preserve_cancelled or stopped_count
            else _run_status(success_count, len(body.results))
        )
        run.phase = (
            "cancelled" if preserve_cancelled or stopped_count else "completed"
        )
        run.progress_completed = len(body.results)
        run.progress_total = len(body.results)
        run.total_count = len(body.results)
        run.success_count = success_count
        run.failed_count = failed_count
        run.ip_ok_count = sum(item.ok for item in body.ipChecks)
        run.ip_total_count = len(body.ipChecks)
        run.error_code = None
        run.error_summary = None
        run.started_at = run.started_at or body.startedAt
        run.completed_at = body.completedAt
        run.last_heartbeat_at = body.completedAt
        run.client_version = client_version
        run.updated_at = now
        # No ORM relationship links these rows, so SQLAlchemy cannot infer
        # that the parent must be inserted before the result batch.
        self.session.flush()
        ip_checks = {item.environmentName: item for item in body.ipChecks}
        resource_conflicts = 0
        for item in body.results:
            ip_check = ip_checks.get(item.environmentName)
            record = self.session.scalar(
                select(EnvironmentCreationResult).where(
                    EnvironmentCreationResult.run_id == run.id,
                    EnvironmentCreationResult.account_ref == item.accountRef,
                )
            )
            if record is None:
                record = EnvironmentCreationResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    tenant_id=tenant_id,
                    account_ref=item.accountRef,
                    account_label=item.accountLabel,
                    purchaser_label=item.purchaserLabel,
                    environment_name=item.environmentName,
                    status=item.status,
                    completed_steps=[],
                    recovered_existing=item.recoveredExisting,
                    feishu_sync_status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(record)
            record.account_label = item.accountLabel
            record.purchaser_label = item.purchaserLabel
            record.environment_name = item.environmentName
            record.environment_ref = item.environmentRef
            record.environment_serial = item.environmentSerial
            record.status = item.status
            record.current_step = "done" if item.status == "success" else item.errorStep or None
            record.error_step = item.errorStep or None
            record.error_summary = item.errorSummary or None
            record.binding_at = item.bindingAt
            record.recovered_existing = item.recoveredExisting
            record.created_in_run = item.createdInRun
            record.cleanup_status = item.cleanupStatus
            record.cleanup_error_code = item.cleanupErrorCode or None
            record.cleanup_error_summary = item.cleanupErrorSummary or None
            record.ip_address = ip_check.ipAddress if ip_check else None
            record.ip_country = ip_check.country if ip_check else None
            record.ip_city = ip_check.city if ip_check else None
            record.ip_isp = ip_check.isp if ip_check else None
            record.ip_verified = ip_check.ok if ip_check else None
            record.ip_error_code = (
                (ip_check.errorCode or None) if ip_check else None
            )
            record.ip_error_summary = (
                (ip_check.errorSummary or None) if ip_check else None
            )
            record.feishu_sync_status = "pending"
            record.updated_at = now
            fields = _nonempty({
                "同步键": f"environment:{tenant_id}:{record.id}",
                "建环境任务ID": body.runKey,
                "站点": body.site,
                "购买日期": body.purchaseDate,
                "环境分组": body.environmentGroup,
                "采购员": item.purchaserLabel,
                "买家号引用": item.accountRef,
                "买家号脱敏标签": item.accountLabel,
                "Hub环境名称": item.environmentName,
                "Hub环境编号": item.environmentRef,
                "环境序号": item.environmentSerial,
                "执行状态": "成功" if item.status == "success" else "失败",
                "失败步骤": item.errorStep,
                "错误摘要": item.errorSummary,
                "绑定时间": _milliseconds(item.bindingAt),
                "已有环境恢复": item.recoveredExisting,
                "出口IP": ip_check.ipAddress if ip_check else "",
                "IP国家": ip_check.country if ip_check else "",
                "IP城市": ip_check.city if ip_check else "",
                "IP运营商": ip_check.isp if ip_check else "",
                "IP验证结果": (
                    "通过" if ip_check and ip_check.ok
                    else "未通过" if ip_check
                    else "未检测"
                ),
                "服务端记录ID": str(record.id),
                "数据库写入时间": _milliseconds(now),
                "回传来源": body.source,
                "客户端版本": client_version or "",
            })
            outbox = self.session.scalar(
                select(OperationalSyncOutbox).where(
                    OperationalSyncOutbox.dedupe_key
                    == f"environment_creation_result:{record.id}"
                )
            )
            if outbox is None:
                self.session.add(OperationalSyncOutbox(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="environment_creation_result",
                    aggregate_id=record.id,
                    dedupe_key=f"environment_creation_result:{record.id}",
                    payload={"syncKey": fields["同步键"], "fields": fields},
                    status="pending",
                    attempt_count=0,
                    available_at=now,
                ))
            if item.status == "success":
                resource_conflicts += self._merge_buyer_resource(
                    tenant_id=tenant_id,
                    run_key=body.runKey,
                    site=body.site,
                    item=item,
                    now=now,
                )
        OperationRunService(self.session).finalize_environment_account_guards(
            run=run,
            status=run.status,
            summary={
                "cleanupTotal": sum(
                    item.createdInRun
                    and item.cleanupStatus != "not_required"
                    for item in body.results
                ),
                "cleanupDone": sum(
                    item.cleanupStatus == "deleted" for item in body.results
                ),
                "cleanupFailed": sum(
                    item.cleanupStatus == "failed" for item in body.results
                ),
            },
            now=now,
        )
        OperationRunService(self.session).finalize_environment_inventory(
            run=run,
            status=run.status,
            now=now,
        )
        self.session.flush()
        result = self._environment_result(run, unchanged=False)
        result["resourceConflictCount"] = resource_conflicts
        return result

    def _merge_buyer_resource(
        self,
        *,
        tenant_id: uuid.UUID,
        run_key: str,
        site: str,
        item: Any,
        now: datetime,
    ) -> int:
        account = self.session.scalar(
            select(BuyerAccount).where(
                BuyerAccount.tenant_id == tenant_id,
                BuyerAccount.account_ref == item.accountRef,
            )
        )
        if account is None:
            account = BuyerAccount(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                account_ref=item.accountRef,
                display_label=item.accountLabel,
                site=site,
                status="available",
                source_availability_status="available",
                credential_status="ready",
                source="environment_creation",
                source_status="已绑定环境",
                hub_environment_ref=item.environmentRef,
                hub_environment_name=item.environmentName,
                operator_label=item.purchaserLabel,
                last_snapshot_key=run_key,
                source_updated_at=item.bindingAt,
                last_synced_at=now,
                feishu_sync_status="pending",
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.session.add(account)
            enqueue_buyer_account_mirror(self.session, account, available_at=now)
            return 0
        if account.site != site:
            return 1
        if account.hub_environment_ref and account.hub_environment_ref != item.environmentRef:
            return 1
        account.display_label = item.accountLabel
        account.credential_status = "ready"
        account.source = "environment_creation"
        account.source_status = "已绑定环境"
        account.hub_environment_ref = item.environmentRef
        account.hub_environment_name = item.environmentName
        account.operator_label = item.purchaserLabel
        account.last_snapshot_key = run_key
        account.source_updated_at = item.bindingAt
        account.last_synced_at = now
        account.version += 1
        account.updated_at = now
        enqueue_buyer_account_mirror(self.session, account, available_at=now)
        return 0

    def ingest_logistics_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        client_version: str | None,
        body: LogisticsQueryRunBody,
        executor_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.source_run_key == body.runKey,
            )
        )
        if existing is not None:
            if executor_id is not None:
                if existing.actor_user_id != actor_user_id:
                    raise PurchaseServiceError(
                        "operation_run_executor_conflict",
                        "物流结果所属用户与执行器配对用户不一致",
                        409,
                    )
                if (
                    existing.executor_id is not None
                    and existing.executor_id != executor_id
                ):
                    raise PurchaseServiceError(
                        "operation_run_executor_conflict",
                        "物流任务已绑定其他执行器",
                        409,
                    )
                existing.executor_id = executor_id
            accepted_hash = existing.result_payload_hash or (
                existing.payload_hash if existing.source != "cloud_web" else None
            )
            if accepted_hash is not None and accepted_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一物流查询任务标识已提交不同结果",
                    409,
                )
            if accepted_hash is not None:
                return self._logistics_result(existing, unchanged=True)
            if existing.site != body.site or existing.query_mode != body.queryMode:
                raise PurchaseServiceError(
                    "operation_run_result_conflict",
                    "物流结果与已受理任务参数不一致",
                    409,
                )

        now = utcnow()
        success_count = sum(item.status == "ok" for item in body.results)
        new_run_id = uuid.uuid4()
        run = existing or LogisticsQueryRun(
            id=new_run_id,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.runKey,
            payload_hash=digest,
            result_payload_hash=digest,
            executor_id=executor_id,
            parent_run_id=None,
            root_run_id=new_run_id,
            query_mode=body.queryMode,
            site=body.site,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=len(body.results),
            total_count=len(body.results),
            success_count=0,
            failed_count=0,
            request_summary={
                "environmentSerials": [
                    item.environmentSerial for item in body.results
                ]
            },
            source=body.source,
            client_version=client_version,
            created_at=now,
            updated_at=now,
        )
        if existing is None:
            self.session.add(run)
        preserve_cancelled = existing is not None and existing.status == "cancelled"
        run.result_payload_hash = digest
        run.status = (
            "cancelled"
            if preserve_cancelled
            else _run_status(success_count, len(body.results))
        )
        run.phase = "cancelled" if preserve_cancelled else "completed"
        run.progress_completed = len(body.results)
        run.progress_total = len(body.results)
        run.total_count = len(body.results)
        run.success_count = success_count
        run.failed_count = len(body.results) - success_count
        run.started_at = run.started_at or body.startedAt
        run.completed_at = body.completedAt
        run.last_heartbeat_at = body.completedAt
        run.client_version = client_version
        run.updated_at = now
        self.session.flush()
        mode_label = {
            "initial": "首次查询",
            "single_retry": "单条重查",
            "failed_retry": "异常重查",
        }[body.queryMode]
        status_labels = {
            "ok": "成功",
            "fail": "失败",
            "login": "需登录",
            "inuse": "使用中",
            "stopped": "已停止",
            "pending": "待处理",
        }
        for item in body.results:
            record = self.session.scalar(
                select(LogisticsQueryResult).where(
                    LogisticsQueryResult.run_id == run.id,
                    LogisticsQueryResult.environment_serial == item.environmentSerial,
                )
            )
            if record is None:
                record = LogisticsQueryResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    tenant_id=tenant_id,
                    environment_serial=item.environmentSerial,
                    status=item.status,
                    completed_steps=[],
                    tracking_numbers=[],
                    package_numbers=[],
                    cancelled=False,
                    risk_order=False,
                    feishu_sync_status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(record)
            record.environment_name = item.environmentName or None
            record.status = item.status
            record.current_step = "done" if item.status == "ok" else item.status
            record.platform_order_no = item.platformOrderNo or None
            record.order_time_text = item.orderTime or None
            record.amount_text = item.amount or None
            record.platform_status = item.platformStatus or None
            record.status_label = item.statusLabel or None
            record.fulfillment_stage = item.fulfillmentStage or None
            record.tracking_numbers = list(item.trackingNumbers)
            record.package_numbers = list(item.packageNumbers)
            record.carrier = item.carrier or None
            record.first_tracking_at = item.firstTrackingAt
            record.first_tracking_time_text = item.firstTrackingTime or None
            record.first_tracking_summary = item.firstTrackingSummary or None
            record.first_tracking_lead_minutes = item.firstTrackingLeadMinutes
            record.cancelled = item.cancelled
            record.risk_order = item.riskOrder
            record.risk_summary = item.riskSummary or None
            record.ip_address = item.ipAddress or None
            record.time_zone = item.timeZone or None
            record.utc_offset_minutes = item.utcOffsetMinutes
            record.queried_at = item.queriedAt
            record.error_summary = item.errorSummary or None
            record.screenshot_status = item.screenshotStatus or None
            record.feishu_sync_status = "pending"
            record.updated_at = now
            fields = _nonempty({
                "同步键": f"logistics:{tenant_id}:{record.id}",
                "查询任务ID": body.runKey,
                "查询模式": mode_label,
                "站点": body.site,
                "环境序号": item.environmentSerial,
                "环境名称": item.environmentName,
                "查询状态": status_labels[item.status],
                "平台订单号": item.platformOrderNo,
                "下单时间": item.orderTime,
                "订单金额": item.amount,
                "平台状态": item.platformStatus,
                "中文状态": item.statusLabel,
                "履约阶段": item.fulfillmentStage,
                "物流单号": "\n".join(item.trackingNumbers),
                "包裹号": "\n".join(item.packageNumbers),
                "承运商": item.carrier,
                "砍单": item.cancelled,
                "风险订单": item.riskOrder,
                "风险提示": item.riskSummary,
                "出口IP": item.ipAddress,
                "时区": item.timeZone,
                "UTC偏移分钟": item.utcOffsetMinutes,
                "查询时间": _milliseconds(item.queriedAt),
                "错误摘要": item.errorSummary,
                "轨迹截图状态": item.screenshotStatus,
                "服务端记录ID": str(record.id),
                "数据库写入时间": _milliseconds(now),
                "回传来源": body.source,
                "客户端版本": client_version or "",
            })
            outbox = self.session.scalar(
                select(OperationalSyncOutbox).where(
                    OperationalSyncOutbox.dedupe_key
                    == f"logistics_query_result:{record.id}"
                )
            )
            if outbox is None:
                self.session.add(OperationalSyncOutbox(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="logistics_query_result",
                    aggregate_id=record.id,
                    dedupe_key=f"logistics_query_result:{record.id}",
                    payload={"syncKey": fields["同步键"], "fields": fields},
                    status="pending",
                    attempt_count=0,
                    available_at=now,
                ))
        self.session.flush()
        return self._logistics_result(run, unchanged=False)

    def _environment_result(
        self, run: EnvironmentCreationRun, *, unchanged: bool
    ) -> dict[str, object]:
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "status": run.status,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "ipOkCount": run.ip_ok_count,
            "ipTotalCount": run.ip_total_count,
            "syncStatus": "pending",
            "unchanged": unchanged,
        }

    def _logistics_result(
        self, run: LogisticsQueryRun, *, unchanged: bool
    ) -> dict[str, object]:
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "status": run.status,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "syncStatus": "pending",
            "unchanged": unchanged,
        }

    def pending_sync_count(self, *, tenant_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(OperationalSyncOutbox)
                .where(
                    OperationalSyncOutbox.tenant_id == tenant_id,
                    OperationalSyncOutbox.status.in_(("pending", "processing")),
                )
            )
            or 0
        )
