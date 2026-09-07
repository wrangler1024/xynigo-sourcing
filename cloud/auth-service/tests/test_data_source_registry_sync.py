from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_executor_channel import (
    CSRF,
    create_pairing_code,
    login,
    pair,
)
from xynigo_auth.models import AuditEvent, TenantDataSourceRegistry, User


def test_admin_publishes_encrypted_registry_and_another_device_reads_it(
    tmp_path,
) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        first_pair = pair(device_client, create_pairing_code(web_client))
        credential = str(first_pair["deviceCredential"])
        headers = {
            **CSRF,
            "X-Xynigo-Executor-Credential": credential,
        }
        with database.session_factory() as session:
            user = session.scalar(select(User).where(User.feishu_open_id == "ou_admin"))
            assert user is not None
            member_id = str(user.id)

        missing_device = web_client.get("/v1/assistant/data-source-registry")
        assert missing_device.status_code == 401
        empty = web_client.get(
            "/v1/assistant/data-source-registry", headers=headers
        )
        assert empty.status_code == 200
        assert empty.json()["configured"] is False

        team_id = "ds_" + "1" * 24
        personal_id = "ds_" + "2" * 24
        registry = {
            "schemaVersion": 1,
            "dataSources": [
                {
                    "id": team_id,
                    "scope": "team",
                    "ownerMemberId": "",
                    "label": "采购团队默认表",
                    "spreadsheetToken": "SpreadsheetCloudTeam123",
                    "sheetId": "sheet_cloud_team",
                    "cellRange": "A1:AQ",
                    "sheetName": "采购执行",
                    "enabled": True,
                    "migrationState": "ready",
                },
                {
                    "id": personal_id,
                    "scope": "personal",
                    "ownerMemberId": member_id,
                    "label": "管理员个人速填表",
                    "spreadsheetToken": "SpreadsheetCloudPersonal123",
                    "sheetId": "sheet_cloud_personal",
                    "cellRange": "A1:H",
                    "sheetName": "个人速填",
                    "enabled": True,
                    "migrationState": "ready",
                },
            ],
            "buyerProfiles": [
                {"memberId": member_id, "defaultDataSourceId": personal_id}
            ],
            "teamDefaultDataSourceId": team_id,
            "environmentBindings": [
                {
                    "memberId": member_id,
                    "containerCode": "must-not-upload",
                    "dataSourceId": team_id,
                }
            ],
        }
        published = web_client.put(
            "/v1/assistant/data-source-registry",
            json={"expectedRevision": 0, "registry": registry},
            headers=headers,
        )
        assert published.status_code == 200, published.text
        payload = published.json()
        assert payload["organizationRevision"] == 1
        assert payload["registry"]["teamDefaultDataSourceId"] == team_id
        assert "environmentBindings" not in payload["registry"]

        with database.session_factory() as session:
            stored = session.scalar(select(TenantDataSourceRegistry))
            assert stored is not None
            assert "SpreadsheetCloudTeam123" not in stored.payload_ciphertext
            assert "SpreadsheetCloudPersonal123" not in stored.payload_ciphertext
            audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action
                        == "assistant.data_source_registry.publish"
                    )
                )
            )
            assert len(audits) == 1
            assert "SpreadsheetCloud" not in str(audits[0].details)
            other = User(
                tenant_id=stored.tenant_id,
                feishu_open_id="ou_other_member",
                display_name="其他采购员",
                status="active",
            )
            session.add(other)
            session.flush()
            member_view = app.state.data_source_registry_service.read(
                session,
                tenant_id=stored.tenant_id,
                user_id=other.id,
                include_all=False,
            )
            assert [
                item["scope"] for item in member_view["registry"]["dataSources"]
            ] == ["team"]
            assert member_view["registry"]["buyerProfiles"] == []
            session.rollback()

        second_pair = pair(device_client, create_pairing_code(web_client))
        second_headers = {
            "X-Xynigo-Web-CSRF": "same-origin",
            "X-Xynigo-Executor-Credential": str(
                second_pair["deviceCredential"]
            ),
        }
        copied = web_client.get(
            "/v1/assistant/data-source-registry", headers=second_headers
        )
        assert copied.status_code == 200
        assert copied.json()["registry"]["dataSources"] == payload["registry"][
            "dataSources"
        ]

        stale = web_client.put(
            "/v1/assistant/data-source-registry",
            json={"expectedRevision": 0, "registry": registry},
            headers=second_headers,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == (
            "data_source_registry_revision_conflict"
        )


def test_registry_rejects_member_from_another_tenant(tmp_path) -> None:
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(device_client, create_pairing_code(web_client))
        headers = {
            **CSRF,
            "X-Xynigo-Executor-Credential": str(paired["deviceCredential"]),
        }
        personal_id = "ds_" + "3" * 24
        response = web_client.put(
            "/v1/assistant/data-source-registry",
            json={
                "expectedRevision": 0,
                "registry": {
                    "schemaVersion": 1,
                    "dataSources": [
                        {
                            "id": personal_id,
                            "scope": "personal",
                            "ownerMemberId": str(uuid.uuid4()),
                            "label": "非法跨租户个人表",
                            "spreadsheetToken": "SpreadsheetCrossTenant123",
                            "sheetId": "sheet_cross_tenant",
                            "cellRange": "A1:H",
                            "sheetName": "个人速填",
                            "enabled": True,
                            "migrationState": "ready",
                        }
                    ],
                    "buyerProfiles": [],
                    "teamDefaultDataSourceId": "",
                },
            },
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == (
            "data_source_registry_member_invalid"
        )
