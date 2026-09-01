"""Durable ingestion of real local procurement-operation results."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .buyer_account_sync import enqueue_buyer_account_mirror
from .models import (
    BuyerAccount,
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    ExecutorTask,
    LogisticsQueryResult,
    LogisticsQueryRun,
    OperationalSyncOutbox,
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
        now = utcnow()
        run = LogisticsQueryRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.idempotencyKey,
            payload_hash=digest,
            executor_id=body.executorId,
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

    def cleanup_failed_account_refs(
        self, *, tenant_id: uuid.UUID, account_refs: set[str]
    ) -> set[str]:
        """Return rows whose owned Hub environment still needs reconciliation."""
        if not account_refs:
            return set()
        return set(
            self.session.scalars(
                select(EnvironmentCreationResult.account_ref)
                .where(
                    EnvironmentCreationResult.tenant_id == tenant_id,
                    EnvironmentCreationResult.account_ref.in_(account_refs),
                    EnvironmentCreationResult.created_in_run.is_(True),
                    EnvironmentCreationResult.cleanup_status == "failed",
                )
                .distinct()
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
        rows = list(
            self.session.scalars(
                select(LogisticsQueryResult)
                .where(LogisticsQueryResult.run_id == run.id)
                .order_by(LogisticsQueryResult.created_at, LogisticsQueryResult.id)
            )
        )
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "executorId": str(run.executor_id) if run.executor_id else None,
            "executorTaskId": str(run.executor_task_id) if run.executor_task_id else None,
            "queryMode": run.query_mode,
            "site": run.site,
            "status": run.status,
            "phase": run.phase,
            "attempt": run.attempt,
            "progressCompleted": run.progress_completed,
            "progressTotal": run.progress_total,
            "stopRequested": run.stop_requested,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "startedAt": _iso(run.started_at),
            "completedAt": _iso(run.completed_at),
            "lastHeartbeatAt": _iso(run.last_heartbeat_at),
            "createdAt": _iso(run.created_at),
            "updatedAt": _iso(run.updated_at),
            "terminal": run.status in self.TERMINAL_STATUSES,
            "unchanged": unchanged,
            "rows": [
                {
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
                    "cancelled": row.cancelled,
                    "riskOrder": row.risk_order,
                    "riskSummary": row.risk_summary,
                    "ipAddress": row.ip_address,
                    "timeZone": row.time_zone,
                    "utcOffsetMinutes": row.utc_offset_minutes,
                    "queriedAt": _iso(row.queried_at),
                    "errorSummary": row.error_summary,
                    "screenshotStatus": row.screenshot_status,
                    "updatedAt": _iso(row.updated_at),
                }
                for row in rows
            ],
        }


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
            if preserve_cancelled
            else _run_status(success_count, len(body.results))
        )
        run.phase = "cancelled" if preserve_cancelled else "completed"
        run.progress_completed = len(body.results)
        run.progress_total = len(body.results)
        run.total_count = len(body.results)
        run.success_count = success_count
        run.failed_count = failed_count
        run.ip_ok_count = sum(item.ok for item in body.ipChecks)
        run.ip_total_count = len(body.ipChecks)
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
    ) -> dict[str, object]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.source_run_key == body.runKey,
            )
        )
        if existing is not None:
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
        run = existing or LogisticsQueryRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.runKey,
            payload_hash=digest,
            result_payload_hash=digest,
            query_mode=body.queryMode,
            site=body.site,
            status="created",
            phase="created",
            progress_completed=0,
            progress_total=len(body.results),
            total_count=len(body.results),
            success_count=0,
            failed_count=0,
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
