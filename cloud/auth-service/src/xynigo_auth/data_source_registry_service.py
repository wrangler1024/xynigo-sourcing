"""Encrypted tenant-wide data-source definitions with device-local bindings."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .data_source_registry_crypto import (
    DataSourceRegistryCipher,
    DataSourceRegistryCipherError,
)
from .models import LocalExecutor, TenantDataSourceRegistry, User


SOURCE_ID_PATTERN = re.compile(r"^ds_[0-9a-f]{24}$")
PRIVATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,200}$")
CELL_RANGE_PATTERN = re.compile(
    r"^[A-Z]{1,3}[1-9][0-9]*:[A-Z]{1,3}(?:[1-9][0-9]*)?$"
)


class TenantDataSourceRegistryError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _plain(value: object, label: str, maximum: int, *, blank: bool = False) -> str:
    text = str(value or "").strip()
    if not text and blank:
        return ""
    if (
        not text
        or len(text) > maximum
        or any(character in text for character in "\r\n\t\x00")
    ):
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", f"{label}格式无效", 422
        )
    return text


def _uuid(value: object, label: str, *, blank: bool = False) -> str:
    text = str(value or "").strip()
    if not text and blank:
        return ""
    try:
        return str(uuid.UUID(text))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", f"{label}格式无效", 422
        ) from exc


def normalize_organization_registry(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "组织数据源注册表版本无效", 422
        )
    try:
        schema_version = int(value.get("schemaVersion") or 1)
    except (TypeError, ValueError) as exc:
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "组织数据源注册表版本无效", 422
        ) from exc
    if schema_version != 1:
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "组织数据源注册表版本无效", 422
        )
    raw_sources = value.get("dataSources") or []
    raw_profiles = value.get("buyerProfiles") or []
    if not isinstance(raw_sources, list) or not isinstance(raw_profiles, list):
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "组织数据源注册表列表无效", 422
        )
    if len(raw_sources) > 1000 or len(raw_profiles) > 1000:
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "组织数据源注册表记录过多", 422
        )
    sources: list[dict[str, Any]] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "数据源记录格式无效", 422
            )
        source_id = str(item.get("id") or "").strip().lower()
        scope = str(item.get("scope") or "").strip().lower()
        owner = _uuid(item.get("ownerMemberId"), "数据源所有者", blank=True)
        migration_state = str(item.get("migrationState") or "ready").strip()
        if not SOURCE_ID_PATTERN.fullmatch(source_id) or scope not in {"personal", "team"}:
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "数据源编号或范围无效", 422
            )
        if scope == "team" and owner:
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "团队数据源不能绑定个人", 422
            )
        if scope == "personal" and not owner:
            migration_state = "needs_owner_confirmation"
        elif migration_state == "needs_owner_confirmation":
            migration_state = "ready"
        if migration_state not in {"ready", "needs_owner_confirmation"}:
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "数据源迁移状态无效", 422
            )
        token = str(item.get("spreadsheetToken") or "").strip()
        sheet_id = str(item.get("sheetId") or "").strip()
        cell_range = str(item.get("cellRange") or "").strip().upper()
        enabled = item.get("enabled", True)
        if (
            not PRIVATE_ID_PATTERN.fullmatch(token)
            or not PRIVATE_ID_PATTERN.fullmatch(sheet_id)
            or not CELL_RANGE_PATTERN.fullmatch(cell_range)
            or not isinstance(enabled, bool)
        ):
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "数据源目标或状态无效", 422
            )
        sources.append(
            {
                "id": source_id,
                "scope": scope,
                "ownerMemberId": owner,
                "label": _plain(item.get("label"), "数据源名称", 120),
                "spreadsheetToken": token,
                "sheetId": sheet_id,
                "cellRange": cell_range,
                "sheetName": _plain(
                    item.get("sheetName"), "工作表名称", 255, blank=True
                ),
                "enabled": enabled,
                "migrationState": migration_state,
            }
        )
    source_index = {item["id"]: item for item in sources}
    if len(source_index) != len(sources):
        raise TenantDataSourceRegistryError(
            "data_source_registry_invalid", "数据源编号重复", 422
        )
    profiles: list[dict[str, str]] = []
    seen_members: set[str] = set()
    for item in raw_profiles:
        if not isinstance(item, dict):
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "采购员默认映射无效", 422
            )
        member = _uuid(item.get("memberId"), "采购员")
        source_id = str(item.get("defaultDataSourceId") or "").strip().lower()
        source = source_index.get(source_id)
        if (
            member in seen_members
            or source is None
            or source["scope"] != "personal"
            or source["ownerMemberId"] != member
            or source["migrationState"] != "ready"
        ):
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "采购员默认数据源归属无效", 422
            )
        seen_members.add(member)
        profiles.append({"memberId": member, "defaultDataSourceId": source_id})
    team_default = str(value.get("teamDefaultDataSourceId") or "").strip().lower()
    if team_default:
        source = source_index.get(team_default)
        if source is None or source["scope"] != "team":
            raise TenantDataSourceRegistryError(
                "data_source_registry_invalid", "团队默认数据源无效", 422
            )
    return {
        "schemaVersion": 1,
        "dataSources": sorted(sources, key=lambda item: item["id"]),
        "buyerProfiles": sorted(profiles, key=lambda item: item["memberId"]),
        "teamDefaultDataSourceId": team_default,
    }


def registry_content_hash(registry: dict[str, Any]) -> str:
    raw = json.dumps(
        registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TenantDataSourceRegistryService:
    def __init__(self, cipher: DataSourceRegistryCipher) -> None:
        self.cipher = cipher

    def _registry(
        self, record: TenantDataSourceRegistry
    ) -> dict[str, Any]:
        try:
            return normalize_organization_registry(
                self.cipher.decrypt(
                    record.payload_ciphertext, tenant_id=record.tenant_id
                )
            )
        except DataSourceRegistryCipherError as exc:
            raise TenantDataSourceRegistryError(
                "data_source_registry_unavailable",
                "组织数据源配置暂时无法解密",
                503,
            ) from exc

    @staticmethod
    def _validate_tenant_members(
        session: Session, tenant_id: uuid.UUID, registry: dict[str, Any]
    ) -> None:
        member_ids = {
            uuid.UUID(item["ownerMemberId"])
            for item in registry["dataSources"]
            if item["ownerMemberId"]
        } | {
            uuid.UUID(item["memberId"])
            for item in registry["buyerProfiles"]
        }
        if not member_ids:
            return
        known = set(
            session.scalars(
                select(User.id).where(
                    User.tenant_id == tenant_id,
                    User.id.in_(member_ids),
                    User.status == "active",
                )
            )
        )
        if known != member_ids:
            raise TenantDataSourceRegistryError(
                "data_source_registry_member_invalid",
                "数据源包含非本组织的采购员",
                422,
            )

    def read(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        include_all: bool,
    ) -> dict[str, Any]:
        record = session.get(TenantDataSourceRegistry, tenant_id)
        if record is None:
            return {
                "configured": False,
                "organizationRevision": 0,
                "contentHash": "",
                "visibility": "all" if include_all else "member",
                "updatedAt": None,
                "sourceExecutorId": None,
                "sourceExecutorName": "",
                "registry": {
                    "schemaVersion": 1,
                    "dataSources": [],
                    "buyerProfiles": [],
                    "teamDefaultDataSourceId": "",
                },
            }
        registry = self._registry(record)
        if not include_all:
            member = str(user_id)
            visible_sources = [
                item
                for item in registry["dataSources"]
                if item["scope"] == "team" or item["ownerMemberId"] == member
            ]
            visible_ids = {item["id"] for item in visible_sources}
            registry = {
                "schemaVersion": 1,
                "dataSources": visible_sources,
                "buyerProfiles": [
                    item
                    for item in registry["buyerProfiles"]
                    if item["memberId"] == member
                    and item["defaultDataSourceId"] in visible_ids
                ],
                "teamDefaultDataSourceId": registry["teamDefaultDataSourceId"],
            }
        source_executor = (
            session.get(LocalExecutor, record.source_executor_id)
            if record.source_executor_id
            else None
        )
        return {
            "configured": True,
            "organizationRevision": record.revision,
            "contentHash": registry_content_hash(registry),
            "visibility": "all" if include_all else "member",
            "updatedAt": record.updated_at.isoformat() if record.updated_at else None,
            "sourceExecutorId": (
                str(record.source_executor_id) if record.source_executor_id else None
            ),
            "sourceExecutorName": source_executor.display_name if source_executor else "",
            "registry": registry,
        }

    def publish(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        executor_id: uuid.UUID,
        expected_revision: int,
        registry: object,
    ) -> dict[str, Any]:
        normalized = normalize_organization_registry(registry)
        if not normalized["dataSources"]:
            raise TenantDataSourceRegistryError(
                "data_source_registry_empty",
                "不能发布空的组织数据源配置",
                422,
            )
        self._validate_tenant_members(session, tenant_id, normalized)
        record = session.get(TenantDataSourceRegistry, tenant_id)
        current_revision = record.revision if record else 0
        if expected_revision != current_revision:
            raise TenantDataSourceRegistryError(
                "data_source_registry_revision_conflict",
                "组织数据源配置已更新，请同步后重试",
                409,
            )
        content_hash = registry_content_hash(normalized)
        if record is not None and record.content_hash == content_hash:
            return self.read(
                session,
                tenant_id=tenant_id,
                user_id=user_id,
                include_all=True,
            )
        try:
            ciphertext = self.cipher.encrypt(normalized, tenant_id=tenant_id)
        except DataSourceRegistryCipherError as exc:
            raise TenantDataSourceRegistryError(
                "data_source_registry_encrypt_failed",
                "组织数据源配置无法加密保存",
                503,
            ) from exc
        now = datetime.now(UTC)
        if record is None:
            record = TenantDataSourceRegistry(
                tenant_id=tenant_id,
                payload_ciphertext=ciphertext,
                content_hash=content_hash,
                revision=1,
                source_executor_id=executor_id,
                updated_by_user_id=user_id,
                updated_at=now,
            )
            session.add(record)
        else:
            record.payload_ciphertext = ciphertext
            record.content_hash = content_hash
            record.revision += 1
            record.source_executor_id = executor_id
            record.updated_by_user_id = user_id
            record.updated_at = now
        session.flush()
        return self.read(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            include_all=True,
        )
