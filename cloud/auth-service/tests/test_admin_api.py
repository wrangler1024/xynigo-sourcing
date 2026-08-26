from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from xynigo_auth.config import Settings
from xynigo_auth.database import Database
from xynigo_auth.feishu import DirectoryProviderError, FeishuDirectoryUser
from xynigo_auth.main import _ensure_super_admin, create_app, utcnow
from xynigo_auth.models import (
    AuditEvent,
    Base,
    Permission,
    Role,
    RolePermission,
    SessionRecord,
    Tenant,
    User,
)
from xynigo_auth.security import hash_token


ADMIN_TOKEN = "a" * 64
MEMBER_TOKEN = "m" * 64
MEMBER_TOKEN_TWO = "n" * 64
OUTSIDER_TOKEN = "o" * 64


class UnusedOAuthClient:
    def authorization_url(self, *, state: str, code_challenge: str | None) -> str:
        return "https://accounts.example.test/authorize"

    def exchange_code(self, *, code: str, code_verifier: str | None) -> str:
        raise AssertionError("synthetic admin tests do not call OAuth")

    def get_identity(self, user_access_token: str):
        raise AssertionError("synthetic admin tests do not call OAuth")


class SyntheticDirectoryClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.users = {
            "13800138000": FeishuDirectoryUser(
                open_id="ou_invited",
                union_id="on_invited",
                name="受邀成员",
                avatar_url="https://example.test/avatar.png",
                department_ids=("od_department",),
                is_activated=True,
                is_frozen=False,
                is_resigned=False,
                is_exited=False,
                is_unjoin=False,
            ),
            "13800138001": FeishuDirectoryUser(
                open_id="ou_resigned",
                union_id=None,
                name="离职成员",
                avatar_url=None,
                department_ids=(),
                is_activated=False,
                is_frozen=False,
                is_resigned=True,
                is_exited=False,
                is_unjoin=False,
            ),
            "13800138002": FeishuDirectoryUser(
                open_id="ou_invited_by_member_manager",
                union_id=None,
                name="成员管理员邀请对象",
                avatar_url=None,
                department_ids=(),
                is_activated=True,
                is_frozen=False,
                is_resigned=False,
                is_exited=False,
                is_unjoin=False,
            ),
        }
        self.error: DirectoryProviderError | None = None

    def find_user_by_mobile(self, mobile: str) -> FeishuDirectoryUser | None:
        self.calls.append(mobile)
        if self.error is not None:
            raise self.error
        return self.users.get(mobile)


