from __future__ import annotations

from sqlalchemy import select

from test_purchase_api import authenticated_client
from xynigo_auth.buyer_account_sync import buyer_account_fields
from xynigo_auth.buyer_credential_crypto import BuyerCredentialCipher
from xynigo_auth.models import (
    AuditEvent,
    BuyerAccount,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)


def snapshot_payload(snapshot_key: str = "synthetic-buyers-001") -> dict[str, object]:
    return {
        "source": "legacy_feishu_migration",
        "snapshotKey": snapshot_key,
        "accounts": [
            {
                "accountRef": "sha256-buyer-us-0001",
                "displayLabel": "u***1@example.test",
                "site": "US",
                "availabilityStatus": "available",
                "credentialStatus": "ready",
                "sourceStatus": "已验证",
                "hubEnvironmentRef": "hub-us-0001",
                "hubEnvironmentName": "US-PUR-0001",
                "operatorLabel": "采购员甲",
                "sourceUpdatedAt": "2026-08-26T09:00:00+08:00",
                "credentials": {
                    "accountIdentifier": "synthetic-buyer-1@example.test",
                    "phoneNumber": "+1-555-0101",
                    "password": "synthetic-password-1",
                    "cookie": "synthetic-cookie-1",
                    "verificationKey": "synthetic-otp-key-1",
                    "verificationKeyLink": "https://example.test/otp/1",
                    "loginLink": "https://example.test/login/1",
                },
                "businessProfile": {
                    "bindingTime": "2026-08-26T09:01:00+08:00",
                    "bindingEnvironment": "US-PUR-0001",
                    "sourcePurchaseOrderNo": "SYNTHETIC-ORDER-1",
                    "environmentSequence": 1,
                    "cumulativeOrderCount": 2,
                    "sourceOperators": ["合成操作员"],
                },
            },
            {
                "accountRef": "sha256-buyer-mx-0002",
                "displayLabel": "m***2@example.test",
                "site": "MX",
                "availabilityStatus": "available",
                "credentialStatus": "unverified",
                "sourceStatus": "未验证",
            },
        ],
    }


def test_buyer_account_snapshot_and_list_are_safe_idempotent_and_filterable(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    first = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=snapshot_payload(),
        headers=headers,
    )
    assert first.status_code == 200
    assert first.json()["data"] == {
        **first.json()["data"],
        "receivedCount": 2,
        "createdCount": 2,
        "updatedCount": 0,
        "unchangedCount": 0,
        "protectedCount": 0,
    }

    repeated = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=snapshot_payload(),
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"]["createdCount"] == 0
    assert repeated.json()["data"]["updatedCount"] == 0
    assert repeated.json()["data"]["unchangedCount"] == 2

    listed = client.get(
        "/v1/resources/buyer-accounts",
        params={"site": "US", "selectableOnly": True},
        headers=headers,
    )
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert data["total"] == 1
    assert data["counts"]["total"] == 1
    assert len(data["rows"]) == 1
    row = data["rows"][0]
    assert row["accountRef"] == "sha256-buyer-us-0001"
    assert row["displayLabel"] == "u***1@example.test"
    assert row["selectable"] is True
    assert row["sourceAvailabilityStatus"] == "available"
    assert row["hubEnvironment"] == {
        "ref": "hub-us-0001",
        "name": "US-PUR-0001",
    }
    serialized = str(listed.json()).casefold()
    assert "password" not in serialized
    assert "cookie" not in serialized
    assert "code_api" not in serialized

    complete = client.get(
        "/v1/resources/buyer-accounts",
        params={"site": "US", "includeCredentials": True},
        headers=headers,
    )
    assert complete.status_code == 200
    complete_row = complete.json()["data"]["rows"][0]
    assert complete_row["credentials"] == {
        "accountIdentifier": "synthetic-buyer-1@example.test",
        "phoneNumber": "+1-555-0101",
        "password": "synthetic-password-1",
        "cookie": "synthetic-cookie-1",
        "verificationKey": "synthetic-otp-key-1",
        "verificationKeyLink": "https://example.test/otp/1",
        "loginLink": "https://example.test/login/1",
    }
    assert complete_row["businessProfile"]["sourcePurchaseOrderNo"] == (
        "SYNTHETIC-ORDER-1"
    )

    with database.session_factory() as session:
        stored = list(session.scalars(select(BuyerAccount).order_by(BuyerAccount.site)))
        assert len(stored) == 2
        assert {item.credential_status for item in stored} == {"ready", "unverified"}
        assert {item.source for item in stored} == {"legacy_feishu_migration"}
        assert {item.feishu_sync_status for item in stored} == {"pending"}
        encrypted = next(item for item in stored if item.site == "US")
        assert encrypted.credentials_ciphertext
        assert "synthetic-password-1" not in encrypted.credentials_ciphertext
        assert "synthetic-cookie-1" not in encrypted.credentials_ciphertext
        fields = buyer_account_fields(
            encrypted,
            BuyerCredentialCipher(
                "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
            ),
        )
        assert fields["账号标签"] == "synthetic-buyer-1@example.test"
        assert fields["密码"] == "synthetic-password-1"
        assert fields["接码Key链接"] == "https://example.test/otp/1"
        audits = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "resource.buyer_account.snapshot_sync"
                )
            )
        )
        assert len(audits) == 2
        assert all("accountRef" not in str(item.details) for item in audits)
        assert all("displayLabel" not in str(item.details) for item in audits)
    client.close()


