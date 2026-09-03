"""Tenant Feishu credential resolution and read-only OpenAPI proxy."""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from .models import TenantFeishuIntegration
from .tenant_integration_crypto import TenantIntegrationCipher, TenantIntegrationCipherError


TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
ALLOWED_READ_PATHS = (
    re.compile(r"^/open-apis/sheets/v2/spreadsheets/[A-Za-z0-9_-]{10,200}/values/[A-Za-z0-9_!:%.-]{3,240}$"),
    re.compile(r"^/open-apis/sheets/v3/spreadsheets/[A-Za-z0-9_-]{10,200}/sheets/query$"),
    re.compile(r"^/open-apis/bitable/v1/apps/[A-Za-z0-9_-]{8,200}$"),
    re.compile(r"^/open-apis/bitable/v1/apps/[A-Za-z0-9_-]{8,200}/tables$"),
    re.compile(r"^/open-apis/bitable/v1/apps/[A-Za-z0-9_-]{8,200}/tables/[A-Za-z0-9_-]{1,128}/(?:fields|records)$"),
    re.compile(r"^/open-apis/bitable/v1/apps/[A-Za-z0-9_-]{8,200}/tables/[A-Za-z0-9_-]{1,128}/records/[A-Za-z0-9_-]{1,160}$"),
    re.compile(r"^/open-apis/wiki/v2/spaces/get_node$"),
)
ALLOWED_PROXY_PERMISSIONS = frozenset({
    "assistant.access", "resource.store.read", "resource.ip.read",
    "system.lark_connection.manage",
})


class TenantFeishuError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ResolvedFeishuCredential:
    app_id: str
    app_secret: str
    revision: int
    source: str


def mask_app_id(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 10:
        return "已配置" if text else ""
    return f"{text[:7]}…{text[-4:]}"


class TenantFeishuService:
    def __init__(
        self,
        *,
        cipher: TenantIntegrationCipher,
        fallback_app_id: str,
        fallback_app_secret: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.cipher = cipher
        self.fallback_app_id = str(fallback_app_id or "").strip()
        self.fallback_app_secret = str(fallback_app_secret or "").strip()
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self._token_cache: dict[tuple[uuid.UUID, int, str], tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    def resolve(self, session: Session, tenant_id: uuid.UUID) -> ResolvedFeishuCredential:
        record = session.get(TenantFeishuIntegration, tenant_id)
        if record is None:
            if not self.fallback_app_id or not self.fallback_app_secret:
                raise TenantFeishuError(
                    "tenant_feishu_not_configured", "组织尚未配置飞书企业应用", 409
                )
            return ResolvedFeishuCredential(
                self.fallback_app_id, self.fallback_app_secret, 0, "deployment"
            )
        try:
            payload = self.cipher.decrypt(record.credential_ciphertext)
        except TenantIntegrationCipherError as exc:
            raise TenantFeishuError(
                "tenant_feishu_credential_unavailable", "组织飞书凭证无法解密", 503
            ) from exc
        app_id = str(payload.get("appId") or "").strip()
        app_secret = str(payload.get("appSecret") or "").strip()
        if app_id != record.app_id or not app_secret:
            raise TenantFeishuError(
                "tenant_feishu_credential_invalid", "组织飞书凭证记录无效", 503
            )
        return ResolvedFeishuCredential(app_id, app_secret, record.revision, "organization")

    def public_status(self, session: Session, tenant_id: uuid.UUID, *, admin: bool) -> dict[str, Any]:
        record = session.get(TenantFeishuIntegration, tenant_id)
        credential = self.resolve(session, tenant_id)
        payload: dict[str, Any] = {
            "configured": True,
            "source": credential.source,
            "revision": record.revision if record else 0,
            "verifiedAt": record.verified_at.isoformat() if record else None,
            "managedInCloud": True,
        }
        if admin:
            payload["appIdMasked"] = mask_app_id(credential.app_id)
        return payload

    def _fetch_token(self, tenant_id: uuid.UUID, credential: ResolvedFeishuCredential) -> str:
        cache_key = (tenant_id, credential.revision, credential.app_id)
        with self._token_lock:
            cached = self._token_cache.get(cache_key)
            if cached and time.monotonic() < cached[1]:
                return cached[0]
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                TOKEN_ENDPOINT,
                json={"app_id": credential.app_id, "app_secret": credential.app_secret},
                headers={"Accept": "application/json"},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TenantFeishuError("tenant_feishu_response_invalid", "飞书令牌响应无效", 502) from exc
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            raise TenantFeishuError("tenant_feishu_verification_failed", "飞书企业应用验证失败", 422)
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise TenantFeishuError("tenant_feishu_response_invalid", "飞书令牌响应无效", 502)
        try:
            expires = max(60, int(payload.get("expire") or 7200))
        except (TypeError, ValueError):
            expires = 7200
        with self._token_lock:
            self._token_cache[cache_key] = (token, time.monotonic() + max(30, expires - 120))
        return token

    def verify(self, tenant_id: uuid.UUID, app_id: str, app_secret: str) -> None:
        self._fetch_token(
            tenant_id,
            ResolvedFeishuCredential(app_id, app_secret, time.time_ns(), "candidate"),
        )

    def proxy_get(
        self,
        *,
        session: Session,
        tenant_id: uuid.UUID,
        path: str,
        query: dict[str, str],
    ) -> dict[str, Any]:
        normalized_path = str(path or "").strip()
        if not any(pattern.fullmatch(normalized_path) for pattern in ALLOWED_READ_PATHS):
            raise TenantFeishuError("tenant_feishu_proxy_path_denied", "飞书只读代理地址不受支持", 403)
        if len(query) > 12 or any(len(str(key)) > 64 or len(str(value)) > 2000 for key, value in query.items()):
            raise TenantFeishuError("tenant_feishu_proxy_query_invalid", "飞书只读代理查询参数无效", 422)
        credential = self.resolve(session, tenant_id)
        token = self._fetch_token(tenant_id, credential)
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.get(
                "https://open.feishu.cn" + normalized_path,
                params=query,
                headers={"Accept": "application/json", "Authorization": "Bearer " + token},
            )
        if len(response.content) > 4 * 1024 * 1024:
            raise TenantFeishuError("tenant_feishu_response_too_large", "飞书只读响应过大", 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise TenantFeishuError("tenant_feishu_response_invalid", "飞书只读响应无效", 502) from exc
        if not isinstance(payload, dict):
            raise TenantFeishuError("tenant_feishu_response_invalid", "飞书只读响应无效", 502)
        return payload
