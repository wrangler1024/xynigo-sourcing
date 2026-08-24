from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

import httpx


AUTHORIZE_ENDPOINT = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_ENDPOINT = "https://accounts.feishu.cn/oauth/v3/token"
USER_INFO_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/user_info"


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


class OAuthClient(Protocol):
    def authorization_url(self, *, state: str, code_challenge: str | None) -> str: ...

    def exchange_code(self, *, code: str, code_verifier: str | None) -> str: ...

    def get_identity(self, user_access_token: str) -> FeishuIdentity: ...


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
