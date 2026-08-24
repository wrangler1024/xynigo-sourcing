from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

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
):
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}")
    Base.metadata.create_all(database.engine)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}",
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret-not-real",
        feishu_redirect_uri="http://testserver/v1/auth/feishu/callback",
        feishu_pkce_method=pkce_method,
        allowed_tenant_keys="tenant_allowed",
        bootstrap_super_admin_open_ids=bootstrap,
        cookie_secure=False,
        allowed_hosts="testserver",
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
    app = create_app(settings=settings, oauth_client=oauth, database=database)
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
        assert callback.headers["location"] == "/v1/auth/me"
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
            assert session.scalar(select(Role.code)) == "super_admin"
            assert len(list(session.scalars(select(Permission.code)))) >= 10

        me = client.get("/v1/auth/me")
        assert me.status_code == 200
        assert me.headers["cache-control"] == "no-store"
        assert me.headers["x-content-type-options"] == "nosniff"
        assert me.json()["roles"] == ["super_admin"]
        assert "system.lark_connection.manage" in me.json()["permissions"]
        assert me.json()["user"]["name"] == "合成测试用户"

        logout = client.post("/v1/auth/logout")
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