def build_admin_app(tmp_path, directory_client=None):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'admin.sqlite3'}"
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
    )
    app = create_app(
        settings=settings,
        oauth_client=UnusedOAuthClient(),
        directory_client=directory_client or SyntheticDirectoryClient(),
        database=database,
    )
    now = utcnow()
    with database.session_factory() as session:
        tenant = Tenant(feishu_tenant_key="tenant_allowed", name="合成组织")
        other_tenant = Tenant(feishu_tenant_key="tenant_other", name="其他组织")
        session.add_all((tenant, other_tenant))
        session.flush()

        admin = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_admin",
            display_name="合成管理员",
            status="active",
        )
        pending = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_pending",
            display_name="待审批成员",
            status="pending",
        )
        member = User(
            tenant_id=tenant.id,
            feishu_open_id="ou_member",
            display_name="普通成员",
            status="active",
        )
        outsider = User(
            tenant_id=other_tenant.id,
            feishu_open_id="ou_outsider",
            display_name="其他组织成员",
            status="active",
        )
        session.add_all((admin, pending, member, outsider))
        session.flush()
        _ensure_super_admin(session, tenant=tenant, user=admin)
        outsider_role = Role(
            tenant_id=other_tenant.id,
            code="other_member",
            name="其他组织角色",
            is_system=True,
        )
        session.add(outsider_role)
        session.flush()

        records = (
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
            SessionRecord(
                user_id=member.id,
                token_hash=hash_token(MEMBER_TOKEN_TWO),
                last_seen_at=now,
                expires_at=now + timedelta(hours=8),
            ),
            SessionRecord(
                user_id=outsider.id,
                token_hash=hash_token(OUTSIDER_TOKEN),
                last_seen_at=now,
                expires_at=now + timedelta(hours=8),
            ),
        )
        session.add_all(records)
        session.commit()
        ids = {
            "admin": str(admin.id),
            "pending": str(pending.id),
            "member": str(member.id),
            "outsider": str(outsider.id),
            "outsider_role": str(outsider_role.id),
            "member_session": str(records[1].id),
            "member_session_two": str(records[2].id),
            "outsider_session": str(records[3].id),
        }
    return app, database, ids


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_member_approval_is_tenant_scoped_and_audited(tmp_path) -> None:
    app, database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        pending = client.get(
            "/v1/admin/members",
            params={"status": "pending"},
            headers=admin_headers(),
        )
        assert pending.status_code == 200
        assert [item["id"] for item in pending.json()["members"]] == [ids["pending"]]

        approved = client.post(
            f"/v1/admin/members/{ids['pending']}/approve",
            headers=admin_headers(),
        )
        assert approved.status_code == 200
        assert approved.json()["member"]["status"] == "active"
        assert approved.json()["member"]["roles"] == []

        repeated = client.post(
            f"/v1/admin/members/{ids['pending']}/approve",
            headers=admin_headers(),
        )
        assert repeated.status_code == 409
        assert repeated.json()["detail"]["code"] == "member_status_conflict"

        cross_tenant = client.get(
            f"/v1/admin/members/{ids['outsider']}",
            headers=admin_headers(),
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["detail"]["code"] == "member_not_found"

    with database.session_factory() as session:
        approved_user = session.get(User, uuid.UUID(ids["pending"]))
        assert approved_user is not None and approved_user.status == "active"
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert any(event.action == "admin.member.approve" and event.result == "success" for event in events)
        assert any(event.action == "admin.member.read" and event.result == "denied" for event in events)


def test_mobile_invitation_resolves_then_creates_pending_member_with_roles(tmp_path) -> None:
    directory = SyntheticDirectoryClient()
    app, database, _ids = build_admin_app(tmp_path, directory_client=directory)
    with TestClient(app) as client:
        roles = client.get("/v1/admin/roles", headers=admin_headers()).json()["roles"]
        member_role = next(item for item in roles if item["code"] == "member")

        resolved = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "138 0013 8000"},
            headers=admin_headers(),
        )
        assert resolved.status_code == 200
        assert resolved.json() == {
            "candidate": {
                "name": "受邀成员",
                "avatarUrl": "https://example.test/avatar.png",
                "departmentCount": 1,
                "isActivated": True,
            },
            "existingMember": None,
        }
        assert "mobile" not in str(resolved.json()).casefold()
        assert "open_id" not in str(resolved.json()).casefold()

        invited = client.post(
            "/v1/admin/members/invitations",
            json={"mobile": "13800138000", "roleIds": [member_role["id"]]},
            headers=admin_headers(),
        )
        assert invited.status_code == 201
        member = invited.json()["member"]
        assert member["name"] == "受邀成员"
        assert member["status"] == "pending"
        assert member["lastLoginAt"] is None
        assert [role["code"] for role in member["roles"]] == ["member"]
        assert directory.calls == ["13800138000", "13800138000"]

        duplicate = client.post(
            "/v1/admin/members/invitations",
            json={"mobile": "13800138000", "roleIds": []},
            headers=admin_headers(),
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "member_already_exists"

        resolved_existing = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "13800138000"},
            headers=admin_headers(),
        )
        assert resolved_existing.status_code == 200
        assert resolved_existing.json()["existingMember"]["id"] == member["id"]

    with database.session_factory() as session:
        invited_user = session.scalar(select(User).where(User.feishu_open_id == "ou_invited"))
        assert invited_user is not None and invited_user.status == "pending"
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert any(
            event.action == "admin.member.invitation.resolve" and event.result == "success"
            for event in events
        )
        assert any(
            event.action == "admin.member.invitation.create" and event.result == "success"
            for event in events
        )
        assert all("13800138000" not in str(event.details) for event in events)


