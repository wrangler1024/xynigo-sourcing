from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app, start_login
from xynigo_auth.models import (
    ExecutorPairingCode,
    ExecutorTask,
    LocalExecutor,
    Tenant,
    User,
)
from xynigo_auth.security import hash_token


CSRF = {"X-Xynigo-Web-CSRF": "same-origin"}
REVISION_A = "a" * 64
REVISION_B = "b" * 64


def login(client: TestClient) -> None:
    state, _challenge = start_login(client)
    response = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303


def create_pairing_code_payload(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/v1/executors/pairing-codes",
        json={"displayNameHint": "采购电脑"},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["expiresIn"] == 300
    assert payload["pairingRequestId"]
    return payload


def create_pairing_code(client: TestClient) -> str:
    return str(create_pairing_code_payload(client)["pairingCode"])


def pair(device_client: TestClient, pairing_code: str) -> dict[str, object]:
    response = device_client.post(
        "/v1/executor-channel/pair",
        json={
            "pairingCode": pairing_code,
            "displayName": "采购电脑 A",
            "platform": "macos",
            "architecture": "arm64",
            "clientVersion": "0.12.5",
            "protocolVersion": 1,
            "capabilities": ["config.read.v1", "config.write.v1"],
        },
        headers={"X-Xynigo-Source": "local_executor_device"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def device_headers(credential: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credential}",
        "X-Xynigo-Source": "local_executor_device",
        "X-Xynigo-Client-Version": "0.12.5",
    }


def heartbeat(
    device_client: TestClient,
    credential: str,
    *,
    revision: str | None = None,
) -> dict[str, object]:
    response = device_client.post(
        "/v1/executor-channel/poll",
        json={
            "waitSeconds": 0,
            "configRevision": revision,
            "hubStatus": "ready",
            "clientVersion": "0.12.5",
            "protocolVersion": 1,
            "capabilities": ["config.read.v1", "config.write.v1"],
        },
        headers=device_headers(credential),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_pairing_is_single_use_and_credentials_are_only_stored_hashed(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        pairing = create_pairing_code_payload(web_client)
        code = str(pairing["pairingCode"])
        pairing_id = str(pairing["pairingRequestId"])
        pending = web_client.get(f"/v1/executors/pairing-codes/{pairing_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending"
        assert pending.json()["executorId"] is None
        paired = pair(device_client, code)
        credential = str(paired["deviceCredential"])
        assert len(credential) >= 32

        reused = device_client.post(
            "/v1/executor-channel/pair",
            json={
                "pairingCode": code,
                "displayName": "另一台电脑",
                "platform": "windows",
                "architecture": "x86_64",
                "clientVersion": "0.12.5",
                "capabilities": ["config.read.v1"],
            },
        )
        assert reused.status_code == 409
        assert reused.json()["detail"]["code"] == "pairing_code_consumed"

        with database.session_factory() as session:
            executor = session.scalar(select(LocalExecutor))
            assert executor is not None
            assert executor.credential_digest == hash_token(credential)
            assert credential not in executor.credential_digest
            stored_code = session.scalar(select(ExecutorPairingCode))
            assert stored_code is not None
            assert stored_code.code_digest == hash_token(code.replace("-", ""))

        listed = web_client.get("/v1/executors")
        assert listed.status_code == 200
        item = listed.json()["items"][0]
        assert item["displayName"] == "采购电脑 A"
        assert item["connectivity"] == "offline"
        assert "deviceCredential" not in str(listed.json())

        consumed = web_client.get(f"/v1/executors/pairing-codes/{pairing_id}")
        assert consumed.status_code == 200
        assert consumed.json()["status"] == "consumed"
        assert consumed.json()["executorId"] == paired["executorId"]

        heartbeat(device_client, credential)
        online = web_client.get("/v1/executors").json()["items"][0]
        assert online["connectivity"] == "online"

        missing = web_client.get(f"/v1/executors/pairing-codes/{uuid.uuid4()}")
        assert missing.status_code == 404


def test_expired_pairing_code_and_device_cookie_are_rejected(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        pairing = create_pairing_code_payload(web_client)
        code = str(pairing["pairingCode"])
        pairing_id = str(pairing["pairingRequestId"])
        with database.session_factory() as session:
            record = session.scalar(select(ExecutorPairingCode))
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        expired_status = web_client.get(
            f"/v1/executors/pairing-codes/{pairing_id}"
        )
        assert expired_status.status_code == 200
        assert expired_status.json()["status"] == "expired"
        assert "pairingCode" not in expired_status.text
        assert "credential" not in expired_status.text.lower()

        expired = device_client.post(
            "/v1/executor-channel/pair",
            json={
                "pairingCode": code,
                "displayName": "采购电脑",
                "platform": "macos",
                "architecture": "arm64",
                "clientVersion": "0.12.5",
                "capabilities": ["config.read.v1"],
            },
        )
        assert expired.status_code == 410
        assert expired.json()["detail"]["code"] == "pairing_code_expired"

        new_code = create_pairing_code(web_client)
        paired = pair(device_client, new_code)
        credential = str(paired["deviceCredential"])
        device_client.cookies.set("xynigo_session", "browser-cookie")
        rejected = device_client.post(
            "/v1/executor-channel/poll",
            json={
                "waitSeconds": 0,
                "hubStatus": "unknown",
                "clientVersion": "0.12.5",
                "capabilities": ["config.read.v1"],
            },
            headers=device_headers(credential),
        )
        assert rejected.status_code == 401
        assert rejected.json()["detail"]["code"] == "executor_cookie_not_allowed"


def test_config_read_write_lease_and_idempotent_finish(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(device_client, create_pairing_code(web_client))
        executor_id = str(paired["executorId"])
        credential = str(paired["deviceCredential"])
        assert heartbeat(device_client, credential, revision=REVISION_A)["task"] is None

        read_created = web_client.post(
            f"/v1/executors/{executor_id}/config/read",
            json={},
            headers=CSRF,
        )
        assert read_created.status_code == 202, read_created.text
        read_task_id = read_created.json()["task"]["id"]

        lease = heartbeat(device_client, credential, revision=REVISION_A)["task"]
        assert lease["id"] == read_task_id
        assert lease["type"] == "config.read.v1"
        lease_token = lease["leaseToken"]
        assert "leaseToken" not in str(read_created.json())

        started = device_client.post(
            f"/v1/executor-channel/tasks/{read_task_id}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(credential),
        )
        assert started.status_code == 200
        assert started.json()["task"]["status"] == "running"

        finish_body = {
            "leaseToken": lease_token,
            "outcome": "succeeded",
            "resultCode": "config_read_succeeded",
            "resultSummary": {
                "configRevision": REVISION_A,
                "config": {
                    "hubPort": 6873,
                    "serverPort": 8765,
                    "concurrency": 2,
                    "importBuyerPlan": "1:新刚",
                    "verifySampleCount": 3,
                    "hiddenQueryColumns": ["envName", "ip"],
                    "purchaseSite": "MX",
                    "purchaseTags": {"MX": "MX采购", "US": "US采购"},
                    "envCreateWorkers": 5,
                    "safeParallelTasks": True,
                },
            },
        }
        finished = device_client.post(
            f"/v1/executor-channel/tasks/{read_task_id}/finish",
            json=finish_body,
            headers=device_headers(credential),
        )
        assert finished.status_code == 200, finished.text
        assert finished.json()["task"]["status"] == "succeeded"
        duplicate = device_client.post(
            f"/v1/executor-channel/tasks/{read_task_id}/finish",
            json=finish_body,
            headers=device_headers(credential),
        )
        assert duplicate.status_code == 200

        write_body = {
            "expectedRevision": REVISION_A,
            "idempotencyKey": "config-write-test-0001",
            "config": {
                "hubPort": 6873,
                "concurrency": 3,
                "verifySampleCount": 3,
                "envCreateWorkers": 5,
                "safeParallelTasks": True,
            },
        }
        write_created = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json=write_body,
            headers=CSRF,
        )
        assert write_created.status_code == 202, write_created.text
        duplicate_write = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json=write_body,
            headers=CSRF,
        )
        assert duplicate_write.status_code == 200 or duplicate_write.status_code == 202
        assert duplicate_write.json()["task"]["id"] == write_created.json()["task"]["id"]

        with database.session_factory() as session:
            tasks = list(session.scalars(select(ExecutorTask)))
            assert len(tasks) == 2
            assert all("proxyLink" not in str(task.payload_envelope) for task in tasks)


def test_offline_revision_conflict_revoke_and_sensitive_config_are_blocked(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(device_client, create_pairing_code(web_client))
        executor_id = str(paired["executorId"])
        credential = str(paired["deviceCredential"])
        heartbeat(device_client, credential, revision=REVISION_A)

        conflict = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json={
                "expectedRevision": REVISION_B,
                "config": {"concurrency": 2},
            },
            headers=CSRF,
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "config_revision_conflict"

        sensitive = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json={
                "expectedRevision": REVISION_A,
                "config": {"proxyLink": "https://secret.invalid"},
            },
            headers=CSRF,
        )
        assert sensitive.status_code == 422
        nested_sensitive = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json={
                "expectedRevision": REVISION_A,
                "config": {"purchaseTags": {"password": "must-not-queue"}},
            },
            headers=CSRF,
        )
        assert nested_sensitive.status_code == 422
        for legacy_config in (
            {"purchaseSite": "MX"},
            {"purchaseTags": {"MX": "MX采购"}},
            {"importBuyerPlan": "1:新刚"},
            {"serverPort": 8765},
            {"hiddenQueryColumns": ["envName"]},
        ):
            legacy_write = web_client.put(
                f"/v1/executors/{executor_id}/config",
                json={
                    "expectedRevision": REVISION_A,
                    "config": legacy_config,
                },
                headers=CSRF,
            )
            assert legacy_write.status_code == 422

        with database.session_factory() as session:
            executor = session.scalar(select(LocalExecutor))
            assert executor is not None
            executor.last_seen_at = datetime.now(UTC) - timedelta(minutes=5)
            session.commit()
        offline = web_client.post(
            f"/v1/executors/{executor_id}/config/read",
            json={},
            headers=CSRF,
        )
        assert offline.status_code == 409
        assert offline.json()["detail"]["code"] == "executor_offline"

        heartbeat(device_client, credential, revision=REVISION_A)
        revoked = web_client.post(
            f"/v1/executors/{executor_id}/revoke",
            json={},
            headers=CSRF,
        )
        assert revoked.status_code == 200
        assert revoked.json()["executor"]["connectivity"] == "revoked"
        after_revoke = heartbeat_response = device_client.post(
            "/v1/executor-channel/poll",
            json={
                "waitSeconds": 0,
                "configRevision": REVISION_A,
                "hubStatus": "ready",
                "clientVersion": "0.12.5",
                "capabilities": ["config.read.v1", "config.write.v1"],
            },
            headers=device_headers(credential),
        )
        assert after_revoke.status_code == 401
        assert heartbeat_response.json()["detail"]["code"] == "executor_revoked"


def test_web_device_access_is_tenant_isolated(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client:
        login(web_client)
        with database.session_factory() as session:
            other_tenant = Tenant(
                feishu_tenant_key="tenant_other",
                name="Other tenant",
                status="active",
            )
            session.add(other_tenant)
            session.flush()
            other_user = User(
                tenant_id=other_tenant.id,
                feishu_open_id="ou_other",
                display_name="Other user",
                status="active",
            )
            session.add(other_user)
            session.flush()
            other_executor = LocalExecutor(
                tenant_id=other_tenant.id,
                owner_user_id=other_user.id,
                display_name="Other device",
                platform="windows",
                architecture="x86_64",
                client_version="0.12.5",
                protocol_version=1,
                capabilities=["config.read.v1"],
                credential_digest=hash_token("other-device-credential-" + "x" * 40),
                status="active",
                hub_status="unknown",
            )
            session.add(other_executor)
            session.commit()
            other_id = str(other_executor.id)

        response = web_client.get(
            f"/v1/executors/{other_id}/runtime-summary"
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "executor_not_found"
        listed = web_client.get("/v1/executors")
        assert listed.status_code == 200
        assert listed.json()["items"] == []


def test_web_device_and_task_access_is_owner_isolated_within_tenant(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client:
        login(web_client)
        with database.session_factory() as session:
            tenant = session.scalar(
                select(Tenant).where(Tenant.feishu_tenant_key == "tenant_allowed")
            )
            assert tenant is not None
            other_user = User(
                tenant_id=tenant.id,
                feishu_open_id="ou_same_tenant_other",
                display_name="Same tenant other user",
                status="active",
            )
            session.add(other_user)
            session.flush()
            other_executor = LocalExecutor(
                tenant_id=tenant.id,
                owner_user_id=other_user.id,
                display_name="Other member device",
                platform="windows",
                architecture="x86_64",
                client_version="0.12.7",
                protocol_version=1,
                capabilities=["config.read.v1", "config.write.v1"],
                credential_digest=hash_token(
                    "same-tenant-other-device-credential-" + "x" * 40
                ),
                status="active",
                hub_status="ready",
                last_seen_at=datetime.now(UTC),
            )
            session.add(other_executor)
            session.flush()
            other_task = ExecutorTask(
                tenant_id=tenant.id,
                executor_id=other_executor.id,
                task_type="config.read.v1",
                idempotency_key="same-tenant-other-task",
                payload_envelope={},
                created_by_user_id=other_user.id,
            )
            session.add(other_task)
            session.commit()
            other_executor_id = str(other_executor.id)
            other_task_id = str(other_task.id)

        listed = web_client.get("/v1/executors")
        assert listed.status_code == 200
        assert listed.json()["items"] == []

        protected_requests = (
            web_client.get(
                f"/v1/executors/{other_executor_id}/runtime-summary"
            ),
            web_client.post(
                f"/v1/executors/{other_executor_id}/config/read",
                json={},
                headers=CSRF,
            ),
            web_client.put(
                f"/v1/executors/{other_executor_id}/config",
                json={
                    "expectedRevision": REVISION_A,
                    "config": {"concurrency": 2},
                },
                headers=CSRF,
            ),
            web_client.post(
                f"/v1/executors/{other_executor_id}/revoke",
                json={},
                headers=CSRF,
            ),
        )
        for response in protected_requests:
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "executor_not_found"

        task_status = web_client.get(f"/v1/executor-tasks/{other_task_id}")
        assert task_status.status_code == 404
        assert task_status.json()["detail"]["code"] == "executor_task_not_found"
        task_cancel = web_client.post(
            f"/v1/executor-tasks/{other_task_id}/cancel",
            json={},
            headers=CSRF,
        )
        assert task_cancel.status_code == 404
        assert task_cancel.json()["detail"]["code"] == "executor_task_not_found"


def test_started_config_write_becomes_uncertain_after_lease_expiry(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)

    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(device_client, create_pairing_code(web_client))
        executor_id = str(paired["executorId"])
        credential = str(paired["deviceCredential"])
        heartbeat(device_client, credential, revision=REVISION_A)
        created = web_client.put(
            f"/v1/executors/{executor_id}/config",
            json={
                "expectedRevision": REVISION_A,
                "config": {"concurrency": 3},
            },
            headers=CSRF,
        )
        assert created.status_code == 202
        task_id = created.json()["task"]["id"]
        lease = heartbeat(device_client, credential, revision=REVISION_A)["task"]
        started = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/start",
            json={"leaseToken": lease["leaseToken"]},
            headers=device_headers(credential),
        )
        assert started.status_code == 200

        with database.session_factory() as session:
            task = session.get(ExecutorTask, uuid.UUID(task_id))
            assert task is not None
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        assert heartbeat(device_client, credential, revision=REVISION_A)["task"] is None
        status_response = web_client.get(f"/v1/executor-tasks/{task_id}")
        assert status_response.status_code == 200
        task_payload = status_response.json()["task"]
        assert task_payload["status"] == "uncertain"
        assert task_payload["resultCode"] == "lease_expired_after_start"
