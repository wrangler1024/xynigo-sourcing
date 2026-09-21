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
# 半托管第二应用（合成值）：平台一应用一合作模式，凭证成对。
SEMI_APP_ID = "163EFAKE00000000000000000000FAKE"
SEMI_APP_SECRET = "fedcba9876543210fedcba9876543210"
SEMI_OPEN_KEY_ID = "44A98872FC05D59CB964B33C77851D03"
SEMI_SECRET_PLAIN = "97B1C60421E94AF3820E7A67746E17DF"


def aes_encrypt_b64(plain: str, app_secret: str) -> str:
    """官方《店铺授权应用手册》同款加密：AES-128-CBC + base64 密文。"""
    key = app_secret.encode("utf-8")[:16]
    data = plain.encode("utf-8")
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    encryptor = Cipher(
        algorithms.AES(key), modes.CBC(b"space-station-de")
    ).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()


def shein_transport(
    *,
    expired_tokens: tuple[str, ...] = (),
    store_info_fail_times: int = 0,
    token_open_keys: dict[str, str] | None = None,
    app_id: str = APP_ID,
    app_secret: str = APP_SECRET,
    open_key_id: str = OPEN_KEY_ID,
    secret_plain: str = SECRET_PLAIN,
    store_name: str = "观潮",
    supplier_id: int = 18301880,
):
    """Fake SHEIN 网关：get-by-token + query-store-info，验证签名头存在。

    store_info_fail_times：前 N 次店铺信息查询返回业务错误（模拟接口闪断，
    用于验证「信息接口失败不裂行」的回归场景）。
    token_open_keys：tempToken → openKeyId 映射（模拟平台换钥轮换）。
    app_id/app_secret/open_key_id/secret_plain/store_name/supplier_id：
    按应用参数化——半托管应用走自己的凭证与店铺身份，密文必须用
    对应应用的 AppSecret 加密才能被服务端解开（一应用一模式的核心约束）。
    """
    failures_left = {"count": store_info_fail_times}
    open_key_map = token_open_keys or {}
    seen = {"get_by_token_appids": [], "store_info_open_key_ids": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-lt-appid") or request.headers.get(
            "x-lt-openKeyId"
        ), "签名身份头缺失"
        assert request.headers.get("x-lt-signature"), "签名头缺失"
        if request.url.path == "/open-api/auth/get-by-token":
            seen["get_by_token_appids"].append(
                request.headers.get("x-lt-appid", "")
            )
            token = json.loads(request.content)["tempToken"]
            if token in expired_tokens:
                return httpx.Response(
                    200, json={"code": "33051002", "msg": "failed", "data": None}
                )
            return httpx.Response(200, json={
                "code": "0",
                "msg": "ok",
                "data": {
                    "openKeyId": open_key_map.get(token, open_key_id),
                    "secretKey": aes_encrypt_b64(secret_plain, app_secret),
                },
            })
        if request.url.path == (
            "/open-api/openapi-business-backend/query-store-info"
        ):
            seen["store_info_open_key_ids"].append(
                request.headers.get("x-lt-openKeyId", "")
            )
            if failures_left["count"] > 0:
                failures_left["count"] -= 1
                return httpx.Response(
                    200, json={"code": "500", "msg": "internal", "data": None}
                )
            if request.headers.get("x-lt-openKeyId") == "BAD":
                return httpx.Response(
                    200, json={"code": "500", "msg": "sign error", "data": None}
                )
            # 真机（20260919）：店铺身份嵌在 info.storeInfo 子对象
            # （supplierId/storeName/storeStatus），外层还有额度段。
            return httpx.Response(200, json={
                "code": "0",
                "msg": "ok",
                "info": {
                    "storeInfo": {
                        "supplierId": supplier_id,
                        "storeName": store_name,
                        "storeStatus": 1,
                    },
                    "storeProductQuota": {
                        "totalLimit": 0, "availableLimit": 0,
                        "usedQuota": 0, "supplierBusinessMode": "",
                    },
                },
            })
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def build_shein_app(
    tmp_path,
    transport=None,
    *,
    semi_transport=None,
    with_semi=False,
):
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
        **(
            {
                "shein_openapi_semi_app_id": SEMI_APP_ID,
                "shein_openapi_semi_app_secret": SEMI_APP_SECRET,
            }
            if with_semi
            else {}
        ),
    )
    app = create_app(
        settings=settings,
        oauth_client=object(),  # 不走登录端点；会话直插
        directory_client=object(),
        database=database,
        shein_openapi_transport=transport or shein_transport(),
        shein_semi_openapi_transport=semi_transport or (
            shein_transport(
                app_id=SEMI_APP_ID,
                app_secret=SEMI_APP_SECRET,
                open_key_id=SEMI_OPEN_KEY_ID,
                secret_plain=SEMI_SECRET_PLAIN,
                store_name="半托管合成店",
                supplier_id=28889999,
            )
            if with_semi
            else None
        ),
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
    ciphertext = aes_encrypt_b64(SECRET_PLAIN, APP_SECRET)
    assert decrypt_secret_key(ciphertext, APP_SECRET) == SECRET_PLAIN
    for broken in ("zz", "00" * 3):
        try:
            decrypt_secret_key(broken, APP_SECRET)
        except SheinOpenApiClientError:
            pass
        else:
            raise AssertionError("坏密文应报错")
    try:
        # 短密钥按官方语义补零成合法 key，但解不出正确填充。
        decrypt_secret_key(ciphertext, "short")
    except SheinOpenApiClientError as exc:
        assert exc.code == "shein_secret_padding_invalid"
    else:
        raise AssertionError("错误密钥应报错")


def test_decrypt_secret_key_matches_official_doc_vector() -> None:
    """官方《店铺授权应用手册》Python 示例自带向量原样解出，钉死参数组合。

    密文是 base64（此前误按 hex 解码导致真机换钥失败）；key=appSecret 前
    16 字节、IV=固定种子前 16 字节、AES-128-CBC、PKCS7。
    """
    key = "14ABE7A4222647CB945DADC76740BA73".encode("utf-8")[:16]
    decryptor = Cipher(
        algorithms.AES(key), modes.CBC(b"space-station-de")
    ).decryptor()
    padded = decryptor.update(
        base64.b64decode("lr4JDSqQkRyZ2YAosX3ZRQ==")
    ) + decryptor.finalize()
    assert padded[:-padded[-1]].decode("utf-8") == "Hello World"


def test_store_payload_timestamps_are_tz_aware(tmp_path) -> None:
    """库列为 naive UTC；序列化须带 +00:00，否则浏览器按本地时区错读。"""
    app, _database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        callback = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": link["state"]},
        )
        assert callback.status_code == 200
        listed = client.get(
            "/v1/shein-auth/stores?page=1&pageSize=10", headers=admin_headers()
        ).json()
        item = listed["items"][0]
        assert item["firstAuthorizedAt"].endswith("+00:00")
        assert item["latestAuthorizedAt"].endswith("+00:00")
        events = client.get(
            "/v1/shein-auth/events", headers=admin_headers()
        ).json()
        assert events["items"][-1]["at"].endswith("+00:00")


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
    # 客户端返回原始业务段；真机（20260919）身份在 info.storeInfo 子对象。
    assert info["storeInfo"]["supplierId"] == 18301880
    assert info["storeInfo"]["storeName"] == "观潮"


