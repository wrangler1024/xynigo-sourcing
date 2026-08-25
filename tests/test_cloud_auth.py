# -*- coding: utf-8 -*-
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import purchase_tool.main as main_module
from purchase_tool.cloud_auth import (
    CloudAuthClient,
    LocalAuthError,
    LocalAuthService,
    MacKeychainAuthSessionStore,
    MemoryAuthSessionStore,
    WindowsDpapiAuthSessionStore,
)
from purchase_tool.main import Handler, admin_cloud_write_target


POLL_TOKEN = 'p' * 64
SESSION_TOKEN = 's' * 64
IDENTITY = {
    'user': {
        'id': 'user-public-id',
        'name': '合成管理员',
        'avatarUrl': '',
        'status': 'active',
    },
    'tenant': {'id': 'tenant-public-id', 'name': '测试组织'},
    'roles': ['super_admin'],
    'permissions': [
        'fulfillment.order.read',
        'system.lark_connection.manage',
    ],
}


class FakeCloudClient(object):
    def __init__(self):
        self.start_calls = 0
        self.poll_results = [
            {'status': 'pending'},
            {
                'status': 'authenticated',
                'sessionToken': SESSION_TOKEN,
                'identity': IDENTITY,
            },
        ]
        self.me_result = IDENTITY
        self.logout_tokens = []
        self.admin_requests = []
        self.purchase_requests = []
        self.procurement_workspace_requests = []

    def start_login(self):
        self.start_calls += 1
        return {
            'loginUrl': 'https://xynigo.example.test/authorize',
            'pollToken': POLL_TOKEN,
            'expiresIn': 300,
        }

    def poll_login(self, token):
        assert token == POLL_TOKEN
        return self.poll_results.pop(0)

    def me(self, token):
        assert token == SESSION_TOKEN
        if isinstance(self.me_result, Exception):
            raise self.me_result
        return self.me_result

    def logout(self, token):
        self.logout_tokens.append(token)
        return {}

    def admin_request(self, path, token, method='GET', payload=None):
        self.admin_requests.append((path, token, method, payload))
        return {'members': []}

    def purchase_request(self, action, token, payload):
        self.purchase_requests.append((action, token, payload))
        return {
            'ok': True,
            'data': {
                'orderKey': payload['orderKey'],
                'submissionStatus': 'submitted' if action == 'submit' else 'draft',
            },
        }

    def procurement_workspace_request(
            self, path, token, method='GET', payload=None):
        self.procurement_workspace_requests.append(
            (path, token, method, payload))
        return {
            'ok': True,
            'data': {'page': 1, 'pageSize': 20, 'total': 0, 'items': []},
        }


