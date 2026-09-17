# -*- coding: utf-8 -*-
"""SHEIN 店铺授权：签名/解密/回调链/权限/结构断言（全部合成数据）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from sqlalchemy import select

from xynigo_auth.config import Settings
from xynigo_auth.database import Database
from xynigo_auth.main import create_app, utcnow
from xynigo_auth.models import (
    Base,
    Permission,
    Role,
    RolePermission,
    SessionRecord,
    SheinAuthEvent,
    SheinAuthLink,
    SheinAuthorizedStore,
    Tenant,
    User,
    UserRole,
)
from xynigo_auth.security import hash_token
from xynigo_auth.shein_openapi_client import (
    SheinOpenApiClient,
    SheinOpenApiClientError,
    decrypt_secret_key,
    sign_headers,
)
from xynigo_auth.shein_store_auth_crypto import SheinStoreSecretCipher

ADMIN_TOKEN = "a" * 64
MEMBER_TOKEN = "b" * 64
APP_ID = "1636F90749001A002A60B0C187361"
APP_SECRET = "0123456789abcdef0123456789abcdef"
OPEN_KEY_ID = "33F87761EBD0428AA8573A22F63295C2"
SECRET_PLAIN = "136CADB9F0B14D878650B4B520648D58"
REDIRECT_BASE = "https://xynigo.example.test/shein-auth/callback"


def aes_encrypt_hex(plain: str, app_secret: str) -> str:
    key = app_secret.encode("utf-8")[:16]
    data = plain.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    encryptor = Cipher(
        algorithms.AES(key), modes.CBC(b"space-station-de")
    ).encryptor()
    return (encryptor.update(data) + encryptor.finalize()).hex()


def shein_transport(temp_token_effect: dict[str, str] | None = None):
    """Fake SHEIN 网关：get-by-token + query-store-info，验证签名头存在。"""
    effects = temp_token_effect or {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-lt-appid") or request.headers.get(
            "x-lt-openKeyId"
        ), "签名身份头缺失"
        assert request.headers.get("x-lt-signature"), "签名头缺失"
        if request.url.path == "/open-api/auth/get-by-token":
            token = json.loads(request.content)["tempToken"]
            if token in effects:
                code = effects[token]
                return httpx.Response(
                    200, json={"code": code, "msg": "failed", "data": None}
                )
            return httpx.Response(200, json={
                "code": "0",
                "msg": "ok",
                "data": {
                    "openKeyId": OPEN_KEY_ID,
                    "secretKey": aes_encrypt_hex(SECRET_PLAIN, APP_SECRET),
                },
            })
        if request.url.path == (
            "/open-api/openapi-business-backend/query-store-info"
        ):
            if request.headers.get("x-lt-openKeyId") == "BAD":
                return httpx.Response(
                    200, json={"code": "500", "msg": "sign error", "data": None}
                )
            return httpx.Response(200, json={
                "code": "0",
                "msg": "ok",
                "data": {"merchantId": "18301880", "storeName": "观潮"},
            })
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    return httpx.MockTransport(handler)


def build_shein_app(tmp_path, transport=None):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'shein.sqlite3'}"
    database = Database(database_url)
    Base.metadata.create_all(database.engine)
    settings = Settings(
        environment="test",
        database_url=database_url,
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret-not-real",
        buyer_credential_encryption_key=(
            "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
        ),
        feishu_redirect_uri="http://testserver/v1/auth/feishu/callback",
        allowed_tenant_keys="tenant_allowed",
        cookie_secure=False,
        allowed_hosts="testserver",
        shein_openapi_app_id=APP_ID,
        shein_openapi_app_secret=APP_SECRET,
        shein_auth_redirect_base=REDIRECT_BASE,
    )
    app = create_app(
        settings=settings,
        oauth_client=object(),  # 不走登录端点；会话直插
        directory_client=object(),
        database=database,
        shein_openapi_transport=transport or shein_transport(),
    )
    now = utcnow()
    with database.session_factory() as session:
        tenant = Tenant(feishu_tenant_key="tenant_allowed", name="合成组织")
        session.add(tenant)
        session.flush()
        admin = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_admin",
            display_name="合成管理员",
            status="active",
        )
        member = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_member",
            display_name="普通成员",
            status="active",
        )
        session.add_all((admin, member))
        session.flush()
        member_role = Role(
            tenant_id=tenant.id, code="member", name="成员", is_system=True
        )
        admin_role = Role(
            tenant_id=tenant.id, code="admin", name="管理员", is_system=True
        )
        session.add_all((member_role, admin_role))
        session.flush()
        permissions = session.scalars(select(Permission)).all()
        admin_permissions = [
            p for p in permissions if not p.code.startswith("system.lark")
            and p.code != "system.integration.manage"
            and p.code != "resource.ip.credential.manage"
        ]
        session.add_all(
            RolePermission(role_id=admin_role.id, permission_id=p.id)
            for p in admin_permissions
        )
        session.add(UserRole(user_id=admin.id, role_id=admin_role.id))
        session.add(UserRole(user_id=member.id, role_id=member_role.id))
        session.add_all((
            SessionRecord(
                user_id=admin.id,
                token_hash=hash_token(ADMIN_TOKEN),
                last_seen_at=now,
                expires_at=now + timedelta(hours=8),
            ),
            SessionRecord(
                user_id=member.id,
                token_hash=hash_token(MEMBER_TOKEN),
                last_seen_at=now,
                expires_at=now + timedelta(hours=8),
            ),
        ))
        session.commit()
        ids = {"admin": str(admin.id), "member": str(member.id)}
    return app, database, ids


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def member_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MEMBER_TOKEN}"}


# ---- 纯函数层 ----


def test_secret_cipher_roundtrip_and_prefix() -> None:
    cipher = SheinStoreSecretCipher(
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )
    ciphertext = cipher.encrypt_secret(SECRET_PLAIN)
    assert ciphertext.startswith("v1:")
    assert SECRET_PLAIN not in ciphertext
    assert cipher.decrypt_secret(ciphertext) == SECRET_PLAIN


def test_sign_headers_reproducible_structure() -> None:
    headers = sign_headers(
        identity_header="x-lt-openKeyId",
        identity=OPEN_KEY_ID,
        secret=SECRET_PLAIN,
        path="/open-api/openapi-business-backend/query-store-info",
    )
    signature = headers["x-lt-signature"]
    assert headers["x-lt-openKeyId"] == OPEN_KEY_ID
    assert len(headers["x-lt-timestamp"]) == 13, "时间戳应为毫秒"
    digest_b64 = signature[-88:]
    random_key = signature[:-88]
    value = (
        f"{OPEN_KEY_ID}&{headers['x-lt-timestamp']}"
        f"&/open-api/openapi-business-backend/query-store-info"
    )
    expected = hmac.new(
        (SECRET_PLAIN + random_key).encode(), value.encode(), hashlib.sha256
    ).hexdigest()
    assert base64.b64decode(digest_b64).decode() == expected


def test_decrypt_secret_key_roundtrip_and_bad_inputs() -> None:
    ciphertext = aes_encrypt_hex(SECRET_PLAIN, APP_SECRET)
    assert decrypt_secret_key(ciphertext, APP_SECRET) == SECRET_PLAIN
    for broken in ("zz", "00" * 3):
        try:
            decrypt_secret_key(broken, APP_SECRET)
        except SheinOpenApiClientError:
            pass
        else:
            raise AssertionError("坏密文应报错")
    try:
        decrypt_secret_key(ciphertext, "short")
    except SheinOpenApiClientError as exc:
        assert exc.code == "shein_app_secret_invalid"
    else:
        raise AssertionError("短应用密钥应报错")


def test_client_exchange_and_store_info_use_mock_gateway() -> None:
    client = SheinOpenApiClient(
        gateway="https://openapi.example.test",
        app_id=APP_ID,
        app_secret=APP_SECRET,
        transport=shein_transport(),
    )
    assert client.configured
    open_key_id, secret = client.exchange_temp_token("temp-token-123456")
    assert open_key_id == OPEN_KEY_ID
    assert secret == SECRET_PLAIN
    info = client.query_store_info(
        open_key_id=OPEN_KEY_ID, secret_key=secret
    )
    assert info["merchantId"] == "18301880"


def test_client_maps_expired_temp_token_code() -> None:
    client = SheinOpenApiClient(
        gateway="https://openapi.example.test",
        app_id=APP_ID,
        app_secret=APP_SECRET,
        transport=shein_transport({"expired-token-000": "33051002"}),
    )
    try:
        client.exchange_temp_token("expired-token-000")
    except SheinOpenApiClientError as exc:
        assert exc.code == "33051002"
    else:
        raise AssertionError("过期 tempToken 应透传平台错误码")


# ---- API 全链 ----


def test_full_authorization_flow_binds_store_with_encrypted_secret(
    tmp_path,
) -> None:
    app, database, ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        )
        assert link.status_code == 200
        payload = link.json()
        assert payload["url"].startswith(
            "https://openapi-sem.sheincorp.com/#/empower"
            f"?appid={APP_ID}&redirectUrl="
        )
        assert base64.b64decode(
            payload["url"].split("redirectUrl=")[1].split("&")[0]
        ).decode() == REDIRECT_BASE

        # 回调端点不要求登录态（店铺主账号浏览器可能未登录工作台）。
        callback = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": payload["state"]},
        )
        assert callback.status_code == 200, callback.text
        store = callback.json()["store"]
        assert store["merchantId"] == "18301880"
        assert store["name"] == "观潮"
        assert store["status"] == "pending"
        assert "secret" not in json.dumps(store).lower()
        assert store["openKeyIdMasked"] == (
            f"{OPEN_KEY_ID[:8]}****{OPEN_KEY_ID[-4:]}"
        )

        stores = client.get(
            "/v1/shein-auth/stores", headers=admin_headers()
        ).json()
        assert stores["total"] == 1
        assert stores["items"][0]["id"] == store["id"]

        with database.session_factory() as session:
            record = session.scalar(select(SheinAuthorizedStore))
            assert record.secret_ciphertext.startswith("v1:")
            assert SECRET_PLAIN not in record.secret_ciphertext
            assert record.open_key_id == OPEN_KEY_ID
            # 重新授权：同店同模式 upsert，密钥轮换时间更新。
            link2 = client.post(
                "/v1/shein-auth/link",
                json={"mode": "self"},
                headers=admin_headers(),
            ).json()
            first_latest = record.latest_authorized_at
            again = client.post(
                "/v1/shein-auth/callback",
                json={
                    "tempToken": "temp-token-987654",
                    "state": link2["state"],
                },
            )
            assert again.status_code == 200
            session.expire_all()
            records = session.scalars(select(SheinAuthorizedStore)).all()
            assert len(records) == 1
            assert records[0].latest_authorized_at > first_latest


def test_state_single_use_unknown_and_expiry(tmp_path) -> None:
    app, database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        state = link["state"]

        unknown = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "whatever-token", "state": "xy" + "0" * 30},
        )
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "shein_auth_state_unknown"

        ok = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": state},
        )
        assert ok.status_code == 200

        replay = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": state},
        )
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "shein_auth_state_reused"

        link3 = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        with database.session_factory() as session:
            record = session.scalar(
                select(SheinAuthLink).where(
                    SheinAuthLink.state == link3["state"]
                )
            )
            record.expires_at = utcnow() - timedelta(minutes=1)
            session.commit()
        expired = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": link3["state"]},
        )
        assert expired.status_code == 410
        assert expired.json()["detail"]["code"] == "shein_auth_state_expired"


def test_temp_token_platform_expiry_maps_to_event_failure(tmp_path) -> None:
    app, database, _ids = build_shein_app(
        tmp_path, transport=shein_transport({"dead-token-000000": "33051002"})
    )
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        failed = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "dead-token-000000", "state": link["state"]},
        )
        assert failed.status_code == 410
        assert failed.json()["detail"]["code"] == "shein_temp_token_expired"
        with database.session_factory() as session:
            events = session.scalars(
                select(SheinAuthEvent).where(SheinAuthEvent.action == "callback")
            ).all()
            assert len(events) == 1
            assert events[0].ok is False
            assert "33051002" in events[0].note
            assert "dead-token-000000" not in events[0].note, (
                "完整 tempToken 只能以掩码出现"
            )


def test_verify_rename_delete_and_events(tmp_path) -> None:
    app, database, ids = build_shein_app(tmp_path)
    # “验证、查看不限”＝可授予普通角色 read；manage 仍只有管理员有。
    # member 是系统锁定角色（权限集固定为空），用自定义角色验证授予路径。
    with TestClient(app) as bootstrap_client:
        created = bootstrap_client.post(
            "/v1/admin/roles",
            json={"name": "运营只读"},
            headers=admin_headers(),
        )
        assert created.status_code == 201, created.text
        custom_role = created.json()["role"]
        catalog = bootstrap_client.get(
            "/v1/admin/permissions", headers=admin_headers()
        ).json()["permissions"]
        read_permission = next(
            item
            for item in catalog
            if item["code"] == "system.shein_store.read"
        )
        assert bootstrap_client.put(
            f"/v1/admin/roles/{custom_role['id']}/permissions",
            json={"permissionCodes": ["system.shein_store.read"]},
            headers=admin_headers(),
        ).status_code == 200
        assert bootstrap_client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [custom_role["id"]]},
            headers=admin_headers(),
        ).status_code == 200
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        store = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": link["state"]},
        ).json()["store"]

        verified = client.post(
            f"/v1/shein-auth/stores/{store['id']}/verify",
            headers=member_headers(),
        )
        assert verified.status_code == 200
        assert verified.json()["status"] == "ok"
        assert verified.json()["lastVerifiedAt"] is not None

        # read 权限可验证，但改名需要 manage。
        member_rename = client.post(
            f"/v1/shein-auth/stores/{store['id']}/rename",
            json={"name": "越权改名"},
            headers=member_headers(),
        )
        assert member_rename.status_code == 403

        renamed = client.post(
            f"/v1/shein-auth/stores/{store['id']}/rename",
            json={"name": "观潮（运营改名）"},
            headers=admin_headers(),
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "观潮（运营改名）"

        events = client.get(
            "/v1/shein-auth/events", headers=admin_headers()
        ).json()["items"]
        actions = [item["action"] for item in events]
        assert actions[:3] == ["rename", "verify", "callback"]
        assert all("secret" not in json.dumps(item).lower() for item in events)

        deleted = client.delete(
            f"/v1/shein-auth/stores/{store['id']}", headers=admin_headers()
        )
        assert deleted.status_code == 200
        with database.session_factory() as session:
            assert session.scalars(select(SheinAuthorizedStore)).all() == []


def test_verify_failure_marks_store_expired(tmp_path) -> None:
    app, database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        store = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": link["state"]},
        ).json()["store"]
        with database.session_factory() as session:
            record = session.get(SheinAuthorizedStore, uuid.UUID(store["id"]))
            record.open_key_id = "BAD"
            session.commit()
        result = client.post(
            f"/v1/shein-auth/stores/{store['id']}/verify",
            headers=admin_headers(),
        )
        assert result.status_code == 200
        assert result.json()["status"] == "expired"
        assert result.json()["verifiedOk"] is False


def test_permissions_and_tenant_isolation_gates(tmp_path) -> None:
    app, _database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        anonymous = client.get("/v1/shein-auth/stores")
        assert anonymous.status_code == 401

        denied = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=member_headers()
        )
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "permission_denied"

        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        )
        assert link.status_code == 200


def test_store_table_column_order_pinned() -> None:
    """钉死列序：迁移与模型漂移会让后续迁移/部署炸（售后 0038 方法论）。"""
    columns = [
        column.name for column in SheinAuthorizedStore.__table__.columns
    ]
    assert columns == [
        "id", "tenant_id", "merchant_id", "store_name", "open_key_id",
        "secret_ciphertext", "app_id", "mode", "first_authorized_at",
        "latest_authorized_at", "last_verified_at", "status", "store_info",
        "created_by_user_id", "created_at", "updated_at",
    ]
    link_columns = [
        column.name for column in SheinAuthLink.__table__.columns
    ]
    assert link_columns == [
        "id", "tenant_id", "created_by_user_id", "state", "mode", "app_id",
        "target_store_id", "expires_at", "consumed_at", "created_at",
    ]
    event_columns = [
        column.name for column in SheinAuthEvent.__table__.columns
    ]
    assert event_columns == [
        "id", "tenant_id", "actor_user_id", "action", "store_id",
        "store_label", "ok", "note", "created_at",
    ]