def test_client_maps_expired_temp_token_code() -> None:
    client = SheinOpenApiClient(
        gateway="https://openapi.example.test",
        app_id=APP_ID,
        app_secret=APP_SECRET,
        transport=shein_transport(expired_tokens=("expired-token-000",)),
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
        # state 嵌进 redirectUrl：平台回跳保留 query 时可原样带回。
        assert base64.b64decode(
            payload["url"].split("redirectUrl=")[1].split("&")[0]
        ).decode() == f"{REDIRECT_BASE}?state={payload['state']}"

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


def test_callback_without_state_claims_newest_unexpired_link(tmp_path) -> None:
    """平台回跳剥掉 query 时 state 缺席：按最新未消费且未过期链接回退。"""
    app, database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        fresh = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        newest_expired = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        with database.session_factory() as session:
            record = session.scalar(
                select(SheinAuthLink).where(
                    SheinAuthLink.state == newest_expired["state"]
                )
            )
            record.expires_at = utcnow() - timedelta(minutes=1)
            session.commit()

        # 时间最新的一条已过期：回退必须跳过它选中次新的；
        # 若实现不过滤过期就会认领最新一条并以 410 失败。
        ok = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": ""},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["store"]["merchantId"] == "18301880"

        # 未消费链接已被清空：空 state 回退应明确 404，不得静默成功。
        drained = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-123456", "state": ""},
        )
        assert drained.status_code == 404
        assert drained.json()["detail"]["code"] == "shein_auth_state_unknown"


def test_callback_page_shares_workspace_csp(tmp_path) -> None:
    """回调页与工作台同一份文档；兜底 CSP 会连内联样式带脚本一起拦掉。"""
    app, _database, _ids = build_shein_app(tmp_path)
    with TestClient(app) as client:
        workspace = client.get("/")
        callback = client.get("/shein-auth/callback?appid=x&tempToken=y")
        assert callback.status_code == 200
        assert (
            callback.headers["content-security-policy"]
            == workspace.headers["content-security-policy"]
        )
        assert "style-src 'unsafe-inline'" in (
            callback.headers["content-security-policy"]
        )


