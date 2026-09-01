"""Cloud parsing and encrypted lifecycle for buyer-account environment plans."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .environment_plan_core import (
    EnvBatchError,
    count_mixed_site_accounts,
    deserialize_buyer_accounts,
    normalize_env_site,
    parse_vendor_workbook,
    serialize_buyer_accounts,
    validate_accounts_site,
)
from .environment_plan_crypto import EnvironmentPlanCipher, EnvironmentPlanCipherError
from .models import EnvironmentAccountPlan, EnvironmentAccountPlanRequest


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PLAN_CREDENTIAL_BYTES = 28 * 1024 * 1024
MAX_EXECUTOR_PLAN_JSON_BYTES = 24 * 1024 * 1024


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CloudEnvironmentPlanError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


def _decode_upload(content_base64: str) -> bytes:
    try:
        source = base64.b64decode(content_base64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise CloudEnvironmentPlanError(
            "environment_plan_upload_invalid",
            "xlsx 上传内容不是合法 Base64",
        ) from exc
    if not source:
        raise CloudEnvironmentPlanError(
            "environment_plan_upload_invalid", "xlsx 上传内容为空"
        )
    if len(source) > MAX_UPLOAD_BYTES:
        raise CloudEnvironmentPlanError(
            "environment_plan_upload_too_large", "xlsx 超过 20MB，拒绝载入", status=413
        )
    try:
        with ZipFile(BytesIO(source)) as archive:
            entries = archive.infolist()
            uncompressed_bytes = sum(item.file_size for item in entries)
            if (
                len(entries) > 1000
                or uncompressed_bytes > MAX_XLSX_UNCOMPRESSED_BYTES
            ):
                raise CloudEnvironmentPlanError(
                    "environment_plan_upload_too_large",
                    "xlsx 解压后内容过大，拒绝载入",
                    status=413,
                )
    except BadZipFile as exc:
        raise CloudEnvironmentPlanError(
            "environment_plan_upload_invalid", "上传文件不是有效的 xlsx"
        ) from exc
    return source


def _order_mask(value: str) -> str:
    token = str(value or "")
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "***" + token[-4:]


class CloudEnvironmentPlanService:
    def __init__(
        self,
        *,
        cipher: EnvironmentPlanCipher,
        plan_ttl_seconds: int = 1800,
        max_active_plans_per_tenant: int = 5,
    ) -> None:
        self.cipher = cipher
        self.plan_ttl_seconds = int(plan_ttl_seconds)
        self.max_active_plans_per_tenant = int(max_active_plans_per_tenant)

    def expire_plans(self, session: Session) -> int:
        records = list(
            session.scalars(
                select(EnvironmentAccountPlan).where(
                    EnvironmentAccountPlan.status != "expired",
                    EnvironmentAccountPlan.expires_at <= utcnow(),
                )
            )
        )
        for record in records:
            record.status = "expired"
            record.encrypted_payload = None
        if records:
            session.flush()
        return len(records)

    @staticmethod
    def _parse_cloud_plan_id(value: object) -> uuid.UUID:
        try:
            return uuid.UUID(str(value or ""))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CloudEnvironmentPlanError(
                "environment_plan_invalid", "解析计划编号格式无效"
            ) from exc

    def _record(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        cloud_plan_id: object,
        for_update: bool = False,
    ) -> EnvironmentAccountPlan:
        statement = select(EnvironmentAccountPlan).where(
            EnvironmentAccountPlan.id == self._parse_cloud_plan_id(cloud_plan_id),
            EnvironmentAccountPlan.tenant_id == tenant_id,
            EnvironmentAccountPlan.created_by_user_id == actor_user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise CloudEnvironmentPlanError(
                "environment_plan_not_found",
                "解析计划不存在或不属于当前用户",
                status=404,
            )
        if record.status == "expired" or _as_aware(record.expires_at) <= utcnow():
            record.status = "expired"
            record.encrypted_payload = None
            session.flush()
            raise CloudEnvironmentPlanError(
                "environment_plan_expired", "解析计划已过期，请重新选择 xlsx", status=410
            )
        return record

    def _accounts(self, record: EnvironmentAccountPlan) -> list[object]:
        if not record.encrypted_payload:
            raise CloudEnvironmentPlanError(
                "environment_plan_consumed", "解析计划已提交，请重新上传 xlsx", status=409
            )
        try:
            payload = self.cipher.decrypt(
                record.encrypted_payload,
                tenant_id=record.tenant_id,
                plan_id=record.id,
            )
        except EnvironmentPlanCipherError as exc:
            raise CloudEnvironmentPlanError(
                "environment_plan_decrypt_failed",
                "解析计划无法解密，请重新上传 xlsx",
                status=409,
            ) from exc
        if payload.get("version") != 1 or not isinstance(payload.get("accounts"), list):
            raise CloudEnvironmentPlanError(
                "environment_plan_invalid", "解析计划结构无效，请重新上传 xlsx", status=409
            )
        try:
            return deserialize_buyer_accounts(payload["accounts"], site=record.site)
        except EnvBatchError as exc:
            raise CloudEnvironmentPlanError(
                "environment_plan_invalid", str(exc), status=409
            ) from exc

    @staticmethod
    def _public_result(
        record: EnvironmentAccountPlan, *, reused: bool
    ) -> dict[str, Any]:
        safe_summary = dict(record.preview_summary or {})
        return {
            "cloudPlanId": str(record.id),
            "site": record.site,
            "environmentGroup": record.environment_group,
            "count": record.account_count,
            "cookieCount": record.cookie_count,
            "mixedSiteCookieCount": record.mixed_site_cookie_count,
            "passwordKindCount": record.password_kind_count,
            "duplicateCount": 0,
            "issueCount": 0,
            "orderCount": record.order_count,
            "expiresAt": _as_aware(record.expires_at).isoformat(),
            "runtime": "cloud",
            "reused": bool(reused),
            "preview": list(safe_summary.get("preview") or [])[:5],
        }

    @staticmethod
    def _preview_summary(accounts: list[object]) -> dict[str, Any]:
        return {
            "preview": [
                {
                    "emailMasked": account.safe_email,
                    "orderMasked": _order_mask(account.order_no),
                    "cookieBytes": len(account.cookie_text.encode("utf-8")),
                }
                for account in accounts[:5]
            ]
        }

    @staticmethod
    def _active_source_record(
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        source_hash: str,
    ) -> EnvironmentAccountPlan | None:
        return session.scalar(
            select(EnvironmentAccountPlan).where(
                EnvironmentAccountPlan.tenant_id == tenant_id,
                EnvironmentAccountPlan.created_by_user_id == actor_user_id,
                EnvironmentAccountPlan.source_hash == source_hash,
                EnvironmentAccountPlan.status == "parsed",
                EnvironmentAccountPlan.expires_at > utcnow(),
            )
        )

    def _idempotency_replay(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
        source_hash: str,
    ) -> dict[str, Any] | None:
        request = session.scalar(
            select(EnvironmentAccountPlanRequest).where(
                EnvironmentAccountPlanRequest.tenant_id == tenant_id,
                EnvironmentAccountPlanRequest.created_by_user_id == actor_user_id,
                EnvironmentAccountPlanRequest.idempotency_key == idempotency_key,
            )
        )
        if request is None:
            return None
        if request.source_hash != source_hash:
            raise CloudEnvironmentPlanError(
                "environment_plan_idempotency_conflict",
                "同一请求编号已用于不同文件，请重新选择 xlsx",
                status=409,
            )
        record = session.scalar(
            select(EnvironmentAccountPlan).where(
                EnvironmentAccountPlan.id == request.cloud_plan_id,
                EnvironmentAccountPlan.tenant_id == tenant_id,
                EnvironmentAccountPlan.created_by_user_id == actor_user_id,
            )
        )
        if record is None:
            raise CloudEnvironmentPlanError(
                "environment_plan_idempotency_invalid",
                "原解析请求记录已失效，请使用新的请求编号重试",
                status=409,
            )
        return self._public_result(record, reused=request.reused)

    @staticmethod
    def _add_request_record(
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        record: EnvironmentAccountPlan,
        idempotency_key: str,
        source_hash: str,
        reused: bool,
    ) -> None:
        session.add(EnvironmentAccountPlanRequest(
            tenant_id=tenant_id,
            created_by_user_id=actor_user_id,
            cloud_plan_id=record.id,
            idempotency_key=idempotency_key,
            source_hash=source_hash,
            reused=reused,
        ))

    def _recover_concurrent_insert(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
        source_hash: str,
        cause: IntegrityError,
    ) -> dict[str, Any]:
        session.rollback()
        replay = self._idempotency_replay(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            source_hash=source_hash,
        )
        if replay is not None:
            return replay
        reusable = self._active_source_record(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_hash=source_hash,
        )
        if reusable is None:
            raise CloudEnvironmentPlanError(
                "environment_plan_concurrent_conflict",
                "并发解析计划状态已变化，请重试上传",
                status=409,
            ) from cause
        self._add_request_record(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            record=reusable,
            idempotency_key=idempotency_key,
            source_hash=source_hash,
            reused=True,
        )
        try:
            session.flush()
        except IntegrityError as retry_exc:
            session.rollback()
            replay = self._idempotency_replay(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                source_hash=source_hash,
            )
            if replay is not None:
                return replay
            raise CloudEnvironmentPlanError(
                "environment_plan_concurrent_conflict",
                "并发解析计划状态已变化，请重试上传",
                status=409,
            ) from retry_exc
        return self._public_result(reusable, reused=True)

    def parse(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        idempotency_key: str,
        filename: str,
        content_base64: str,
        site: str,
        environment_group: str,
    ) -> dict[str, Any]:
        source = _decode_upload(content_base64)
        normalized_site = normalize_env_site(site)
        normalized_group = str(environment_group or "").strip()
        source_hash = hashlib.sha256(
            source
            + b"\0"
            + normalized_site.encode("ascii")
            + b"\0"
            + normalized_group.encode("utf-8")
        ).hexdigest()
        replay = self._idempotency_replay(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            source_hash=source_hash,
        )
        if replay is not None:
            return replay
        self.expire_plans(session)
        reusable = self._active_source_record(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_hash=source_hash,
        )
        if reusable is not None:
            self._add_request_record(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                record=reusable,
                idempotency_key=idempotency_key,
                source_hash=source_hash,
                reused=True,
            )
            try:
                session.flush()
            except IntegrityError as exc:
                return self._recover_concurrent_insert(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    idempotency_key=idempotency_key,
                    source_hash=source_hash,
                    cause=exc,
                )
            return self._public_result(reusable, reused=True)
        active_count = int(
            session.scalar(
                select(func.count(EnvironmentAccountPlan.id)).where(
                    EnvironmentAccountPlan.tenant_id == tenant_id,
                    EnvironmentAccountPlan.status == "parsed",
                    EnvironmentAccountPlan.expires_at > utcnow(),
                )
            )
            or 0
        )
        if active_count >= self.max_active_plans_per_tenant:
            raise CloudEnvironmentPlanError(
                "environment_plan_limit",
                "当前组织的短时解析计划过多，请等待旧计划过期后重试",
                status=429,
            )
        try:
            accounts = parse_vendor_workbook(BytesIO(source))
            validate_accounts_site(accounts, normalized_site, allow_mixed=True)
        except EnvBatchError as exc:
            message = str(exc)
            code = (
                "environment_plan_site_mismatch"
                if message.startswith("Cookie 站点校验失败")
                else "environment_plan_parse_failed"
            )
            raise CloudEnvironmentPlanError(code, message, status=422) from exc
        if len(accounts) > 2000:
            raise CloudEnvironmentPlanError(
                "environment_plan_account_limit",
                "单批买家号最多 2000 行，请拆分文件后重试",
                status=422,
            )
        credential_bytes = sum(
            len(account.email.encode("utf-8"))
            + len(account.password.encode("utf-8"))
            + len(account.key_url.encode("utf-8"))
            + len(account.cookie_text.encode("utf-8"))
            for account in accounts
        )
        if credential_bytes > MAX_PLAN_CREDENTIAL_BYTES:
            raise CloudEnvironmentPlanError(
                "environment_plan_payload_too_large",
                "解析后的账号凭证总量过大，请拆分文件后重试",
                status=413,
            )
        serialized_accounts = serialize_buyer_accounts(accounts)
        serialized_size = len(
            json.dumps(
                {"version": 1, "accounts": serialized_accounts},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if serialized_size > MAX_EXECUTOR_PLAN_JSON_BYTES:
            raise CloudEnvironmentPlanError(
                "environment_plan_payload_too_large",
                "加密执行计划过大，请拆分文件后重试",
                status=413,
            )
        identifier = uuid.uuid4()
        encrypted = self.cipher.encrypt(
            {"version": 1, "accounts": serialized_accounts},
            tenant_id=tenant_id,
            plan_id=identifier,
        )
        now = utcnow()
        record = EnvironmentAccountPlan(
            id=identifier,
            tenant_id=tenant_id,
            created_by_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            filename=filename,
            site=normalized_site,
            environment_group=normalized_group,
            source_hash=source_hash,
            preview_summary=self._preview_summary(accounts),
            encrypted_payload=encrypted,
            status="parsed",
            account_count=len(accounts),
            cookie_count=sum(bool(account.cookie_text) for account in accounts),
            mixed_site_cookie_count=count_mixed_site_accounts(accounts),
            password_kind_count=len({account.password for account in accounts}),
            order_count=len(accounts),
            expires_at=now + timedelta(seconds=self.plan_ttl_seconds),
        )
        session.add(record)
        self._add_request_record(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            record=record,
            idempotency_key=idempotency_key,
            source_hash=source_hash,
            reused=False,
        )
        try:
            session.flush()
        except IntegrityError as exc:
            return self._recover_concurrent_insert(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                source_hash=source_hash,
                cause=exc,
            )
        return self._public_result(record, reused=False)

    def load_for_execution(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        cloud_plan_id: object,
        site: str,
        environment_group: str,
        total_count: int,
    ) -> tuple[EnvironmentAccountPlan, list[dict[str, Any]]]:
        record = self._record(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            cloud_plan_id=cloud_plan_id,
            for_update=True,
        )
        if record.status != "parsed":
            raise CloudEnvironmentPlanError(
                "environment_plan_consumed", "解析计划已提交，请重新上传 xlsx", status=409
            )
        if (
            record.site != normalize_env_site(site)
            or record.environment_group != str(environment_group or "").strip()
            or record.account_count != int(total_count)
        ):
            raise CloudEnvironmentPlanError(
                "environment_plan_context_mismatch",
                "站点、分组或账号数量已变化，请重新上传 xlsx",
                status=409,
            )
        accounts = self._accounts(record)
        return record, serialize_buyer_accounts(accounts)

    def latest(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        site: str,
        environment_group: str,
    ) -> dict[str, Any] | None:
        self.expire_plans(session)
        record = session.scalar(
            select(EnvironmentAccountPlan)
            .where(
                EnvironmentAccountPlan.tenant_id == tenant_id,
                EnvironmentAccountPlan.created_by_user_id == actor_user_id,
                EnvironmentAccountPlan.status == "parsed",
                EnvironmentAccountPlan.site == normalize_env_site(site),
                EnvironmentAccountPlan.environment_group
                == str(environment_group or "").strip(),
                EnvironmentAccountPlan.expires_at > utcnow(),
            )
            .order_by(EnvironmentAccountPlan.created_at.desc())
            .limit(1)
        )
        if record is None:
            return None
        return self._public_result(record, reused=True)

    @staticmethod
    def mark_submitted(record: EnvironmentAccountPlan) -> None:
        record.status = "submitted"
        record.submitted_at = utcnow()
        record.encrypted_payload = None
