# -*- coding: utf-8 -*-
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import purchase_tool.main as main_module
from purchase_tool.main import Handler
from purchase_tool.purchase_assistant import (
    PurchaseAssistantConfig,
    PurchaseAssistantError,
    PurchaseAssistantService,
    PurchaseAssistantSheetProvider,
    find_recipient,
    rows_to_tasks,
    search_tasks,
)


EXTENSION_ORIGIN = 'chrome-extension://' + 'a' * 32


def sample_row(**overrides):
    row = {
        '__row_number': '2',
        '销售订单号': 'ORDER-DEMO-001',
        '店铺': 'MX-示例店铺',
        '包裹号': 'PACKAGE-DEMO-001',
        '采购状态': '待采购',
        '主规格': 'Rosa',
        '次规格': '1 pieza',
        '需求数量': '1',
        '采购指导价': '126.00',
        '收货人姓名': 'Lucia Prueba',
        '收货人国家': 'Mexico',
        '收货人州/省': 'Guanajuato',
        '收货人城市': 'Guanajuato',
        '地址1': 'Calle Prueba 100',
        '地址2': 'Piso 2',
        '邮编': '36000',
        '收货人电话': '+52 477 000 0001',
        '系统订单键': 'demo|ORDER-DEMO-001|PACKAGE-DEMO-001',
    }
    row.update(overrides)
    return row


class FakeProvider(object):
    def __init__(self):
        self.rows = [sample_row()]

    def list_tasks(self):
        return rows_to_tasks(self.rows)

    def get_recipient(self, key):
        return find_recipient(self.rows, key)


class FakeHubControls(object):
    def __init__(self):
        self.calls = []
        self.environment = {
            'containerCode': 'container-test-1',
            'serialNumber': '4254',
            'containerName': '脱敏测试环境',
            'tagName': '测试分组',
        }

    def list_environment_summaries(self, query='', limit=100):
        del query, limit
        return [dict(self.environment)]

    def locate_environment(self, identifier):
        self.calls.append(('locate', str(identifier)))
        return dict(self.environment)

    def environment_summary(self, env):
        return dict(env)

    def browser_start(self, code, headless=False):
        self.calls.append(('open', str(code), bool(headless)))

    def browser_stop(self, code):
        self.calls.append(('close', str(code)))

    def batch_browser_control(self, action, identifiers, headless=False):
        self.calls.append(('batch', action, list(identifiers), bool(headless)))
        return [{'identifier': str(identifiers[0]),
                 'containerCode': 'container-test-1', 'ok': True,
                 'reasonCode': 'ok'}]


class FakeTransport(object):
    def __init__(self):
        self.calls = []

    def request_json(self, method, url, headers=None, payload=None,
                     timeout=15.0):
        del timeout
        self.calls.append((method, url, headers, payload))
        if url.endswith('/auth/v3/tenant_access_token/internal'):
            return {
                'code': 0,
                'tenant_access_token': 'tenant-token-for-test',
                'expire': 7200,
            }
        row = sample_row()
        headers_row = [key for key in row if key != '__row_number']
        return {
            'code': 0,
            'data': {'valueRange': {'values': [
                headers_row, [row[key] for key in headers_row],
            ]}},
        }


class PurchaseAssistantUnitTests(unittest.TestCase):
    def test_task_search_excludes_recipient_and_requires_query(self):
        tasks = rows_to_tasks([sample_row()])
        self.assertEqual(search_tasks(tasks, ''), ([], 0))
        matched, total = search_tasks(tasks, 'ORDER-DEMO-001')
        self.assertEqual(total, 1)
        self.assertEqual(matched[0]['taskKey'],
                         'demo|ORDER-DEMO-001|PACKAGE-DEMO-001')
        self.assertNotIn('recipientName', matched[0])
        self.assertNotIn('收货人电话', matched[0])
        self.assertNotIn('地址1', matched[0])

    def test_conflicting_recipient_rows_fail_closed(self):
        with self.assertRaisesRegex(PurchaseAssistantError, '多组不同'):
            find_recipient([
                sample_row(),
                sample_row(__row_number='3', 地址1='Otra Calle 999'),
            ], 'demo|ORDER-DEMO-001|PACKAGE-DEMO-001')

    def test_sheet_provider_reuses_xynigo_credential_getter(self):
        config = PurchaseAssistantConfig(
            spreadsheet_token='spreadsheet-test',
            sheet_id='sheet-test',
        )
        transport = FakeTransport()
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        provider = PurchaseAssistantSheetProvider(
            config, lambda: credentials, transport=transport)
        tasks = provider.list_tasks()
        recipient = provider.get_recipient(tasks[0]['taskKey'])
        self.assertEqual(recipient['recipientName'], 'Lucia Prueba')
        self.assertEqual(len(transport.calls), 2)
        self.assertIn('sheet-test%21A1%3AAQ', transport.calls[1][1])

    def test_missing_sheet_coordinates_disable_service_safely(self):
        service = PurchaseAssistantService.from_runtime_config({}, lambda: None)
        self.assertFalse(service.configured)
        with self.assertRaisesRegex(PurchaseAssistantError, 'token'):
            service.search('ORDER')


class PurchaseAssistantHttpTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.service = PurchaseAssistantService(provider=FakeProvider())
        self.hub = FakeHubControls()
        self.hub_capability = {
            'available': True,
            'clientRunning': True,
            'localApiEnabled': True,
            'authenticated': True,
            'apiVersion': 'v1',
            'endpoint': 'http://127.0.0.1:6873/api/v1',
            'reasonCode': 'ok',
            'message': 'HubStudio Local API 已就绪',
        }
        main_module.STATE = SimpleNamespace(purchase_assistant=self.service)
        main_module.STATE.hub = self.hub
        main_module.STATE.hub_capabilities = (
            lambda force=False: dict(self.hub_capability))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        main_module.STATE = self.original_state

    def _get(self, path, headers=None):
        request = Request(self.base_url + path, headers=headers or {})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        payload = json.loads(response.read().decode('utf-8'))
        return response.status, dict(response.headers), payload

    def _post(self, path, payload, headers=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json', **(headers or {})},
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        body = json.loads(response.read().decode('utf-8'))
        return response.status, dict(response.headers), body

    def _pair(self):
        status, headers, payload = self._get(
            '/api/purchase-assistant/v1/session', {
                'Origin': EXTENSION_ORIGIN,
                'X-Xynigo-Client': 'chrome-extension',
                'X-Xynigo-Pairing': 'auto',
            })
        self.assertEqual(status, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'],
                         EXTENSION_ORIGIN)
        return payload['sessionToken']

    def test_pairing_accepts_hubstudio_request_without_origin(self):
        status, headers, payload = self._get(
            '/api/purchase-assistant/v1/session', {
                'X-Xynigo-Client': 'chrome-extension',
                'X-Xynigo-Pairing': 'auto',
            })
        self.assertEqual(status, 200)
        self.assertTrue(payload['sessionToken'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_pairing_rejects_non_extension_web_origin(self):
        status, _headers, payload = self._get(
            '/api/purchase-assistant/v1/session', {
                'Origin': 'https://example.com',
                'X-Xynigo-Client': 'chrome-extension',
                'X-Xynigo-Pairing': 'auto',
            })
        self.assertEqual(status, 403)
        self.assertEqual(payload['code'], 'pairing_denied')

    def test_health_and_pairing_bypass_web_login_without_leaking_rows(self):
        status, _headers, health = self._get(
            '/api/purchase-assistant/v1/health', {
                'Origin': EXTENSION_ORIGIN,
            })
        self.assertEqual(status, 200)
        self.assertTrue(health['configured'])
        self.assertEqual(health['service'], 'xynigo-sourcing')
        self.assertEqual(health['apiVersion'], 2)
        self.assertTrue(health['features']['taskSearch'])
        self.assertTrue(health['features']['recipientRead'])
        self.assertTrue(health['features']['hubStudioAutomation'])
        self.assertNotIn('recipient', health)
        self._pair()

    def test_tasks_require_session_and_recipient_requires_exact_key(self):
        status, _headers, denied = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER', {
                'Origin': EXTENSION_ORIGIN,
            })
        self.assertEqual(status, 401)
        self.assertEqual(denied['code'], 'session_required')
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        status, _headers, listed = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER-DEMO-001',
            headers)
        self.assertEqual(status, 200)
        self.assertEqual(listed['total'], 1)
        self.assertNotIn('recipientName', listed['tasks'][0])
        key = quote(listed['tasks'][0]['taskKey'], safe='')
        status, _headers, detail = self._get(
            '/api/purchase-assistant/v1/tasks/%s/recipient' % key,
            headers)
        self.assertEqual(status, 200)
        self.assertEqual(detail['recipient']['postalCode'], '36000')

    def test_authenticated_get_still_requires_local_extension_source(self):
        token = self._pair()
        status, _headers, payload = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER-DEMO-001', {
                'Origin': EXTENSION_ORIGIN,
                'Authorization': 'Bearer ' + token,
            })
        self.assertEqual(status, 403)
        self.assertEqual(payload['code'], 'origin_forbidden')

    def test_hubstudio_unavailable_does_not_block_recipient_reading(self):
        self.hub_capability.update({
            'available': False,
            'localApiEnabled': False,
            'reasonCode': 'hubstudio_local_api_disabled',
            'message': 'HubStudio Local API 未开启',
        })
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        capability_status, _headers, capability = self._get(
            '/api/purchase-assistant/v1/capabilities', headers)
        self.assertEqual(capability_status, 200)
        self.assertFalse(capability['hubStudio']['available'])
        task_status, _headers, tasks = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER-DEMO-001',
            headers)
        self.assertEqual(task_status, 200)
        key = quote(tasks['tasks'][0]['taskKey'], safe='')
        recipient_status, _headers, recipient = self._get(
            '/api/purchase-assistant/v1/tasks/%s/recipient' % key,
            headers)
        self.assertEqual(recipient_status, 200)
        self.assertEqual(recipient['recipient']['postalCode'], '36000')

    def test_mock_environment_open_close_and_batch_use_restricted_bridge(self):
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        for action in ('open', 'close'):
            status, _response_headers, payload = self._post(
                '/api/purchase-assistant/v1/hub/environments/' + action,
                {'identifier': '4254'}, headers)
            self.assertEqual(status, 200)
            self.assertTrue(payload['ok'])
        status, _response_headers, payload = self._post(
            '/api/purchase-assistant/v1/hub/environments/batch',
            {'action': 'open', 'identifiers': ['4254']}, headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload['ok'])
        self.assertIn(('open', 'container-test-1', False), self.hub.calls)
        self.assertIn(('close', 'container-test-1'), self.hub.calls)
        self.assertIn(('batch', 'open', ['4254'], False), self.hub.calls)

    def test_mock_environment_list_and_locate_return_only_safe_summary(self):
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        status, _response_headers, payload = self._get(
            '/api/purchase-assistant/v1/hub/environments?query=4254',
            headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload['environments'][0]['serialNumber'], '4254')
        self.assertNotIn('remark', payload['environments'][0])
        status, _response_headers, payload = self._get(
            '/api/purchase-assistant/v1/hub/environments/locate?identifier=4254',
            headers)
        self.assertEqual(status, 200)
        self.assertEqual(
            payload['environment']['containerCode'], 'container-test-1')


if __name__ == '__main__':
    unittest.main()
