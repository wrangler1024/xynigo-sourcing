"""Durable pairing and lease service for local Xynigo executors."""

from __future__ import annotations

import secrets
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .executor_contract import (
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
    ExecutorPairingCode,
    ExecutorTask,
    ExecutorTaskEvent,
    LocalExecutor,
)
from .security import hash_token, random_url_token


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ACTIVE_TASK_STATUSES = frozenset({"queued", "leased", "running", "cancel_requested"})
MAX_QUEUED_WORKSPACE_RPC_TASKS = 32
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
        "proxyConfigured",
        "proxySource",
        "buyers",
        "buyerDefaultSplit",
        "backupMaxCount",
        "larkLedgerTargetConfigured",
    }
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
                else:
                    task.status = "uncertain"
                    task.finished_at = now
                    task.result_code = "executor_revoked_during_execution"
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
        return {
            "executor": self.executor_payload(executor),
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
    ) -> ExecutorTask:
        executor = self.require_executor(
            tenant_id=tenant_id,
            user_id=user_id,
            executor_id=executor_id,
        )
        now = utcnow()
        if executor.status != "active":
            raise ExecutorServiceError("executor_revoked", status_code=409)
        if not self._online(executor, now=now):
            raise ExecutorServiceError("executor_offline", status_code=409)
        if task_type not in set(executor.capabilities or []):
            raise ExecutorServiceError("executor_capability_missing", status_code=409)
        if task_type == "workspace.rpc.v1" and self.payload_cipher is None:
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
                if task_type == "workspace.rpc.v1"
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
                {} if task_type == "workspace.rpc.v1" else payload
            ),
            created_by_user_id=user_id,
        )
        self.session.add(task)
        self.session.flush()
        if task_type == "workspace.rpc.v1":
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
        self.session.commit()
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
            self._event(task, "cancelled", stable_code=task.result_code)
        else:
            task.status = "cancel_requested"
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
        self.session.commit()

        deadline = time.monotonic() + body.waitSeconds
        while True:
            now = utcnow()
            self._recover_expired_leases(executor=executor, now=now, trace_id=trace_id)
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
        task.lease_until = utcnow() + timedelta(seconds=self.lease_seconds)
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
        if task.status in {"succeeded", "failed"}:
            if (
                task.status == body.outcome
                and task.result_code == body.resultCode
                and self._result_summary(task) == body.resultSummary
            ):
                return task
            raise ExecutorServiceError("executor_task_finish_conflict", status_code=409)
        if task.status not in {"leased", "running", "cancel_requested"}:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        self._validate_config_result(task, body)
        now = utcnow()
        task.status = body.outcome
        task.finished_at = now
        task.result_code = body.resultCode
        if task.task_type == "workspace.rpc.v1":
            if self.payload_cipher is None:
                raise ExecutorServiceError(
                    "executor_payload_encryption_unavailable", status_code=503
                )
            try:
                task.result_summary = {
                    "schemaVersion": 1,
                    "resultHash": self._payload_hash(body.resultSummary),
                    "encryptedResult": self.payload_cipher.encrypt(
                        body.resultSummary,
                        tenant_id=task.tenant_id,
                        task_id=task.id,
                        purpose="result",
                    ),
                }
            except ExecutorPayloadCipherError as exc:
                raise ExecutorServiceError(str(exc), status_code=503) from exc
        else:
            task.result_summary = body.resultSummary
        task.lease_until = None
        if body.outcome == "succeeded" and task.task_type.startswith("config."):
            revision = str(body.resultSummary.get("configRevision") or "")
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
            "revokedAt": executor.revoked_at.isoformat() if executor.revoked_at else None,
            "createdAt": executor.created_at.isoformat() if executor.created_at else None,
        }

    def task_payload(self, task: ExecutorTask) -> dict[str, Any]:
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
        self._event(task, "leased", trace_id=trace_id)
        self.session.commit()
        payload = task.payload_envelope
        if task.task_type == "workspace.rpc.v1":
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
                self._event(task, "lease_recovered", trace_id=trace_id)
            else:
                task.status = "uncertain"
                task.finished_at = now
                task.result_code = "lease_expired_after_start"
                task.lease_until = None
                self._event(
                    task,
                    "execution_uncertain",
                    stable_code=task.result_code,
                    trace_id=trace_id,
                )
        self.session.commit()

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
        if task.task_type != "workspace.rpc.v1" or not summary:
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
            )
        )
