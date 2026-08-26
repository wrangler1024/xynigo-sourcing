"""Retryable Feishu Base mirror for operational result outbox rows."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .buyer_account_sync import buyer_account_fields
from .buyer_credential_crypto import BuyerCredentialCipher
from .models import (
    BuyerAccount,
    EnvironmentCreationResult,
    LogisticsQueryResult,
    OperationalSyncOutbox,
)


TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
BASE_ORIGIN = "https://open.feishu.cn"
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})
RETRYABLE_CODES = frozenset({1254291, 800004135})
TOKEN_INVALID_CODES = frozenset({99991663, 99991671})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FeishuBaseSyncError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = str(code or "feishu_sync_failed")[:128]
        self.retryable = bool(retryable)
        super().__init__(self.code)


class FeishuOperationBaseClient:
    """Small app-identity Base client; credentials and tokens never leave memory."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_token: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_token = base_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    def _tenant_token(self, client: httpx.Client, *, force: bool = False) -> str:
        with self._lock:
            if not force and self._token and time.time() < self._token_expires_at:
                return self._token
            response = client.post(
                TOKEN_ENDPOINT,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                headers={"Accept": "application/json"},
            )
            payload = self._decode(response, "tenant_token")
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise FeishuBaseSyncError("tenant_token_response", retryable=True)
            try:
                expires = max(60, int(payload.get("expire") or 7200))
            except (TypeError, ValueError):
                expires = 7200
            self._token = token
            self._token_expires_at = time.time() + max(30, expires - 120)
            return token

    @staticmethod
    def _decode(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuBaseSyncError(
                f"{stage}_response", retryable=response.status_code in RETRYABLE_HTTP
            ) from exc
        if not isinstance(payload, dict):
            raise FeishuBaseSyncError(f"{stage}_response", retryable=False)
        code = payload.get("code", 0 if response.status_code < 400 else response.status_code)
        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            numeric_code = -1
        if response.status_code < 400 and numeric_code == 0:
            return payload
        raise FeishuBaseSyncError(
            f"feishu_{numeric_code}",
            retryable=(
                response.status_code in RETRYABLE_HTTP or numeric_code in RETRYABLE_CODES
            ),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            refreshed = False
            while True:
                token = self._tenant_token(client, force=refreshed)
                response = client.request(
                    method,
                    BASE_ORIGIN + path,
                    json=payload,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
                try:
                    return self._decode(response, "base")
                except FeishuBaseSyncError as exc:
                    provider_code = exc.code.removeprefix("feishu_")
                    if not refreshed and provider_code.isdigit() and int(provider_code) in TOKEN_INVALID_CODES:
                        refreshed = True
                        with self._lock:
                            self._token = ""
                            self._token_expires_at = 0.0
                        continue
                    raise

    def _records_path(self, table_id: str) -> str:
        return "/open-apis/bitable/v1/apps/%s/tables/%s/records" % (
            quote(self.base_token, safe=""),
            quote(table_id, safe=""),
        )

    def _find_by_sync_key(
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
                if str(fields.get(key_field) or "") == sync_key:
                    matches.append(item)
            if not data.get("has_more"):
                return matches
            next_token = str(data.get("page_token") or "")
            if not next_token or next_token == page_token:
                raise FeishuBaseSyncError("record_pagination_stalled", retryable=True)
            page_token = next_token

    def upsert(
        self,
        *,
        table_id: str,
        sync_key: str,
        fields: dict[str, Any],
        key_field: str = "同步键",
    ) -> str:
        matches = self._find_by_sync_key(table_id, key_field, sync_key)
        if len(matches) > 1:
            raise FeishuBaseSyncError("duplicate_sync_key", retryable=False)
        if matches:
            record_id = str(matches[0].get("record_id") or "")
            if not record_id:
                raise FeishuBaseSyncError("record_id_missing", retryable=False)
            self._request(
                "PUT",
                self._records_path(table_id) + "/" + quote(record_id, safe=""),
                payload={"fields": fields},
            )
        else:
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
        read_fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        if str(read_fields.get(key_field) or "") != sync_key:
            raise FeishuBaseSyncError("record_readback_mismatch", retryable=True)
        return record_id


class FeishuOperationSyncWorker:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client: FeishuOperationBaseClient,
        buyer_account_table_id: str,
        environment_table_id: str,
        logistics_table_id: str,
        buyer_credential_cipher: BuyerCredentialCipher | None = None,
        interval_seconds: int = 15,
        max_attempts: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.buyer_account_table_id = buyer_account_table_id
        self.environment_table_id = environment_table_id
        self.logistics_table_id = logistics_table_id
        self.buyer_credential_cipher = buyer_credential_cipher
        self.interval_seconds = max(5, int(interval_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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
            processed = self.run_once(limit=50)
            if processed == 0:
                self._stop.wait(self.interval_seconds)

    def recover_stale(self) -> None:
        cutoff = utcnow() - timedelta(minutes=10)
        with self.session_factory() as session:
            session.execute(
                update(OperationalSyncOutbox)
                .where(
                    OperationalSyncOutbox.status == "processing",
                    OperationalSyncOutbox.updated_at < cutoff,
                )
                .values(status="pending", available_at=utcnow(), last_error_code="worker_recovered")
            )
            session.execute(
                update(BuyerAccount)
                .where(BuyerAccount.feishu_sync_status == "processing")
                .values(feishu_sync_status="pending")
            )
            session.execute(
                update(EnvironmentCreationResult)
                .where(EnvironmentCreationResult.feishu_sync_status == "processing")
                .values(feishu_sync_status="pending")
            )
            session.execute(
                update(LogisticsQueryResult)
                .where(LogisticsQueryResult.feishu_sync_status == "processing")
                .values(feishu_sync_status="pending")
            )
            session.commit()

    def run_once(self, *, limit: int = 50) -> int:
        processed = 0
        for _index in range(max(1, int(limit))):
            claimed = self._claim_one()
            if claimed is None:
                break
            processed += 1
            outbox_id, aggregate_type, aggregate_id, payload, attempt_count = claimed
            if aggregate_type == "buyer_account" and self._buyer_event_is_stale(
                aggregate_id, payload
            ):
                self._mark_obsolete(outbox_id)
                continue
            if aggregate_type == "buyer_account":
                self._mark_aggregate_processing(aggregate_type, aggregate_id)
            try:
                table_id = {
                    "buyer_account": self.buyer_account_table_id,
                    "environment_creation_result": self.environment_table_id,
                    "logistics_query_result": self.logistics_table_id,
                }[aggregate_type]
                fields = (
                    self._buyer_fields(aggregate_id)
                    if aggregate_type == "buyer_account"
                    else dict(payload.get("fields") or {})
                )
                timestamp_field = str(
                    payload.get("syncTimestampField") or "飞书同步时间"
                )
                fields[timestamp_field] = int(utcnow().timestamp() * 1000)
                record_id = self.client.upsert(
                    table_id=table_id,
                    sync_key=str(payload.get("syncKey") or ""),
                    fields=fields,
                    key_field=str(payload.get("keyField") or "同步键"),
                )
            except FeishuBaseSyncError as exc:
                self._mark_failure(
                    outbox_id=outbox_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    attempt_count=attempt_count,
                    code=exc.code,
                    retryable=exc.retryable,
                )
            except Exception:
                self._mark_failure(
                    outbox_id=outbox_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    attempt_count=attempt_count,
                    code="unexpected_sync_failure",
                    retryable=True,
                )
            else:
                self._mark_success(
                    outbox_id=outbox_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    record_id=record_id,
                )
        return processed

    def _buyer_fields(self, aggregate_id: uuid.UUID) -> dict[str, Any]:
        with self.session_factory() as session:
            account = session.get(BuyerAccount, aggregate_id)
            if account is None:
                raise FeishuBaseSyncError("buyer_account_missing", retryable=False)
            if account.credentials_ciphertext and self.buyer_credential_cipher is None:
                raise FeishuBaseSyncError(
                    "buyer_credential_key_unavailable", retryable=False
                )
            try:
                return buyer_account_fields(account, self.buyer_credential_cipher)
            except Exception as exc:
                raise FeishuBaseSyncError(
                    "buyer_credential_decrypt_failed", retryable=False
                ) from exc

    def _buyer_event_is_stale(
        self, aggregate_id: uuid.UUID, payload: dict[str, Any]
    ) -> bool:
        try:
            event_version = int(payload.get("version") or 0)
        except (TypeError, ValueError):
            event_version = 0
        with self.session_factory() as session:
            current_version = session.scalar(
                select(BuyerAccount.version).where(BuyerAccount.id == aggregate_id)
            )
        return current_version is None or event_version < int(current_version)

    def _mark_obsolete(self, outbox_id: uuid.UUID) -> None:
        now = utcnow()
        with self.session_factory() as session:
            event = session.get(OperationalSyncOutbox, outbox_id)
            if event is None:
                return
            event.status = "completed"
            event.last_error_code = "superseded"
            event.processed_at = now
            event.updated_at = now
            session.commit()

    def _mark_aggregate_processing(
        self, aggregate_type: str, aggregate_id: uuid.UUID
    ) -> None:
        with self.session_factory() as session:
            self._set_aggregate_status(
                session,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                status="processing",
            )
            session.commit()

    def _claim_one(
        self,
    ) -> tuple[uuid.UUID, str, uuid.UUID, dict[str, Any], int] | None:
        with self.session_factory() as session:
            event = session.scalar(
                select(OperationalSyncOutbox)
                .where(
                    OperationalSyncOutbox.status == "pending",
                    OperationalSyncOutbox.available_at <= utcnow(),
                )
                .order_by(OperationalSyncOutbox.created_at, OperationalSyncOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None
            event.status = "processing"
            event.attempt_count += 1
            event.updated_at = utcnow()
            if event.aggregate_type != "buyer_account":
                self._set_aggregate_status(
                    session,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    status="processing",
                )
            result = (
                event.id,
                event.aggregate_type,
                event.aggregate_id,
                dict(event.payload or {}),
                event.attempt_count,
            )
            session.commit()
            return result

    def _mark_success(
        self,
        *,
        outbox_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        record_id: str,
    ) -> None:
        now = utcnow()
        with self.session_factory() as session:
            event = session.get(OperationalSyncOutbox, outbox_id)
            if event is None:
                return
            event.status = "completed"
            event.external_record_id = record_id
            event.last_error_code = None
            event.processed_at = now
            event.updated_at = now
            self._set_aggregate_status(
                session,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                status="completed",
                record_id=record_id,
                synced_at=now,
            )
            session.commit()

    def _mark_failure(
        self,
        *,
        outbox_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        attempt_count: int,
        code: str,
        retryable: bool,
    ) -> None:
        terminal = not retryable or attempt_count >= self.max_attempts
        with self.session_factory() as session:
            event = session.get(OperationalSyncOutbox, outbox_id)
            if event is None:
                return
            event.status = "failed" if terminal else "pending"
            event.available_at = utcnow() + timedelta(
                seconds=min(900, 15 * (2 ** max(0, attempt_count - 1)))
            )
            event.last_error_code = str(code or "feishu_sync_failed")[:128]
            event.updated_at = utcnow()
            self._set_aggregate_status(
                session,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                status="failed" if terminal else "pending",
            )
            session.commit()

    @staticmethod
    def _set_aggregate_status(
        session: Session,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        status: str,
        record_id: str | None = None,
        synced_at: datetime | None = None,
    ) -> None:
        model = {
            "buyer_account": BuyerAccount,
            "environment_creation_result": EnvironmentCreationResult,
            "logistics_query_result": LogisticsQueryResult,
        }[aggregate_type]
        values: dict[str, Any] = {"feishu_sync_status": status}
        if record_id is not None:
            values["feishu_record_id"] = record_id
        if synced_at is not None:
            values["feishu_synced_at"] = synced_at
        session.execute(update(model).where(model.id == aggregate_id).values(**values))