def test_temp_token_platform_expiry_maps_to_event_failure(tmp_path) -> None:
    app, database, _ids = build_shein_app(
        tmp_path, transport=shein_transport(expired_tokens=("dead-token-000000",))
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


# ---- 评审必须改项回归（20260917 Cursor 评审） ----


def test_store_info_failure_then_reauth_stays_single_row(tmp_path) -> None:
    """评审必须改 #1：信息接口闪断不得把同店拆成两行，也不得用 openKeyId 冒充商家ID。

    首次换钥成功但店铺信息失败 → 行 A（merchant_id 为空、openKeyId 已落）；
    再次授权（同 openKeyId，平台信息恢复）→ 仍是一行，并回填真实商家ID。
    """
    app, database, _ids = build_shein_app(
        tmp_path, transport=shein_transport(store_info_fail_times=1)
    )
    with TestClient(app) as client:
        link1 = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        first = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-first", "state": link1["state"]},
        )
        assert first.status_code == 200, first.text
        assert first.json()["store"]["merchantId"] == "", (
            "商家ID 暂缺时必须是空值，不得用 openKeyId 前缀冒充"
        )
        with database.session_factory() as session:
            rows = session.scalars(select(SheinAuthorizedStore)).all()
            assert len(rows) == 1 and rows[0].merchant_id == ""

        link2 = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        second = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-second", "state": link2["state"]},
        )
        assert second.status_code == 200
        with database.session_factory() as session:
            rows = session.scalars(select(SheinAuthorizedStore)).all()
            assert len(rows) == 1, f"信息接口失败后重新授权裂行：{len(rows)} 行"
            assert rows[0].merchant_id == "18301880", "重新授权应回填真实商家ID"


def test_reauth_with_store_id_updates_target_row_even_if_openkey_rotates(
    tmp_path,
) -> None:
    """评审必须改 #1：重新授权带 storeId 时，即便平台签发新 openKeyId 也更新该行。"""
    app, database, _ids = build_shein_app(
        tmp_path,
        transport=shein_transport(token_open_keys={
            "temp-token-second": "ROTATED0KEY0ID0000000000000000001",
        }),
    )
    with TestClient(app) as client:
        link1 = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        store = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-first", "state": link1["state"]},
        ).json()["store"]

        reauth = client.post(
            "/v1/shein-auth/link",
            json={"mode": "self", "storeId": store["id"]},
            headers=admin_headers(),
        ).json()
        second = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-second", "state": reauth["state"]},
        )
        assert second.status_code == 200
        assert second.json()["store"]["id"] == store["id"]
        with database.session_factory() as session:
            rows = session.scalars(select(SheinAuthorizedStore)).all()
            assert len(rows) == 1
            assert rows[0].open_key_id == "ROTATED0KEY0ID0000000000000000001"


def test_failed_exchange_still_consumes_state_once(tmp_path) -> None:
    """评审必须改 #2：换钥失败也计入一次性消费，重放必须 409（不能重试刷）。"""
    app, _database, _ids = build_shein_app(
        tmp_path, transport=shein_transport(expired_tokens=("expired-token-000",))
    )
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        first = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "expired-token-000", "state": link["state"]},
        )
        assert first.status_code == 410
        replay = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "expired-token-000", "state": link["state"]},
        )
        assert replay.status_code == 409, "失败路径的 state 也必须已消费"


def test_zero_order_placeholder_merchant_and_verify_backfills(tmp_path) -> None:
    """信息缺失时占位名可读；验证成功后回填商家ID 与真实店名。"""
    app, database, _ids = build_shein_app(
        tmp_path, transport=shein_transport(store_info_fail_times=1)
    )
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        ).json()
        store = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "temp-token-x", "state": link["state"]},
        ).json()["store"]
        assert store["name"] == "待命名店铺"
        verified = client.post(
            f"/v1/shein-auth/stores/{store['id']}/verify", headers=admin_headers()
        ).json()
        assert verified["merchantId"] == "18301880"
        assert verified["name"] == "观潮"


# ---- 半托管第二应用（一应用一合作模式）----

def test_semi_not_configured_rejects_link_but_self_mode_unaffected(
    tmp_path,
) -> None:
    """未配置半托管凭证：半托管链接生成明确拒绝，自营链路照常。"""
    app, _database, _ids = build_shein_app(tmp_path)  # with_semi=False
    with TestClient(app) as client:
        rejected = client.post(
            "/v1/shein-auth/link",
            json={"mode": "semi"},
            headers=admin_headers(),
        )
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["code"] == "shein_auth_not_configured"

        ok = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        )
        assert ok.status_code == 200
        assert f"appid={APP_ID}&" in ok.json()["url"]