def test_buyer_account_snapshot_rejects_unmasked_display_labels_invalid_fields_and_duplicates(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    full_account = snapshot_payload("synthetic-buyers-full-account")
    full_account["accounts"][0]["displayLabel"] = "buyer@example.test"  # type: ignore[index]
    rejected_full = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=full_account,
        headers=headers,
    )
    assert rejected_full.status_code == 422
    assert "buyer@example.test" not in rejected_full.text

    invalid_field = snapshot_payload("synthetic-buyers-invalid-field")
    invalid_field["accounts"][0]["rawSecret"] = "must-not-enter-api"  # type: ignore[index]
    rejected_credential = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=invalid_field,
        headers=headers,
    )
    assert rejected_credential.status_code == 422
    assert "must-not-enter-api" not in rejected_credential.text

    duplicate = snapshot_payload("synthetic-buyers-duplicate")
    duplicate["accounts"][1]["accountRef"] = duplicate["accounts"][0][  # type: ignore[index]
        "accountRef"
    ]
    rejected_duplicate = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=duplicate,
        headers=headers,
    )
    assert rejected_duplicate.status_code == 422
    with database.session_factory() as session:
        assert session.scalar(select(BuyerAccount)) is None
    client.close()


def test_buyer_account_credentials_require_the_additional_permission(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)
    created = client.put(
        "/v1/resources/buyer-accounts/snapshot",
        json=snapshot_payload("synthetic-credential-permission"),
        headers=headers,
    )
    assert created.status_code == 200

    with database.session_factory() as session:
        user = session.scalar(select(User))
        read_permission = session.scalar(
            select(Permission).where(
                Permission.code == "resource.buyer.read"
            )
        )
        assert user is not None and read_permission is not None
        metadata_role = Role(
            tenant_id=user.tenant_id,
            code="buyer_metadata_only",
            name="买家号基础资料测试角色",
        )
        session.add(metadata_role)
        session.flush()
        session.query(UserRole).filter(UserRole.user_id == user.id).delete()
        session.add(UserRole(user_id=user.id, role_id=metadata_role.id))
        session.add(
            RolePermission(
                role_id=metadata_role.id,
                permission_id=read_permission.id,
            )
        )
        session.commit()

    metadata_only = client.get("/v1/resources/buyer-accounts", headers=headers)
    assert metadata_only.status_code == 200
    assert "credentials" not in metadata_only.json()["data"]["rows"][0]

    denied = client.get(
        "/v1/resources/buyer-accounts",
        params={"includeCredentials": True},
        headers=headers,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"
    assert "synthetic-password-1" not in denied.text
    client.close()


def test_buyer_account_import_preflight_checks_database_refs_and_safe_order_hashes(
    tmp_path,
) -> None:
    client, database, headers = authenticated_client(tmp_path)
    payload = snapshot_payload("vendor-import-safe-0001")
    payload["source"] = "vendor_import"
    payload["accounts"] = [payload["accounts"][0]]  # type: ignore[index]
    payload["accounts"][0].update(  # type: ignore[index]
        {
            "sourceVendorLabel": "测试号商",
            "sourceBatchRef": "batch-safe-001",
            "sourcePurchaseDate": "2026-08-26",
            "sourceOrderRef": "sha256:order-safe-0001",
        }
    )
    created = client.put(
        "/v1/resources/buyer-accounts/snapshot", json=payload, headers=headers
    )
    assert created.status_code == 200

    preflight = client.post(
        "/v1/resources/buyer-accounts/preflight",
        json={
            "items": [
                {
                    "accountRef": "sha256-buyer-us-0001",
                    "sourceOrderRef": "sha256:order-new-safe-0002",
                },
                {
                    "accountRef": "sha256-buyer-us-new2",
                    "sourceOrderRef": "sha256:order-safe-0001",
                },
            ]
        },
        headers=headers,
    )
    assert preflight.status_code == 200
    data = preflight.json()["data"]
    assert data["ready"] is False
    assert data["conflictCount"] == 2
    assert data["conflicts"][0]["accountExists"] is True
    assert data["conflicts"][1]["sourceOrderExists"] is True

    listed = client.get("/v1/resources/buyer-accounts", headers=headers)
    row = listed.json()["data"]["rows"][0]
    assert row["sourceVendorLabel"] == "测试号商"
    assert row["sourceBatchRef"] == "batch-safe-001"
    assert "sourceOrderRef" not in row
    client.close()
