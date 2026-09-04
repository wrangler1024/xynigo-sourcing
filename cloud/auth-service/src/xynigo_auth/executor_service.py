"""Durable pairing and lease service for local Xynigo executors."""

from __future__ import annotations

import base64
import binascii
import secrets
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .executor_contract import (
    ExecutorEnvironmentPreviewResult,
    ExecutorHubEnvironmentSnapshot,
    ExecutorPairBody,
    ExecutorPollBody,
    ExecutorTaskFinishBody,
    ExecutorTaskProgressBody,
)
from .executor_payload_crypto import (
    ExecutorPayloadCipher,
    ExecutorPayloadCipherError,
)
from .models import (
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    ExecutorPairingCode,
    ExecutorTask,
    ExecutorTaskEvent,
    HubEnvironmentInventory,
    HubEnvironmentInventorySync,
    HubEnvironmentObservation,
    LogisticsQueryResult,
    LogisticsQueryRun,
    LocalExecutor,
    Tenant,
)
from .operation_contract import (
    EnvironmentPlanParseResult,
    EnvironmentRunProgressItem,
    ExecutorWorkspaceSnapshotResult,
    LogisticsRunProgressItem,
    LogisticsScreenshotProgressItem,
    WorkspaceEnvironmentPreferences,
    WorkspaceRuntimeConfig,
)
from .operation_service import OperationRunService
from .security import hash_token, random_url_token
from .workspace_rpc import workspace_rpc_is_local_config


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ACTIVE_TASK_STATUSES = frozenset({"queued", "leased", "running", "cancel_requested"})
MAX_QUEUED_WORKSPACE_RPC_TASKS = 32
BUSINESS_TASK_TYPES = frozenset(
    {
        "logistics.query.v1",
        "environment.create-bound.v1",
        "environment.create-backup.v1",
        "environment.retry-row.v1",
        "environment.retry-failed.v1",
    }
)
MIN_SAFE_LOGISTICS_CLIENT_VERSION = (0, 13, 18)
BACKGROUND_TASK_TYPES = frozenset(
    {"config.read.v1", "workspace.rpc.v1", "workspace.snapshot.v1"}
)
ENCRYPTED_TASK_TYPES = frozenset(
    {
        "workspace.rpc.v1",
        "workspace.snapshot.v1",
        "environment.parse.v1",
        "environment.preview-bound.v1",
        *BUSINESS_TASK_TYPES,
    }
)
HUB_TASK_TYPES = frozenset({
    "environment.preview-bound.v1",
    *BUSINESS_TASK_TYPES,
})


def _client_version_tuple(value: object) -> tuple[int, int, int]:
    core = str(value or '').strip().split('-', 1)[0]
    parts = core.split('.')
    if len(parts) < 3:
        return (0, 0, 0)
    try:
        return tuple(int(part) for part in parts[:3])
    except (TypeError, ValueError):
        return (0, 0, 0)


TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "uncertain", "cancelled"}
)
# Read results remain backward compatible with v0.12.6 executors, which may
# still report legacy local-business preferences. New cloud writes are narrowed
# by ExecutorConfigWriteBody to device runtime and safety settings only.
PUBLIC_CONFIG_RESULT_KEYS = frozenset(
    {
        "hubPort",
        "serverPort",
        "concurrency",
        "importBuyerPlan",
        "verifySampleCount",
        "hiddenQueryColumns",
        "purchaseSite",
        "purchaseTag",
        "purchaseTags",
        "envCreateWorkers",
        "safeParallelTasks",
        "queryBrowserMode",
        "queryAllowOpenEnvironment",
        "proxyConfigured",
        "proxySource",
        "buyers",
        "buyerDefaultSplit",
        "backupMaxCount",
        "larkLedgerTargetConfigured",
    }
)
BUSINESS_RESULT_KEYS = frozenset(
    {
        "runStatus",
        "phase",
        "progressCompleted",
        "progressTotal",
        "totalCount",
        "successCount",
        "failedCount",
        "stoppedCount",
        "cleanupTotal",
        "cleanupDone",
        "cleanupFailed",
        "ipOkCount",
        "ipTotalCount",
        "errorCode",
        "errorSummary",
    }
)
BUSINESS_RUN_STATUSES = frozenset(
    {"completed", "partial_failure", "failed", "cancelled", "uncertain"}
)
DESKTOP_CONFIG_ONLY_CAPABILITY = "local.config.desktop.v1"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