def test_semi_link_and_callback_use_semi_app_credentials(tmp_path) -> None:
    """半托管链路全流程：链接带半托管 appid，回调换钥/店铺信息都打半托管
    应用（x-lt-appid 断言），落库店铺 mode=semi 且密文可解（应用配对正确）。"""
    app, database, _ids = build_shein_app(tmp_path, with_semi=True)
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "semi"}, headers=admin_headers()
        )
        assert link.status_code == 200, link.text
        payload = link.json()
        assert f"appid={SEMI_APP_ID}&" in payload["url"]

        callback = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "semi-token-1", "state": payload["state"]},
        )
        assert callback.status_code == 200, callback.text
        store = callback.json()["store"]
        assert store["mode"] == "semi"
        assert store["appId"] == SEMI_APP_ID
        assert store["name"] == "半托管合成店"
        assert store["merchantId"] == "28889999"

        with database.session_factory() as session:
            record = session.scalar(select(SheinAuthorizedStore))
            assert record.mode == "semi"
            assert record.app_id == SEMI_APP_ID
            assert record.open_key_id == SEMI_OPEN_KEY_ID
            # 落库密文用部署密钥解开后应是半托管密钥明文（应用配对正确）。
            cipher = SheinStoreSecretCipher(
                "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
            )
            assert cipher.decrypt_secret(record.secret_ciphertext) == (
                SEMI_SECRET_PLAIN
            )


def test_semi_callback_hits_semi_transport_only(tmp_path) -> None:
    """半托管回调的网络调用只打半托管应用网关；自营 transport 零换钥请求。

    换钥签名与密文解密都依赖发起应用的 AppSecret，打到错误应用必然失败，
    所以 transport 侧的 appid 归属是 per-mode 正确性的直接证据。
    """
    self_transport = shein_transport()
    semi_transport = shein_transport(
        app_id=SEMI_APP_ID,
        app_secret=SEMI_APP_SECRET,
        open_key_id=SEMI_OPEN_KEY_ID,
        secret_plain=SEMI_SECRET_PLAIN,
        store_name="半托管合成店",
        supplier_id=28889999,
    )
    app, _database, _ids = build_shein_app(
        tmp_path,
        transport=self_transport,
        semi_transport=semi_transport,
        with_semi=True,
    )
    with TestClient(app) as client:
        link = client.post(
            "/v1/shein-auth/link", json={"mode": "semi"}, headers=admin_headers()
        ).json()
        callback = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "semi-token-2", "state": link["state"]},
        )
        assert callback.status_code == 200, callback.text

        assert semi_transport.seen["get_by_token_appids"] == [SEMI_APP_ID]
        assert semi_transport.seen["store_info_open_key_ids"] == [
            SEMI_OPEN_KEY_ID
        ]
        # 自营应用的 mock 网关不应收到任何请求。
        assert self_transport.seen["get_by_token_appids"] == []
        assert self_transport.seen["store_info_open_key_ids"] == []


def test_empty_state_fallback_claims_latest_link_across_apps(tmp_path) -> None:
    """空 state 回退：两应用各有未消费链接时认领时间最新一条（半托管），
    且换钥走该链接所属应用——IN 过滤不漏半托管链接是本回归的核心。"""
    self_transport = shein_transport()
    semi_transport = shein_transport(
        app_id=SEMI_APP_ID,
        app_secret=SEMI_APP_SECRET,
        open_key_id=SEMI_OPEN_KEY_ID,
        secret_plain=SEMI_SECRET_PLAIN,
        store_name="半托管合成店",
        supplier_id=28889999,
    )
    app, database, _ids = build_shein_app(
        tmp_path,
        transport=self_transport,
        semi_transport=semi_transport,
        with_semi=True,
    )
    with TestClient(app) as client:
        first = client.post(
            "/v1/shein-auth/link", json={"mode": "self"}, headers=admin_headers()
        )
        assert first.status_code == 200
        second = client.post(
            "/v1/shein-auth/link", json={"mode": "semi"}, headers=admin_headers()
        )
        assert second.status_code == 200

        # SQLite 的 CURRENT_TIMESTAMP 只有秒级精度，两次请求常落在同一秒；
        # 显式把半托管链接时间推后，直接测「按时间最新认领」的查询语义
        # （生产 PostgreSQL 为微秒精度，不存在此歧义）。
        with database.session_factory() as session:
            latest = session.scalar(
                select(SheinAuthLink).where(
                    SheinAuthLink.state == second.json()["state"]
                )
            )
            latest.created_at = latest.created_at + timedelta(seconds=1)
            session.commit()

        callback = client.post(
            "/v1/shein-auth/callback",
            json={"tempToken": "semi-token-3", "state": ""},
        )
        assert callback.status_code == 200, callback.text
        store = callback.json()["store"]
        assert store["mode"] == "semi", "应认领时间最新的半托管链接"
        assert store["appId"] == SEMI_APP_ID

        assert semi_transport.seen["get_by_token_appids"] == [SEMI_APP_ID]
        assert self_transport.seen["get_by_token_appids"] == []
