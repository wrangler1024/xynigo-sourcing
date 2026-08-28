# -*- coding: utf-8 -*-
import json
from pathlib import Path
import unittest
from urllib.error import URLError

from purchase_tool.hub_api import HubStudioLocalApiAdapter
from purchase_tool.hub_api_key import (
    HubApiKeyStoreError, SystemHubApiKeyStore, _wrap_key)


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


class FakeLocalApiOpener(object):
    def __init__(self, unavailable_ports=None, response_code=0,
                 response_message='Success', raw_payload=None):
        self.unavailable_ports = set(unavailable_ports or [])
        self.response_code = response_code
        self.response_message = response_message
        self.raw_payload = raw_payload
        self.calls = []
        self.timeouts = []
        self.environments = [{
            'containerCode': 'container-test-1',
            'serialNumber': 4254,
            'containerName': '脱敏测试环境',
            'tagName': '测试分组',
            'remark': '不得返回到插件',
        }]

    def open(self, request, timeout=None):
        self.timeouts.append(timeout)
        url = request.full_url
        port = int(url.split(':')[2].split('/')[0])
        body = json.loads((request.data or b'{}').decode('utf-8'))
        path = '/' + url.split('/api/v1/', 1)[1]
        self.calls.append((port, path, body, dict(request.headers)))
        if port in self.unavailable_ports:
            raise URLError(ConnectionRefusedError('refused'))
        if self.raw_payload is not None:
            return FakeResponse(self.raw_payload)
        if self.response_code != 0:
            return FakeResponse({
                'code': self.response_code,
                'msg': self.response_message,
                'data': None,
            })
        if path == '/group/list':
            data = [{'tagName': '测试分组', 'tagCode': 'tag-test'}]
        elif path == '/env/list':
            data = {'list': list(self.environments), 'total': 1}
        elif path == '/browser/all-browser-status':
            data = {'containers': []}
        elif path in {'/browser/start', '/browser/stop'}:
            data = {'accepted': True}
        else:
            data = {}
        return FakeResponse({'code': 0, 'msg': 'Success', 'data': data})


class MemoryTokenBackend(object):
    def __init__(self):
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = None


class HubStudioLocalApiAdapterTests(unittest.TestCase):
    def test_available_adapter_lists_locates_opens_and_closes_mock_environment(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['available'])
        self.assertEqual(capability['reasonCode'], 'ok')

        listed = adapter.list_environment_summaries('4254')
        self.assertEqual(listed, [{
            'containerCode': 'container-test-1',
            'serialNumber': '4254',
            'containerName': '脱敏测试环境',
            'tagName': '测试分组',
        }])
        self.assertNotIn('remark', listed[0])
        self.assertEqual(
            adapter.locate_environment('container-test-1')['serialNumber'],
            4254)
        adapter.browser_start('container-test-1')
        adapter.browser_stop('container-test-1')
        paths = [path for _port, path, _body, _headers in opener.calls]
        self.assertIn('/browser/start', paths)
        self.assertIn('/browser/stop', paths)

    def test_batch_management_uses_only_explicit_mock_targets(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True, retries=1)
        results = adapter.batch_browser_control('open', ['4254'])
        self.assertEqual(results, [{
            'identifier': '4254',
            'containerCode': 'container-test-1',
            'ok': True,
            'reasonCode': 'ok',
        }])

    def test_client_not_running_has_distinct_reason(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(unavailable_ports={6873, 6874, 6875}),
            client_running_getter=lambda: False, retries=1)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertFalse(capability['clientRunning'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_client_not_running')

    def test_running_client_with_unreachable_api_reports_api_disabled(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(unavailable_ports={6873, 6874, 6875}),
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['clientRunning'])
        self.assertFalse(capability['localApiEnabled'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_disabled')

    def test_limited_known_port_fallback_updates_endpoint(self):
        opener = FakeLocalApiOpener(unavailable_ports={7000})
        adapter = HubStudioLocalApiAdapter(
            port=7000, known_ports=(6873,), opener=opener,
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['available'])
        self.assertEqual(
            capability['endpoint'], 'http://127.0.0.1:6873/api/v1')
        attempted_ports = [port for port, path, _body, _headers in opener.calls
                           if path == '/group/list']
        self.assertEqual(attempted_ports, [7000, 6873])

    def test_missing_or_wrong_key_is_reason_coded_without_secret_leak(self):
        secret = 'local-api-key-secret-for-test'
        missing = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(
                response_code='E010403',
                response_message='API Key required'),
            client_running_getter=lambda: True, retries=1)
        missing_capability = missing.capability_snapshot()
        self.assertEqual(
            missing_capability['reasonCode'],
            'hubstudio_local_api_authentication_required')

        wrong = HubStudioLocalApiAdapter(
            api_key=secret,
            opener=FakeLocalApiOpener(
                response_code='E010403',
                response_message='API Key=' + secret),
            client_running_getter=lambda: True, retries=1)
        wrong_capability = wrong.capability_snapshot()
        self.assertEqual(
            wrong_capability['reasonCode'],
            'hubstudio_local_api_authentication_failed')
        self.assertNotIn(secret, json.dumps(wrong_capability))

    def test_incompatible_api_response_has_distinct_reason(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(raw_payload={'unexpected': True}),
            client_running_getter=lambda: True, retries=3, timeout=30)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['clientRunning'])
        self.assertTrue(capability['localApiEnabled'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_incompatible')
        # Capability discovery is a single short probe, not a business retry.
        self.assertEqual(len(adapter.opener.calls), 1)
        self.assertLessEqual(adapter.opener.timeouts[0], 2.5)

    def test_api_business_error_proves_local_api_is_enabled(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(
                response_code='E010500', response_message='test failure'),
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['localApiEnabled'])
        self.assertTrue(capability['authenticated'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_error')

    def test_api_key_store_wraps_key_before_secure_backend(self):
        backend = MemoryTokenBackend()
        store = SystemHubApiKeyStore(backend)
        secret = 'hub-local-key-for-test'
        store.save(secret)
        self.assertNotIn(secret, backend.value)
        self.assertEqual(store.load(), secret)
        store.clear()
        self.assertIsNone(store.load())
        self.assertGreaterEqual(len(_wrap_key('short')), 32)

    def test_api_key_length_matches_secure_backend_envelope_limit(self):
        backend = MemoryTokenBackend()
        store = SystemHubApiKeyStore(backend)
        store.save('k' * 180)
        self.assertLessEqual(len(backend.value), 256)
        with self.assertRaisesRegex(HubApiKeyStoreError, '格式无效'):
            store.save('k' * 181)
        with self.assertRaisesRegex(HubApiKeyStoreError, '格式无效'):
            store.save('密' * 61)

    def test_source_and_installers_have_no_hubstudio_cli_dependency(self):
        root = Path(__file__).resolve().parents[1]
        files = []
        for source_root in (root / 'src', root / 'packaging', root / 'scripts'):
            if source_root.is_dir():
                files.extend(path for path in source_root.rglob('*')
                             if path.is_file() and path.suffix in {
                                 '.py', '.sh', '.go', '.command', '.ps1'})
        files.extend(path for path in root.glob('*.sh') if path.is_file())
        combined = '\n'.join(path.read_text(
            encoding='utf-8', errors='ignore') for path in files)
        self.assertNotIn('hubstudio-cli', combined.casefold())


if __name__ == '__main__':
    unittest.main()
