from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from xynigo_auth.config import Settings
from xynigo_auth.database import Database
from xynigo_auth.feishu import FeishuIdentity
from xynigo_auth.main import create_app
from xynigo_auth.models import (
    Base,
    LocalLoginRequest,
    OAuthLoginAttempt,
    Permission,
    Role,
    SessionRecord,
    TenantFeishuIntegration,
    User,
)
from xynigo_auth.security import hash_token, pkce_challenge


@dataclass
class FakeOAuthClient:
    identity: FeishuIdentity
    exchanges: list[tuple[str, str | None]] = field(default_factory=list)

    def authorization_url(self, *, state: str, code_challenge: str | None) -> str:
        suffix = f"&code_challenge={code_challenge}" if code_challenge else ""
        return f"https://accounts.example.test/authorize?state={state}{suffix}"

    def exchange_code(self, *, code: str, code_verifier: str | None) -> str:
        self.exchanges.append((code, code_verifier))
        return "temporary-user-access-token"

    def get_identity(self, user_access_token: str) -> FeishuIdentity:
        assert user_access_token == "temporary-user-access-token"
        return self.identity


def build_test_app(
    tmp_path,
    *,
    open_id: str = "ou_admin",
    bootstrap: str = "ou_admin",
    pkce_method: str = "S256",
    procurement_import_enabled: bool = False,
    procurement_import_gateway=None,
    local_executor_asset_dir: str | None = None,
    session_ttl_seconds: int = 8 * 60 * 60,
    session_absolute_ttl_seconds: int = 7 * 24 * 60 * 60,
    session_refresh_threshold_seconds: int = 4 * 60 * 60,
    feishu_integration_transport: httpx.BaseTransport | None = None,
):
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}")
    Base.metadata.create_all(database.engine)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}",
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret-not-real",
        buyer_credential_encryption_key=(
            "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
        ),
        feishu_redirect_uri="http://testserver/v1/auth/feishu/callback",
        feishu_pkce_method=pkce_method,
        allowed_tenant_keys="tenant_allowed",
        bootstrap_super_admin_open_ids=bootstrap,
        cookie_secure=False,
        allowed_hosts="testserver",
        procurement_import_enabled=procurement_import_enabled,
        procurement_import_worker_interval_seconds=1,
        local_executor_asset_dir=(
            local_executor_asset_dir or str(tmp_path / "release-assets")
        ),
        session_ttl_seconds=session_ttl_seconds,
        session_absolute_ttl_seconds=session_absolute_ttl_seconds,
        session_refresh_threshold_seconds=session_refresh_threshold_seconds,
    )
    oauth = FakeOAuthClient(
        FeishuIdentity(
            tenant_key="tenant_allowed",
            open_id=open_id,
            union_id="on_union",
            name="合成测试用户",
            avatar_url=None,
        )
    )
    app = create_app(
        settings=settings,
        oauth_client=oauth,
        database=database,
        procurement_import_gateway=procurement_import_gateway,
        feishu_integration_transport=feishu_integration_transport,
    )
    return app, database, oauth


def start_login(client: TestClient) -> tuple[str, str | None]:
    response = client.get("/v1/auth/feishu/start", follow_redirects=False)
    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    challenge = query.get("code_challenge", [None])[0]
    return query["state"][0], challenge


def start_local_login(client: TestClient) -> tuple[str, str]:
    response = client.post("/v1/auth/local/start")
    assert response.status_code == 201
    payload = response.json()
    query = parse_qs(urlparse(payload["loginUrl"]).query)
    return query["state"][0], payload["pollToken"]


def test_cloud_workspace_shell_and_assets_are_public_but_api_stays_protected(
    tmp_path,
) -> None:
    app, _database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        workspace = client.get("/")
        assert workspace.status_code == 200
        assert "<title>Xynigo Sourcing v0.17.2</title>" in workspace.text
        assert 'src="xynigo-logo.png?v=6"' in workspace.text
        assert 'href="/favicon.ico?v=6"' in workspace.text
        assert "const CLOUD_WEB_MODE" in workspace.text
        csp = workspace.headers["content-security-policy"]
        assert "script-src 'sha256-" in csp
        assert "script-src 'unsafe-inline'" not in csp
        assert "style-src 'unsafe-inline'" in csp
        assert "img-src 'self' data: blob: https:" in csp
        assert client.get("/xynigo-logo.png").status_code == 200
        assert client.get("/xynigo-x.png").status_code == 200
        x_icon = client.get("/xynigo-x.ico")
        favicon = client.get("/favicon.ico")
        assert x_icon.status_code == 200
        assert favicon.status_code == 200
        assert favicon.content == x_icon.content
        assert client.get("/preview-product-a.svg").status_code == 200
        assert client.get("/assets/not-allowed.js").status_code == 404
        assert client.get("/v1/auth/web/status").json() == {"authenticated": False}
        assert client.get("/v1/auth/me").status_code == 401
        assert client.get("/v1/local-executor/releases/latest").status_code == 401
        assert client.get(
            "/v1/local-executor/releases/macos-arm64/primary/download"
        ).status_code == 401


