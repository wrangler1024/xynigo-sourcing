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
from purchase_tool.cloud_feishu_transport import CloudFeishuTransport
from purchase_tool.data_source_registry import (
    DataSourceMappingRequired, DataSourceRegistry)
from purchase_tool.main import Handler
from purchase_tool.purchase_assistant import (
    MAX_CACHE_TTL_SECONDS,
    PurchaseAssistantConfig,
    PurchaseAssistantError,
    PurchaseAssistantService,
    PurchaseAssistantSheetProvider,
    TaskNotFoundError,
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

    def list_tasks(self, force=False):
        return rows_to_tasks(self.rows)

    def get_recipient(self, key, force=False):
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
    def test_cloud_source_setup_without_local_credentials(self):
        calls = []
        upstream = FakeTransport()

        def cloud_request(path, query, permission):
            calls.append((path, query, permission))
            return upstream.request_json('GET', 'https://open.feishu.cn' + path)

        # Match AppState: the cloud transport replaces the local credential getter.
        service = PurchaseAssistantService.from_runtime_config(
            {}, transport_factory=lambda: CloudFeishuTransport(
                cloud_request, 'assistant.access'))
        inspected = service.inspect_source(
            'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123',
            owner_key=MEMBER_A)
        self.assertEqual(len(inspected['sheets']), 1)
        checked = service.validate_source(
            inspected['inspectionId'], inspected['sheets'][0]['selectionId'],
            owner_key=MEMBER_A)
        target = service.consume_validated_target(
            checked['validationId'], owner_key=MEMBER_A)
        self.assertEqual(target['sheetId'], 'sheet_test')
        self.assertEqual(target['cellRange'], 'A1:Q')
        self.assertTrue(service.revalidate_target(target)['valid'])
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call[2] == 'assistant.access' for call in calls))
        self.assertTrue(all(call[0].startswith('/open-apis/sheets/')
                            for call in calls))
        public_result = json.dumps([inspected, checked], ensure_ascii=False)
        self.assertNotIn('SpreadsheetPersonal123', public_result)
        self.assertNotIn('sheet_test', public_result)

    def test_cloud_source_setup_preserves_proxy_errors(self):
        for message in ('组织尚未配置飞书企业应用，请联系超级管理员',
                        '登录已失效，请重新登录'):
            with self.subTest(message=message):
                def cloud_request(_path, _query, _permission):
                    raise RuntimeError(message)

                service = PurchaseAssistantService.from_runtime_config(
                    {}, transport_factory=lambda: CloudFeishuTransport(
                        cloud_request, 'assistant.access'))
                with self.assertRaisesRegex(PurchaseAssistantError, message):
                    service.inspect_source(
                        'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123',
                        owner_key=MEMBER_A)
                self.assertEqual(service._inspections, {})

    def test_direct_source_setup_requires_local_credentials_before_network(self):
        for getter in (None, lambda: None):
            with self.subTest(getter=getter):
                transport = FakeTransport()
                service = PurchaseAssistantService.from_runtime_config(
                    {}, credential_getter=getter,
                    transport_factory=lambda: transport)
                with self.assertRaisesRegex(PurchaseAssistantError, '凭证尚未配置'):
                    service.inspect_source(
                        'https://tenant.feishu.cn/sheets/SpreadsheetPersonal123',
                        owner_key=MEMBER_A)
                self.assertEqual(transport.calls, [])

    def test_cloud_managed_provider_never_reads_local_secret_and_clears_legacy_after_success(self):
        calls = []
        cleared = []

        def cloud_request(path, query, permission):
            calls.append((path, dict(query), permission))
            row = sample_row()
            headers_row = [key for key in row if key != '__row_number']
            return {
                'code': 0,
                'data': {'valueRange': {'values': [
                    headers_row, [row[key] for key in headers_row],
                ]}},
            }

        provider = PurchaseAssistantSheetProvider(
            PurchaseAssistantConfig(
                spreadsheet_token='spreadsheet-test',
                sheet_id='sheet-test',
            ),
            lambda: self.fail('cloud-managed provider requested local credentials'),
            transport=CloudFeishuTransport(
                cloud_request, 'assistant.access',
                legacy_clearer=lambda: cleared.append(True),
            ),
        )

        tasks = provider.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(calls[0][2], 'assistant.access')
        self.assertTrue(calls[0][0].startswith('/open-apis/sheets/v2/'))
        self.assertNotIn('secret', json.dumps(calls))
        self.assertEqual(cleared, [True])

    def test_cloud_managed_provider_keeps_legacy_credential_when_proxy_fails(self):
        cleared = []

        def cloud_request(_path, _query, _permission):
            raise RuntimeError('cloud unavailable')

        provider = PurchaseAssistantSheetProvider(
            PurchaseAssistantConfig(
                spreadsheet_token='spreadsheet-test',
                sheet_id='sheet-test',
            ),
            None,
            transport=CloudFeishuTransport(
                cloud_request, 'assistant.access',
                legacy_clearer=lambda: cleared.append(True),
            ),
        )
        with self.assertRaisesRegex(PurchaseAssistantError, 'cloud unavailable'):
            provider.list_tasks()
        self.assertEqual(cleared, [])

    def test_task_search_excludes_recipient_and_requires_query(self):
        tasks = rows_to_tasks([sample_row()])
        self.assertEqual(search_tasks(tasks, ''), ([], 0))
        matched, total = search_tasks(tasks, 'ORDER-DEMO-001')
        self.assertEqual(total, 1)
        self.assertRegex(matched[0]['taskKey'], r'^PT1-[0-9a-f]{64}$')
        self.assertEqual(matched[0]['sourceOrderKey'],
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

    def test_split_children_are_independent_through_extension_http_contract(self):
        self.service.provider.rows = [sample_row(**{
            '销售订单号': 'ORDER-DEMO-001-%d' % suffix,
            '__row_number': str(20 + suffix), '主规格': color,
            '地址1': 'Synthetic child address %d' % suffix,
        }) for suffix, color in [(1, 'Black'), (2, 'White')]]
        headers = {'Origin': EXTENSION_ORIGIN,
                   'X-Xynigo-Client': 'chrome-extension',
                   'Authorization': 'Bearer ' + self._pair()}
        keys = []
        for suffix in (1, 2):
            order_no = 'ORDER-DEMO-001-%d' % suffix
            status, _headers, listed = self._get(
                '/api/purchase-assistant/v1/tasks?query=' + order_no, headers)
            self.assertEqual(status, 200)
            self.assertEqual(listed['total'], 1)
            task = listed['tasks'][0]
            self.assertEqual(task['salesOrderNo'], order_no)
            keys.append(task['taskKey'])
            self.assertNotIn('addressLine1', task)
            status, response_headers, detail = self._get(
                '/api/purchase-assistant/v1/tasks/%s/recipient' % quote(task['taskKey'], safe=''), headers)
            self.assertEqual(status, 200)
            self.assertEqual(response_headers['Cache-Control'], 'no-store')
            self.assertEqual(detail['recipient']['addressLine1'], 'Synthetic child address %d' % suffix)
        self.assertNotEqual(keys[0], keys[1])
        status, _headers, blocked = self._get(
            '/api/purchase-assistant/v1/tasks/%s/recipient' % quote(
                self.service.provider.rows[0]['系统订单键'], safe=''), headers)
        self.assertEqual(status, 422)
        self.assertNotIn('recipient', blocked)
        self.assertIn('重新搜索', blocked['error'])

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


class BatchRowsTransport(object):
    """Fake transport whose sheet rows change between full-range fetches."""

    def __init__(self, row_batches):
        self.row_batches = list(row_batches)
        self.values_calls = 0

    def request_json(self, method, url, headers=None, payload=None,
                     timeout=15.0):
        del headers, payload, timeout
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
        index = min(self.values_calls, len(self.row_batches) - 1)
        self.values_calls += 1
        rows = self.row_batches[index]
        headers_row = [
            key for key in rows[0] if key != '__row_number'] if rows else []
        values = [headers_row] + [
            [row.get(key, '') for key in headers_row] for row in rows]
        return {'code': 0, 'data': {'valueRange': {'values': values}}}


def resident_mapping(token='tok-resident-000001', **overrides):
    mapping = {
        'purchaseAssistantSpreadsheetToken': token,
        'purchaseAssistantSheetId': 'sheet_test',
        'purchaseAssistantCellRange': 'A1:R',
        'purchaseAssistantCacheTtlSeconds': 3600,
    }
    mapping.update(overrides)
    return mapping


class PurchaseAssistantResidentCacheTests(unittest.TestCase):
    """Resident per-source providers plus cache-miss fallback refetch."""

    def _service_pair(self, transport, mapping=None):
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        base = PurchaseAssistantService.from_runtime_config(
            mapping or resident_mapping(),
            credential_getter=lambda: credentials,
            transport_factory=lambda: transport)
        request_service = base.for_runtime_config(
            mapping or resident_mapping())
        return base, request_service

    def test_resident_cache_serves_second_request_without_refetch(self):
        transport = BatchRowsTransport([[sample_row()]])
        _base, request_service = self._service_pair(transport)

        first = request_service.search('ORDER-DEMO-001')
        self.assertEqual(len(first[0]), 1)
        request_service.recipient(first[0][0]['taskKey'])
        self.assertEqual(transport.values_calls, 1)

    def test_recipient_refetches_once_when_cache_misses_new_order(self):
        transport = BatchRowsTransport([
            [sample_row()],
            [sample_row(**{
                '销售订单号': 'ORDER-NEW-042',
                '系统订单键': 'demo|ORDER-NEW-042|PACKAGE-NEW-042',
            })],
        ])
        _base, request_service = self._service_pair(transport)

        request_service.search('ORDER-DEMO-001')
        self.assertEqual(transport.values_calls, 1)

        matched, total = request_service.search('ORDER-NEW-042')
        self.assertEqual(total, 1)
        self.assertEqual(transport.values_calls, 2)
        recipient = request_service.recipient(matched[0]['taskKey'])
        self.assertEqual(recipient['recipientName'], 'Lucia Prueba')
        self.assertEqual(transport.values_calls, 2)

    def test_absent_task_on_fresh_rows_skips_refetch(self):
        transport = BatchRowsTransport([[sample_row()]])
        _base, request_service = self._service_pair(transport)

        with self.assertRaises(PurchaseAssistantError) as ctx:
            request_service.recipient('demo|missing|key')
        self.assertIn('协作表中没有对应的采购任务', str(ctx.exception))
        self.assertEqual(transport.values_calls, 1)

    def test_absent_search_after_cached_miss_costs_single_refetch(self):
        transport = BatchRowsTransport([[sample_row()]])
        _base, request_service = self._service_pair(transport)

        request_service.search('ORDER-DEMO-001')
        self.assertEqual(transport.values_calls, 1)
        matched, total = request_service.search('NOTHING-MATCHES')
        self.assertEqual(total, 0)
        self.assertEqual(matched, [])
        self.assertEqual(transport.values_calls, 2)

    def test_distinct_source_configs_keep_separate_resident_providers(self):
        transport = BatchRowsTransport([[sample_row()]])
        credentials = SimpleNamespace(
            app_id='cli_test', app_secret='secret-for-test')
        base = PurchaseAssistantService.from_runtime_config(
            resident_mapping(),
            credential_getter=lambda: credentials,
            transport_factory=lambda: transport)
        first = base.for_runtime_config(resident_mapping(
            token='tok-source-a-0001'))
        second = base.for_runtime_config(resident_mapping(
            token='tok-source-b-0001'))
        first.search('ORDER-DEMO-001')
        second.search('ORDER-DEMO-001')
        self.assertEqual(transport.values_calls, 2)

    def test_task_not_found_error_stays_a_purchase_assistant_error(self):
        with self.assertRaises(PurchaseAssistantError) as ctx:
            find_recipient([sample_row()], 'demo|missing|key')
        self.assertIsInstance(ctx.exception, TaskNotFoundError)

    def test_cache_ttl_accepts_hour_scale_and_rejects_beyond(self):
        config = PurchaseAssistantConfig.from_runtime_config(resident_mapping())
        self.assertEqual(config.cache_ttl_seconds, 3600)
        self.assertEqual(MAX_CACHE_TTL_SECONDS, 3600)
        with self.assertRaises(PurchaseAssistantError) as ctx:
            PurchaseAssistantConfig.from_runtime_config(
                resident_mapping(**{'purchaseAssistantCacheTtlSeconds': 3601}))
        self.assertIn('0 到 3600 秒', str(ctx.exception))


class FakeLocalConfigService(object):
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.commits = []

    def load(self):
        return dict(self.cfg)

    def commit(self, cfg, source='test', expected_revision=None):
        del source, expected_revision
        self.cfg = dict(cfg)
        self.commits.append(dict(self.cfg))
        return {'config': dict(self.cfg),
                'configRevision': 'rev-%d' % len(self.commits),
                'changedFields': ['purchaseAssistantCacheTtlSeconds']}

    def revision(self, cfg):
        del cfg
        return 'rev-test'


class PurchaseAssistantCacheTtlRouteTests(unittest.TestCase):
    """桌面端缓存时间档位入口：云端校验通过才落盘。"""

    def setUp(self):
        self.original_state = main_module.STATE
        self.original_config_service = main_module.state_local_config_service
        self.service = PurchaseAssistantService(provider=FakeProvider())
        self.config_service = FakeLocalConfigService({})
        main_module.state_local_config_service = lambda: self.config_service
        self.source = {
            'id': 'ds_' + 'a' * 24,
            'scope': 'personal',
            'ownerMemberId': MEMBER_A,
            'enabled': True,
            'spreadsheetToken': 'spreadsheet-source',
            'sheetId': 'sheet_test',
            'cellRange': 'A1:R',
            'sheetName': '收件信息（粘贴区）',
        }
        self.validation_result = {
            'valid': True, 'sheetName': '收件信息（粘贴区）',
            'cellRange': 'A1:Q', 'headerCount': 17,
        }
        main_module.STATE = SimpleNamespace(
            purchase_assistant=self.service,
            data_sources=SimpleNamespace(
                source=lambda source_id: self.source,
                public_snapshot=lambda member_id, include_all=False: {
                    'dataSources': [], 'buyerProfiles': [],
                    'registryRevision': 'reg-1'}),
            cfg={'purchaseAssistantCacheTtlSeconds': 8},
            config_lock=None,
            auth=SimpleNamespace(require=lambda *args, **kwargs: {
                'user': {'id': MEMBER_A, 'name': '脱敏测试成员'},
                'tenant': {'id': 'tenant-test'},
                'roles': ['operator'],
                'permissions': [],
            }))
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
        main_module.state_local_config_service = (
            self.original_config_service)

    def _post(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        return response.status, json.loads(response.read().decode('utf-8'))

    def test_cache_ttl_save_runs_cloud_validation_then_persists(self):
        calls = []
        self.service.revalidate_target = lambda target: (
            calls.append(dict(target)), dict(self.validation_result))[1]
        status, payload = self._post(
            '/api/local-config/data-sources/cache-ttl',
            {'ttlSeconds': 1800, 'sourceId': self.source['id']})
        self.assertEqual(status, 200)
        self.assertTrue(payload['saved'])
        self.assertEqual(payload['ttlSeconds'], 1800)
        self.assertTrue(payload['validation']['valid'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['id'], self.source['id'])
        self.assertEqual(
            self.config_service.cfg['purchaseAssistantCacheTtlSeconds'],
            1800)
        self.assertEqual(
            main_module.STATE.cfg['purchaseAssistantCacheTtlSeconds'], 1800)
        self.assertEqual(len(self.config_service.commits), 1)

    def test_cache_ttl_save_is_rejected_when_cloud_validation_fails(self):
        def broken(target):
            del target
            raise PurchaseAssistantError('数据源云端校验未通过')
        self.service.revalidate_target = broken
        status, payload = self._post(
            '/api/local-config/data-sources/cache-ttl',
            {'ttlSeconds': 1800, 'sourceId': self.source['id']})
        self.assertEqual(status, 422)
        self.assertIn('校验未通过', payload['error'])
        self.assertNotIn('saved', payload)
        self.assertEqual(self.config_service.commits, [])
        self.assertEqual(
            main_module.STATE.cfg['purchaseAssistantCacheTtlSeconds'], 8)

    def test_cache_ttl_rejects_values_beyond_supported_range(self):
        status, payload = self._post(
            '/api/local-config/data-sources/cache-ttl',
            {'ttlSeconds': 9999, 'sourceId': self.source['id']})
        self.assertEqual(status, 422)
        self.assertIn('0 到 3600 秒', payload['error'])
        self.assertEqual(self.config_service.commits, [])

    def test_data_source_snapshot_exposes_current_cache_ttl(self):
        request = Request(self.base_url + '/api/local-config/data-sources')
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        payload = json.loads(response.read().decode('utf-8'))
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['cacheTtlSeconds'], 8)
