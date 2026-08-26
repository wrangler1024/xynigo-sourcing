"""Tenant-scoped buyer-account metadata and checkout allocation rules."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .buyer_credential_crypto import BuyerCredentialCipher, BuyerCredentialError
from .buyer_account_sync import enqueue_buyer_account_mirror
from .models import BuyerAccount, Tenant
from .purchase_service import PurchaseServiceError


BUYER_ACCOUNT_STATUSES = frozenset(
    {
        "available",
        "reserved",
        "in_use",
        "cleanup_pending",
        "post_payment_hold",
        "manual_review",
        "disabled",
    }
)
CREDENTIAL_STATUSES = frozenset({"ready", "unverified", "invalid", "unknown"})
SYNC_PROTECTED_STATUSES = frozenset(
    {"reserved", "in_use", "cleanup_pending", "post_payment_hold"}
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _comparable_value(value: object) -> object:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class BuyerAccountService:
    """Store buyer facts and encrypted credentials; serialize allocation in PostgreSQL."""

    def __init__(
        self,
        session: Session,
        clock=_utcnow,  # type: ignore[no-untyped-def]
        credential_cipher: BuyerCredentialCipher | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        self.credential_cipher = credential_cipher

    def _decrypt_credentials(self, account: BuyerAccount) -> dict[str, Any]:
        if not account.credentials_ciphertext:
            return {}
        if self.credential_cipher is None:
            raise PurchaseServiceError(
                "buyer_credential_key_unavailable",
                "买家号凭证解密服务尚未配置",
                503,
            )
        try:
            return self.credential_cipher.decrypt(account.credentials_ciphertext)
        except BuyerCredentialError as exc:
            raise PurchaseServiceError(
                "buyer_credential_decrypt_failed",
                "买家号凭证暂时无法读取，请联系管理员复核密钥",
                503,
            ) from exc

    def _encrypt_credentials(self, payload: dict[str, Any]) -> str:
        if self.credential_cipher is None:
            raise PurchaseServiceError(
                "buyer_credential_key_unavailable",
                "买家号凭证加密服务尚未配置",
                503,
            )
        try:
            return self.credential_cipher.encrypt(payload)
        except BuyerCredentialError as exc:
            raise PurchaseServiceError(
                "buyer_credential_encrypt_failed",
                "买家号凭证加密失败",
                503,
            ) from exc

    def sync_snapshot(
        self,
        *,
        tenant_id: uuid.UUID,
        source: str,
        snapshot_key: str,
        accounts: list[dict[str, Any]],
    ) -> dict[str, object]:
        # Serialize tenant snapshots so two first-time imports cannot both miss
        # the same account_ref and race on the unique constraint.
        self.session.scalar(
            select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
        )
        refs = [str(item["accountRef"]) for item in accounts]
        order_refs = [
            str(item["sourceOrderRef"])
            for item in accounts
            if item.get("sourceOrderRef")
        ]
        existing = {
            item.account_ref: item
            for item in self.session.scalars(
                select(BuyerAccount)
                .where(
                    BuyerAccount.tenant_id == tenant_id,
                    BuyerAccount.account_ref.in_(refs),
                )
                .with_for_update()
            )
        }
        order_owners = {
            str(item.source_order_ref): item.account_ref
            for item in self.session.scalars(
                select(BuyerAccount).where(
                    BuyerAccount.tenant_id == tenant_id,
                    BuyerAccount.source_order_ref.in_(order_refs),
                )
            )
            if item.source_order_ref
        }
        for payload in accounts:
            source_order_ref = str(payload.get("sourceOrderRef") or "")
            owner = order_owners.get(source_order_ref)
            if source_order_ref and owner and owner != str(payload["accountRef"]):
                raise PurchaseServiceError(
                    "buyer_account_duplicate",
                    "买家号或号商单号已经入库",
                    409,
                )
        now = self.clock()
        created = 0
        updated = 0
        unchanged = 0
        protected = 0
        for payload in accounts:
            account_ref = str(payload["accountRef"])
            desired_status = str(payload["availabilityStatus"])
            if payload["credentialStatus"] == "invalid" and desired_status == "available":
                desired_status = "manual_review"
            account = existing.get(account_ref)
            incoming_credentials = payload.get("credentials")
            incoming_credentials = (
                dict(incoming_credentials)
                if isinstance(incoming_credentials, dict)
                else None
            )
            incoming_profile = payload.get("businessProfile")
            incoming_profile = (
                dict(incoming_profile)
                if isinstance(incoming_profile, dict)
                else None
            )
            if account is None:
                account = BuyerAccount(
                    tenant_id=tenant_id,
                    account_ref=account_ref,
                    display_label=str(payload["displayLabel"]),
                    site=str(payload["site"]),
                    status=desired_status,
                    source_availability_status=str(payload["availabilityStatus"]),
                    credential_status=str(payload["credentialStatus"]),
                    source=source,
                    source_status=str(payload.get("sourceStatus") or "") or None,
                    source_vendor_label=(
                        str(payload.get("sourceVendorLabel") or "") or None
                    ),
                    source_batch_ref=(
                        str(payload.get("sourceBatchRef") or "") or None
                    ),
                    source_purchase_date=payload.get("sourcePurchaseDate") or None,
                    source_order_ref=payload.get("sourceOrderRef") or None,
                    credentials_ciphertext=(
                        self._encrypt_credentials(incoming_credentials)
                        if incoming_credentials is not None
                        else None
                    ),
                    source_business_profile=incoming_profile or {},
                    hub_environment_ref=payload.get("hubEnvironmentRef") or None,
                    hub_environment_name=(
                        str(payload.get("hubEnvironmentName") or "") or None
                    ),
                    operator_label=str(payload.get("operatorLabel") or "") or None,
                    last_snapshot_key=snapshot_key,
                    source_updated_at=_as_utc(payload.get("sourceUpdatedAt")),
                    last_synced_at=now,
                    feishu_sync_status="pending",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                self.session.add(account)
                existing[account_ref] = account
                created += 1
                continue

            sync_protected = (
                account.status in SYNC_PROTECTED_STATUSES
                or account.current_checkout_attempt_id is not None
            )
            next_status = (
                account.status
                if account.current_checkout_attempt_id is not None
                else self._synced_status(account.status, desired_status)
            )
            if next_status == account.status and sync_protected:
                protected += 1
            incoming_hub_ref = payload.get("hubEnvironmentRef") or None
            incoming_hub_name = str(payload.get("hubEnvironmentName") or "") or None
            if sync_protected:
                incoming_hub_ref = account.hub_environment_ref
                incoming_hub_name = account.hub_environment_name
            changes = {
                "display_label": str(payload["displayLabel"]),
                "site": account.site if sync_protected else str(payload["site"]),
                "status": next_status,
                "source_availability_status": str(payload["availabilityStatus"]),
                "credential_status": str(payload["credentialStatus"]),
                "source": source,
                "source_status": str(payload.get("sourceStatus") or "") or None,
                "source_vendor_label": (
                    str(payload.get("sourceVendorLabel") or "") or None
                ),
                "source_batch_ref": (
                    str(payload.get("sourceBatchRef") or "") or None
                ),
                "source_purchase_date": payload.get("sourcePurchaseDate") or None,
                "source_order_ref": payload.get("sourceOrderRef") or None,
                "hub_environment_ref": incoming_hub_ref,
                "hub_environment_name": incoming_hub_name,
                "operator_label": str(payload.get("operatorLabel") or "") or None,
                "source_updated_at": _as_utc(payload.get("sourceUpdatedAt")),
                "source_business_profile": (
                    incoming_profile
                    if incoming_profile is not None
                    else dict(account.source_business_profile or {})
                ),
            }
            credentials_changed = False
            encrypted_credentials = account.credentials_ciphertext
            if incoming_credentials is not None:
                credentials_changed = (
                    self._decrypt_credentials(account) != incoming_credentials
                )
                if credentials_changed:
                    encrypted_credentials = self._encrypt_credentials(
                        incoming_credentials
                    )
            changed = any(
                _comparable_value(getattr(account, field)) != _comparable_value(value)
                for field, value in changes.items()
            ) or credentials_changed
            for field, value in changes.items():
                setattr(account, field, value)
            account.credentials_ciphertext = encrypted_credentials
            account.last_snapshot_key = snapshot_key
            account.last_synced_at = now
            if changed:
                account.version += 1
                account.updated_at = now
                updated += 1
            else:
                unchanged += 1
        self.session.flush()
        for account_ref in refs:
            enqueue_buyer_account_mirror(self.session, existing[account_ref], available_at=now)
        self.session.flush()
        return {
            "snapshotKey": snapshot_key,
            "receivedCount": len(accounts),
            "createdCount": created,
            "updatedCount": updated,
            "unchangedCount": unchanged,
            "protectedCount": protected,
            "syncedAt": now.isoformat(),
        }

    def preflight_import(
        self,
        *,
        tenant_id: uuid.UUID,
        items: list[dict[str, Any]],
    ) -> dict[str, object]:
        account_refs = [str(item["accountRef"]) for item in items]
        source_order_refs = [
            str(item["sourceOrderRef"])
            for item in items
            if item.get("sourceOrderRef")
        ]
        existing_accounts = set(
            self.session.scalars(
                select(BuyerAccount.account_ref).where(
                    BuyerAccount.tenant_id == tenant_id,
                    BuyerAccount.account_ref.in_(account_refs),
                )
            )
        )
        existing_orders = set(
            self.session.scalars(
                select(BuyerAccount.source_order_ref).where(
                    BuyerAccount.tenant_id == tenant_id,
                    BuyerAccount.source_order_ref.in_(source_order_refs),
                )
            )
        )
        conflicts = [
            {
                "accountRef": str(item["accountRef"]),
                "accountExists": str(item["accountRef"]) in existing_accounts,
                "sourceOrderExists": bool(
                    item.get("sourceOrderRef") in existing_orders
                ),
            }
            for item in items
            if (
                str(item["accountRef"]) in existing_accounts
                or item.get("sourceOrderRef") in existing_orders
            )
        ]
        return {
            "receivedCount": len(items),
            "conflictCount": len(conflicts),
            "ready": not conflicts,
            "conflicts": conflicts,
        }

    def list_accounts(
        self,
        *,
        tenant_id: uuid.UUID,
        site: str = "",
        status: str = "",
        credential_status: str = "",
        keyword: str = "",
        selectable_only: bool = False,
        page: int = 1,
        page_size: int = 100,
        include_credentials: bool = False,
    ) -> dict[str, object]:
        site = site.strip().upper()
        status = status.strip()
        credential_status = credential_status.strip()
        keyword = " ".join(keyword.split())[:100]
        if site and site not in {"US", "MX"}:
            raise PurchaseServiceError(
                "buyer_account_filter_invalid", "买家号站点筛选无效", 422
            )
        if status and status not in BUYER_ACCOUNT_STATUSES:
            raise PurchaseServiceError(
                "buyer_account_filter_invalid", "买家号状态筛选无效", 422
            )
        if credential_status and credential_status not in CREDENTIAL_STATUSES:
            raise PurchaseServiceError(
                "buyer_account_filter_invalid", "买家号凭证状态筛选无效", 422
            )
        page = max(1, min(int(page), 100_000))
        page_size = max(1, min(int(page_size), 200))
        base_filters = [BuyerAccount.tenant_id == tenant_id]
        if site:
            base_filters.append(BuyerAccount.site == site)
        count_rows = self.session.execute(
            select(BuyerAccount.status, func.count(BuyerAccount.id))
            .where(*base_filters)
            .group_by(BuyerAccount.status)
        ).all()
        counts = {name: 0 for name in BUYER_ACCOUNT_STATUSES}
        for name, count in count_rows:
            counts[str(name)] = int(count or 0)

        filters = list(base_filters)
        if status:
            filters.append(BuyerAccount.status == status)
        if credential_status:
            filters.append(BuyerAccount.credential_status == credential_status)
        if selectable_only:
            filters.extend(
                (
                    BuyerAccount.status == "available",
                    BuyerAccount.source_availability_status == "available",
                    BuyerAccount.credential_status == "ready",
                    BuyerAccount.current_checkout_attempt_id.is_(None),
                )
            )
        if keyword:
            escaped_keyword = keyword.replace("%", r"\%").replace("_", r"\_")
            pattern = f"%{escaped_keyword}%"
            filters.append(
                or_(
                    BuyerAccount.display_label.ilike(pattern, escape="\\"),
                    BuyerAccount.account_ref.ilike(pattern, escape="\\"),
                    BuyerAccount.hub_environment_name.ilike(pattern, escape="\\"),
                    BuyerAccount.operator_label.ilike(pattern, escape="\\"),
                )
            )
        total = int(
            self.session.scalar(select(func.count(BuyerAccount.id)).where(*filters)) or 0
        )
        rows = list(
            self.session.scalars(
                select(BuyerAccount)
                .where(*filters)
                .order_by(BuyerAccount.updated_at.desc(), BuyerAccount.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return {
            "counts": {"total": sum(counts.values()), **counts},
            "rows": [
                self.public_payload(item, include_credentials=include_credentials)
                for item in rows
            ],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "hasMore": page * page_size < total,
        }

    def checkout_candidate(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        site: str,
        current_attempt_id: uuid.UUID | None = None,
    ) -> BuyerAccount:
        account = self.session.scalar(
            select(BuyerAccount)
            .where(BuyerAccount.id == account_id, BuyerAccount.tenant_id == tenant_id)
            .with_for_update()
        )
        if account is None:
            raise PurchaseServiceError(
                "buyer_account_not_found", "买家号不存在或不属于当前组织", 404
            )
        if account.site != site:
            raise PurchaseServiceError(
                "checkout_resource_site_mismatch",
                "Hub 环境、买家号与采购单站点不一致",
                422,
            )
        owned_by_current = (
            current_attempt_id is not None
            and account.current_checkout_attempt_id == current_attempt_id
        )
        if not owned_by_current and account.status == "post_payment_hold":
            raise PurchaseServiceError(
                "checkout_resource_retained",
                "买家号已绑定成功采购批次，当前不可复用",
                409,
            )
        if not owned_by_current and (
            account.status != "available"
            or account.current_checkout_attempt_id is not None
        ):
            raise PurchaseServiceError(
                "buyer_account_unavailable", "买家号当前不可用于本次下单", 409
            )
        if account.source_availability_status != "available":
            raise PurchaseServiceError(
                "buyer_account_unavailable", "买家号源台账当前不可用于下单", 409
            )
        if account.credential_status != "ready":
            raise PurchaseServiceError(
                "buyer_account_credential_unavailable",
                "买家号凭证尚未验证或已经失效",
                409,
            )
        return account

    def public_payload(
        self, account: BuyerAccount, *, include_credentials: bool = False
    ) -> dict[str, object]:
        selectable = (
            account.status == "available"
            and account.source_availability_status == "available"
            and account.credential_status == "ready"
            and account.current_checkout_attempt_id is None
        )
        if account.status != "available":
            unavailable_reason = f"status:{account.status}"
        elif account.source_availability_status != "available":
            unavailable_reason = f"source:{account.source_availability_status}"
        elif account.credential_status != "ready":
            unavailable_reason = f"credential:{account.credential_status}"
        elif account.current_checkout_attempt_id is not None:
            unavailable_reason = "checkout_reserved"
        else:
            unavailable_reason = ""
        payload: dict[str, object] = {
            "accountId": str(account.id),
            "accountRef": account.account_ref,
            "displayLabel": account.display_label,
            "site": account.site,
            "status": account.status,
            "sourceAvailabilityStatus": account.source_availability_status,
            "credentialStatus": account.credential_status,
            "source": account.source,
            "sourceStatus": account.source_status or "",
            "sourceVendorLabel": account.source_vendor_label or "",
            "sourceBatchRef": account.source_batch_ref or "",
            "sourcePurchaseDate": (
                account.source_purchase_date.isoformat()
                if account.source_purchase_date
                else None
            ),
            "hubEnvironment": (
                {
                    "ref": account.hub_environment_ref,
                    "name": account.hub_environment_name,
                }
                if account.hub_environment_ref and account.hub_environment_name
                else None
            ),
            "operatorLabel": account.operator_label or "",
            "selectable": selectable,
            "unavailableReason": unavailable_reason,
            "version": account.version,
            "sourceUpdatedAt": (
                account.source_updated_at.isoformat()
                if account.source_updated_at
                else None
            ),
            "lastSyncedAt": account.last_synced_at.isoformat(),
            "baseSyncStatus": account.feishu_sync_status,
            "baseSyncedAt": (
                account.feishu_synced_at.isoformat()
                if account.feishu_synced_at
                else None
            ),
            "updatedAt": account.updated_at.isoformat(),
            "businessProfile": dict(account.source_business_profile or {}),
        }
        if include_credentials:
            payload["credentials"] = self._decrypt_credentials(account)
        return payload

    @staticmethod
    def _synced_status(current: str, incoming: str) -> str:
        # A ledger refresh must never release an active checkout or a
        # post-payment hold. Manual review/disabled records also require an
        # explicit future recovery action instead of being revived by sync.
        if current in SYNC_PROTECTED_STATUSES:
            return current
        if current == "disabled":
            return "disabled"
        if current == "manual_review":
            return "disabled" if incoming == "disabled" else "manual_review"
        return incoming