def test_authenticated_member_can_read_immutable_local_executor_release(
    tmp_path,
) -> None:
    app, _database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        state, _challenge = start_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303

        response = client.get("/v1/local-executor/releases/latest")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        payload = response.json()
        assert payload["schemaVersion"] == 1
        assert payload["version"] == "0.17.2"
        assert payload["channel"] == "test"
        assert payload["releaseUrl"] == ""
        assert payload["manifestUrl"] == ""
        assert set(payload["platforms"]) == {"windows-x86_64", "macos-arm64"}
        for platform, info in payload["platforms"].items():
            assert info["size"] > 1_000_000
            assert len(info["sha256"]) == 64
            assert info["installMode"].startswith("standard")
            assert info["internalUnsignedTest"] is True
            assert info["downloadUrl"] == (
                f"/v1/local-executor/releases/{platform}/primary/download"
            )
            if platform == "windows-x86_64":
                assert info["runtimeId"].startswith("0.17.2-")
            assert "github.com" not in info["downloadUrl"]
            assert "greenFallback" not in info


def test_authenticated_member_downloads_allowlisted_asset_through_system_origin(
    tmp_path,
    monkeypatch,
) -> None:
    from xynigo_auth import local_executor_release as release_catalog

    payload = b"synthetic-installer-payload"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        release_catalog,
        "_PLATFORMS",
        {
            "macos-arm64": {
                "label": "macOS Apple Silicon",
                "installMode": "standard_system_application",
                "assetName": "Xynigo_Test.pkg",
                "sha256": digest,
                "size": len(payload),
                "internalUnsignedTest": True,
            }
        },
    )

    asset_root = tmp_path / "release-assets"
    asset_root.mkdir()
    (asset_root / "Xynigo_Test.pkg").write_bytes(payload)
    app, _database, _oauth = build_test_app(
        tmp_path,
        local_executor_asset_dir=str(asset_root),
    )
    with TestClient(app) as client:
        state, _challenge = start_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303

        response = client.get(
            "/v1/local-executor/releases/macos-arm64/primary/download"
        )
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-length"] == str(len(payload))
        assert "Xynigo_Test.pkg" in response.headers["content-disposition"]
        assert response.headers["x-xynigo-asset-sha256"] == digest


def test_concurrent_installer_downloads_do_not_block_authenticated_requests(
    tmp_path,
    monkeypatch,
) -> None:
    from xynigo_auth import local_executor_release as release_catalog

    payload = b"synthetic-concurrent-installer-payload"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        release_catalog,
        "_PLATFORMS",
        {
            "macos-arm64": {
                "label": "macOS Apple Silicon",
                "installMode": "standard_system_application",
                "assetName": "Xynigo_Concurrent_Test.pkg",
                "sha256": digest,
                "size": len(payload),
                "internalUnsignedTest": True,
            }
        },
    )
    asset_root = tmp_path / "release-assets"
    asset_root.mkdir()
    (asset_root / "Xynigo_Concurrent_Test.pkg").write_bytes(payload)
    app, _database, _oauth = build_test_app(
        tmp_path,
        local_executor_asset_dir=str(asset_root),
    )
    verifier = app.state.local_executor_asset_integrity
    original_sha256 = verifier._sha256
    first_hash_started = threading.Event()
    allow_hash_to_finish = threading.Event()
    hash_calls = []

    def slow_sha256(path: Path) -> str:
        hash_calls.append(path)
        first_hash_started.set()
        assert allow_hash_to_finish.wait(2)
        return original_sha256(path)

    monkeypatch.setattr(verifier, "_sha256", slow_sha256)
    download_path = (
        "/v1/local-executor/releases/macos-arm64/primary/download"
    )
    with TestClient(app) as client:
        state, _challenge = start_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.get, download_path)
            assert first_hash_started.wait(1)
            second = pool.submit(client.get, download_path)
            time.sleep(0.05)
            assert len(hash_calls) == 1

            status_started_at = time.monotonic()
            identity = client.get("/v1/auth/me")
            status_elapsed = time.monotonic() - status_started_at
            assert identity.status_code == 200
            assert status_elapsed < 1

            allow_hash_to_finish.set()
            responses = [first.result(timeout=2), second.result(timeout=2)]

    assert [response.status_code for response in responses] == [200, 200]
    assert [response.content for response in responses] == [payload, payload]
    assert len(hash_calls) == 1


