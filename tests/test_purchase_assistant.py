# -*- coding: utf-8 -*-
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import purchase_tool.main as main_module
from purchase_tool.data_source_registry import (
    DataSourceMappingRequired, DataSourceRegistry)
from purchase_tool.main import Handler
from purchase_tool.purchase_assistant import (
    PurchaseAssistantConfig,
    PurchaseAssistantError,
    PurchaseAssistantService,
    PurchaseAssistantSheetProvider,
    find_recipient,
    parse_spreadsheet_url,
    rows_to_tasks,
    search_tasks,
    validate_source_headers,
)


EXTENSION_ORIGIN = 'chrome-extension://' + 'a' * 32
MEMBER_A = '11111111-1111-4111-8111-111111111111'
MEMBER_B = '22222222-2222-4222-8222-222222222222'


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
        if url.endswith('/sheets/query'):
            return {
                'code': 0,
                'data': {'revision': 1, 'sheets': [{
                    'resource_type': 'sheet',
                    'sheet_id': 'sheet_test',
                    'title': '收件信息（粘贴区）',
                    'grid_properties': {
                        'row_count': 500,
                        'column_count': 20,
                    },
                    'hidden': False,
                }]},
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
        with self.assertRaisesRegex(PurchaseAssistantError, '收件信息数据源'):
            service.search('ORDER')

    def test_personal_sheet_inspection_uses_opaque_ids_and_validates_headers(self):
        transport = FakeTransport()
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        mapping = {
            'purchaseAssistantSourceMode': 'team',
            'purchaseAssistantSpreadsheetToken': 'spreadsheet-team',
            'purchaseAssistantSheetId': 'sheet_team',
            'purchaseAssistantCellRange': 'A1:AQ',
            'purchaseAssistantTeamSpreadsheetToken': 'spreadsheet-team',
            'purchaseAssistantTeamSheetId': 'sheet_team',
            'purchaseAssistantTeamCellRange': 'A1:AQ',
            'purchaseAssistantTeamSheetName': '采购执行协作区',
        }
        service = PurchaseAssistantService(
            credential_getter=lambda: credentials,
            source_config=mapping,
            transport_factory=lambda: transport,
        )
        service.reconfigure(mapping)
        inspected = service.inspect_source(
            'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123',
            owner_key=MEMBER_A)
        self.assertEqual(len(inspected['sheets']), 1)
        self.assertNotIn('sheetId', inspected['sheets'][0])
        self.assertNotIn('spreadsheetToken', inspected)
        with self.assertRaisesRegex(PurchaseAssistantError, '当前登录成员'):
            service.validate_source(
                inspected['inspectionId'],
                inspected['sheets'][0]['selectionId'],
                owner_key=MEMBER_B)
        checked = service.validate_source(
            inspected['inspectionId'],
            inspected['sheets'][0]['selectionId'],
            owner_key=MEMBER_A)
        self.assertEqual(checked['cellRange'], 'A1:Q')
        self.assertNotIn('sheetId', checked)
        with self.assertRaisesRegex(PurchaseAssistantError, '当前登录成员'):
            service.consume_validated_target(
                checked['validationId'], owner_key=MEMBER_B)
        target = service.consume_validated_target(
            checked['validationId'], owner_key=MEMBER_A)
        self.assertEqual(target['spreadsheetToken'], 'SpreadsheetPersonal123')
        self.assertEqual(target['sheetId'], 'sheet_test')

    def test_sheet_url_and_header_contract_fail_closed(self):
        self.assertEqual(
            parse_spreadsheet_url(
                'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123'),
            'SpreadsheetPersonal123')
        with self.assertRaisesRegex(PurchaseAssistantError, '企业飞书'):
            parse_spreadsheet_url(
                'https://example.com/sheets/SpreadsheetPersonal123')
        with self.assertRaisesRegex(PurchaseAssistantError, '缺少必要字段'):
            validate_source_headers([['销售订单号', '收货人姓名']])

    def test_stored_source_can_be_revalidated_without_returning_private_ids(self):
        transport = FakeTransport()
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        service = PurchaseAssistantService(
            credential_getter=lambda: credentials,
            source_config={
                'purchaseAssistantApiBase':
                    'https://open.feishu.cn/open-apis',
            },
            transport_factory=lambda: transport,
        )
        checked = service.revalidate_target({
            'spreadsheetToken': 'SpreadsheetPersonal123',
            'sheetId': 'sheet_test',
            'cellRange': 'A1:H',
            'sheetName': '收件信息（粘贴区）',
        })
        rendered = json.dumps(checked, ensure_ascii=False)

        self.assertTrue(checked['valid'])
        self.assertEqual(checked['cellRange'], 'A1:Q')
        self.assertEqual(checked['headerCount'], 17)
        self.assertNotIn('SpreadsheetPersonal123', rendered)
        self.assertNotIn('sheet_test', rendered)

    def test_app_state_saves_member_profile_without_mutating_global_config(self):
        transport = FakeTransport()
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        mapping = {
            'purchaseAssistantSourceMode': 'team',
            'purchaseAssistantSpreadsheetToken': 'SpreadsheetTeam123',
            'purchaseAssistantSheetId': 'sheet_team',
            'purchaseAssistantCellRange': 'A1:AQ',
            'purchaseAssistantApiBase': 'https://open.feishu.cn/open-apis',
            'purchaseAssistantCacheTtlSeconds': 8,
            'purchaseAssistantTeamSpreadsheetToken': 'SpreadsheetTeam123',
            'purchaseAssistantTeamSheetId': 'sheet_team',
            'purchaseAssistantTeamCellRange': 'A1:AQ',
            'purchaseAssistantTeamSheetName': '采购执行协作区',
        }
        service = PurchaseAssistantService(
            credential_getter=lambda: credentials,
            source_config=mapping,
            transport_factory=lambda: transport,
        )
        service.reconfigure(mapping)
        session_token = service.session_token
        inspected = service.inspect_source(
            'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123',
            owner_key=MEMBER_A)
        checked = service.validate_source(
            inspected['inspectionId'],
            inspected['sheets'][0]['selectionId'],
            owner_key=MEMBER_A)
        with tempfile.TemporaryDirectory() as tempdir:
            registry = DataSourceRegistry(
                Path(tempdir) / 'local-bindings-v1.json')
            migrated = registry.migrate_legacy(mapping)
            team_id = next(
                item['id'] for item in migrated['registry']['dataSources']
                if item['scope'] == 'team')
            registry.set_team_default(
                team_id,
                expected_revision=migrated['registryRevision'])
            state = SimpleNamespace(
                config_lock=threading.RLock(),
                cfg=dict(mapping),
                data_sources=registry,
                data_source_registry_error='',
                purchase_assistant=service,
            )
            state.purchase_assistant_for_member = (
                lambda member_id, container_code='':
                    main_module.AppState.purchase_assistant_for_member(
                        state, member_id, container_code))
            personal = main_module.AppState.apply_purchase_assistant_source(
                state, MEMBER_A, 'personal', checked['validationId'])
            self.assertEqual(personal['mode'], 'personal')
            self.assertEqual(
                registry.resolve(MEMBER_A)['cellRange'], 'A1:Q')
            self.assertEqual(service.session_token, session_token)
            team = main_module.AppState.apply_purchase_assistant_source(
                state, MEMBER_A, 'team')
            self.assertEqual(team['mode'], 'team')
            self.assertEqual(
                state.cfg['purchaseAssistantSpreadsheetToken'],
                'SpreadsheetTeam123')
        self.assertEqual(
            service.session_token, session_token)


class PurchaseAssistantHttpTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.service = PurchaseAssistantService(
            provider=FakeProvider(),
            source_config={
                'purchaseAssistantSourceMode': 'team',
                'purchaseAssistantTeamSpreadsheetToken': 'spreadsheet-team',
                'purchaseAssistantTeamSheetId': 'sheet-team',
                'purchaseAssistantTeamCellRange': 'A1:AQ',
                'purchaseAssistantTeamSheetName': '采购执行协作区',
            },
        )
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
        self.member_id = MEMBER_A
        self.member_services = {
            MEMBER_A: self.service,
            MEMBER_B: self.service,
        }
        self.member_service_calls = []
        main_module.STATE = SimpleNamespace(purchase_assistant=self.service)
        main_module.STATE.auth = SimpleNamespace(require=lambda *args, **kwargs: {
            'user': {'id': self.member_id, 'name': '脱敏测试成员'},
            'tenant': {'id': 'tenant-test'},
            'roles': ['operator'],
            'permissions': [],
        })
        def service_for_member(member_id, container_code=''):
            self.member_service_calls.append((member_id, container_code))
            scoped = self.member_services[member_id]
            status = scoped.source_status()
            status.update({
                'dataSourceId': 'ds_' + '1' * 24,
                'scope': 'team',
                'label': '脱敏团队数据源',
                'resolution': 'team_default',
            })
            return scoped, status
        main_module.STATE.purchase_assistant_for_member = service_for_member
        main_module.STATE.hub = self.hub
        main_module.STATE.hub_capabilities = (
            lambda force=False: dict(self.hub_capability))
        main_module.STATE.apply_purchase_assistant_source = (
            lambda member_id, mode, validation_id='',
            expected_revision=None: service_for_member(member_id)[1])
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
        self.assertEqual(health['apiVersion'], 4)
        self.assertTrue(health['features']['taskSearch'])
        self.assertTrue(health['features']['recipientRead'])
        self.assertFalse(health['features']['sourceConfiguration'])
        self.assertTrue(health['features']['desktopManagedDataSources'])
        self.assertTrue(health['features']['memberScopedDataSources'])
        self.assertFalse(health['features']['environmentScopedDataSources'])
        self.assertTrue(health['features']['hubStudioAutomation'])
        self.assertEqual(health['settingsUrl'], 'xynigo://settings')
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

    def test_extension_session_still_requires_current_feishu_login(self):
        from purchase_tool.cloud_auth import LocalAuthError
        token = self._pair()
        main_module.STATE.auth.require = lambda *args, **kwargs: (
            (_ for _ in ()).throw(LocalAuthError(
                'authentication_required', status=401)))
        status, _headers, payload = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER', {
                'Origin': EXTENSION_ORIGIN,
                'X-Xynigo-Client': 'chrome-extension',
                'Authorization': 'Bearer ' + token,
            })
        self.assertEqual(status, 401)
        self.assertEqual(payload['code'], 'authentication_required')

    def test_member_switch_selects_a_new_request_scoped_provider(self):
        provider_b = FakeProvider()
        provider_b.rows = [sample_row(
            **{'销售订单号': 'ORDER-MEMBER-B',
               '系统订单键': 'demo|ORDER-MEMBER-B'})]
        service_b = PurchaseAssistantService(
            provider=provider_b,
            source_config=self.service.source_config,
        )
        self.member_services[MEMBER_B] = service_b
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        self.member_id = MEMBER_B
        status, _headers, payload = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER-MEMBER-B'
            '&containerCode=container-member-b', headers)

        self.assertEqual(status, 200)
        self.assertEqual(payload['tasks'][0]['salesOrderNo'], 'ORDER-MEMBER-B')
        self.assertEqual(payload['source']['management'], 'desktop')
        self.assertEqual(payload['source']['member']['name'], '脱敏测试成员')
        self.assertFalse(payload['source']['containerContextApplied'])
        self.assertIn((MEMBER_B, 'container-member-b'),
                      self.member_service_calls)

    def test_missing_member_mapping_fails_closed_with_stable_code(self):
        def missing(_member_id, container_code=''):
            del container_code
            raise DataSourceMappingRequired()
        main_module.STATE.purchase_assistant_for_member = missing
        token = self._pair()
        status, _headers, payload = self._get(
            '/api/purchase-assistant/v1/tasks?query=ORDER', {
                'Origin': EXTENSION_ORIGIN,
                'X-Xynigo-Client': 'chrome-extension',
                'Authorization': 'Bearer ' + token,
            })
        self.assertEqual(status, 409)
        self.assertEqual(payload['code'], 'data_source_mapping_required')

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

    def test_data_source_status_is_read_only_and_does_not_depend_on_hubstudio(self):
        self.hub_capability.update({
            'available': False,
            'reasonCode': 'hubstudio_local_api_disabled',
            'message': 'HubStudio Local API 未开启',
        })
        token = self._pair()
        headers = {
            'Origin': EXTENSION_ORIGIN,
            'X-Xynigo-Client': 'chrome-extension',
            'Authorization': 'Bearer ' + token,
        }
        status, _response_headers, payload = self._get(
            '/api/purchase-assistant/v1/data-source', headers)
        self.assertEqual(status, 200)
        self.assertEqual(payload['source']['mode'], 'team')
        self.assertEqual(payload['source']['management'], 'desktop')
        self.assertEqual(payload['source']['resolution'], 'team_default')
        self.assertEqual(payload['source']['settingsUrl'], 'xynigo://settings')
        self.assertNotIn('spreadsheetToken', json.dumps(payload))
        status, _response_headers, payload = self._post(
            '/api/purchase-assistant/v1/data-source/save',
            {'mode': 'team'}, headers)
        self.assertEqual(status, 410)
        self.assertEqual(payload['code'], 'local_config_desktop_only')
        self.assertEqual(payload['settingsUrl'], 'xynigo://settings')

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
