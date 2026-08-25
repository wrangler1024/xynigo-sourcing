from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

import httpx


AUTHORIZE_ENDPOINT = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_ENDPOINT = "https://accounts.feishu.cn/oauth/v3/token"
USER_INFO_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/user_info"
TENANT_TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
CONTACT_BATCH_ID_ENDPOINT = "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id"
CONTACT_USER_ENDPOINT = "https://open.feishu.cn/open-apis/contact/v3/users"


class OAuthProviderError(RuntimeError):
    """A deliberately redacted OAuth provider failure."""

    def __init__(self, stage: str, provider_code: int | str | None = None) -> None:
        self.stage = stage
        self.provider_code = provider_code
        suffix = f" ({provider_code})" if provider_code is not None else ""
        super().__init__(f"Feishu OAuth {stage} failed{suffix}")


@dataclass(frozen=True)
class FeishuIdentity:
    tenant_key: str
    open_id: str
    union_id: str | None
    name: str
    avatar_url: str | None


@dataclass(frozen=True)
class FeishuDirectoryUser:
    open_id: str
    union_id: str | None
    name: str
    avatar_url: str | None
    department_ids: tuple[str, ...]
    is_activated: bool
    is_frozen: bool
    is_resigned: bool
    is_exited: bool
    is_unjoin: bool


class OAuthClient(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str | None) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str | None) -> str: ...

    def get_identity(self, user_access_token: str) -> FeishuIdentity: ...


class DirectoryClient(Protocol):
    def find_user_by_mobile(self, mobile: str) -> FeishuDirectoryUser | None: ...


class DirectoryProviderError(RuntimeError):
    """A redacted failure from Feishu app-identity directory APIs."""

    def __init__(self, stage: str, provider_code: int | str | None = None) -> None:
        self.stage = stage
        self.provider_code = provider_code
        suffix = f" ({provider_code})" if provider_code is not None else ""
        super().__init__(f"Feishu directory {stage} failed{suffix}")


class FeishuDirectoryClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def find_user_by_mobile(self, mobile: str) -> FeishuDirectoryUser | None:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            token = self._tenant_access_token(client)
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            id_response = client.post(
                CONTACT_BATCH_ID_ENDPOINT,
                params={"user_id_type": "open_id"},
                json={"mobiles": [mobile], "include_resigned": False},
                headers=headers,
            )
            id_payload = self._json(id_response, "batch_get_id")
            if id_response.status_code >= 400 or id_payload.get("code", 0) != 0:
                raise DirectoryProviderError(
                    "batch_get_id", id_payload.get("code", id_response.status_code)
                )
            data = id_payload.get("data")
            user_list = data.get("user_list") if isinstance(data, dict) else None
            if not isinstance(user_list, list) or not user_list:
                return None
            if len(user_list) != 1 or not isinstance(user_list[0], dict):
                raise DirectoryProviderError("batch_get_id_response")
            open_id = user_list[0].get("user_id")
            if not isinstance(open_id, str) or not open_id:
                raise DirectoryProviderError("batch_get_id_response")

            user_response = client.get(
                f"{CONTACT_USER_ENDPOINT}/{quote(open_id, safe='')}",
                params={
                    "user_id_type": "open_id",
                    "department_id_type": "open_department_id",
                },
                headers=headers,
            )
            user_payload = self._json(user_response, "user_info")
            if user_response.status_code >= 400 or user_payload.get("code", 0) != 0:
                raise DirectoryProviderError(
                    "user_info", user_payload.get("code", user_response.status_code)
                )
            user_data = user_payload.get("data")
            user = user_data.get("user") if isinstance(user_data, dict) else None
            if not isinstance(user, dict) or user.get("open_id") != open_id:
                raise DirectoryProviderError("user_info_response")
            status = user.get("status") if isinstance(user.get("status"), dict) else {}
            avatar = user.get("avatar") if isinstance(user.get("avatar"), dict) else {}
            department_ids = user.get("department_ids")
            return FeishuDirectoryUser(
                open_id=open_id,
                union_id=self._optional_text(user, "union_id"),
                name=self._optional_text(user, "name") or "飞书成员",
                avatar_url=self._optional_text(avatar, "avatar_240")
                or self._optional_text(avatar, "avatar_72"),
                department_ids=tuple(
                    item for item in department_ids
                    if isinstance(item, str) and item
                ) if isinstance(department_ids, list) else (),
                is_activated=status.get("is_activated") is True,
                is_frozen=status.get("is_frozen") is True,
                is_resigned=status.get("is_resigned") is True,
                is_exited=status.get("is_exited") is True,
                is_unjoin=status.get("is_unjoin") is True,
            )

    def _tenant_access_token(self, client: httpx.Client) -> str:
        response = client.post(
            TENANT_TOKEN_ENDPOINT,
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            headers={"Accept": "application/json"},
        )
        payload = self._json(response, "tenant_token")
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            raise DirectoryProviderError(
                "tenant_token", payload.get("code", response.status_code)
            )
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise DirectoryProviderError("tenant_token_response")
        return token

    @staticmethod
    def _json(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise DirectoryProviderError(f"{stage}_response", response.status_code) from exc
        if not isinstance(payload, dict):
            raise DirectoryProviderError(f"{stage}_response", response.status_code)
        return payload

    @staticmethod
    def _optional_text(data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None


class FeishuOAuthClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        code_challenge_method: Literal["S256", "plain", "disabled"] = "S256",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        self.code_challenge_method = code_challenge_method
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def authorization_url(self, *, state: str, code_challenge: str | None) -> str:
        parameters = {
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        if self.code_challenge_method != "disabled":
            if not code_challenge:
                raise ValueError("PKCE challenge is required when PKCE is enabled")
            parameters["code_challenge"] = code_challenge
            parameters["code_challenge_method"] = self.code_challenge_method
        query = urlencode(parameters)
        return f"{AUTHORIZE_ENDPOINT}?{query}"

    def exchange_code(self, *, code: str, code_verifier: str | None) -> str:
        token_request = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        if self.code_challenge_method != "disabled":
            if not code_verifier:
                raise ValueError("PKCE verifier is required when PKCE is enabled")
            token_request["code_verifier"] = code_verifier
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(
                TOKEN_ENDPOINT,
                json=token_request,
                headers={"Accept": "application/json"},
            )
        payload = self._json(response, "token")
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            raise OAuthProviderError("token", payload.get("code", response.status_code))
        token_payload = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthProviderError("token_response")
        return access_token

    def get_identity(self, user_access_token: str) -> FeishuIdentity:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.get(
                USER_INFO_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {user_access_token}",
                    "Accept": "application/json",
                },
            )
        payload = self._json(response, "user_info")
        if response.status_code >= 400 or payload.get("code", 0) != 0:
            raise OAuthProviderError("user_info", payload.get("code", response.status_code))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OAuthProviderError("user_info_response")
        tenant_key = data.get("tenant_key")
        open_id = data.get("open_id")
        if not isinstance(tenant_key, str) or not tenant_key:
            raise OAuthProviderError("user_info_identity")
        if not isinstance(open_id, str) or not open_id:
            raise OAuthProviderError("user_info_identity")
        return FeishuIdentity(
            tenant_key=tenant_key,
            open_id=open_id,
            union_id=self._optional_text(data, "union_id"),
            name=self._optional_text(data, "name") or "飞书用户",
            avatar_url=self._optional_text(data, "avatar_url"),
        )

    @staticmethod
    def _json(response: httpx.Response, stage: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthProviderError(f"{stage}_response", response.status_code) from exc
        if not isinstance(payload, dict):
            raise OAuthProviderError(f"{stage}_response", response.status_code)
        return payload

    @staticmethod
    def _optional_text(data: dict[str, Any], key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None