def test_bootstrap_admin_login_creates_hashed_session_and_rbac(tmp_path) -> None:
    app, database, oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        state, challenge = start_login(client)
        assert challenge is not None

        with database.session_factory() as session:
            attempt = session.scalar(select(OAuthLoginAttempt))
            assert attempt is not None
            assert attempt.state_hash == hash_token(state)
            assert state not in attempt.state_hash
            verifier = attempt.code_verifier
            assert verifier is not None
            assert len(verifier) == 43
            assert challenge == pkce_challenge(verifier)

        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/"
        assert "HttpOnly" in callback.headers["set-cookie"]
        raw_cookie = client.cookies.get("xynigo_session")
        assert raw_cookie
        assert oauth.exchanges == [("authorization-code", verifier)]

        with database.session_factory() as session:
            stored_session = session.scalar(select(SessionRecord))
            assert stored_session is not None
            assert stored_session.token_hash == hash_token(raw_cookie)
            assert raw_cookie not in stored_session.token_hash
            assert session.scalar(select(User.status)) == "active"
            assert set(session.scalars(select(Role.code))) == {
                "admin",
                "member",
                "super_admin",
            }
            assert len(list(session.scalars(select(Permission.code)))) >= 10

        me = client.get("/v1/auth/me")
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.headers["x-content-type-options"] == "nosniff"
        assert me.json()["roles"] == ["super_admin"]
        assert "system.lark_connection.manage" in me.json()["permissions"]
        assert me.json()["user"]["name"] == "合成测试用户"
        web_status = client.get("/v1/auth/web/status")
        assert web_status.status_code == 200
        assert web_status.json()["authenticated"] is True
        assert web_status.json()["identity"]["user"]["name"] == "合成测试用户"

        csrf_rejected = client.post("/v1/auth/logout")
        assert csrf_rejected.status_code == 403
        assert csrf_rejected.json()["detail"]["code"] == "web_csrf_required"
        assert client.get("/v1/auth/me").status_code == 200

        logout = client.post(
            "/v1/auth/logout",
            headers={"X-Xynigo-Web-CSRF": "same-origin"},
        )
        assert logout.status_code == 204
        assert client.get("/v1/auth/me").status_code == 401


def test_unknown_user_is_pending_and_receives_no_session(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path, open_id="ou_new", bootstrap="ou_admin")

    with TestClient(app) as client:
        state, _challenge = start_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 403
        assert callback.json()["detail"]["code"] == "user_pending_approval"
        assert client.cookies.get("xynigo_session") is None

    with database.session_factory() as session:
        assert session.scalar(select(User.status)) == "pending"
        assert session.scalar(select(SessionRecord)) is None


def test_web_status_slides_active_cookie_session_without_exceeding_absolute_limit(
    tmp_path,
) -> None:
    app, database, _oauth = build_test_app(
        tmp_path,
        session_ttl_seconds=60 * 60,
        session_absolute_ttl_seconds=24 * 60 * 60,
        session_refresh_threshold_seconds=30 * 60,
    )

    with TestClient(app) as client:
        state, _challenge = start_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        raw_cookie = client.cookies.get("xynigo_session")
        assert raw_cookie

        now = datetime.now(UTC)
        with database.session_factory() as session:
            record = session.scalar(select(SessionRecord))
            assert record is not None
            record.created_at = now - timedelta(hours=23, minutes=30)
            record.expires_at = now + timedelta(minutes=5)
            previous_expiry = record.expires_at
            absolute_expiry = record.created_at + timedelta(hours=24)
            session.commit()

        status = client.get("/v1/auth/web/status")
        assert status.status_code == 200
        assert status.json()["authenticated"] is True
        assert "Max-Age=" in status.headers["set-cookie"]
        assert client.cookies.get("xynigo_session") == raw_cookie

        with database.session_factory() as session:
            record = session.scalar(select(SessionRecord))
            assert record is not None
            stored_expiry = record.expires_at.replace(tzinfo=UTC)
            assert stored_expiry > previous_expiry
            assert stored_expiry <= absolute_expiry


def test_oauth_state_is_single_use(tmp_path) -> None:
    app, _database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        state, _challenge = start_login(client)
        first = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "first-code", "state": state},
            follow_redirects=False,
        )
        assert first.status_code == 303

        second = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "second-code", "state": state},
            follow_redirects=False,
        )
        assert second.status_code == 400
        assert second.json()["detail"]["code"] == "oauth_state_invalid"


def test_plain_pkce_uses_the_verifier_as_the_challenge(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path, pkce_method="plain")

    with TestClient(app) as client:
        _state, challenge = start_login(client)

    with database.session_factory() as session:
        verifier = session.scalar(select(OAuthLoginAttempt.code_verifier))
        assert verifier is not None
        assert challenge == verifier