class QueueRunner(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.responses.pop(0)


class CloudAuthTests(unittest.TestCase):
    def test_local_service_never_returns_session_or_poll_token_to_page(self):
        client = FakeCloudClient()
        store = MemoryAuthSessionStore()
        service = LocalAuthService(client=client, store=store)

        started = service.start_login()
        self.assertNotIn('pollToken', started)
        self.assertEqual(service.poll_login(), {'status': 'pending'})
        result = service.poll_login()
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result['status'], 'authenticated')
        self.assertNotIn(SESSION_TOKEN, rendered)
        self.assertNotIn(POLL_TOKEN, rendered)
        self.assertFalse(service.status(force=False)['loginPending'])
        self.assertEqual(store.load(), SESSION_TOKEN)
        self.assertEqual(service.require()['user']['name'], '合成管理员')
        self.assertEqual(
            service.require('system.lark_connection.manage')['roles'],
            ['super_admin'])
        with self.assertRaisesRegex(LocalAuthError, '没有此功能权限'):
            service.require('system.role.manage')

        self.assertEqual(service.logout(), {'loggedOut': True})
        self.assertIsNone(store.load())
        self.assertEqual(client.logout_tokens, [SESSION_TOKEN])

    def test_pending_login_survives_page_refresh_and_reuses_request(self):
        now = [100.0]
        client = FakeCloudClient()
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(),
            clock=lambda: now[0],
        )

        started = service.start_login()
        status = service.status()
        resumed = service.start_login()

        self.assertTrue(started['started'])
        self.assertFalse(started['resumed'])
        self.assertTrue(status['loginPending'])
        self.assertNotIn(POLL_TOKEN, json.dumps(status))
        self.assertFalse(resumed['started'])
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['loginUrl'], started['loginUrl'])
        self.assertEqual(client.start_calls, 1)

        now[0] = 401.0
        self.assertFalse(service.status()['loginPending'])
        self.assertTrue(service.start_login()['started'])
        self.assertEqual(client.start_calls, 2)

    def test_terminal_poll_error_clears_pending_login(self):
        client = FakeCloudClient()
        client.poll_results = [
            LocalAuthError('user_pending_approval', status=403),
        ]

        def poll_login(token):
            result = client.poll_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        client.poll_login = poll_login
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(),
        )
        service.start_login()
        with self.assertRaises(LocalAuthError) as caught:
            service.poll_login()
        self.assertEqual(caught.exception.code, 'user_pending_approval')
        self.assertFalse(service.status()['loginPending'])

    def test_transient_poll_error_keeps_pending_login_for_retry(self):
        client = FakeCloudClient()

        def poll_login(_token):
            raise LocalAuthError('cloud_unreachable', status=503)

        client.poll_login = poll_login
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(),
        )
        service.start_login()
        with self.assertRaises(LocalAuthError) as caught:
            service.poll_login()
        self.assertEqual(caught.exception.code, 'cloud_unreachable')
        self.assertTrue(service.status()['loginPending'])

    def test_invalid_cloud_session_is_removed_from_secure_store(self):
        client = FakeCloudClient()
        client.me_result = LocalAuthError('session_invalid', status=401)
        store = MemoryAuthSessionStore(SESSION_TOKEN)
        service = LocalAuthService(client=client, store=store)
        status = service.status(force=True)
        self.assertFalse(status['authenticated'])
        self.assertEqual(status['code'], 'session_invalid')
        self.assertIsNone(store.load())

    def test_admin_role_cannot_pass_super_admin_configuration_guard(self):
        client = FakeCloudClient()
        client.me_result = {
            'user': dict(IDENTITY['user']),
            'tenant': dict(IDENTITY['tenant']),
            'roles': ['admin'],
            # 即使云端目录误授了权限，本地敏感配置入口仍必须校验角色。
            'permissions': ['system.lark_connection.manage'],
        }
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(SESSION_TOKEN),
        )
        with self.assertRaises(LocalAuthError) as caught:
            service.require(
                'system.lark_connection.manage', role='super_admin')
        self.assertEqual(caught.exception.code, 'super_admin_required')
        self.assertEqual(caught.exception.status, 403)

    def test_admin_proxy_keeps_bearer_session_inside_local_service(self):
        client = FakeCloudClient()
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(SESSION_TOKEN),
        )
        result = service.admin_request('/v1/admin/members?status=pending')
        self.assertEqual(result, {'members': []})
        self.assertEqual(client.admin_requests, [
            ('/v1/admin/members?status=pending', SESSION_TOKEN, 'GET', None),
        ])
        self.assertNotIn(SESSION_TOKEN, json.dumps(result))

    def test_purchase_proxy_keeps_bearer_session_inside_local_service(self):
        client = FakeCloudClient()
        client.me_result = {
            **IDENTITY,
            'permissions': ['procurement.request.submit'],
        }
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(SESSION_TOKEN),
        )
        draft = {'orderKey': '测试店铺|GSH-DEMO|XMWU-DEMO'}
        result = service.purchase_request(
            'submit', draft, 'procurement.request.submit')
        self.assertEqual(result['data']['submissionStatus'], 'submitted')
        self.assertEqual(client.purchase_requests, [
            ('submit', SESSION_TOKEN, draft),
        ])
        self.assertNotIn(SESSION_TOKEN, json.dumps(result, ensure_ascii=False))

    def test_procurement_workspace_proxy_keeps_bearer_session_local(self):
        client = FakeCloudClient()
        client.me_result = {
            **IDENTITY,
            'permissions': ['procurement.request.read'],
        }
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(SESSION_TOKEN),
        )
        result = service.procurement_workspace_request(
            '/v1/procurement/orders?page=1&pageSize=20')
        self.assertEqual(result['data']['items'], [])
        self.assertEqual(client.procurement_workspace_requests, [
            ('/v1/procurement/orders?page=1&pageSize=20',
             SESSION_TOKEN, 'GET', None),
        ])
        self.assertNotIn(SESSION_TOKEN, json.dumps(result))

    def test_procurement_workspace_write_uses_execution_permission(self):
        client = FakeCloudClient()
        client.me_result = {
            **IDENTITY,
            'permissions': ['procurement.execution.manage'],
        }
        service = LocalAuthService(
            client=client,
            store=MemoryAuthSessionStore(SESSION_TOKEN),
        )
        payload = {'purchaseOrderIds': [
            '00000000-0000-0000-0000-000000000001']}
        result = service.procurement_workspace_request(
            '/v1/procurement/claims',
            method='POST',
            payload=payload,
            permission='procurement.execution.manage',
        )
        self.assertEqual(result['data']['items'], [])
        self.assertEqual(client.procurement_workspace_requests, [
            ('/v1/procurement/claims', SESSION_TOKEN, 'POST', payload),
        ])

    def test_cloud_procurement_client_allows_only_workspace_read_paths(self):
        client = CloudAuthClient('https://xynigo.example.test')
        calls = []
        client._request = lambda path, **kwargs: calls.append((path, kwargs)) or {
            'ok': True,
            'data': {},
        }
        detail_id = '00000000-0000-0000-0000-000000000001'
        client.procurement_workspace_request(
            '/v1/procurement/overview', SESSION_TOKEN)
        client.procurement_workspace_request(
            '/v1/procurement/orders?submissionStatus=submitted', SESSION_TOKEN)
        client.procurement_workspace_request(
            '/v1/procurement/orders/' + detail_id, SESSION_TOKEN)
        client.procurement_workspace_request(
            '/v1/procurement/execution/splits?pageSize=100', SESSION_TOKEN)
        client.procurement_workspace_request(
            '/v1/procurement/claims', SESSION_TOKEN,
            method='POST', payload={'purchaseOrderIds': [detail_id]})
        client.procurement_workspace_request(
            '/v1/procurement/orders/' + detail_id + '/splits', SESSION_TOKEN,
            method='POST', payload={'expectedRevision': 0, 'groups': []})
        self.assertEqual([item[0] for item in calls], [
            '/v1/procurement/overview',
            '/v1/procurement/orders?submissionStatus=submitted',
            '/v1/procurement/orders/' + detail_id,
            '/v1/procurement/execution/splits?pageSize=100',
            '/v1/procurement/claims',
            '/v1/procurement/orders/' + detail_id + '/splits',
        ])
        with self.assertRaises(LocalAuthError):
            client.procurement_workspace_request(
                '/v1/procurement/claims', SESSION_TOKEN)
        with self.assertRaises(LocalAuthError):
            client.procurement_workspace_request(
                '/v1/procurement/execution/splits', SESSION_TOKEN,
                method='POST', payload={})
        for unsafe_path in (
                '/v1/admin/members',
                '/v1/procurement/../admin/members',
                'https://evil.example.test/v1/procurement/orders'):
            with self.subTest(path=unsafe_path):
                with self.assertRaises(LocalAuthError):
                    client.procurement_workspace_request(
                        unsafe_path, SESSION_TOKEN)

    def test_cloud_admin_client_supports_delete_without_a_request_body(self):
        client = CloudAuthClient('https://xynigo.example.test')
        calls = []
        client._request = lambda path, **kwargs: calls.append((path, kwargs)) or {
            'deleted': True}
        result = client.admin_request(
            '/v1/admin/roles/role-id',
            SESSION_TOKEN,
            method='DELETE',
            payload={'ignored': True},
        )
        self.assertEqual(result, {'deleted': True})
        self.assertEqual(calls, [('/v1/admin/roles/role-id', {
            'method': 'DELETE',
            'payload': None,
            'token': SESSION_TOKEN,
        })])

    def test_role_write_proxy_maps_browser_posts_to_cloud_methods(self):
        self.assertEqual(
            admin_cloud_write_target('/api/admin/roles'),
            ('/v1/admin/roles', 'POST'))
        self.assertEqual(
            admin_cloud_write_target('/api/admin/roles/role-id/rename'),
            ('/v1/admin/roles/role-id', 'PUT'))
        self.assertEqual(
            admin_cloud_write_target('/api/admin/roles/role-id/delete'),
            ('/v1/admin/roles/role-id', 'DELETE'))
        self.assertEqual(
            admin_cloud_write_target('/api/admin/roles/role-id/permissions'),
            ('/v1/admin/roles/role-id/permissions', 'PUT'))
        self.assertEqual(
            admin_cloud_write_target('/api/admin/members/invitations/resolve'),
            ('/v1/admin/members/invitations/resolve', 'POST'))
        self.assertEqual(
            admin_cloud_write_target('/api/admin/members/invitations'),
            ('/v1/admin/members/invitations', 'POST'))

    def test_macos_store_sends_session_as_hex_stdin_not_argv(self):
        runner = QueueRunner([
            subprocess.CompletedProcess([], 0, '', ''),
        ])
        store = MacKeychainAuthSessionStore(runner=runner)
        store.save(SESSION_TOKEN)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv, ['/usr/bin/security', '-i'])
        self.assertNotIn(SESSION_TOKEN, ' '.join(argv))
        self.assertNotIn(SESSION_TOKEN, kwargs['input'])
        self.assertIn(SESSION_TOKEN.encode('utf-8').hex(), kwargs['input'])

    def test_windows_store_round_trips_with_injected_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cloud-session.bin'
            store = WindowsDpapiAuthSessionStore(
                path=path,
                protect_fn=lambda value: bytes(item ^ 0xAA for item in value),
                unprotect_fn=lambda value: bytes(item ^ 0xAA for item in value))
            store.save(SESSION_TOKEN)
            self.assertNotIn(SESSION_TOKEN.encode('utf-8'), path.read_bytes())
            self.assertEqual(store.load(), SESSION_TOKEN)
            store.clear()
            self.assertFalse(path.exists())

    def test_client_rejects_non_https_remote_base_url(self):
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            CloudAuthClient('http://xynigo.example.test')

    def test_client_accepts_only_cloud_or_official_feishu_login_hosts(self):
        client = CloudAuthClient('https://xynigo.example.test')
        client._request = lambda *args, **kwargs: {
            'loginUrl': 'https://accounts.feishu.cn/open-apis/authen/v1/authorize',
            'pollToken': POLL_TOKEN,
            'expiresIn': 300,
        }
        self.assertEqual(
            client.start_login()['loginUrl'],
            'https://accounts.feishu.cn/open-apis/authen/v1/authorize')
        client._request = lambda *args, **kwargs: {
            'loginUrl': 'https://accounts.feishu.cn.evil.test/authorize',
            'pollToken': POLL_TOKEN,
            'expiresIn': 300,
        }
        with self.assertRaisesRegex(LocalAuthError, '登录地址无效'):
            client.start_login()

    def test_corrupt_stored_session_is_cleared_without_crashing_startup(self):
        store = MemoryAuthSessionStore('not-a-valid-session')
        service = LocalAuthService(client=FakeCloudClient(), store=store)
        status = service.status()
        self.assertFalse(status['authenticated'])
        self.assertEqual(status['code'], 'authentication_required')
        self.assertIsNone(store.load())

    def test_logout_clears_memory_even_when_secure_store_clear_fails(self):
        class BrokenClearStore(MemoryAuthSessionStore):
            def clear(self):
                raise OSError('synthetic secure store failure')

        service = LocalAuthService(
            client=FakeCloudClient(),
            store=BrokenClearStore(SESSION_TOKEN),
        )
        with self.assertRaisesRegex(LocalAuthError, '安全保存登录会话'):
            service.logout()
        self.assertIsNone(service.session_token)
        self.assertEqual(service.status()['code'], 'credential_store_failed')


