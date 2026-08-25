# -*- coding: utf-8 -*-
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import purchase_tool.main as main_module
from purchase_tool.extension_bridge import ExtensionBridge, ExtensionBridgeError
from purchase_tool.main import Handler


CLIENT_ID = 'a' * 32
EXTENSION_ORIGIN = 'chrome-extension://' + CLIENT_ID


class FakeAuth(object):
    def __init__(self):
        self.required = []
        self.purchase_requests = []
        self.identity = {
            'user': {
                'id': 'user-public-id',
                'name': '合成运营',
                'avatarUrl': '',
                'status': 'active',
            },
            'tenant': {'id': 'tenant-public-id', 'name': '测试组织'},
            'roles': ['operator'],
            'permissions': [
                'operations.access',
                'procurement.request.read',
                'procurement.request.save',
                'procurement.request.submit',
            ],
        }

    def require(self, permission=None, role=None):
        self.required.append((permission, role))
        return self.identity

    def status(self, force=True):
        return {
            'authenticated': True,
            'cloudReachable': True,
            'identity': self.identity,
            'code': '',
            'message': '',
        }

    def purchase_request(self, action, payload, permission):
        self.purchase_requests.append((action, payload, permission))
        return {
            'identity': self.identity,
            'data': {
                'orderKey': payload.get('orderKey') or payload.get('orderKey', ''),
                'submissionStatus': 'submitted' if action == 'submit' else 'draft',
                'syncStatus': 'pending',
            },
        }


class ExtensionBridgeUnitTests(unittest.TestCase):
    def test_pairing_is_bound_to_extension_origin_and_rotates_token(self):
        bridge = ExtensionBridge()
        bridge.request_pairing(CLIENT_ID, '0.2.0', EXTENSION_ORIGIN)
        first = bridge.approve(CLIENT_ID)['bridgeToken']
        self.assertEqual(
            bridge.authenticate(CLIENT_ID, first, EXTENSION_ORIGIN),
            CLIENT_ID)

        bridge.request_pairing(CLIENT_ID, '0.2.1', EXTENSION_ORIGIN)
        second = bridge.approve(CLIENT_ID)['bridgeToken']
        self.assertNotEqual(first, second)
        with self.assertRaises(ExtensionBridgeError):
            bridge.authenticate(CLIENT_ID, first, EXTENSION_ORIGIN)
        self.assertEqual(
            bridge.authenticate(CLIENT_ID, second, EXTENSION_ORIGIN),
            CLIENT_ID)

    def test_pairing_rejects_mismatched_origin(self):
        bridge = ExtensionBridge()
        with self.assertRaises(ExtensionBridgeError) as caught:
            bridge.request_pairing(CLIENT_ID, '0.2.0', 'chrome-extension://' + 'b' * 32)
        self.assertEqual(caught.exception.code, 'extension_origin_forbidden')


class ExtensionBridgeHttpTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.auth = FakeAuth()
        main_module.STATE = SimpleNamespace(
            auth=self.auth,
            extension_bridge=ExtensionBridge(),
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        main_module.STATE = self.original_state

    def _url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.server.server_address[1], path)

    def _post(self, path, payload, origin):
        request = Request(
            self._url(path),
            data=json.dumps(payload).encode('utf-8'),
            method='POST',
            headers={
                'Content-Type': 'text/plain;charset=UTF-8',
                'Origin': origin,
            },
        )
        with urlopen(request, timeout=3) as response:
            return response.status, dict(response.headers), json.loads(response.read())

    def test_user_approved_bridge_reuses_local_identity_for_cloud_purchase_calls(self):
        status, headers, requested = self._post(
            '/api/extension/v1/pair/request',
            {'clientId': CLIENT_ID, 'clientVersion': '0.2.0'},
            EXTENSION_ORIGIN,
        )
        self.assertEqual(status, 202)
        self.assertEqual(headers['Access-Control-Allow-Origin'], EXTENSION_ORIGIN)
        self.assertEqual(requested['service'], 'xynigo-sourcing')
        self.assertIn('/extension-connect?clientId=' + CLIENT_ID, requested['approvalUrl'])

        approved_status, _headers, approved = self._post(
            '/api/extension/pair/approve',
            {'clientId': CLIENT_ID},
            self._url(''),
        )
        self.assertEqual(approved_status, 200)
        token = approved['bridgeToken']
        self.assertNotIn(token, requested['approvalUrl'])
        self.assertEqual(self.auth.required, [('operations.access', None)])

        connection_status, _headers, connection = self._post(
            '/api/extension/v1/status',
            {'clientId': CLIENT_ID, 'bridgeToken': token},
            EXTENSION_ORIGIN,
        )
        self.assertEqual(connection_status, 200)
        self.assertTrue(connection['authenticated'])
        self.assertEqual(connection['identity']['user']['name'], '合成运营')

        draft = {'orderKey': 'demo-order-key'}
        saved_status, _headers, saved = self._post(
            '/api/extension/v1/purchase-orders/draft',
            {'clientId': CLIENT_ID, 'bridgeToken': token, 'draft': draft},
            EXTENSION_ORIGIN,
        )
        submitted_status, _headers, submitted = self._post(
            '/api/extension/v1/purchase-orders/submit',
            {'clientId': CLIENT_ID, 'bridgeToken': token, 'draft': draft},
            EXTENSION_ORIGIN,
        )
        self.assertEqual((saved_status, submitted_status), (200, 200))
        self.assertEqual(saved['data']['submissionStatus'], 'draft')
        self.assertEqual(submitted['data']['submissionStatus'], 'submitted')
        self.assertEqual(self.auth.purchase_requests, [
            ('draft', draft, 'procurement.request.save'),
            ('submit', draft, 'procurement.request.submit'),
        ])

    def test_extension_connection_page_is_static_and_no_store(self):
        with urlopen(self._url('/extension-connect?clientId=' + CLIENT_ID), timeout=3) as response:
            html = response.read().decode('utf-8')
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn('连接运营采购助手', html)
        self.assertIn('/extension-connect.js', html)
        self.assertNotIn('bridgeToken', html)

    def test_extension_permission_errors_keep_exact_extension_cors_origin(self):
        self._post(
            '/api/extension/v1/pair/request',
            {'clientId': CLIENT_ID, 'clientVersion': '0.2.0'},
            EXTENSION_ORIGIN,
        )
        _status, _headers, approved = self._post(
            '/api/extension/pair/approve',
            {'clientId': CLIENT_ID},
            self._url(''),
        )

        def reject_purchase(_action, _payload, _permission):
            raise main_module.LocalAuthError(
                'permission_denied', status=403)

        self.auth.purchase_request = reject_purchase
        request = Request(
            self._url('/api/extension/v1/purchase-orders/submit'),
            data=json.dumps({
                'clientId': CLIENT_ID,
                'bridgeToken': approved['bridgeToken'],
                'draft': {'orderKey': 'demo-order-key'},
            }).encode('utf-8'),
            method='POST',
            headers={
                'Content-Type': 'text/plain;charset=UTF-8',
                'Origin': EXTENSION_ORIGIN,
            },
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=3)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(
            caught.exception.headers['Access-Control-Allow-Origin'],
            EXTENSION_ORIGIN,
        )
        payload = json.loads(caught.exception.read().decode('utf-8'))
        self.assertEqual(payload['code'], 'permission_denied')


if __name__ == '__main__':
    unittest.main()