def test_disabled_pkce_omits_challenge_and_verifier(tmp_path) -> None:
    app, database, oauth = build_test_app(tmp_path, pkce_method="disabled")

    with TestClient(app) as client:
        state, challenge = start_login(client)
        assert challenge is None
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303

    with database.session_factory() as session:
        assert session.scalar(select(OAuthLoginAttempt.code_verifier)) is None
    assert oauth.exchanges == [("authorization-code", None)]


def test_local_login_bridge_issues_bearer_session_once(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        state, poll_token = start_local_login(client)
        pending = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
        assert pending.status_code == 202
        assert pending.json() == {"status": "pending"}

        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "/v1/auth/local/complete"
        assert "set-cookie" not in callback.headers

        complete = client.get("/v1/auth/local/complete")
        assert complete.status_code == 200
        assert complete.headers["content-type"].startswith("text/html")
        assert "window.close()" in complete.text
        assert "正在自动关闭此页面" in complete.text
        assert "script-src 'sha256-" in complete.headers["content-security-policy"]
        assert "'unsafe-inline'" not in complete.headers["content-security-policy"]

        exchanged = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
        assert exchanged.status_code == 200
        payload = exchanged.json()
        assert payload["status"] == "authenticated"
        assert payload["identity"]["roles"] == ["super_admin"]
        bearer_token = payload["sessionToken"]

        consumed = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
        assert consumed.status_code == 409
        assert consumed.json()["detail"]["code"] == "local_login_consumed"

        me = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        assert me.status_code == 200
        assert me.json()["user"]["status"] == "active"
        logout = client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {bearer_token}"},
        )
        assert logout.status_code == 204
        assert client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {bearer_token}"},
        ).status_code == 401

    with database.session_factory() as session:
        local_login = session.scalar(select(LocalLoginRequest))
        assert local_login is not None
        assert local_login.status == "consumed"
        stored_session = session.scalar(select(SessionRecord))
        assert stored_session is not None
        assert stored_session.token_hash == hash_token(bearer_token)
        assert stored_session.revoked_at is not None


def test_local_login_reports_pending_user_denial(tmp_path) -> None:
    app, database, _oauth = build_test_app(
        tmp_path,
        open_id="ou_pending",
        bootstrap="ou_admin",
    )

    with TestClient(app) as client:
        state, poll_token = start_local_login(client)
        callback = client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 403
        denied = client.post("/v1/auth/local/poll", json={"pollToken": poll_token})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "user_pending_approval"

    with database.session_factory() as session:
        local_login = session.scalar(select(LocalLoginRequest))
        assert local_login is not None
        assert local_login.status == "denied"
        assert session.scalar(select(SessionRecord)) is None


def test_health_and_readiness(tmp_path) -> None:
    app, _database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/v1/auth/me").status_code == 401


def test_super_admin_manages_tenant_feishu_credential_and_proxy_hides_secret(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"valueRange": {"values": [["销售订单号"]]}}},
        )

    app, database, _oauth = build_test_app(
        tmp_path,
        feishu_integration_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        state, _challenge = start_login(client)
        assert client.get(
            "/v1/auth/feishu/callback",
            params={"code": "authorization-code", "state": state},
            follow_redirects=False,
        ).status_code == 303
        initial = client.get("/v1/admin/integrations/feishu")
        assert initial.status_code == 200
        assert initial.json()["source"] == "deployment"
        assert "appSecret" not in initial.text

        secret = "organization-secret-value"
        saved = client.put(
            "/v1/admin/integrations/feishu",
            json={
                "expectedRevision": 0,
                "appId": "cli_test01",
                "appSecret": secret,
            },
            headers={"X-Xynigo-Web-CSRF": "same-origin"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["source"] == "organization"
        assert saved.json()["revision"] == 1
        assert secret not in saved.text
        assert "appSecret" not in saved.text

        proxied = client.post(
            "/v1/integrations/feishu/read",
            json={
                "permission": "assistant.access",
                "path": "/open-apis/sheets/v2/spreadsheets/shtcnSynthetic01/values/sheet1%21A1%3AAQ",
                "query": {},
            },
            headers={"X-Xynigo-Web-CSRF": "same-origin"},
        )
        assert proxied.status_code == 200, proxied.text
        assert proxied.json()["data"]["code"] == 0
        denied_path = client.post(
            "/v1/integrations/feishu/read",
            json={
                "permission": "assistant.access",
                "path": "/open-apis/contact/v3/users",
                "query": {},
            },
            headers={"X-Xynigo-Web-CSRF": "same-origin"},
        )
        assert denied_path.status_code == 403

    with database.session_factory() as session:
        record = session.scalar(select(TenantFeishuIntegration))
        assert record is not None
        assert record.app_id == "cli_test01"
        assert secret not in record.credential_ciphertext
    assert any(
        request.headers.get("authorization") == "Bearer tenant-token"
        for request in requests
        if not request.url.path.endswith("/tenant_access_token/internal")
    )