class ExecutorServiceError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class ExecutorChannelService:
    def __init__(
        self,
        session: Session,
        *,
        pairing_ttl_seconds: int = 300,
        lease_seconds: int = 45,
        online_window_seconds: int = 60,
        payload_cipher: ExecutorPayloadCipher | None = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.session = session
        self.pairing_ttl_seconds = pairing_ttl_seconds
        self.lease_seconds = lease_seconds
        self.online_window_seconds = online_window_seconds
        self.payload_cipher = payload_cipher
        self.sleep = sleep_fn

    def create_pairing_code(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        display_name_hint: str | None,
    ) -> tuple[ExecutorPairingCode, str]:
        now = utcnow()
        # A user never needs a pile of simultaneously valid codes. Expire any
        # older unconsumed codes without deleting their audit history.
        existing = list(
            self.session.scalars(
                select(ExecutorPairingCode).where(
                    ExecutorPairingCode.tenant_id == tenant_id,
                    ExecutorPairingCode.created_by_user_id == user_id,
                    ExecutorPairingCode.consumed_at.is_(None),
                    ExecutorPairingCode.expires_at > now,
                )
            )
        )
        for item in existing:
            item.expires_at = now

        for _attempt in range(8):
            raw_code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
            record = ExecutorPairingCode(
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                display_name_hint=display_name_hint,
                code_digest=hash_token(raw_code),
                expires_at=now + timedelta(seconds=self.pairing_ttl_seconds),
            )
            self.session.add(record)
            try:
                self.session.commit()
                return record, raw_code
            except IntegrityError:
                self.session.rollback()
        raise ExecutorServiceError("pairing_code_generation_failed", status_code=503)

    def pair(self, body: ExecutorPairBody) -> tuple[LocalExecutor, str]:
        now = utcnow()
        code = self.session.scalar(
            select(ExecutorPairingCode)
            .where(ExecutorPairingCode.code_digest == hash_token(body.pairingCode))
            .with_for_update()
        )
        if code is None:
            raise ExecutorServiceError("pairing_code_invalid", status_code=404)
        if code.consumed_at is not None:
            raise ExecutorServiceError("pairing_code_consumed", status_code=409)
        if as_utc(code.expires_at) <= now:
            raise ExecutorServiceError("pairing_code_expired", status_code=410)

        credential = random_url_token(48)
        executor = LocalExecutor(
            tenant_id=code.tenant_id,
            owner_user_id=code.created_by_user_id,
            display_name=body.displayName,
            platform=body.platform,
            architecture=body.architecture,
            client_version=body.clientVersion,
            protocol_version=body.protocolVersion,
            capabilities=body.capabilities,
            credential_digest=hash_token(credential),
            device_public_key=body.devicePublicKey,
            status="active",
            hub_status="unknown",
            # Pairing proves possession of the short-lived code, not that the
            # executor loop is running. Only an authenticated poll heartbeat
            # may make the device appear online.
            last_seen_at=None,
        )
        self.session.add(executor)
        self.session.flush()
        code.consumed_at = now
        code.executor_id = executor.id
        self.session.commit()
        return executor, credential

    def pairing_status(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        pairing_id: uuid.UUID,
    ) -> dict[str, Any]:
        record = self.session.scalar(
            select(ExecutorPairingCode).where(
                ExecutorPairingCode.id == pairing_id,
                ExecutorPairingCode.tenant_id == tenant_id,
                ExecutorPairingCode.created_by_user_id == user_id,
            )
        )
        if record is None:
            raise ExecutorServiceError("pairing_request_not_found", status_code=404)
        now = utcnow()
        if record.consumed_at is not None:
            pairing_state = "consumed"
        elif as_utc(record.expires_at) <= now:
            pairing_state = "expired"
        else:
            pairing_state = "pending"
        return {
            "id": str(record.id),
            "status": pairing_state,
            "executorId": str(record.executor_id) if record.executor_id else None,
            "expiresAt": record.expires_at.isoformat(),
            "consumedAt": (
                record.consumed_at.isoformat() if record.consumed_at else None
            ),
        }

    def authenticate(self, raw_credential: str | None) -> LocalExecutor:
        if not raw_credential:
            raise ExecutorServiceError("executor_authentication_required", status_code=401)
        executor = self.session.scalar(
            select(LocalExecutor).where(
                LocalExecutor.credential_digest == hash_token(raw_credential)
            )
        )
        if executor is None:
            raise ExecutorServiceError("executor_credential_invalid", status_code=401)
        if executor.status == "revoked" or executor.revoked_at is not None:
            raise ExecutorServiceError("executor_revoked", status_code=401)
        return executor

    def list_executors(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        rows = list(
            self.session.scalars(
                select(LocalExecutor)
                .where(
                    LocalExecutor.tenant_id == tenant_id,
                    LocalExecutor.owner_user_id == user_id,
                )
                .order_by(LocalExecutor.created_at.desc())
            )
        )
        now = utcnow()
        return [self.executor_payload(row, now=now) for row in rows]

    def require_executor(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
    ) -> LocalExecutor:
        executor = self.session.scalar(
            select(LocalExecutor).where(
                LocalExecutor.id == executor_id,
                LocalExecutor.tenant_id == tenant_id,
                LocalExecutor.owner_user_id == user_id,
            )
        )
        if executor is None:
            raise ExecutorServiceError("executor_not_found", status_code=404)
        return executor

    def revoke(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
    ) -> LocalExecutor:
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        if executor.status != "revoked":
            now = utcnow()
            executor.status = "revoked"
            executor.revoked_at = now
            active_tasks = list(
                self.session.scalars(
                    select(ExecutorTask).where(
                        ExecutorTask.executor_id == executor.id,
                        ExecutorTask.status.in_(ACTIVE_TASK_STATUSES),
                    )
                )
            )
            for task in active_tasks:
                if task.status in {"queued", "leased"}:
                    task.status = "cancelled"
                    task.finished_at = now
                    task.result_code = "executor_revoked"
                    self._sync_operation_run(
                        task,
                        status="cancelled",
                        phase="cancelled",
                        attempt=task.attempt,
                        heartbeat_at=now,
                        completed_at=now,
                    )
                else:
                    task.status = "uncertain"
                    task.finished_at = now
                    task.result_code = "executor_revoked_during_execution"
                    self._sync_operation_run(
                        task,
                        status="uncertain",
                        phase="uncertain",
                        attempt=task.attempt,
                        heartbeat_at=now,
                        completed_at=now,
                    )
                self._purge_sensitive_request(task, now=now)
                self._event(task, "device_revoked", stable_code=task.result_code)
            self.session.commit()
        return executor

    def runtime_summary(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
    ) -> dict[str, Any]:
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        tasks = list(
            self.session.scalars(
                select(ExecutorTask)
                .where(ExecutorTask.executor_id == executor.id)
                .order_by(ExecutorTask.created_at.desc())
                .limit(10)
            )
        )
        config_summary = (executor.workspace_snapshot or {}).get(
            "configSummary")
        if not isinstance(config_summary, dict):
            config_summary = None
        summary_age_seconds = None
        if config_summary is not None:
            try:
                captured_at = datetime.fromisoformat(
                    str(config_summary.get("capturedAt") or ""))
                summary_age_seconds = max(
                    0, int((utcnow() - as_utc(captured_at)).total_seconds()))
            except (TypeError, ValueError):
                config_summary = None
        return {
            "executor": self.executor_payload(executor),
            "configSummary": config_summary,
            "configSummaryAgeSeconds": summary_age_seconds,
            "configSummaryStale": (
                summary_age_seconds is None or summary_age_seconds > 120
            ),
            "tasks": [self.task_payload(task) for task in tasks],
        }

    def create_config_task(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
        task_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        commit: bool = True,
    ) -> ExecutorTask:
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        now = utcnow()
        if executor.status != "active":
            raise ExecutorServiceError("executor_revoked", status_code=409)
        # A worker that is retrying a failed completion request cannot poll,
        # so poll-only lease recovery can otherwise leave a phantom active
        # task blocking every later preview. Sweep it before busy checks too.
        self._recover_expired_leases(
            executor=executor, now=now, trace_id=None, commit=False
        )
        if DESKTOP_CONFIG_ONLY_CAPABILITY in set(executor.capabilities or []):
            config_task = task_type in {"config.read.v1", "config.write.v1"}
            config_rpc = (
                task_type == "workspace.rpc.v1"
                and workspace_rpc_is_local_config(str(payload.get("path") or ""))
            )
            if config_task or config_rpc:
                raise ExecutorServiceError(
                    "local_config_desktop_only", status_code=410
                )
        if not self._online(executor, now=now):
            raise ExecutorServiceError("executor_offline", status_code=409)
        if task_type in HUB_TASK_TYPES and executor.hub_status != "ready":
            raise ExecutorServiceError(
                "executor_hub_unavailable", status_code=409
            )
        if (
            task_type == "logistics.query.v1"
            and _client_version_tuple(executor.client_version)
            < MIN_SAFE_LOGISTICS_CLIENT_VERSION
        ):
            raise ExecutorServiceError(
                "executor_upgrade_required", status_code=409
            )
        if task_type not in set(executor.capabilities or []):
            raise ExecutorServiceError("executor_capability_missing", status_code=409)
        if task_type in ENCRYPTED_TASK_TYPES and self.payload_cipher is None:
            raise ExecutorServiceError("executor_payload_encryption_unavailable", status_code=503)
        if task_type == "config.write.v1":
            expected = str(payload.get("expectedRevision") or "")
            if not executor.config_revision or executor.config_revision != expected:
                raise ExecutorServiceError("config_revision_conflict", status_code=409)

        key = idempotency_key or uuid.uuid4().hex
        payload_hash = self._payload_hash(payload)
        existing = self.session.scalar(
            select(ExecutorTask).where(
                ExecutorTask.tenant_id == tenant_id,
                ExecutorTask.executor_id == executor.id,
                ExecutorTask.idempotency_key == key,
            )
        )
        if existing is not None:
            same_payload = (
                existing.payload_envelope.get("payloadHash") == payload_hash
                if task_type in ENCRYPTED_TASK_TYPES
                else existing.payload_envelope == payload
            )
            if existing.task_type != task_type or not same_payload:
                raise ExecutorServiceError("executor_task_idempotency_conflict", status_code=409)
            return existing

        if task_type == "workspace.rpc.v1":
            # One device worker still executes tasks serially, but a workspace
            # page issues several short reads together (groups, task state,
            # progress, preflight). Keep those reads in a bounded FIFO instead
            # of rejecting all but the first request as busy.
            active_workspace_tasks = list(
                self.session.scalars(
                    select(ExecutorTask).where(
                        ExecutorTask.tenant_id == tenant_id,
                        ExecutorTask.executor_id == executor.id,
                        ExecutorTask.created_by_user_id == user_id,
                        ExecutorTask.task_type == "workspace.rpc.v1",
                        ExecutorTask.status.in_(ACTIVE_TASK_STATUSES),
                    )
                )
            )
            matching_active = next(
                (
                    active_task
                    for active_task in active_workspace_tasks
                    if active_task.payload_envelope.get("payloadHash") == payload_hash
                ),
                None,
            )
            if matching_active is not None:
                return matching_active
            active_non_workspace = self.session.scalar(
                select(ExecutorTask.id).where(
                    ExecutorTask.executor_id == executor.id,
                    ExecutorTask.task_type != "workspace.rpc.v1",
                    ExecutorTask.status.in_(ACTIVE_TASK_STATUSES),
                ).limit(1)
            )
            active_count = len(active_workspace_tasks)
            if (
                active_non_workspace is not None
                or active_count >= MAX_QUEUED_WORKSPACE_RPC_TASKS
            ):
                raise ExecutorServiceError("executor_task_busy", status_code=409)
        elif task_type in BUSINESS_TASK_TYPES:
            # A page refresh can have a short config/snapshot/RPC read queued
            # or leased at the exact moment the user submits a formal Run.
            # Accept the Run and serialize it behind that read instead of
            # making the user click repeatedly. Other formal writes remain a
            # hard conflict so two business batches cannot overlap.
            active_business_or_write = self.session.scalar(
                select(ExecutorTask.id).where(
                    ExecutorTask.executor_id == executor.id,
                    ExecutorTask.task_type.not_in(BACKGROUND_TASK_TYPES),
                    ExecutorTask.status.in_(ACTIVE_TASK_STATUSES),
                ).limit(1)
            )
            if active_business_or_write is not None:
                raise ExecutorServiceError("executor_task_busy", status_code=409)
        else:
            active = self.session.scalar(
                select(ExecutorTask.id).where(
                    ExecutorTask.executor_id == executor.id,
                    ExecutorTask.status.in_(ACTIVE_TASK_STATUSES),
                ).limit(1)
            )
            if active is not None:
                raise ExecutorServiceError("executor_task_busy", status_code=409)

        task = ExecutorTask(
            tenant_id=tenant_id,
            executor_id=executor.id,
            task_type=task_type,
            idempotency_key=key,
            payload_version=1,
            payload_envelope=(
                {} if task_type in ENCRYPTED_TASK_TYPES else payload
            ),
            priority=10 if task_type in BUSINESS_TASK_TYPES else 100,
            created_by_user_id=user_id,
        )
        self.session.add(task)
        self.session.flush()
        if task_type in ENCRYPTED_TASK_TYPES:
            try:
                task.payload_envelope = {
                    "schemaVersion": 1,
                    "payloadHash": payload_hash,
                    "encryptedPayload": self.payload_cipher.encrypt(
                        payload,
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        purpose="request",
                    ),
                }
            except ExecutorPayloadCipherError as exc:
                raise ExecutorServiceError(str(exc), status_code=503) from exc
        self._event(task, "queued")
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return task

    def get_task(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> ExecutorTask:
        task = self.session.scalar(
            select(ExecutorTask).where(
                ExecutorTask.id == task_id,
                ExecutorTask.tenant_id == tenant_id,
                ExecutorTask.created_by_user_id == user_id,
            )
        )
        if task is None:
            raise ExecutorServiceError("executor_task_not_found", status_code=404)
        return task

    def cancel_task(
        self, *, tenant_id: uuid.UUID, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> ExecutorTask:
        task = self.get_task(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
        )
        now = utcnow()
        if task.status in TERMINAL_TASK_STATUSES:
            return task
        task.cancel_requested_at = now
        if task.status == "queued":
            task.status = "cancelled"
            task.finished_at = now
            task.result_code = "cancelled_by_user"
            self._sync_operation_run(
                task,
                status="cancelled",
                phase="cancelled",
                attempt=task.attempt,
                heartbeat_at=now,
                completed_at=now,
            )
            self._purge_sensitive_request(task, now=now)
            self._event(task, "cancelled", stable_code=task.result_code)
        else:
            task.status = "cancel_requested"
            run_status = "running" if task.started_at else "leased"
            self._sync_operation_run(
                task,
                status=run_status,
                phase="cancel_requested",
                attempt=task.attempt,
                heartbeat_at=now,
            )
            self._event(task, "cancel_requested")
        self.session.commit()
        return task

    def poll(
        self,
        *,
        executor: LocalExecutor,
        body: ExecutorPollBody,
        trace_id: str | None,
    ) -> dict[str, Any]:
        now = utcnow()
        executor.last_seen_at = now
        executor.client_version = body.clientVersion
        executor.protocol_version = body.protocolVersion
        executor.capabilities = body.capabilities
        executor.config_revision = body.configRevision
        executor.hub_status = body.hubStatus
        if body.configSummary is not None:
            if as_utc(body.configSummary.capturedAt) > now + timedelta(minutes=5):
                raise ExecutorServiceError(
                    "config_summary_timestamp_invalid", status_code=422
                )
            snapshot = dict(executor.workspace_snapshot or {})
            snapshot["configSummary"] = body.configSummary.model_dump(
                mode="json")
            executor.workspace_snapshot = snapshot
        self.session.commit()

        deadline = time.monotonic() + body.waitSeconds
        while True:
            now = utcnow()
            self._recover_expired_leases(executor=executor, now=now, trace_id=trace_id)
            if not body.acceptTasks:
                executor.last_seen_at = now
                self.session.commit()
                return {
                    "serverTime": now.isoformat(),
                    "pollAfterSeconds": 1,
                    "task": None,
                }
            task = self._lease_next(executor=executor, now=now, trace_id=trace_id)
            if task is not None:
                lease_token = task.pop("_leaseToken")
                return {
                    "serverTime": now.isoformat(),
                    "pollAfterSeconds": 0,
                    "task": {**task, "leaseToken": lease_token},
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                executor.last_seen_at = utcnow()
                self.session.commit()
                return {
                    "serverTime": utcnow().isoformat(),
                    "pollAfterSeconds": 1,
                    "task": None,
                }
            self.sleep(min(0.25, remaining))
            self.session.expire_all()
            executor = self.session.get(LocalExecutor, executor.id) or executor

    def start_task(
        self,
        *,
        executor: LocalExecutor,
        task_id: uuid.UUID,
        lease_token: str,
        trace_id: str | None,
    ) -> ExecutorTask:
        task = self._device_task(executor=executor, task_id=task_id)
        self._require_lease(task, lease_token)
        if task.status == "running":
            return task
        if task.status == "cancel_requested":
            raise ExecutorServiceError("executor_task_cancel_requested", status_code=409)
        if task.status != "leased":
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        now = utcnow()
        task.status = "running"
        task.started_at = task.started_at or now
        task.lease_until = now + timedelta(seconds=self.lease_seconds)
        executor.last_seen_at = now
        self._sync_operation_run(
            task,
            status="running",
            phase="starting",
            attempt=task.attempt,
            heartbeat_at=now,
            started_at=now,
        )
        self._event(task, "started", trace_id=trace_id)
        self.session.commit()
        return task

    def renew_lease(
        self,
        *,
        executor: LocalExecutor,
        task_id: uuid.UUID,
        lease_token: str,
    ) -> ExecutorTask:
        task = self._device_task(executor=executor, task_id=task_id)
        self._require_lease(task, lease_token)
        if task.status not in {"leased", "running", "cancel_requested"}:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        now = utcnow()
        task.lease_until = now + timedelta(seconds=self.lease_seconds)
        executor.last_seen_at = now
        self._sync_operation_run(
            task,
            status="running" if task.started_at else "leased",
            phase=None,
            attempt=task.attempt,
            heartbeat_at=now,
        )
        self.session.commit()
        return task

    def progress(
        self,
        *,
        executor: LocalExecutor,
        task_id: uuid.UUID,
        body: ExecutorTaskProgressBody,
        trace_id: str | None,
    ) -> ExecutorTask:
        task = self._device_task(executor=executor, task_id=task_id)
        self._require_lease(task, body.leaseToken)
        if task.status not in {"running", "cancel_requested"}:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        now = utcnow()
        task.lease_until = now + timedelta(seconds=self.lease_seconds)
        executor.last_seen_at = now
        self._sync_operation_run(
            task,
            status="running",
            phase=body.phase,
            attempt=task.attempt,
            progress_current=body.current,
            progress_total=body.total,
            heartbeat_at=now,
            progress_snapshot=body.snapshot,
        )
        self._event(
            task,
            "progress",
            phase=body.phase,
            current=body.current,
            total=body.total,
            stable_code=body.stableCode,
            trace_id=trace_id,
        )
        self.session.commit()
        return task

    def finish(
        self,
        *,
        executor: LocalExecutor,
        task_id: uuid.UUID,
        body: ExecutorTaskFinishBody,
        trace_id: str | None,
    ) -> ExecutorTask:
        task = self._device_task(executor=executor, task_id=task_id)
        self._require_lease(task, body.leaseToken, allow_expired=True)
        preview_result = self._validate_environment_preview_result(task, body)
        workspace_snapshot = self._validated_workspace_snapshot(task, body)
        workspace_inventory = None
        result_summary = body.resultSummary
        if preview_result is not None:
            result_summary = preview_result.model_dump(
                mode="json", exclude={"inventorySnapshot"}
            )
        if workspace_snapshot is not None:
            raw_inventory = workspace_snapshot.inventorySnapshot
            if raw_inventory is not None:
                try:
                    workspace_inventory = (
                        ExecutorHubEnvironmentSnapshot.model_validate(
                            raw_inventory
                        )
                    )
                except ValidationError as exc:
                    raise ExecutorServiceError(
                        "executor_result_invalid", status_code=422
                    ) from exc
            result_summary = workspace_snapshot.model_dump(
                mode="json", exclude={"inventorySnapshot"}
            )
        late_expired_finish = (
            task.status == "uncertain"
            and task.result_code == "lease_expired_after_start"
        )
        if task.status in TERMINAL_TASK_STATUSES and not late_expired_finish:
            if (
                task.status in {"succeeded", "failed"}
                and task.status == body.outcome
                and task.result_code == body.resultCode
                and self._result_summary(task) == result_summary
            ):
                return task
            raise ExecutorServiceError("executor_task_finish_conflict", status_code=409)
        if (
            task.status not in {"leased", "running", "cancel_requested"}
            and not late_expired_finish
        ):
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        self._validate_config_result(task, body)
        self._validate_business_result(task, body)
        self._validate_environment_parse_result(task, body)
        self._sync_workspace_preferences(
            executor=executor,
            task=task,
            body=body,
        )
        now = utcnow()
        if preview_result is not None and preview_result.inventorySnapshot is not None:
            self._sync_hub_environment_snapshot_safely(
                executor=executor,
                snapshot=preview_result.inventorySnapshot,
                now=now,
            )
        if workspace_inventory is not None:
            self._sync_hub_environment_snapshot_safely(
                executor=executor,
                snapshot=workspace_inventory,
                now=now,
            )
        task.status = body.outcome
        task.finished_at = now
        task.result_code = body.resultCode
        if task.task_type in ENCRYPTED_TASK_TYPES:
            if self.payload_cipher is None:
                raise ExecutorServiceError(
                    "executor_payload_encryption_unavailable", status_code=503
                )
            try:
                task.result_summary = {
                    "schemaVersion": 1,
                    "resultHash": self._payload_hash(result_summary),
                    "encryptedResult": self.payload_cipher.encrypt(
                        result_summary,
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        purpose="result",
                    ),
                }
            except ExecutorPayloadCipherError as exc:
                raise ExecutorServiceError(str(exc), status_code=503) from exc
        else:
            task.result_summary = result_summary
        task.lease_until = None
        self._purge_sensitive_request(task, now=now)
        executor.last_seen_at = now
        if workspace_snapshot is not None:
            previous_summary = (executor.workspace_snapshot or {}).get(
                "configSummary")
            next_snapshot = workspace_snapshot.model_dump(
                mode="json", exclude={"inventorySnapshot"}
            )
            if isinstance(previous_summary, dict):
                next_snapshot["configSummary"] = previous_summary
            executor.workspace_snapshot = next_snapshot
            executor.workspace_snapshot_revision = workspace_snapshot.snapshotRevision
            executor.workspace_snapshot_at = workspace_snapshot.capturedAt
        terminal_status = "completed" if body.outcome == "succeeded" else "failed"
        requested_status = str(result_summary.get("runStatus") or "")
        if requested_status in {
            "completed",
            "partial_failure",
            "failed",
            "cancelled",
            "uncertain",
        }:
            terminal_status = requested_status
        self._sync_operation_run(
            task,
            status=terminal_status,
            phase=str(result_summary.get("phase") or terminal_status),
            attempt=task.attempt,
            progress_current=_safe_nonnegative_int(
                result_summary.get("progressCompleted")
            ),
            progress_total=_safe_nonnegative_int(result_summary.get("progressTotal")),
            heartbeat_at=now,
            completed_at=now,
            result_summary=result_summary,
        )
        if body.outcome == "succeeded" and task.task_type.startswith("config."):
            revision = str(result_summary.get("configRevision") or "")
            if len(revision) == 64 and all(char in "0123456789abcdef" for char in revision):
                executor.config_revision = revision
        self._event(task, "finished", stable_code=body.resultCode, trace_id=trace_id)
        self.session.commit()
        return task

    def executor_payload(
        self, executor: LocalExecutor, *, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or utcnow()
        if executor.status == "revoked":
            connectivity = "revoked"
        else:
            connectivity = "online" if self._online(executor, now=now) else "offline"
        return {
            "id": str(executor.id),
            "displayName": executor.display_name,
            "platform": executor.platform,
            "architecture": executor.architecture,
            "clientVersion": executor.client_version,
            "protocolVersion": executor.protocol_version,
            "capabilities": list(executor.capabilities or []),
            "status": executor.status,
            "connectivity": connectivity,
            "lastSeenAt": executor.last_seen_at.isoformat() if executor.last_seen_at else None,
            "configRevision": executor.config_revision,
            "hubStatus": executor.hub_status,
            "workspaceSnapshotRevision": executor.workspace_snapshot_revision,
            "workspaceSnapshotAt": (
                executor.workspace_snapshot_at.isoformat()
                if executor.workspace_snapshot_at
                else None
            ),
            "revokedAt": executor.revoked_at.isoformat() if executor.revoked_at else None,
            "createdAt": executor.created_at.isoformat() if executor.created_at else None,
        }

    def task_payload(self, task: ExecutorTask) -> dict[str, Any]:
        latest_event = self.session.scalar(
            select(ExecutorTaskEvent)
            .where(
                ExecutorTaskEvent.task_id == task.id,
                ExecutorTaskEvent.phase.is_not(None),
            )
            .order_by(
                ExecutorTaskEvent.created_at.desc(),
                ExecutorTaskEvent.id.desc(),
            )
            .limit(1)
        )
        return {
            "id": str(task.id),
            "executorId": str(task.executor_id),
            "type": task.task_type,
            "status": task.status,
            "attempt": task.attempt,
            "cancellationRequested": task.cancel_requested_at is not None,
            "leaseUntil": task.lease_until.isoformat() if task.lease_until else None,
            "resultCode": task.result_code,
            "resultSummary": self._result_summary(task),
            "phase": latest_event.phase if latest_event is not None else None,
            "progressCurrent": (
                latest_event.progress_current if latest_event is not None else None
            ),
            "progressTotal": (
                latest_event.progress_total if latest_event is not None else None
            ),
            "stableCode": (
                latest_event.stable_code if latest_event is not None else None
            ),
            "progressAt": (
                latest_event.created_at.isoformat()
                if latest_event is not None and latest_event.created_at
                else None
            ),
            "createdAt": task.created_at.isoformat() if task.created_at else None,
            "startedAt": task.started_at.isoformat() if task.started_at else None,
            "finishedAt": task.finished_at.isoformat() if task.finished_at else None,
        }

    def _online(self, executor: LocalExecutor, *, now: datetime) -> bool:
        return bool(
            executor.last_seen_at
            and as_utc(executor.last_seen_at)
            >= now - timedelta(seconds=self.online_window_seconds)
        )

    def _lease_next(
        self,
        *,
        executor: LocalExecutor,
        now: datetime,
        trace_id: str | None,
    ) -> dict[str, Any] | None:
        task = self.session.scalar(
            select(ExecutorTask)
            .where(
                ExecutorTask.executor_id == executor.id,
                ExecutorTask.status == "queued",
            )
            .order_by(ExecutorTask.priority.asc(), ExecutorTask.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return None
        lease_token = random_url_token(48)
        task.status = "leased"
        task.lease_token_digest = hash_token(lease_token)
        task.lease_until = now + timedelta(seconds=self.lease_seconds)
        task.attempt += 1
        self._sync_operation_run(
            task,
            status="leased",
            phase="leased",
            attempt=task.attempt,
            heartbeat_at=now,
        )
        self._event(task, "leased", trace_id=trace_id)
        self.session.commit()
        payload = task.payload_envelope
        if task.task_type in ENCRYPTED_TASK_TYPES:
            if self.payload_cipher is None:
                raise ExecutorServiceError(
                    "executor_payload_encryption_unavailable", status_code=503
                )
            try:
                payload = self.payload_cipher.decrypt(
                    task.payload_envelope.get("encryptedPayload"),
                    tenant_id=task.tenant_id,
                    task_id=task.id,
                    purpose="request",
                )
            except ExecutorPayloadCipherError as exc:
                raise ExecutorServiceError(str(exc), status_code=503) from exc
        return {
            "id": str(task.id),
            "type": task.task_type,
            "payloadVersion": task.payload_version,
            "payload": payload,
            "attempt": task.attempt,
            "leaseUntil": task.lease_until.isoformat(),
            "cancellationRequested": False,
            "_leaseToken": lease_token,
        }

    def _recover_expired_leases(
        self,
        *,
        executor: LocalExecutor,
        now: datetime,
        trace_id: str | None,
        commit: bool = True,
    ) -> None:
        expired = list(
            self.session.scalars(
                select(ExecutorTask).where(
                    ExecutorTask.executor_id == executor.id,
                    ExecutorTask.status.in_({"leased", "running", "cancel_requested"}),
                    ExecutorTask.lease_until.is_not(None),
                    ExecutorTask.lease_until <= now,
                )
            )
        )
        if not expired:
            return
        for task in expired:
            if task.status == "leased" or task.task_type == "config.read.v1":
                task.status = "queued"
                task.lease_token_digest = None
                task.lease_until = None
                self._sync_operation_run(
                    task,
                    status="queued",
                    phase="lease_recovered",
                    attempt=task.attempt,
                    heartbeat_at=now,
                )
                self._event(task, "lease_recovered", trace_id=trace_id)
            else:
                task.status = "uncertain"
                task.finished_at = now
                task.result_code = "lease_expired_after_start"
                task.lease_until = None
                self._sync_operation_run(
                    task,
                    status="uncertain",
                    phase="uncertain",
                    attempt=task.attempt,
                    heartbeat_at=now,
                    completed_at=now,
                )
                self._event(
                    task,
                    "execution_uncertain",
                    stable_code=task.result_code,
                    trace_id=trace_id,
                )
                self._purge_sensitive_request(task, now=now)
        if commit:
            self.session.commit()
        else:
            self.session.flush()

    @staticmethod
    def _purge_sensitive_request(task: ExecutorTask, *, now: datetime) -> None:
        """Erase buyer credentials once a bound environment task is terminal."""
        if task.task_type not in {
            "environment.create-bound.v1",
            "environment.preview-bound.v1",
        }:
            return
        envelope = dict(task.payload_envelope or {})
        if "encryptedPayload" not in envelope:
            return
        envelope.pop("encryptedPayload", None)
        envelope["purgedAt"] = now.isoformat()
        task.payload_envelope = envelope

    def _device_task(
        self, *, executor: LocalExecutor, task_id: uuid.UUID
    ) -> ExecutorTask:
        task = self.session.scalar(
            select(ExecutorTask).where(
                ExecutorTask.id == task_id,
                ExecutorTask.executor_id == executor.id,
                ExecutorTask.tenant_id == executor.tenant_id,
            )
        )
        if task is None:
            raise ExecutorServiceError("executor_task_not_found", status_code=404)
        return task

    @staticmethod
    def _validate_config_result(
        task: ExecutorTask, body: ExecutorTaskFinishBody
    ) -> None:
        if not task.task_type.startswith("config."):
            return
        summary = body.resultSummary
        if set(summary) - {"configRevision", "config"}:
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        revision = str(summary.get("configRevision") or "")
        if len(revision) != 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        config = summary.get("config")
        if body.outcome == "succeeded":
            if not isinstance(config, dict) or set(config) - PUBLIC_CONFIG_RESULT_KEYS:
                raise ExecutorServiceError("executor_result_invalid", status_code=422)
        elif config is not None:
            raise ExecutorServiceError("executor_result_invalid", status_code=422)

    @staticmethod
    def _validate_business_result(
        task: ExecutorTask, body: ExecutorTaskFinishBody
    ) -> None:
        if task.task_type not in BUSINESS_TASK_TYPES:
            return
        summary = body.resultSummary
        if set(summary) - BUSINESS_RESULT_KEYS:
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        run_status = str(summary.get("runStatus") or "")
        if run_status not in BUSINESS_RUN_STATUSES:
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        phase = str(summary.get("phase") or "")
        if not 2 <= len(phase) <= 64 or any(character.isspace() for character in phase):
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        error_code = str(summary.get("errorCode") or "")
        if error_code and (
            len(error_code) > 128
            or not error_code[0].isalpha()
            or any(
                not (character.islower() or character.isdigit() or character in "_.-")
                for character in error_code
            )
        ):
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        error_summary = summary.get("errorSummary")
        if error_summary is not None and (
            not isinstance(error_summary, str) or len(error_summary) > 300
        ):
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        count_keys = BUSINESS_RESULT_KEYS - {
            "runStatus", "phase", "errorCode", "errorSummary"
        }
        for key in count_keys:
            if key not in summary:
                continue
            value = summary[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExecutorServiceError("executor_result_invalid", status_code=422)
        completed = summary.get("progressCompleted")
        total = summary.get("progressTotal")
        if isinstance(completed, int) and isinstance(total, int) and completed > total:
            raise ExecutorServiceError("executor_result_invalid", status_code=422)

    @staticmethod
    def _validate_environment_parse_result(
        task: ExecutorTask, body: ExecutorTaskFinishBody
    ) -> None:
        if task.task_type != "environment.parse.v1":
            return
        if body.outcome != "succeeded":
            if body.resultSummary:
                raise ExecutorServiceError("executor_result_invalid", status_code=422)
            return
        try:
            EnvironmentPlanParseResult.model_validate(body.resultSummary)
        except ValidationError as exc:
            raise ExecutorServiceError("executor_result_invalid", status_code=422) from exc

    @staticmethod
    def _validate_environment_preview_result(
        task: ExecutorTask, body: ExecutorTaskFinishBody
    ) -> ExecutorEnvironmentPreviewResult | None:
        if task.task_type != "environment.preview-bound.v1":
            return None
        if body.outcome != "succeeded":
            summary = body.resultSummary
            if set(summary) - {
                "runStatus", "phase", "errorCode", "errorSummary"
            }:
                raise ExecutorServiceError(
                    "executor_result_invalid", status_code=422
                )
            return None
        try:
            return ExecutorEnvironmentPreviewResult.model_validate(
                body.resultSummary
            )
        except ValidationError as exc:
            raise ExecutorServiceError(
                "executor_result_invalid", status_code=422
            ) from exc

    def _sync_hub_environment_snapshot(
        self,
        *,
        executor: LocalExecutor,
        snapshot: ExecutorHubEnvironmentSnapshot,
        now: datetime,
    ) -> None:
        """Replace one tenant cache from a complete, credential-free snapshot."""
        self.session.scalar(
            select(Tenant.id)
            .where(Tenant.id == executor.tenant_id)
            .with_for_update()
        )
        marker = self.session.get(
            HubEnvironmentInventorySync, executor.tenant_id
        )
        captured_at = as_utc(snapshot.capturedAt)
        if captured_at > now + timedelta(minutes=5):
            raise ExecutorServiceError(
                "executor_result_invalid", status_code=422
            )
        if marker is not None and as_utc(marker.completed_at) > captured_at:
            return

        existing = list(self.session.scalars(
            select(HubEnvironmentObservation).where(
                HubEnvironmentObservation.tenant_id == executor.tenant_id
            )
        ))
        by_key = {row.environment_key: row for row in existing}
        seen: set[str] = set()
        for item in snapshot.rows:
            seen.add(item.environmentKey)
            observation = by_key.get(item.environmentKey)
            if observation is None:
                observation = HubEnvironmentObservation(
                    id=uuid.uuid4(),
                    tenant_id=executor.tenant_id,
                    environment_key=item.environmentKey,
                    environment_name=item.environmentName,
                    environment_group=item.environmentGroup,
                    snapshot_revision=snapshot.snapshotRevision,
                    last_observed_at=captured_at,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(observation)
            observation.environment_name = item.environmentName
            observation.environment_ref = item.environmentRef
            observation.environment_serial = item.environmentSerial
            observation.environment_group = item.environmentGroup
            observation.site = item.site
            observation.source_order_ref = item.sourceOrderRef
            observation.snapshot_revision = snapshot.snapshotRevision
            observation.last_observed_at = captured_at
            observation.updated_at = now
        for observation in existing:
            if observation.environment_key not in seen:
                self.session.delete(observation)

        inventories = list(self.session.scalars(
            select(HubEnvironmentInventory).where(
                HubEnvironmentInventory.tenant_id == executor.tenant_id
            )
        ))
        by_order: dict[str, list[HubEnvironmentInventory]] = {}
        by_name: dict[str, list[HubEnvironmentInventory]] = {}
        by_ref: dict[str, list[HubEnvironmentInventory]] = {}
        for inventory in inventories:
            if inventory.source_order_ref:
                by_order.setdefault(inventory.source_order_ref, []).append(inventory)
            if inventory.environment_name:
                by_name.setdefault(inventory.environment_name, []).append(inventory)
            if inventory.environment_ref:
                by_ref.setdefault(inventory.environment_ref, []).append(inventory)
        snapshot_order_refs = {
            item.sourceOrderRef for item in snapshot.rows
            if item.sourceOrderRef
        }
        touched: set[uuid.UUID] = set()

        def unique_rows(
            rows: list[HubEnvironmentInventory],
        ) -> list[HubEnvironmentInventory]:
            deduplicated: dict[uuid.UUID, HubEnvironmentInventory] = {}
            for row in rows:
                deduplicated[row.id] = row
            return list(deduplicated.values())

        for item in snapshot.rows:
            ref_matches = unique_rows(
                list(by_ref.get(item.environmentRef, ()))
                if item.environmentRef else []
            )
            order_matches = unique_rows(
                list(by_order.get(item.sourceOrderRef, ()))
                if item.sourceOrderRef else []
            )
            name_matches = unique_rows(
                list(by_name.get(item.environmentName, ()))
            )
            # A complete snapshot is authoritative, but two durable rows
            # claiming the same strong identity require manual reconciliation.
            # Persist the observation cache and leave those reservations alone
            # instead of corrupting both rows or failing the whole task.
            if len(ref_matches) > 1 or len(order_matches) > 1:
                continue
            ref_match = ref_matches[0] if ref_matches else None
            order_match = order_matches[0] if order_matches else None
            if (
                ref_match is not None
                and order_match is not None
                and ref_match.id != order_match.id
            ):
                continue
            canonical = ref_match or order_match
            if canonical is None:
                if len(name_matches) != 1:
                    continue
                canonical = name_matches[0]
                if (
                    item.sourceOrderRef
                    and canonical.source_order_ref
                    and item.sourceOrderRef != canonical.source_order_ref
                ):
                    continue
                if (
                    item.environmentRef
                    and canonical.environment_ref
                    and item.environmentRef != canonical.environment_ref
                ):
                    continue
            if canonical.id in touched:
                continue

            name_conflicts = [
                row for row in name_matches if row.id != canonical.id
            ]
            if name_conflicts:
                safely_retired = all(
                    row.state == "deleted"
                    and not row.environment_ref
                    and (
                        not row.source_order_ref
                        or row.source_order_ref not in snapshot_order_refs
                    )
                    for row in name_conflicts
                )
                if not safely_retired:
                    continue
                # Flush tombstones before renaming the canonical row so SQL
                # statement ordering cannot trip the tenant/name constraint.
                for stale in name_conflicts:
                    self.session.delete(stale)
                self.session.flush()

            touched.add(canonical.id)
            canonical.environment_name = item.environmentName
            canonical.environment_ref = item.environmentRef
            canonical.environment_serial = item.environmentSerial
            if item.environmentGroup:
                canonical.environment_group = item.environmentGroup
            if item.site:
                canonical.site = item.site
            canonical.state = "active"
            canonical.last_observed_at = captured_at
            canonical.updated_at = now

        if marker is None:
            marker = HubEnvironmentInventorySync(
                tenant_id=executor.tenant_id,
                executor_id=executor.id,
                snapshot_revision=snapshot.snapshotRevision,
                environment_count=snapshot.environmentCount,
                completed_at=captured_at,
                created_at=now,
                updated_at=now,
            )
            self.session.add(marker)
        else:
            marker.executor_id = executor.id
            marker.snapshot_revision = snapshot.snapshotRevision
            marker.environment_count = snapshot.environmentCount
            marker.completed_at = captured_at
            marker.updated_at = now
        self.session.flush()

    def _sync_hub_environment_snapshot_safely(
        self,
        *,
        executor: LocalExecutor,
        snapshot: ExecutorHubEnvironmentSnapshot,
        now: datetime,
    ) -> None:
        """Keep cache reconciliation from poisoning task completion.

        Inventory reservations are a safety index, while observations are a
        replaceable performance cache. A future legacy-data edge case must not
        strand a completed local task in an infinite finish retry loop.
        """
        try:
            with self.session.begin_nested():
                self._sync_hub_environment_snapshot(
                    executor=executor, snapshot=snapshot, now=now
                )
        except IntegrityError:
            # The savepoint rollback leaves the previous complete cache intact.
            # The task result itself can still be committed and the next fresh
            # snapshot can retry reconciliation after data repair.
            self.session.expire_all()

    @staticmethod
    def _validated_workspace_snapshot(
        task: ExecutorTask, body: ExecutorTaskFinishBody
    ) -> ExecutorWorkspaceSnapshotResult | None:
        if task.task_type != "workspace.snapshot.v1":
            return None
        if body.outcome != "succeeded":
            if body.resultSummary:
                raise ExecutorServiceError("executor_result_invalid", status_code=422)
            return None
        try:
            return ExecutorWorkspaceSnapshotResult.model_validate(body.resultSummary)
        except ValidationError as exc:
            raise ExecutorServiceError("executor_result_invalid", status_code=422) from exc

    def _sync_workspace_preferences(
        self,
        *,
        executor: LocalExecutor,
        task: ExecutorTask,
        body: ExecutorTaskFinishBody,
    ) -> None:
        """Mirror an acknowledged local preference write into the cached snapshot."""
        if (
            task.task_type != "workspace.rpc.v1"
            or body.outcome != "succeeded"
            or not executor.workspace_snapshot
            or self.payload_cipher is None
        ):
            return
        try:
            request_payload = self.payload_cipher.decrypt(
                task.payload_envelope.get("encryptedPayload"),
                tenant_id=task.tenant_id,
                task_id=task.id,
                purpose="request",
            )
        except ExecutorPayloadCipherError as exc:
            raise ExecutorServiceError(str(exc), status_code=503) from exc
        if not isinstance(request_payload, dict):
            raise ExecutorServiceError("executor_result_invalid", status_code=422)
        if (
            request_payload.get("method") != "POST"
            or request_payload.get("path") != "/api/envbatch/preferences"
        ):
            return
        result = body.resultSummary
        result_body = result.get("body") if isinstance(result, dict) else None
        try:
            http_status = int(result.get("httpStatus") or 0)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ExecutorServiceError("executor_result_invalid", status_code=422) from exc
        if (
            not isinstance(result_body, dict)
            or not 200 <= http_status < 300
            or result.get("responseType") != "json"
        ):
            return
        preference_keys = {
            "purchaseSite",
            "purchaseTags",
            "importBuyerPlan",
            "verifySampleCount",
            "buyers",
            "buyerDefaultSplit",
            "backupMaxCount",
        }
        try:
            preferences = WorkspaceEnvironmentPreferences.model_validate(
                {key: result_body[key] for key in preference_keys}
            )
        except (KeyError, ValidationError) as exc:
            raise ExecutorServiceError("executor_result_invalid", status_code=422) from exc
        snapshot = dict(executor.workspace_snapshot)
        snapshot["preferences"] = preferences.model_dump(mode="json")
        executor.workspace_snapshot = snapshot
        executor.workspace_snapshot_revision = self._payload_hash(snapshot)

    def workspace_snapshot_payload(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
    ) -> dict[str, Any]:
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        captured_at = executor.workspace_snapshot_at
        public_snapshot = dict(executor.workspace_snapshot or {})
        public_snapshot.pop("configSummary", None)
        age_seconds = (
            max(0, int((utcnow() - as_utc(captured_at)).total_seconds()))
            if captured_at else None
        )
        return {
            "executorId": str(executor.id),
            "snapshot": public_snapshot or None,
            "snapshotRevision": executor.workspace_snapshot_revision,
            "capturedAt": captured_at.isoformat() if captured_at else None,
            "ageSeconds": age_seconds,
            "stale": age_seconds is None or age_seconds > 300,
        }

    def cached_config_payload(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return the last safe config snapshot without scheduling device work."""
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        latest_config_task = self.session.scalar(
            select(ExecutorTask)
            .where(
                ExecutorTask.tenant_id == tenant_id,
                ExecutorTask.executor_id == executor.id,
                ExecutorTask.task_type.in_({"config.read.v1", "config.write.v1"}),
                ExecutorTask.status == "succeeded",
            )
            .order_by(ExecutorTask.finished_at.desc())
            .limit(1)
        )
        snapshot = executor.workspace_snapshot or {}
        snapshot_runtime = snapshot.get("runtimeConfig")
        config_summary = snapshot.get("configSummary")
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        latest_summary = (
            latest_config_task.result_summary
            if latest_config_task is not None else None
        )
        if (
            latest_config_task is not None
            and latest_config_task.finished_at is not None
            and isinstance(latest_summary, dict)
            and isinstance(latest_summary.get("config"), dict)
        ):
            candidates.append((
                as_utc(latest_config_task.finished_at),
                {
                    "configRevision": latest_summary.get("configRevision"),
                    **latest_summary.get("config", {}),
                },
            ))
        if (
            isinstance(config_summary, dict)
            and isinstance(config_summary.get("runtimeConfig"), dict)
        ):
            try:
                summary_at = as_utc(datetime.fromisoformat(
                    str(config_summary.get("capturedAt") or "")
                ))
            except (TypeError, ValueError):
                pass
            else:
                candidates.append((
                    summary_at,
                    {
                        "configRevision": config_summary.get(
                            "configRevision"
                        ),
                        **config_summary.get("runtimeConfig", {}),
                    },
                ))
        if (
            isinstance(snapshot_runtime, dict)
            and executor.workspace_snapshot_at is not None
        ):
            candidates.append((
                as_utc(executor.workspace_snapshot_at),
                snapshot_runtime,
            ))
        if not candidates:
            return None
        runtime_keys = {
            "configRevision",
            "hubPort",
            "concurrency",
            "envCreateWorkers",
            "verifySampleCount",
            "safeParallelTasks",
            "queryBrowserMode",
            "queryAllowOpenEnvironment",
        }
        for captured_at, runtime in sorted(
            candidates, key=lambda item: item[0], reverse=True
        ):
            try:
                validated = WorkspaceRuntimeConfig.model_validate(
                    {
                        key: runtime[key]
                        for key in runtime_keys
                        if key in runtime
                    }
                )
            except ValidationError:
                continue
            validated_payload = validated.model_dump(mode="json")
            revision = validated_payload.pop("configRevision")
            return {
                "configRevision": revision,
                "config": validated_payload,
                "capturedAt": captured_at.isoformat(),
            }
        return None

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _result_summary(self, task: ExecutorTask) -> dict[str, Any]:
        summary = task.result_summary or {}
        if task.task_type not in ENCRYPTED_TASK_TYPES or not summary:
            return summary
        if self.payload_cipher is None:
            raise ExecutorServiceError(
                "executor_payload_encryption_unavailable", status_code=503
            )
        try:
            return self.payload_cipher.decrypt(
                summary.get("encryptedResult"),
                tenant_id=task.tenant_id,
                task_id=task.id,
                purpose="result",
            )
        except ExecutorPayloadCipherError as exc:
            raise ExecutorServiceError(str(exc), status_code=503) from exc

    def _sync_operation_run(
        self,
        task: ExecutorTask,
        *,
        status: str,
        phase: str | None,
        attempt: int,
        heartbeat_at: datetime,
        progress_current: int | None = None,
        progress_total: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        result_summary: dict[str, Any] | None = None,
        progress_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if task.task_type.startswith(("environment.create-", "environment.retry-")) \
                or task.task_type == "environment.preview-bound.v1":
            run = self.session.scalar(
                select(EnvironmentCreationRun).where(
                    EnvironmentCreationRun.executor_task_id == task.id
                )
            )
        elif task.task_type == "logistics.query.v1":
            run = self.session.scalar(
                select(LogisticsQueryRun).where(
                    LogisticsQueryRun.executor_task_id == task.id
                )
            )
        else:
            return
        if run is None:
            return
        run.status = status
        if phase is not None:
            run.phase = phase[:64]
        if phase == "cancel_requested" or status == "cancelled":
            run.stop_requested = True
        run.attempt = max(0, int(attempt))
        run.last_heartbeat_at = heartbeat_at
        run.updated_at = heartbeat_at
        if started_at is not None:
            run.started_at = run.started_at or started_at
        if completed_at is not None:
            run.completed_at = completed_at
        if progress_total is not None and not (
            isinstance(run, EnvironmentCreationRun) and run.run_mode == "dry_run"
        ):
            run.progress_total = max(0, progress_total)
        if progress_current is not None and not (
            isinstance(run, EnvironmentCreationRun) and run.run_mode == "dry_run"
        ):
            run.progress_completed = min(
                max(0, progress_current), max(0, run.progress_total)
            )
        summary = result_summary or {}
        success_count = _safe_nonnegative_int(summary.get("successCount"))
        failed_count = _safe_nonnegative_int(summary.get("failedCount"))
        if success_count is not None:
            run.success_count = min(success_count, run.total_count)
        if failed_count is not None:
            run.failed_count = min(failed_count, run.total_count)
        if isinstance(run, EnvironmentCreationRun):
            error_code = str(summary.get("errorCode") or "").strip()
            error_summary = str(summary.get("errorSummary") or "").strip()
            run.error_code = error_code[:128] or None
            run.error_summary = error_summary[:300] or None
            ip_ok = _safe_nonnegative_int(summary.get("ipOkCount"))
            ip_total = _safe_nonnegative_int(summary.get("ipTotalCount"))
            if ip_ok is not None:
                run.ip_ok_count = ip_ok
            if ip_total is not None:
                run.ip_total_count = ip_total
            if (
                run.run_mode == "dry_run"
                and status in OperationRunService.TERMINAL_STATUSES
            ):
                preview_rows = summary.get("rows")
                if status == "completed" and isinstance(preview_rows, list):
                    OperationRunService(self.session).complete_environment_preview(
                        run=run,
                        rows=[dict(item) for item in preview_rows],
                        completed_at=completed_at or heartbeat_at,
                    )
                else:
                    run.progress_completed = 0
                    run.progress_total = run.total_count
                    run.success_count = 0
                    run.failed_count = run.total_count
                return
            if progress_snapshot is not None:
                self._upsert_environment_progress(run, progress_snapshot, heartbeat_at)
            guard_service = OperationRunService(self.session)
            if phase == "cancel_requested" or str(phase or "").endswith(
                "rolling_back"
            ):
                guard_service.mark_environment_guards_cleanup_pending(
                    run_id=run.id,
                    now=heartbeat_at,
                )
            if status in OperationRunService.TERMINAL_STATUSES:
                guard_service.finalize_environment_account_guards(
                    run=run,
                    status=status,
                    summary=summary,
                    now=heartbeat_at,
                )
                guard_service.finalize_environment_inventory(
                    run=run,
                    status=status,
                    now=heartbeat_at,
                )
        elif progress_snapshot is not None:
            self._upsert_logistics_progress(run, progress_snapshot, heartbeat_at)

    def _upsert_environment_progress(
        self,
        run: EnvironmentCreationRun,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> None:
        if set(snapshot) != {"rows"} or not isinstance(snapshot.get("rows"), list):
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422)
        try:
            rows = [EnvironmentRunProgressItem.model_validate(item) for item in snapshot["rows"]]
        except ValidationError as exc:
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422) from exc
        refs = [row.accountRef for row in rows]
        if len(refs) != len(set(refs)) or len(rows) > run.total_count:
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422)
        existing = {
            row.account_ref: row
            for row in self.session.scalars(
                select(EnvironmentCreationResult).where(
                    EnvironmentCreationResult.run_id == run.id
                )
            )
        }
        for item in rows:
            row = existing.get(item.accountRef)
            if row is None:
                row = EnvironmentCreationResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    account_ref=item.accountRef,
                    account_label=item.accountLabel,
                    purchaser_label=item.purchaserLabel,
                    environment_name=item.environmentName,
                    status=item.status,
                    completed_steps=list(item.completedSteps),
                    recovered_existing=item.recoveredExisting,
                    feishu_sync_status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(row)
            row.account_label = item.accountLabel
            row.purchaser_label = item.purchaserLabel
            row.environment_name = item.environmentName
            row.environment_ref = item.environmentRef
            row.environment_serial = item.environmentSerial
            row.status = item.status
            row.current_step = item.currentStep or None
            row.completed_steps = list(item.completedSteps)
            row.error_step = item.errorStep or None
            row.error_summary = item.errorSummary or None
            row.recovered_existing = item.recoveredExisting
            row.created_in_run = item.createdInRun
            row.cleanup_status = item.cleanupStatus
            row.cleanup_error_code = item.cleanupErrorCode or None
            row.cleanup_error_summary = item.cleanupErrorSummary or None
            row.ip_address = item.ipAddress or None
            row.ip_country = item.ipCountry or None
            row.ip_verified = item.ipVerified
            row.ip_error_code = item.ipErrorCode or None
            row.ip_error_summary = item.ipErrorSummary or None
            row.updated_at = now

    def _upsert_logistics_progress(
        self,
        run: LogisticsQueryRun,
        snapshot: dict[str, Any],
        now: datetime,
    ) -> None:
        for stale in self.session.scalars(
            select(LogisticsQueryResult).where(
                LogisticsQueryResult.tenant_id == run.tenant_id,
                LogisticsQueryResult.screenshot_expires_at <= now,
                LogisticsQueryResult.screenshot_content.is_not(None),
            ).limit(100)
        ):
            stale.screenshot_content = None
            stale.screenshot_content_type = None
            stale.screenshot_sha256 = None
            stale.screenshot_size = None
        if not set(snapshot).issubset({"rows", "screenshots"}) \
                or "rows" not in snapshot \
                or not isinstance(snapshot.get("rows"), list) \
                or not isinstance(snapshot.get("screenshots", []), list):
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422)
        try:
            rows = [LogisticsRunProgressItem.model_validate(item) for item in snapshot["rows"]]
            screenshots = [
                LogisticsScreenshotProgressItem.model_validate(item)
                for item in snapshot.get("screenshots", [])
            ]
        except ValidationError as exc:
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422) from exc
        serials = [row.environmentSerial for row in rows]
        screenshot_serials = [item.environmentSerial for item in screenshots]
        if (
            len(serials) != len(set(serials)) or len(rows) > run.total_count
            or len(screenshot_serials) != len(set(screenshot_serials))
            or not set(screenshot_serials).issubset(set(serials))
        ):
            raise ExecutorServiceError("executor_progress_snapshot_invalid", status_code=422)
        existing = {
            row.environment_serial: row
            for row in self.session.scalars(
                select(LogisticsQueryResult).where(LogisticsQueryResult.run_id == run.id)
            )
        }
        for item in rows:
            row = existing.get(item.environmentSerial)
            if row is None:
                row = LogisticsQueryResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    environment_serial=item.environmentSerial,
                    status=item.status,
                    completed_steps=list(item.completedSteps),
                    tracking_numbers=list(item.trackingNumbers),
                    package_numbers=list(item.packageNumbers),
                    cancelled=False,
                    risk_order=False,
                    feishu_sync_status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(row)
                existing[item.environmentSerial] = row
            row.environment_name = item.environmentName or None
            row.status = item.status
            row.current_step = item.currentStep or None
            row.completed_steps = list(item.completedSteps)
            row.platform_order_no = item.platformOrderNo or None
            row.order_time_text = item.orderTime or None
            row.amount_text = item.amount or None
            row.platform_status = item.platformStatus or None
            row.status_label = item.statusLabel or None
            row.fulfillment_stage = item.fulfillmentStage or None
            row.tracking_numbers = list(item.trackingNumbers)
            row.package_numbers = list(item.packageNumbers)
            row.carrier = item.carrier or None
            row.first_tracking_at = item.firstTrackingAt
            row.first_tracking_time_text = item.firstTrackingTime or None
            row.first_tracking_summary = item.firstTrackingSummary or None
            row.first_tracking_lead_minutes = item.firstTrackingLeadMinutes
            row.cancelled = item.cancelled
            row.risk_order = item.riskOrder
            row.risk_summary = item.riskSummary or None
            row.ip_address = item.ipAddress or None
            row.time_zone = item.timeZone or None
            row.utc_offset_minutes = item.utcOffsetMinutes
            row.queried_at = item.queriedAt
            row.execution_attempted = item.executionAttempted
            row.execution_duration_ms = item.executionDurationMs
            row.error_summary = item.errorSummary or None
            row.screenshot_status = item.screenshotStatus or None
            row.updated_at = now
        for item in screenshots:
            try:
                content = base64.b64decode(item.contentBase64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ExecutorServiceError(
                    "executor_progress_snapshot_invalid", status_code=422
                ) from exc
            if len(content) != item.size \
                    or hashlib.sha256(content).hexdigest() != item.sha256 \
                    or not content.startswith(b"\xff\xd8") \
                    or not content.endswith(b"\xff\xd9"):
                raise ExecutorServiceError(
                    "executor_progress_snapshot_invalid", status_code=422
                )
            row = existing.get(item.environmentSerial)
            if row is None:
                raise ExecutorServiceError(
                    "executor_progress_snapshot_invalid", status_code=422
                )
            row.screenshot_content = content
            row.screenshot_content_type = item.contentType
            row.screenshot_sha256 = item.sha256
            row.screenshot_size = item.size
            row.screenshot_expires_at = now + timedelta(hours=24)
            row.screenshot_status = "ok"
            row.updated_at = now

    @staticmethod
    def _require_lease(
        task: ExecutorTask, lease_token: str, *, allow_expired: bool = False
    ) -> None:
        if not task.lease_token_digest or task.lease_token_digest != hash_token(lease_token):
            raise ExecutorServiceError("executor_lease_invalid", status_code=401)
        if (
            not allow_expired
            and task.lease_until is not None
            and as_utc(task.lease_until) <= utcnow()
        ):
            raise ExecutorServiceError("executor_lease_expired", status_code=409)

    def _event(
        self,
        task: ExecutorTask,
        event_type: str,
        *,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
        stable_code: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.session.add(
            ExecutorTaskEvent(
                task_id=task.id,
                tenant_id=task.tenant_id,
                executor_id=task.executor_id,
                event_type=event_type,
                phase=phase,
                progress_current=current,
                progress_total=total,
                stable_code=stable_code,
                trace_id=trace_id,
                created_at=utcnow(),
            )
        )
