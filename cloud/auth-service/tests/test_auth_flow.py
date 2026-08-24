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
    OAuthLoginAttempt,
    Permission,
    Role,
    SessionRecord,
    User,
)
from xynigo_auth.security import hash_token


@dataclass
class FakeOAuthClient:
    identity: FeishuIdentity
    exchanges: list[tuple[str, str]] = field(default_factory=list)

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        return f"https://accounts.example.test/authorize?state={state}&code_challenge={code_challenge}"

    def exchange_code(self, *, code: str, code_verifier: str) -> str:
        self.exchanges.append((code, code_verifier))
        return "temporary-user-access-token"

    def get_identity(self, user_access_token: str) -> FeishuIdentity:
        assert user_access_token == "temporary-user-access-token"
        return self.identity


def build_test_app(tmp_path, *, open_id: str = "ou_admin", bootstrap: str = "ou_admin"):
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}")
    Base.metadata.create_all(database.engine)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'identity.sqlite3'}",
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret-not-real",
        feishu_redirect_uri="http://testserver/v1/auth/feishu/callback",
        allowed_tenant_keys="tenant_allowed",
        bootstrap_super_admin_open_ids=bootstrap,
        cookie_secure=False,
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


def start_login(client: TestClient) -> tuple[str, str]:
    response = client.get("/v1/auth/feishu/start", follow_redirects=False)
    assert response.status_code == 307
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["state"][0], query["code_challenge"][0]


def test_bootstrap_admin_login_creates_hashed_session_and_rbac(tmp_path) -> None:
    app, database, oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        state, challenge = start_login(client)
        assert challenge

        with database.session_factory() as session:
            attempt = session.scalar(select(OAuthLoginAttempt))
            assert attempt is not None
            assert attempt.state_hash == hash_token(state)
            assert state not in attempt.state_hash
            verifier = attempt.code_verifier

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


def test_health_and_readiness(tmp_path) -> None:
    app, _database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/v1/auth/me").status_code == 401