def test_mobile_invitation_rejects_missing_ineligible_and_provider_failure(tmp_path) -> None:
    directory = SyntheticDirectoryClient()
    app, database, _ids = build_admin_app(tmp_path, directory_client=directory)
    with TestClient(app) as client:
        invalid = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "123"},
            headers=admin_headers(),
        )
        assert invalid.status_code == 422

        missing = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "+12025550123"},
            headers=admin_headers(),
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "feishu_member_not_found"

        ineligible = client.post(
            "/v1/admin/members/invitations",
            json={"mobile": "13800138001", "roleIds": []},
            headers=admin_headers(),
        )
        assert ineligible.status_code == 409
        assert ineligible.json()["detail"]["code"] == "feishu_member_ineligible"

        directory.error = DirectoryProviderError("batch_get_id", 50000)
        unavailable = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "13800138000"},
            headers=admin_headers(),
        )
        assert unavailable.status_code == 502
        assert unavailable.json()["detail"]["code"] == "feishu_directory_unavailable"

    with database.session_factory() as session:
        assert session.scalar(select(User).where(User.feishu_open_id == "ou_resigned")) is None
        events = list(session.scalars(select(AuditEvent)))
        assert any(
            event.action == "admin.member.invitation.resolve"
            and event.result == "denied"
            and event.details.get("reason") == "feishu_directory_unavailable"
            for event in events
        )


def test_mobile_invitation_reports_missing_feishu_directory_permission(tmp_path) -> None:
    directory = SyntheticDirectoryClient()
    directory.error = DirectoryProviderError("batch_get_id", 99991672)
    app, database, _ids = build_admin_app(tmp_path, directory_client=directory)
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/members/invitations/resolve",
            json={"mobile": "13800138000"},
            headers=admin_headers(),
        )
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "feishu_directory_permission_missing"

    with database.session_factory() as session:
        event = session.scalar(select(AuditEvent))
        assert event is not None
        assert event.details["reason"] == "feishu_directory_permission_missing"


