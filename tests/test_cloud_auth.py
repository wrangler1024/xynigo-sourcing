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
from purchase_tool.main import Handler


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

    def start_login(self):
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

    def test_invalid_cloud_session_is_removed_from_secure_store(self):
        client = FakeCloudClient()
        client.me_result = LocalAuthError('session_invalid', status=401)
        store = MemoryAuthSessionStore(SESSION_TOKEN)
        service = LocalAuthService(client=client, store=store)
        status = service.status(force=True)
        self.assertFalse(status['authenticated'])
        self.assertEqual(status['code'], 'session_invalid')
        self.assertIsNone(store.load())

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

    def status(self, force=True):
        return {
            'authenticated': False,
            'cloudReachable': True,
            'identity': None,
            'code': 'authentication_required',
            'message': '',
        }

    def require(self, permission=None):
        self.required_permissions.append(permission)
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

    def test_business_modules_require_their_server_side_permissions(self):
        for path, permission in [
                ('/api/progress', 'fulfillment.order.read'),
                ('/api/export', 'fulfillment.order.export'),
                ('/api/buyer-library', 'resource.buyer.read'),
                ('/api/register/progress', 'resource.buyer.import'),
                ('/api/envbatch/progress', 'resource.environment.create')]:
            with self.subTest(path=path):
                self.auth.required_permissions.clear()
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self._url(path), timeout=3)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(
                    self.auth.required_permissions, [permission])


if __name__ == '__main__':
    unittest.main()
