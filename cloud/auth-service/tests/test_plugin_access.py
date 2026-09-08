"""Operators can submit their own orders without obtaining workspace access."""
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app, start_local_login, start_login
from test_purchase_api import sample_draft
from xynigo_auth.models import Permission, Role, RolePermission, Tenant, User, UserRole
from xynigo_auth.plugin_access import PLUGIN_PURCHASE_PERMISSIONS
from xynigo_auth.purchase_contract import PurchaseDraft
from xynigo_auth.purchase_service import PurchaseOrderService


def setup_operator(tmp_path, extra_permissions=()):
    app, database, oauth = build_test_app(tmp_path)
    client = TestClient(app)
    state, _ = start_local_login(client)
    assert client.get('/v1/auth/feishu/callback', params={'state': state, 'code': 'admin'},
                      follow_redirects=False).status_code == 303
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        admin = session.scalar(select(User))
        admin_id = admin.id
        operator = User(tenant_id=tenant.id, feishu_open_id='ou_synthetic_plugin',
                        display_name='合成提单运营', status='active')
        role = Role(tenant_id=tenant.id, code='synthetic_plugin', name='合成插件角色')
        session.add_all([operator, role])
        session.flush()
        role_id, user_id = role.id, operator.id
        for permission in session.scalars(select(Permission).where(
                Permission.code.in_(PLUGIN_PURCHASE_PERMISSIONS | set(extra_permissions)))):
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        session.add(UserRole(user_id=operator.id, role_id=role.id))
        session.commit()
    oauth.identity = replace(oauth.identity, open_id='ou_synthetic_plugin', name='合成提单运营')
    state, poll = start_local_login(client)
    assert client.get('/v1/auth/feishu/callback', params={'state': state, 'code': 'plugin'},
                      follow_redirects=False).status_code == 303
    exchange = client.post('/v1/auth/local/poll', json={'pollToken': poll})
    assert exchange.status_code == 200
    token = exchange.json()['sessionToken']
    return client, database, {'Authorization': 'Bearer ' + token}, role_id, user_id, admin_id


def test_plugin_only_login_and_own_order_flow_work_without_workspace_access(tmp_path):
    client, database, headers, _, _, _ = setup_operator(tmp_path)
    with client:
        identity = client.get('/v1/auth/me', headers=headers).json()
        assert set(identity['permissions']) == PLUGIN_PURCHASE_PERMISSIONS
        assert identity['workspaceAccess'] is False
        draft = sample_draft()
        assert client.post('/v1/purchase-orders/draft', json=draft, headers=headers).status_code == 200
        assert client.post('/v1/purchase-orders/submit', json=draft, headers=headers).status_code == 200
        assert client.post('/v1/purchase-orders/get', json={'orderKey': draft['orderKey']},
                           headers=headers).status_code == 200
        for path in ['/v1/procurement/overview', '/v1/procurement/orders', '/v1/admin/members']:
            denied = client.get(path, headers=headers)
            assert denied.status_code == 403, (path, denied.text)
        assert client.post('/v1/assistant/procurement-import/parse', headers=headers,
            json={'filename': 'synthetic.xlsx', 'contentBase64': 'eA=='}).status_code == 403


def test_plugin_only_web_login_denied_and_old_cookie_has_no_workspace_access(tmp_path):
    client, _, headers, _, _, _ = setup_operator(tmp_path)
    with client:
        state, _ = start_login(client)
        callback = client.get('/v1/auth/feishu/callback', params={'state': state, 'code': 'web'},
                              follow_redirects=False)
        assert callback.status_code == 403
        assert callback.json()['detail']['code'] == 'plugin_only_access'
        assert 'set-cookie' not in callback.headers
        # Old browser sessions must be re-evaluated after roles are reduced.
        client.cookies.set('xynigo_session', headers['Authorization'].split()[1])
        result = client.get('/v1/auth/web/status').json()
        assert result['identity']['workspaceAccess'] is False
        assert client.get('/v1/procurement/orders').status_code == 403
        client.cookies.clear()
        assert client.post('/v1/auth/session/refresh', headers=headers).status_code == 200


def test_plugin_cannot_read_or_overwrite_another_members_order(tmp_path):
    client, database, headers, _, _, admin_id = setup_operator(tmp_path)
    draft = sample_draft()
    with database.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        PurchaseOrderService(session).save_draft(tenant_id=tenant.id, actor_user_id=admin_id,
                                               draft=PurchaseDraft.model_validate(draft))
        session.commit()
    with client:
        for path, body in [('/v1/purchase-orders/get', {'orderKey': draft['orderKey']}),
                           ('/v1/purchase-orders/draft', draft), ('/v1/purchase-orders/submit', draft)]:
            result = client.post(path, json=body, headers=headers)
            assert result.status_code == 404, (path, result.text)
            assert result.json()['detail']['code'] == 'purchase_order_not_found'


def test_supervisor_needs_explicit_procurement_entry_permission(tmp_path):
    client, _, headers, _, _, _ = setup_operator(tmp_path, {'procurement.access'})
    with client:
        assert client.get('/v1/auth/me', headers=headers).json()['workspaceAccess'] is True
        assert client.get('/v1/procurement/orders', headers=headers).status_code == 200
        state, _ = start_login(client)
        assert client.get('/v1/auth/feishu/callback', params={'state': state, 'code': 'supervisor'},
                          follow_redirects=False).status_code == 303


def test_import_access_alone_does_not_grant_procurement_workspace(tmp_path):
    client, _, headers, _, _, _ = setup_operator(tmp_path, {'assistant.access'})
    with client:
        assert client.get('/v1/auth/me', headers=headers).json()['workspaceAccess'] is True
        assert client.get('/v1/procurement/orders', headers=headers).status_code == 403