class FakeRouteAuth(object):
    def __init__(self):
        self.required_permissions = []
        self.required_roles = []

    def status(self, force=True):
        return {
            'authenticated': False,
            'cloudReachable': True,
            'identity': None,
            'code': 'authentication_required',
            'message': '',
        }

    def require(self, permission=None, role=None):
        self.required_permissions.append(permission)
        self.required_roles.append(role)
        if permission:
            raise LocalAuthError('permission_denied', status=403)
        raise LocalAuthError('authentication_required', status=401)

    def start_login(self):
        return {
            'started': True,
            'loginUrl': 'https://xynigo.example.test/authorize',
            'expiresIn': 300,
        }

    def poll_login(self):
        return {'status': 'pending'}

    def logout(self):
        return {'loggedOut': True}


class AuthRouteGuardTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.auth = FakeRouteAuth()
        main_module.STATE = SimpleNamespace(auth=self.auth)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        main_module.STATE = self.original_state

    def _url(self, path):
        return 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], path)

    def test_auth_status_and_start_remain_public(self):
        with urlopen(self._url('/api/auth/status'), timeout=3) as response:
            status = json.loads(response.read().decode('utf-8'))
        self.assertFalse(status['authenticated'])
        request = Request(
            self._url('/api/auth/start'), data=b'{}', method='POST',
            headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            started = json.loads(response.read().decode('utf-8'))
        self.assertTrue(started['started'])
        self.assertEqual(self.auth.required_permissions, [])

    def test_cross_site_browser_post_is_rejected_before_local_auth(self):
        request = Request(
            self._url('/api/auth/logout'), data=b'{}', method='POST',
            headers={
                'Content-Type': 'application/json',
                'Origin': 'https://malicious.example.test',
                'Sec-Fetch-Site': 'cross-site',
            })
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 403)
        payload = json.loads(caught.exception.read().decode('utf-8'))
        self.assertEqual(payload['code'], 'origin_forbidden')
        self.assertEqual(self.auth.required_permissions, [])

        origin = 'http://127.0.0.1:%d' % self.server.server_address[1]
        allowed = Request(
            self._url('/api/auth/logout'), data=b'{}', method='POST',
            headers={
                'Content-Type': 'application/json',
                'Origin': origin,
                'Sec-Fetch-Site': 'same-origin',
            })
        with urlopen(allowed, timeout=3) as response:
            self.assertEqual(
                json.loads(response.read().decode('utf-8')),
                {'loggedOut': True})

    def test_business_api_requires_authentication(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self._url('/api/config'), timeout=3)
        self.assertEqual(caught.exception.code, 401)
        payload = json.loads(caught.exception.read().decode('utf-8'))
        self.assertEqual(payload['code'], 'authentication_required')
        self.assertEqual(self.auth.required_permissions, [None])

    def test_lark_connection_api_requires_super_admin_permission(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self._url('/api/lark/config'), timeout=3)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(
            self.auth.required_permissions,
            ['system.lark_connection.manage'])
        self.assertEqual(self.auth.required_roles, ['super_admin'])

    def test_business_modules_require_their_server_side_permissions(self):
        for path, permission in [
                ('/api/progress', 'fulfillment.order.read'),
                ('/api/export', 'fulfillment.order.export'),
                ('/api/buyer-library', 'resource.buyer.read'),
                ('/api/register/progress', 'resource.buyer.import'),
                ('/api/envbatch/progress', 'resource.environment.create'),
                ('/api/resources/stores', 'resource.store.read'),
                ('/api/resources/stores/export', 'resource.store.read'),
                ('/api/resources/proxies', 'resource.ip.read'),
                ('/api/resources/proxies/export', 'resource.ip.read'),
                ('/api/resources/proxies/check/history', 'resource.ip.read'),
                ('/api/resources/proxies/check/progress', 'resource.ip.test'),
                ('/api/procurement/overview', 'procurement.request.read'),
                ('/api/procurement/orders', 'procurement.request.read'),
                ('/api/procurement/execution/splits',
                 'procurement.request.read'),
                ('/api/procurement/claims', 'procurement.execution.manage'),
                ('/api/procurement/orders/00000000-0000-0000-0000-000000000001',
                 'procurement.request.read'),
                ('/api/procurement/orders/00000000-0000-0000-0000-000000000001/splits',
                 'procurement.execution.manage')]:
            with self.subTest(path=path):
                self.auth.required_permissions.clear()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self._url(path), timeout=3)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(
                    self.auth.required_permissions, [permission])

    def test_procurement_write_routes_forward_only_safe_payload_and_permission(self):
        forwarded = []

        def allow(permission=None, role=None):
            self.auth.required_permissions.append(permission)
            self.auth.required_roles.append(role)
            return IDENTITY

        def forward(path, method='GET', payload=None, permission=None):
            forwarded.append((path, method, payload, permission))
            return {'ok': True, 'data': {'saved': True}}

        self.auth.require = allow
        self.auth.procurement_workspace_request = forward
        order_id = '00000000-0000-0000-0000-000000000001'
        requests = [
            ('/api/procurement/claims', {'purchaseOrderIds': [order_id]}),
            ('/api/procurement/orders/%s/splits' % order_id,
             {'expectedRevision': 0, 'groups': []}),
        ]
        for path, payload in requests:
            with self.subTest(path=path):
                request = Request(
                    self._url(path),
                    data=json.dumps(payload).encode('utf-8'),
                    method='POST',
                    headers={'Content-Type': 'application/json'},
                )
                with urlopen(request, timeout=3) as response:
                    result = json.loads(response.read().decode('utf-8'))
                self.assertTrue(result['data']['saved'])
        self.assertEqual(self.auth.required_permissions, [
            'procurement.execution.manage',
            'procurement.execution.manage',
        ])
        self.assertEqual(forwarded, [
            ('/v1/procurement/claims', 'POST',
             {'purchaseOrderIds': [order_id]},
             'procurement.execution.manage'),
            ('/v1/procurement/orders/%s/splits' % order_id, 'POST',
             {'expectedRevision': 0, 'groups': []},
             'procurement.execution.manage'),
        ])

    def test_admin_proxy_paths_have_local_permission_guards(self):
        for path, permission in [
                ('/api/admin/members', 'system.member.manage'),
                ('/api/admin/members/invitations/resolve', 'system.member.manage'),
                ('/api/admin/sessions', 'system.member.manage'),
                ('/api/admin/roles', 'system.role.manage'),
                ('/api/admin/permissions', 'system.role.manage'),
                ('/api/admin/members/00000000-0000-0000-0000-000000000001/roles',
                 'system.role.manage')]:
            with self.subTest(path=path):
                self.auth.required_permissions.clear()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self._url(path), timeout=3)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(self.auth.required_permissions, [permission])


if __name__ == '__main__':
    unittest.main()