def test_member_manager_can_invite_without_roles_but_cannot_preassign_roles(tmp_path) -> None:
    directory = SyntheticDirectoryClient()
    app, _database, ids = build_admin_app(tmp_path, directory_client=directory)
    with TestClient(app) as client:
        roles = client.get("/v1/admin/roles", headers=admin_headers()).json()["roles"]
        member_role = next(item for item in roles if item["code"] == "member")
        assert client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["system.member.manage"]},
            headers=admin_headers(),
        ).status_code == 200
        assert client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [member_role["id"]]},
            headers=admin_headers(),
        ).status_code == 200

        invited = client.post(
            "/v1/admin/members/invitations",
            json={"mobile": "13800138002", "roleIds": []},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert invited.status_code == 201
        assert invited.json()["member"]["status"] == "pending"
        assert invited.json()["member"]["roles"] == []

        denied_role_assignment = client.post(
            "/v1/admin/members/invitations",
            json={"mobile": "13800138002", "roleIds": [member_role["id"]]},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert denied_role_assignment.status_code == 403
        assert denied_role_assignment.json()["detail"]["code"] == "permission_denied"


def test_role_permissions_and_member_roles_use_server_catalog(tmp_path) -> None:
    app, _database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        roles_response = client.get("/v1/admin/roles", headers=admin_headers())
        assert roles_response.status_code == 200
        roles = roles_response.json()["roles"]
        admin_role = next(item for item in roles if item["code"] == "admin")
        member_role = next(item for item in roles if item["code"] == "member")
        super_admin_role = next(item for item in roles if item["code"] == "super_admin")
        assert admin_role["isSystem"] is True
        assert admin_role["permissionsEditable"] is False
        assert member_role["permissionsEditable"] is True
        assert super_admin_role["permissionsEditable"] is False

        permissions = client.get("/v1/admin/permissions", headers=admin_headers()).json()[
            "permissions"
        ]
        permission_codes = {item["code"] for item in permissions}
        assert "fulfillment.order.read" in permission_codes
        module_access_codes = {
            "workbench.access",
            "procurement.access",
            "operations.access",
            "finance.access",
            "assistant.access",
            "analytics.access",
        }
        assert module_access_codes.issubset(permission_codes)
        assert set(admin_role["permissionCodes"]) == permission_codes - {
            "system.integration.manage",
            "system.lark_connection.manage",
            "resource.ip.credential.manage",
        }
        assert module_access_codes.issubset(set(super_admin_role["permissionCodes"]))
        assert "system.integration.manage" not in admin_role["permissionCodes"]
        assert "system.lark_connection.manage" not in admin_role["permissionCodes"]
        assert "resource.ip.credential.manage" not in admin_role["permissionCodes"]
        assert {
            "resource.store.read",
            "resource.store.configure",
            "resource.store.credential.update",
            "resource.store.clone",
            "resource.ip.read",
            "resource.ip.test",
            "resource.ip.allocate",
        }.issubset(set(admin_role["permissionCodes"]))

        configured = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["fulfillment.order.read"]},
            headers=admin_headers(),
        )
        assert configured.status_code == 200
        assert configured.json()["role"]["permissionCodes"] == ["fulfillment.order.read"]

        unknown_permission = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["frontend.invented.permission"]},
            headers=admin_headers(),
        )
        assert unknown_permission.status_code == 422
        assert unknown_permission.json()["detail"]["code"] == "permission_code_invalid"

        restricted_permission = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["system.integration.manage"]},
            headers=admin_headers(),
        )
        assert restricted_permission.status_code == 403
        assert (
            restricted_permission.json()["detail"]["code"]
            == "super_admin_only_permission"
        )

        immutable = client.put(
            f"/v1/admin/roles/{super_admin_role['id']}/permissions",
            json={"permissionCodes": []},
            headers=admin_headers(),
        )
        assert immutable.status_code == 409
        assert immutable.json()["detail"]["code"] == "system_role_immutable"

        immutable_admin = client.put(
            f"/v1/admin/roles/{admin_role['id']}/permissions",
            json={"permissionCodes": []},
            headers=admin_headers(),
        )
        assert immutable_admin.status_code == 409
        assert immutable_admin.json()["detail"]["code"] == "system_role_immutable"

        assigned = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [member_role["id"]]},
            headers=admin_headers(),
        )
        assert assigned.status_code == 200
        assert [item["code"] for item in assigned.json()["member"]["roles"]] == ["member"]

        me = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert me.status_code == 200
        assert me.json()["permissions"] == ["fulfillment.order.read"]

        denied_admin = client.get(
            "/v1/admin/roles",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert denied_admin.status_code == 403
        assert denied_admin.json()["detail"]["code"] == "permission_denied"

        promoted_role_manager = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["system.role.manage"]},
            headers=admin_headers(),
        )
        assert promoted_role_manager.status_code == 200
        assert client.get(
            "/v1/admin/roles",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        ).status_code == 200

        exceeds_actor = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["system.member.manage", "system.role.manage"]},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert exceeds_actor.status_code == 403
        assert exceeds_actor.json()["detail"]["code"] == "permission_grant_exceeds_actor"

        cannot_assign_super_admin = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [super_admin_role["id"]]},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert cannot_assign_super_admin.status_code == 403
        assert cannot_assign_super_admin.json()["detail"]["code"] == "super_admin_required"

        cross_tenant_role = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [ids["outsider_role"]]},
            headers=admin_headers(),
        )
        assert cross_tenant_role.status_code == 404
        assert cross_tenant_role.json()["detail"]["code"] == "role_not_found"


def test_catalog_upgrade_flushes_new_permissions_before_a_second_sync(tmp_path) -> None:
    app, database, _ids = build_admin_app(tmp_path)
    new_codes = {
        "procurement.request.read",
        "procurement.request.save",
        "procurement.request.submit",
    }
    with database.session_factory() as session:
        permission_ids = list(
            session.scalars(select(Permission.id).where(Permission.code.in_(new_codes)))
        )
        session.execute(
            delete(RolePermission).where(RolePermission.permission_id.in_(permission_ids))
        )
        session.execute(delete(Permission).where(Permission.id.in_(permission_ids)))
        session.commit()

    with TestClient(app) as client:
        permissions_response = client.get(
            "/v1/admin/permissions", headers=admin_headers()
        )
        assert permissions_response.status_code == 200
        permission_codes = {
            item["code"] for item in permissions_response.json()["permissions"]
        }
        assert new_codes.issubset(permission_codes)

        roles_response = client.get("/v1/admin/roles", headers=admin_headers())
        assert roles_response.status_code == 200
        roles = {item["code"]: item for item in roles_response.json()["roles"]}
        assert new_codes.issubset(set(roles["super_admin"]["permissionCodes"]))
        assert new_codes.issubset(set(roles["admin"]["permissionCodes"]))


