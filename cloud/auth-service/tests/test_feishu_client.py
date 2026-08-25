from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx

from xynigo_auth.feishu import FeishuOAuthClient


def test_feishu_client_uses_oauth_v3_json_pkce_and_user_info() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/v3/token":
            body = json.loads(request.content)
            assert request.headers["content-type"].startswith("application/json")
            assert body == {
                "grant_type": "authorization_code",
                "client_id": "cli_test",
                "client_secret": "secret-not-real",
                "code": "auth-code",
                "redirect_uri": "https://xynigo.example.com/v1/auth/feishu/callback",
                "code_verifier": "v" * 64,
            }
            return httpx.Response(200, json={"code": 0, "access_token": "user-token"})
        assert request.url.path == "/open-apis/authen/v1/user_info"
        assert request.headers["authorization"] == "Bearer user-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "tenant_key": "tenant_allowed",
                    "open_id": "ou_user",
                    "union_id": "on_user",
                    "name": "合成用户",
                    "avatar_url": "https://example.test/avatar.png",
                },
            },
        )

    client = FeishuOAuthClient(
        app_id="cli_test",
        app_secret="secret-not-real",
        redirect_uri="https://xynigo.example.com/v1/auth/feishu/callback",
        transport=httpx.MockTransport(handler),
    )
    authorization_url = client.authorization_url(state="state-token", code_challenge="challenge")
    parsed = urlparse(authorization_url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.feishu.cn"
    assert parsed.path == "/open-apis/authen/v1/authorize"
    assert query["state"] == ["state-token"]
    assert query["code_challenge"] == ["challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query

    token = client.exchange_code(code="auth-code", code_verifier="v" * 64)
    identity = client.get_identity(token)

    assert identity.tenant_key == "tenant_allowed"
    assert identity.open_id == "ou_user"
    assert len(requests) == 2


def test_feishu_client_omits_pkce_when_disabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "code_verifier" not in body
        return httpx.Response(200, json={"code": 0, "access_token": "user-token"})

    client = FeishuOAuthClient(
        app_id="cli_test",
        app_secret="secret-not-real",
        redirect_uri="https://xynigo.example.com/v1/auth/feishu/callback",
        code_challenge_method="disabled",
        transport=httpx.MockTransport(handler),
    )
    authorization_url = client.authorization_url(state="state-token", code_challenge=None)
    query = parse_qs(urlparse(authorization_url).query)
    assert "code_challenge" not in query
    assert "code_challenge_method" not in query
    assert client.exchange_code(code="auth-code", code_verifier=None) == "user-token"
