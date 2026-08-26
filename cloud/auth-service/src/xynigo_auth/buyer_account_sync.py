"""Transactional outbox pointers and projections for the buyer-account mirror."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .buyer_credential_crypto import BuyerCredentialCipher
from .models import BuyerAccount, OperationalSyncOutbox


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _milliseconds(value: datetime | date | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _profile_milliseconds(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (datetime, date)):
        return _milliseconds(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _milliseconds(parsed)


def buyer_account_fields(
    account: BuyerAccount,
    credential_cipher: BuyerCredentialCipher | None = None,
) -> dict[str, Any]:
    """Build the approved plaintext Base projection at worker execution time."""

    credentials = (
        credential_cipher.decrypt(account.credentials_ciphertext)
        if account.credentials_ciphertext and credential_cipher is not None
        else {}
    )
    profile = dict(account.source_business_profile or {})
    account_identifier = str(credentials.get("accountIdentifier") or "")

    fields: dict[str, Any] = {
        "账号引用": account.account_ref,
        "账号标签": account_identifier or account.display_label,
        "邮箱账号": account_identifier,
        "手机号": str(credentials.get("phoneNumber") or ""),
        "密码": str(credentials.get("password") or ""),
        "Cookie": str(credentials.get("cookie") or ""),
        "接码Key": str(credentials.get("verificationKey") or ""),
        "接码Key链接": str(credentials.get("verificationKeyLink") or ""),
        "登录链接": str(credentials.get("loginLink") or ""),
        "站点": account.site,
        "源可用性": account.source_availability_status,
        "凭证状态": account.credential_status,
        "源业务状态": account.source_status or account.source,
        "账号状态": account.source_status or account.status,
        "绑定时间": _profile_milliseconds(profile.get("bindingTime")),
        "首次登录日期": _profile_milliseconds(profile.get("firstLoginAt")),
        "绑定环境": str(
            profile.get("bindingEnvironment") or account.hub_environment_name or ""
        ),
        "采购员": account.operator_label or "",
        "异常记录": str(profile.get("abnormalRecord") or ""),
        "备注": str(profile.get("note") or ""),
        "号商购买单号": str(profile.get("sourcePurchaseOrderNo") or ""),
        "操作人": "、".join(
            str(item) for item in profile.get("sourceOperators") or [] if str(item)
        ),
        "最后使用日期": _profile_milliseconds(profile.get("lastUsedAt")),
        "创建时间": _profile_milliseconds(profile.get("sourceCreatedAt")),
        "环境序号": profile.get("environmentSequence"),
        "环境分组名": str(profile.get("environmentGroupName") or ""),
        "账号ID": str(profile.get("sourceAccountId") or ""),
        "创建人": str(profile.get("sourceCreatedBy") or ""),
        "累计下单数": profile.get("cumulativeOrderCount"),
        "购买日期": _milliseconds(account.source_purchase_date),
        "迁移状态": str(profile.get("migrationStatus") or ""),
        "Hub环境引用": account.hub_environment_ref or "",
        "Hub环境名称": account.hub_environment_name or "",
        "采购员标签": account.operator_label or "",
        "源更新时间": _milliseconds(account.source_updated_at),
        "云端买家号ID": str(account.id),
        "流程状态": account.status,
        "当前下单尝试ID": (
            str(account.current_checkout_attempt_id)
            if account.current_checkout_attempt_id is not None
            else ""
        ),
        "同步错误": "",
    }
    return {key: value for key, value in fields.items() if value is not None}


def enqueue_buyer_account_mirror(
    session: Session,
    account: BuyerAccount,
    *,
    available_at: datetime | None = None,
) -> bool:
    """Queue one idempotent mirror event for the current database version."""

    if account.id is None:
        account.id = uuid.uuid4()
    now = available_at or utcnow()
    dedupe_key = f"buyer_account:{account.id}:v{account.version}"
    existing = session.scalar(
        select(OperationalSyncOutbox).where(
            OperationalSyncOutbox.dedupe_key == dedupe_key
        )
    )
    if existing is not None:
        if existing.status == "failed":
            existing.status = "pending"
            existing.attempt_count = 0
            existing.available_at = now
            existing.last_error_code = None
            existing.processed_at = None
            existing.updated_at = now
            account.feishu_sync_status = "pending"
        else:
            account.feishu_sync_status = existing.status
        return False
    account.feishu_sync_status = "pending"
    session.add(
        OperationalSyncOutbox(
            id=uuid.uuid4(),
            tenant_id=account.tenant_id,
            aggregate_type="buyer_account",
            aggregate_id=account.id,
            dedupe_key=dedupe_key,
            payload={
                "syncKey": account.account_ref,
                "keyField": "账号引用",
                "syncTimestampField": "最近同步时间",
                "version": account.version,
            },
            status="pending",
            attempt_count=0,
            available_at=now,
        )
    )
    return True