def test_builtin_admin_has_full_non_cloud_permissions_and_restricted_ceiling(tmp_path) -> None:
    app, _database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        roles = client.get("/v1/admin/roles", headers=admin_headers()).json()["roles"]
        admin_role = next(item for item in roles if item["code"] == "admin")
        member_role = next(item for item in roles if item["code"] == "member")

        assigned = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [admin_role["id"]]},
            headers=admin_headers(),
        )
        assert assigned.status_code == 200
        assert [item["code"] for item in assigned.json()["member"]["roles"]] == ["admin"]

        me = client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert me.status_code == 200
        assert me.json()["roles"] == ["admin"]
        assert "fulfillment.order.read" in me.json()["permissions"]
        assert "resource.environment.create" in me.json()["permissions"]
        assert "system.member.manage" in me.json()["permissions"]
        assert "system.role.manage" in me.json()["permissions"]
        assert "system.integration.manage" not in me.json()["permissions"]
        assert "system.lark_connection.manage" not in me.json()["permissions"]
        assert "resource.store.configure" in me.json()["permissions"]
        assert "resource.ip.test" in me.json()["permissions"]
        assert "resource.ip.allocate" in me.json()["permissions"]
        assert "resource.ip.credential.manage" not in me.json()["permissions"]

        assert client.get(
            "/v1/admin/members",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        ).status_code == 200
        assert client.get(
            "/v1/admin/roles",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        ).status_code == 200

        denied_cloud_permission = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["system.integration.manage"]},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert denied_cloud_permission.status_code == 403
        assert (
            denied_cloud_permission.json()["detail"]["code"]
            == "super_admin_only_permission"
        )

        denied_proxy_credential = client.put(
            f"/v1/admin/roles/{member_role['id']}/permissions",
            json={"permissionCodes": ["resource.ip.credential.manage"]},
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        )
        assert denied_proxy_credential.status_code == 403
        assert (
            denied_proxy_credential.json()["detail"]["code"]
            == "super_admin_only_permission"
        )


