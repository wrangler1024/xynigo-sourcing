"""Lifecycle of the shared short-lived environment-plan quota.

The quota is counted per organization while plans belong to one user, so a run
of retry uploads used to lock every operator out for up to the full TTL.  These
cases pin the two escape hatches: an explicit release, and the automatic
supersede of a user's earlier plan for the same site and group.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_auth_flow import build_test_app
from test_environment_plan_cloud import login, workbook_base64
from xynigo_auth.models import EnvironmentAccountPlan


CSRF = {"X-Xynigo-Web-CSRF": "same-origin"}
GROUPS = ["合成一", "合成二", "合成三", "合成四", "合成五"]


def parse(
    client: TestClient,
    *,
    key: str,
    marker: str,
    group: str = GROUPS[0],
    status: int = 201,
) -> dict:
    response = client.post(
        "/v1/environment-plans/parse",
        json={
            "idempotencyKey": key,
            "filename": "synthetic-buyers.xlsx",
            "contentBase64": workbook_base64(marker=marker),
            "site": "MX",
            "environmentGroup": group,
        },
        headers=CSRF,
    )
    assert response.status_code == status, response.text
    return response.json()


def active_plans(database) -> int:
    with database.session_factory() as session:
        return int(
            session.scalar(
                select(func.count(EnvironmentAccountPlan.id)).where(
                    EnvironmentAccountPlan.status == "parsed"
                )
            )
            or 0
        )


def test_release_frees_a_shared_slot_and_is_idempotent(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        plans = [
            parse(client, key=f"environment-quota-000{index + 1}",
                  marker=f"quota-{index}", group=GROUPS[index])
            for index in range(5)
        ]
        assert active_plans(database) == 5

        blocked = client.post(
            "/v1/environment-plans/parse",
            json={
                "idempotencyKey": "environment-quota-0006",
                "filename": "synthetic-buyers.xlsx",
                "contentBase64": workbook_base64(marker="quota-6"),
                "site": "MX",
                "environmentGroup": GROUPS[0],
            },
            headers=CSRF,
        )
        assert blocked.status_code == 429, blocked.text
        assert blocked.headers.get("Retry-After")
        detail = blocked.json()["detail"]
        assert detail["code"] == "environment_plan_limit"
        assert "约" in detail["message"]
        diagnostics = detail["diagnostics"]
        assert diagnostics["limit"] == 5
        assert diagnostics["activePlans"] == 5
        assert diagnostics["retryAfterSeconds"] >= 1
        assert diagnostics["nextExpiryAt"]

        released = client.post(
            f"/v1/environment-plans/{plans[0]['cloudPlanId']}/release",
            json={},
            headers=CSRF,
        )
        assert released.status_code == 200, released.text
        assert released.json()["released"] is True
        assert released.json()["releasedBy"] == "user"
        assert active_plans(database) == 4

        retried = parse(
            client, key="environment-quota-0007", marker="quota-7",
            group=GROUPS[0],
        )
        assert retried["reused"] is False

        repeated = client.post(
            f"/v1/environment-plans/{plans[0]['cloudPlanId']}/release",
            json={},
            headers=CSRF,
        )
        assert repeated.status_code == 200
        assert repeated.json()["released"] is False
        assert repeated.json()["alreadyReleased"] is True
        assert repeated.json()["releasedBy"] == "user"

        missing = client.post(
            f"/v1/environment-plans/{uuid.uuid4()}/release",
            json={},
            headers=CSRF,
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "environment_plan_not_found"


def test_new_upload_supersedes_the_earlier_plan_for_the_same_group(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    service = app.state.environment_plan_service
    with TestClient(app) as client:
        login(client)
        first = parse(client, key="environment-supersede-0001", marker="super-1")
        second = parse(client, key="environment-supersede-0002", marker="super-2")
        assert second["cloudPlanId"] != first["cloudPlanId"]
        assert active_plans(database) == 1

        with database.session_factory() as session:
            superseded = session.get(
                EnvironmentAccountPlan, uuid.UUID(first["cloudPlanId"])
            )
            assert superseded is not None
            assert superseded.status == "expired"
            assert superseded.encrypted_payload is None
            assert superseded.preview_summary["releasedBy"] == "superseded"
            tenant_id = superseded.tenant_id
            user_id = superseded.created_by_user_id
            try:
                service.load_for_execution(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=user_id,
                    cloud_plan_id=first["cloudPlanId"],
                    site="MX",
                    environment_group=GROUPS[0],
                    total_count=1,
                )
            except Exception as exc:  # noqa: BLE001 - assert the stable code
                assert getattr(exc, "code", "") == "environment_plan_superseded"
            else:  # pragma: no cover - the plan must not load
                raise AssertionError("superseded plan still loads")

        reused = parse(
            client, key="environment-supersede-0003", marker="super-2"
        )
        assert reused["reused"] is True
        assert reused["cloudPlanId"] == second["cloudPlanId"]
        assert active_plans(database) == 1


def test_release_does_not_count_as_active_but_keeps_the_audit_row(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        plan = parse(client, key="environment-release-audit-0001", marker="audit")
        released = client.post(
            f"/v1/environment-plans/{plan['cloudPlanId']}/release",
            json={},
            headers=CSRF,
        )
        assert released.status_code == 200
        assert active_plans(database) == 0
        with database.session_factory() as session:
            record = session.get(
                EnvironmentAccountPlan, uuid.UUID(plan["cloudPlanId"])
            )
            assert record is not None
            assert record.encrypted_payload is None
            assert record.preview_summary["releasedBy"] == "user"

        latest = client.get(
            "/v1/environment-plans/latest",
            params={"site": "MX", "environmentGroup": GROUPS[0]},
        )
        assert latest.status_code == 200
        assert latest.json()["plan"] is None
