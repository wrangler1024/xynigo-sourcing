from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
import threading
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from test_auth_flow import build_test_app, start_login
import xynigo_auth.environment_plan_service as environment_plan_module
from xynigo_auth.models import (
    EnvironmentAccountPlan,
    EnvironmentAccountPlanRequest,
    Tenant,
    User,
)
from xynigo_auth.operation_contract import EnvironmentCreationRunCreateBody


CSRF = {"X-Xynigo-Web-CSRF": "same-origin"}


def login(client: TestClient) -> None:
    state, _challenge = start_login(client)
    response = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303


def workbook_base64(*, marker: str = "a", cookie_site: str | None = None) -> str:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["邮箱账号", "密码", "接码Key链接", "Cookie"])
    cookie = {"name": "session", "value": f"cookie-{marker}"}
    if cookie_site == "MX":
        cookie["domain"] = ".shein.com.mx"
    elif cookie_site == "US":
        cookie["domain"] = ".us.shein.com"
    sheet.append([
        f"buyer-{marker}@example.test",
        f"password-{marker}",
        f"https://vendor.example/api?orderNo={marker.encode().hex()}",
        json.dumps([cookie]),
    ])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return base64.b64encode(output.getvalue()).decode("ascii")


def parse_request(
    client: TestClient,
    *,
    key: str,
    content: str,
    site: str = "MX",
    group: str = "MX采购",
):
    return client.post(
        "/v1/environment-plans/parse",
        headers=CSRF,
        json={
            "idempotencyKey": key,
            "filename": "buyers.xlsx",
            "contentBase64": content,
            "site": site,
            "environmentGroup": group,
        },
    )


def test_legacy_plan_ref_is_accepted_but_normalized_to_cloud_plan_id() -> None:
    body = EnvironmentCreationRunCreateBody.model_validate({
        "idempotencyKey": "environment-contract-0001",
        "executorId": "22222222-2222-4222-8222-222222222222",
        "mode": "bound",
        "site": "MX",
        "purchaseDate": "20260901",
        "environmentGroup": "MX采购",
        "planRef": "11111111-1111-4111-8111-111111111111",
        "totalCount": 1,
        "assignments": [{"purchaserLabel": "新刚", "count": 1}],
    })
    assert body.cloudPlanId == "11111111-1111-4111-8111-111111111111"
    dumped = body.model_dump(mode="json")
    assert dumped["cloudPlanId"] == body.cloudPlanId
    assert "planRef" not in dumped


def test_sequential_duplicate_reuses_one_encrypted_plan_and_not_the_limit(
    tmp_path,
) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    content = workbook_base64(marker="same")
    with patch.object(
        environment_plan_module,
        "parse_vendor_workbook",
        wraps=environment_plan_module.parse_vendor_workbook,
    ) as parser:
        with TestClient(app) as client:
            login(client)
            first = parse_request(
                client, key="environment-dedupe-0001", content=content
            )
            assert first.status_code == 201, first.text
            first_data = first.json()
            assert first_data["reused"] is False
            assert first_data["cloudPlanId"]
            assert "planId" not in first_data

            exact_replay = parse_request(
                client, key="environment-dedupe-0001", content=content
            )
            assert exact_replay.status_code == 201
            assert exact_replay.json() == first_data

            for index in range(2, 9):
                repeated = parse_request(
                    client,
                    key=f"environment-dedupe-{index:04d}",
                    content=content,
                )
                assert repeated.status_code == 201, repeated.text
                assert repeated.json()["reused"] is True
                assert repeated.json()["cloudPlanId"] == first_data["cloudPlanId"]

            conflict = parse_request(
                client,
                key="environment-dedupe-0001",
                content=workbook_base64(marker="different"),
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == (
                "environment_plan_idempotency_conflict"
            )
        assert parser.call_count == 1

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EnvironmentAccountPlan.id))) == 1
        assert session.scalar(
            select(func.count(EnvironmentAccountPlanRequest.id))
        ) == 8
        record = session.scalar(select(EnvironmentAccountPlan))
        assert record is not None and record.encrypted_payload
        for secret in (
            b"buyer-same@example.test",
            b"password-same",
            b"cookie-same",
            b"vendor.example",
        ):
            assert secret not in record.encrypted_payload
        serialized_preview = json.dumps(record.preview_summary, ensure_ascii=False)
        assert "buyer-same@example.test" not in serialized_preview
        assert "password-same" not in serialized_preview
        assert "cookie-same" not in serialized_preview
        assert "vendor.example" not in serialized_preview