def test_custom_role_lifecycle_is_safe_tenant_scoped_and_audited(tmp_path) -> None:
    app, database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/v1/admin/roles",
            json={"name": "  采购   审核员  "},
            headers=admin_headers(),
        )
        assert created.status_code == 201
        role = created.json()["role"]
        assert role["name"] == "采购 审核员"
        assert role["code"].startswith("custom_")
        assert role["isSystem"] is False
        assert role["nameEditable"] is True
        assert role["deletable"] is True
        assert role["assignedMemberCount"] == 0
        assert role["permissionCodes"] == []

        duplicate = client.post(
            "/v1/admin/roles",
            json={"name": "采购 审核员"},
            headers=admin_headers(),
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "role_name_conflict"

        renamed = client.put(
            f"/v1/admin/roles/{role['id']}",
            json={"name": "采购复核员"},
            headers=admin_headers(),
        )
        assert renamed.status_code == 200
        assert renamed.json()["role"]["name"] == "采购复核员"

        roles = client.get("/v1/admin/roles", headers=admin_headers()).json()["roles"]
        system_role = next(item for item in roles if item["code"] == "member")
        immutable_rename = client.put(
            f"/v1/admin/roles/{system_role['id']}",
            json={"name": "不可改名"},
            headers=admin_headers(),
        )
        assert immutable_rename.status_code == 409
        assert immutable_rename.json()["detail"]["code"] == "system_role_immutable"
        immutable_delete = client.delete(
            f"/v1/admin/roles/{system_role['id']}",
            headers=admin_headers(),
        )
        assert immutable_delete.status_code == 409
        assert immutable_delete.json()["detail"]["code"] == "system_role_immutable"

        cross_tenant = client.put(
            f"/v1/admin/roles/{ids['outsider_role']}",
            json={"name": "越权角色"},
            headers=admin_headers(),
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["detail"]["code"] == "role_not_found"

        assigned = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": [role["id"]]},
            headers=admin_headers(),
        )
        assert assigned.status_code == 200
        in_use = client.delete(
            f"/v1/admin/roles/{role['id']}",
            headers=admin_headers(),
        )
        assert in_use.status_code == 409
        assert in_use.json()["detail"]["code"] == "role_in_use"

        unassigned = client.put(
            f"/v1/admin/members/{ids['member']}/roles",
            json={"roleIds": []},
            headers=admin_headers(),
        )
        assert unassigned.status_code == 200
        deleted = client.delete(
            f"/v1/admin/roles/{role['id']}",
            headers=admin_headers(),
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "roleId": role["id"]}
        remaining = client.get("/v1/admin/roles", headers=admin_headers()).json()["roles"]
        assert role["id"] not in {item["id"] for item in remaining}

    with database.session_factory() as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert any(event.action == "admin.role.create" and event.result == "success" for event in events)
        assert any(event.action == "admin.role.update" and event.result == "success" for event in events)
        assert any(event.action == "admin.role.delete" and event.result == "denied" for event in events)
        assert any(event.action == "admin.role.delete" and event.result == "success" for event in events)


def test_session_revoke_disable_and_restore_are_effective(tmp_path) -> None:
    app, database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        sessions = client.get("/v1/admin/sessions", headers=admin_headers())
        assert sessions.status_code == 200
        payload = sessions.json()["sessions"]
        assert len(payload) == 3
        assert all("token" not in str(item).casefold() for item in payload)
        assert sum(bool(item["isCurrent"]) for item in payload) == 1

        revoked = client.post(
            f"/v1/admin/sessions/{ids['member_session']}/revoke",
            headers=admin_headers(),
        )
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] is True
        assert client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN}"},
        ).status_code == 401

        cross_tenant = client.post(
            f"/v1/admin/sessions/{ids['outsider_session']}/revoke",
            headers=admin_headers(),
        )
        assert cross_tenant.status_code == 404
        assert cross_tenant.json()["detail"]["code"] == "session_not_found"

        disabled = client.post(
            f"/v1/admin/members/{ids['member']}/disable",
            headers=admin_headers(),
        )
        assert disabled.status_code == 200
        assert disabled.json()["member"]["status"] == "disabled"
        assert disabled.json()["revokedSessionCount"] == 1
        assert client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {MEMBER_TOKEN_TWO}"},
        ).status_code == 401

        restored = client.post(
            f"/v1/admin/members/{ids['member']}/restore",
            headers=admin_headers(),
        )
        assert restored.status_code == 200
        assert restored.json()["member"]["status"] == "active"
        assert restored.json()["member"]["activeSessionCount"] == 0

        cannot_disable_self = client.post(
            f"/v1/admin/members/{ids['admin']}/disable",
            headers=admin_headers(),
        )
        assert cannot_disable_self.status_code == 409
        assert cannot_disable_self.json()["detail"]["code"] == "cannot_disable_self"

    with database.session_factory() as session:
        member = session.get(User, uuid.UUID(ids["member"]))
        assert member is not None and member.status == "active"
        member_sessions = list(
            session.scalars(select(SessionRecord).where(SessionRecord.user_id == member.id))
        )
        assert all(record.revoked_at is not None for record in member_sessions)
        actions = set(session.scalars(select(AuditEvent.action)))
        assert {
            "admin.session.revoke",
            "admin.member.disable",
            "admin.member.restore",
        }.issubset(actions)


def test_bulk_session_revoke_does_not_change_member_status(tmp_path) -> None:
    app, database, ids = build_admin_app(tmp_path)
    with TestClient(app) as client:
        revoked = client.post(
            f"/v1/admin/members/{ids['member']}/sessions/revoke",
            headers=admin_headers(),
        )
        assert revoked.status_code == 200
        assert revoked.json()["revokedSessionCount"] == 2
        detail = client.get(
            f"/v1/admin/members/{ids['member']}",
            headers=admin_headers(),
        )
        assert detail.status_code == 200
        assert detail.json()["member"]["status"] == "active"
        assert detail.json()["member"]["activeSessionCount"] == 0

    with database.session_factory() as session:
        actions = list(session.scalars(select(AuditEvent.action)))
        assert "admin.member.sessions.revoke" in actions