def test_same_file_with_different_site_or_group_creates_new_plans(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    content = workbook_base64(marker="context")
    with TestClient(app) as client:
        login(client)
        mx = parse_request(
            client, key="environment-context-0001", content=content
        ).json()
        us_response = parse_request(
            client,
            key="environment-context-0002",
            content=content,
            site="US",
            group="美国采购",
        )
        assert us_response.status_code == 201, us_response.text
        us = us_response.json()
        other_group = parse_request(
            client,
            key="environment-context-0003",
            content=content,
            group="MX采购二组",
        ).json()
        other_content = parse_request(
            client,
            key="environment-context-0004",
            content=workbook_base64(marker="context-changed"),
        ).json()

        assert len({
            mx["cloudPlanId"],
            us["cloudPlanId"],
            other_group["cloudPlanId"],
            other_content["cloudPlanId"],
        }) == 4
        assert mx["reused"] is False
        assert us["reused"] is False
        assert other_group["reused"] is False
        assert other_content["reused"] is False

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EnvironmentAccountPlan.id))) == 4


def test_submitted_and_expired_plans_are_not_reused(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    content = workbook_base64(marker="lifecycle")
    with TestClient(app) as client:
        login(client)
        first = parse_request(
            client, key="environment-lifecycle-0001", content=content
        ).json()
        first_id = uuid.UUID(first["cloudPlanId"])
        with database.session_factory() as session:
            record = session.get(EnvironmentAccountPlan, first_id)
            assert record is not None
            record.status = "submitted"
            record.submitted_at = datetime.now(UTC)
            record.encrypted_payload = None
            session.commit()

        replay = parse_request(
            client, key="environment-lifecycle-0001", content=content
        )
        assert replay.status_code == 201
        assert replay.json()["cloudPlanId"] == first["cloudPlanId"]
        assert replay.json()["reused"] is False

        second = parse_request(
            client, key="environment-lifecycle-0002", content=content
        ).json()
        assert second["cloudPlanId"] != first["cloudPlanId"]
        assert second["reused"] is False

        with database.session_factory() as session:
            record = session.get(
                EnvironmentAccountPlan, uuid.UUID(second["cloudPlanId"])
            )
            assert record is not None
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        third = parse_request(
            client, key="environment-lifecycle-0003", content=content
        ).json()
        assert third["cloudPlanId"] not in {
            first["cloudPlanId"],
            second["cloudPlanId"],
        }
        assert third["reused"] is False


def test_concurrent_duplicate_uploads_return_one_cloud_plan(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    content = workbook_base64(marker="concurrent")
    barrier = threading.Barrier(2)
    with TestClient(app) as client:
        login(client)

        def upload(index: int):
            barrier.wait(timeout=5)
            return parse_request(
                client,
                key=f"environment-concurrent-{index:04d}",
                content=content,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(upload, (1, 2)))

        assert [response.status_code for response in responses] == [201, 201]
        cloud_plan_ids = {
            response.json()["cloudPlanId"] for response in responses
        }
        assert len(cloud_plan_ids) == 1
        assert sorted(response.json()["reused"] for response in responses) == [
            False,
            True,
        ]

    with database.session_factory() as session:
        assert session.scalar(select(func.count(EnvironmentAccountPlan.id))) == 1
        assert session.scalar(
            select(func.count(EnvironmentAccountPlanRequest.id))
        ) == 2


def test_plan_reuse_is_isolated_by_tenant_and_user(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    service = app.state.environment_plan_service
    content = workbook_base64(marker="isolation")
    with TestClient(app) as client:
        login(client)

    with database.session_factory() as session:
        first_user = session.scalar(select(User).where(User.feishu_open_id == "ou_admin"))
        assert first_user is not None
        first_tenant = session.get(Tenant, first_user.tenant_id)
        assert first_tenant is not None
        second_user = User(
            tenant_id=first_tenant.id,
            feishu_open_id="ou_second",
            display_name="第二用户",
            status="active",
        )
        second_tenant = Tenant(
            feishu_tenant_key="tenant_other",
            name="另一租户",
            status="active",
        )
        session.add_all([second_user, second_tenant])
        session.flush()
        third_user = User(
            tenant_id=second_tenant.id,
            feishu_open_id="ou_third",
            display_name="第三用户",
            status="active",
        )
        session.add(third_user)
        session.commit()
        identities = (
            (first_tenant.id, first_user.id, "environment-isolation-0001"),
            (first_tenant.id, second_user.id, "environment-isolation-0002"),
            (second_tenant.id, third_user.id, "environment-isolation-0003"),
        )

    cloud_plan_ids = []
    for tenant_id, user_id, key in identities:
        with database.session_factory() as session:
            result = service.parse(
                session,
                tenant_id=tenant_id,
                actor_user_id=user_id,
                idempotency_key=key,
                filename="buyers.xlsx",
                content_base64=content,
                site="MX",
                environment_group="MX采购",
            )
            session.commit()
            cloud_plan_ids.append(result["cloudPlanId"])

    assert len(set(cloud_plan_ids)) == 3
    with database.session_factory() as session:
        assert session.scalar(select(func.count(EnvironmentAccountPlan.id))) == 3
        latest = service.latest(
            session,
            tenant_id=identities[0][0],
            actor_user_id=identities[0][1],
            site="MX",
            environment_group="MX采购",
        )
        assert latest is not None
        assert latest["cloudPlanId"] == cloud_plan_ids[0]
