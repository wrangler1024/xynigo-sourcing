# -*- coding: utf-8 -*-
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

import purchase_tool.main as main_module

from purchase_tool.data_source_registry import (
    DataSourceMappingRequired,
    DataSourceRegistry,
    DataSourceRegistryError,
    runtime_config_for_source,
)
from purchase_tool.local_config_service import LocalConfigRevisionConflict
from purchase_tool.main import Handler


MEMBER_A = '11111111-1111-4111-8111-111111111111'
MEMBER_B = '22222222-2222-4222-8222-222222222222'


def legacy_config():
    return {
        'purchaseAssistantSourceMode': 'personal',
        'purchaseAssistantPersonalSpreadsheetToken': 'SpreadsheetPersonal123',
        'purchaseAssistantPersonalSheetId': 'sheet_personal',
        'purchaseAssistantPersonalCellRange': 'A1:H',
        'purchaseAssistantPersonalSheetName': '个人速填测试表',
        'purchaseAssistantTeamSpreadsheetToken': 'SpreadsheetTeam123',
        'purchaseAssistantTeamSheetId': 'sheet_team',
        'purchaseAssistantTeamCellRange': 'A1:AQ',
        'purchaseAssistantTeamSheetName': '团队协作测试表',
    }


class DataSourceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / 'local-bindings-v1.json'
        self.registry = DataSourceRegistry(self.path)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_profiles_import_as_candidates_without_global_default(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        sources = snapshot['registry']['dataSources']

        self.assertEqual(len(sources), 2)
        personal = next(item for item in sources
                        if item['scope'] == 'personal')
        team = next(item for item in sources if item['scope'] == 'team')
        self.assertEqual(personal['ownerMemberId'], '')
        self.assertEqual(
            personal['migrationState'], 'needs_owner_confirmation')
        self.assertEqual(team['migrationState'], 'ready')
        self.assertEqual(snapshot['registry']['buyerProfiles'], [])
        self.assertEqual(snapshot['registry']['environmentBindings'], [])
        self.assertEqual(snapshot['registry']['teamDefaultDataSourceId'], '')
        self.assertEqual(
            snapshot['registry']['legacyMigration']['activeMode'], 'personal')
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_A)

    def test_public_snapshot_never_returns_spreadsheet_identifiers(self):
        self.registry.migrate_legacy(legacy_config())

        public = self.registry.public_snapshot(MEMBER_A)
        rendered = json.dumps(public, ensure_ascii=False)

        self.assertEqual(public['counts']['dataSourceCount'], 2)
        self.assertNotIn('SpreadsheetPersonal123', rendered)
        self.assertNotIn('SpreadsheetTeam123', rendered)
        self.assertNotIn('sheet_personal', rendered)
        self.assertNotIn('spreadsheetToken', rendered)
        self.assertNotIn('sheetId', rendered)
        targets = {item['scope']: item for item in public['dataSources']}
        self.assertEqual(
            targets['personal']['targetMasked'],
            'https://*.feishu.cn/sheets/••••l123')
        self.assertEqual(
            targets['team']['targetMasked'],
            'https://*.feishu.cn/sheets/••••m123')
        self.assertEqual(
            targets['personal']['worksheetMasked'], '••••onal')

    def test_source_metadata_can_change_without_exposing_private_target(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')

        updated = self.registry.update_source_metadata(
            team_id, '采购团队默认协作表', False,
            expected_revision=snapshot['registryRevision'])
        source = self.registry.source(team_id)
        public = self.registry.public_snapshot(MEMBER_A, include_all=True)

        self.assertEqual(source['label'], '采购团队默认协作表')
        self.assertFalse(source['enabled'])
        self.assertNotIn('SpreadsheetTeam123', json.dumps(public))
        self.assertNotEqual(
            updated['configRevision'], snapshot['registryRevision'])

    def test_replacing_team_target_preserves_default_and_environment_mapping(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        defaulted = self.registry.set_team_default(
            team_id, expected_revision=snapshot['registryRevision'])
        bound = self.registry.bind_environment(
            'container-001', MEMBER_A, team_id,
            expected_revision=defaulted['configRevision'])

        replaced = self.registry.replace_source_target(
            team_id, {
                'spreadsheetToken': 'SpreadsheetTeamReplacement123',
                'sheetId': 'sheet_team_replacement',
                'cellRange': 'A1:AQ',
                'sheetName': '新团队协作表',
            }, expected_revision=bound['configRevision'])
        registry = self.registry.snapshot()['registry']
        new_id = replaced['dataSourceId']

        self.assertNotEqual(new_id, team_id)
        self.assertEqual(registry['teamDefaultDataSourceId'], new_id)
        self.assertEqual(
            registry['environmentBindings'][0]['dataSourceId'], new_id)
        self.assertEqual(
            registry['legacyMigration']['teamSourceId'], new_id)
        self.assertFalse(any(
            item['id'] == team_id for item in registry['dataSources']))

    def test_migration_is_idempotent_and_does_not_follow_active_mode(self):
        first = self.registry.migrate_legacy(legacy_config())
        changed_legacy = legacy_config()
        changed_legacy['purchaseAssistantSourceMode'] = 'team'
        second = self.registry.migrate_legacy(changed_legacy)

        self.assertEqual(first, second)
        self.assertEqual(
            second['registry']['legacyMigration']['activeMode'], 'personal')

    def test_blank_first_run_can_import_legacy_sources_later_once(self):
        empty = self.registry.migrate_legacy({
            'purchaseAssistantSourceMode': 'team',
        })
        self.assertEqual(empty['registry']['dataSources'], [])

        imported = self.registry.migrate_legacy(legacy_config())
        repeated = self.registry.migrate_legacy(legacy_config())

        self.assertEqual(len(imported['registry']['dataSources']), 2)
        self.assertEqual(imported, repeated)
        self.assertEqual(
            imported['registry']['legacyMigration']['activeMode'], 'team')

    def test_claimed_personal_source_becomes_only_that_members_default(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        personal_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'personal')

        claimed = self.registry.claim_legacy_personal(
            MEMBER_A, personal_id,
            expected_revision=snapshot['registryRevision'])

        source = self.registry.resolve(MEMBER_A)
        self.assertEqual(source['id'], personal_id)
        self.assertEqual(source['ownerMemberId'], MEMBER_A)
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_B)
        with self.assertRaises(DataSourceRegistryError):
            self.registry.claim_legacy_personal(
                MEMBER_B, personal_id,
                expected_revision=claimed['configRevision'])

    def test_exact_container_binding_overrides_member_default(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        personal_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'personal')
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        claimed = self.registry.claim_legacy_personal(
            MEMBER_A, personal_id,
            expected_revision=snapshot['registryRevision'])
        bound = self.registry.bind_environment(
            'container-001', MEMBER_A, team_id,
            expected_revision=claimed['configRevision'])

        default_source = self.registry.resolve(MEMBER_A)
        exact_source = self.registry.resolve(
            MEMBER_A, container_code='container-001')

        self.assertEqual(default_source['id'], personal_id)
        self.assertEqual(exact_source['id'], team_id)
        self.assertNotEqual(
            bound['configRevision'], claimed['configRevision'])

    def test_team_default_requires_policy_and_allowed_source(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        updated = self.registry.set_team_default(
            team_id, expected_revision=snapshot['registryRevision'])

        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_B)
        resolved = self.registry.resolve(
            MEMBER_B, allow_team_default=True,
            allowed_data_source_ids=[team_id])
        self.assertEqual(resolved['id'], team_id)
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(
                MEMBER_B, allow_team_default=True,
                allowed_data_source_ids=[])
        self.assertRegex(updated['configRevision'], r'^[0-9a-f]{64}$')

    def test_validated_personal_sources_are_member_scoped(self):
        first = self.registry.upsert_personal(MEMBER_A, {
            'spreadsheetToken': 'SpreadsheetPersonal123',
            'sheetId': 'sheet_personal',
            'cellRange': 'A1:Q',
            'sheetName': '成员 A 个人表',
        })
        second = self.registry.upsert_personal(MEMBER_B, {
            'spreadsheetToken': 'SpreadsheetPersonal123',
            'sheetId': 'sheet_personal',
            'cellRange': 'A1:Q',
            'sheetName': '成员 B 个人表',
        }, expected_revision=first['configRevision'])

        source_a = self.registry.resolve(MEMBER_A)
        source_b = self.registry.resolve(MEMBER_B)
        self.assertNotEqual(source_a['id'], source_b['id'])
        self.assertEqual(source_a['ownerMemberId'], MEMBER_A)
        self.assertEqual(source_b['ownerMemberId'], MEMBER_B)
        runtime = runtime_config_for_source(legacy_config(), source_a)
        self.assertEqual(
            runtime['purchaseAssistantSpreadsheetToken'],
            'SpreadsheetPersonal123')
        self.assertEqual(runtime['purchaseAssistantSourceMode'], 'personal')
        self.assertEqual(runtime['purchaseAssistantTeamSpreadsheetToken'], '')
        self.assertRegex(second['configRevision'], r'^[0-9a-f]{64}$')

    def test_fresh_install_can_create_and_clear_team_source_policy(self):
        created = self.registry.upsert_team({
            'spreadsheetToken': 'SpreadsheetFreshTeam123',
            'sheetId': 'sheet_fresh_team',
            'cellRange': 'A1:AQ',
            'sheetName': '全新安装团队表',
        }, set_default=True)

        source = self.registry.resolve(
            MEMBER_B, allow_team_default=True)
        public = self.registry.public_snapshot(MEMBER_A, include_all=True)
        rendered = json.dumps(public, ensure_ascii=False)

        self.assertEqual(source['scope'], 'team')
        self.assertEqual(
            public['teamDefaultDataSourceId'], created['dataSourceId'])
        self.assertNotIn('SpreadsheetFreshTeam123', rendered)
        self.assertNotIn('sheet_fresh_team', rendered)

        cleared = self.registry.clear_team_default(
            expected_revision=created['configRevision'])
        self.assertEqual(
            cleared['config']['teamDefaultDataSourceId'], '')
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_B, allow_team_default=True)

    def test_environment_binding_can_be_removed_without_removing_source(self):
        created = self.registry.upsert_team({
            'spreadsheetToken': 'SpreadsheetFreshTeam123',
            'sheetId': 'sheet_fresh_team',
            'cellRange': 'A1:AQ',
            'sheetName': '全新安装团队表',
        })
        bound = self.registry.bind_environment(
            'container-009', MEMBER_A, created['dataSourceId'],
            expected_revision=created['configRevision'])
        removed = self.registry.unbind_environment(
            'container-009', MEMBER_A,
            expected_revision=bound['configRevision'])

        self.assertEqual(removed['config']['environmentBindings'], [])
        self.assertEqual(len(removed['config']['dataSources']), 1)
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_A, container_code='container-009')

    def test_clearing_personal_default_uses_only_explicit_team_policy(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        personal_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'personal')
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        claimed = self.registry.claim_legacy_personal(
            MEMBER_A, personal_id,
            expected_revision=snapshot['registryRevision'])
        with self.assertRaises(DataSourceMappingRequired):
            self.registry.use_team_default(
                MEMBER_A, expected_revision=claimed['configRevision'])
        self.assertEqual(self.registry.resolve(MEMBER_A)['id'], personal_id)
        policy = self.registry.set_team_default(
            team_id, expected_revision=claimed['configRevision'])
        self.registry.use_team_default(
            MEMBER_A, expected_revision=policy['configRevision'])

        with self.assertRaises(DataSourceMappingRequired):
            self.registry.resolve(MEMBER_A)
        self.assertEqual(
            self.registry.resolve(MEMBER_A, allow_team_default=True)['id'],
            team_id)

    def test_stale_revision_and_environment_name_are_rejected(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        with self.assertRaises(LocalConfigRevisionConflict):
            self.registry.set_team_default(
                team_id, expected_revision='0' * 64)
        with self.assertRaisesRegex(
                DataSourceRegistryError, 'containerCode'):
            self.registry.bind_environment(
                '同名环境 001', MEMBER_A, team_id,
                expected_revision=snapshot['registryRevision'])

    def test_cloud_summary_v2_contains_counts_but_no_private_identifiers(self):
        snapshot = self.registry.migrate_legacy(legacy_config())
        personal_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'personal')
        claimed = self.registry.claim_legacy_personal(
            MEMBER_A, personal_id,
            expected_revision=snapshot['registryRevision'])
        team_id = next(
            item['id'] for item in snapshot['registry']['dataSources']
            if item['scope'] == 'team')
        self.registry.set_team_default(
            team_id, expected_revision=claimed['configRevision'])
        state = SimpleNamespace(
            cfg={
                'larkBuyerBaseToken': 'private-base-token',
                'larkBuyerTableId': 'private-table-id',
            },
            local_config=SimpleNamespace(summary=lambda _cfg: {
                'schemaVersion': 2,
                'configRevision': 'a' * 64,
                'runtimeConfig': {
                    'hubPort': 6873,
                    'concurrency': 2,
                    'envCreateWorkers': 5,
                    'verifySampleCount': 1,
                    'safeParallelTasks': True,
                },
            }),
            data_sources=self.registry,
            data_source_registry_error='',
            lark_credentials=SimpleNamespace(load=lambda: object()),
            hub_api_key_store=SimpleNamespace(
                load=lambda: 'private-hub-key'),
        )

        summary = main_module.AppState.config_summary_v2(state)
        rendered = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary['schemaVersion'], 2)
        self.assertEqual(summary['dataSources']['dataSourceCount'], 2)
        self.assertEqual(summary['dataSources']['buyerProfileCount'], 1)
        self.assertTrue(summary['configured']['hubApiKey'])
        self.assertTrue(summary['configured']['larkAppCredentials'])
        for forbidden in (
                'private-base-token', 'private-table-id', 'private-hub-key',
                'SpreadsheetPersonal123', 'SpreadsheetTeam123',
                'spreadsheetToken', 'sheetId', 'containerCode'):
            self.assertNotIn(forbidden, rendered)


class FakeAuth(object):
    def __init__(self, member_id=MEMBER_A, roles=None):
        self.member_id = member_id
        self.roles = list(roles or ['operator'])

    def require(self, permission=None, role=None):
        del permission
        if role and role not in self.roles:
            from purchase_tool.cloud_auth import LocalAuthError
            raise LocalAuthError('permission_denied', status=403)
        return {
            'user': {'id': self.member_id, 'name': '脱敏测试成员'},
            'tenant': {'id': 'tenant-test'},
            'roles': list(self.roles),
            'permissions': [],
        }


class FakePurchaseAssistant(object):
    def __init__(self):
        self.consume_count = 0

    def inspect_source(self, spreadsheet_url, owner_key=''):
        if spreadsheet_url != 'https://example.test/sheets/fresh':
            raise AssertionError('unexpected spreadsheet URL')
        return {
            'inspectionId': 'inspection-safe',
            'sheets': [{
                'selectionId': 'selection-safe',
                'sheetName': '测试工作表',
                'rowCount': 20,
                'columnCount': 43,
                'hidden': False,
            }],
            'expiresInSeconds': 600,
            '_ownerForTest': owner_key,
        }

    def validate_source(self, inspection_id, selection_id, owner_key=''):
        if (inspection_id, selection_id) != (
                'inspection-safe', 'selection-safe'):
            raise AssertionError('unexpected inspection selection')
        return {
            'validationId': 'validation-safe',
            'sheetName': '测试工作表',
            'cellRange': 'A1:AQ',
            'headerCount': 43,
            'requiredFieldCount': 8,
            '_ownerForTest': owner_key,
        }

    def consume_validated_target(self, validation_id, owner_key=''):
        if validation_id != 'validation-safe' or owner_key != MEMBER_A:
            raise AssertionError('unexpected validation owner')
        self.consume_count += 1
        return {
            'spreadsheetToken': 'SpreadsheetFreshRoute123',
            'sheetId': 'sheet_fresh_route',
            'cellRange': 'A1:AQ',
            'sheetName': '测试工作表',
        }

    def revalidate_target(self, target):
        if 'spreadsheetToken' not in target or 'sheetId' not in target:
            raise AssertionError('private target is required internally')
        return {
            'valid': True,
            'sheetName': target.get('sheetName') or '测试工作表',
            'cellRange': target['cellRange'],
            'headerCount': 43,
            'requiredFieldCount': 8,
        }


class FakeHub(object):
    def list_environment_summaries(self, query='', limit=100):
        self.query = query
        self.limit = int(limit)
        return [{
            'containerCode': 'container-001',
            'serialNumber': '7',
            'containerName': '脱敏测试环境',
            'tagName': '采购组',
        }]


class DataSourceRegistryRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry = DataSourceRegistry(
            Path(self.tempdir.name) / 'local-bindings-v1.json')
        self.registry.migrate_legacy(legacy_config())
        self.auth = FakeAuth()
        self.purchase_assistant = FakePurchaseAssistant()
        main_module.STATE = type('State', (), {
            'auth': self.auth,
            'data_sources': self.registry,
            'purchase_assistant': self.purchase_assistant,
            'hub': FakeHub(),
        })()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        main_module.STATE = self.original_state
        self.tempdir.cleanup()

    def _request(self, path, payload=None):
        url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], path)
        data = (json.dumps(payload).encode('utf-8')
                if payload is not None else None)
        request = urllib.request.Request(
            url, data=data, method='POST' if data is not None else 'GET',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode('utf-8'))

    def test_current_member_can_claim_personal_source_without_id_leak(self):
        initial = self._request('/api/local-config/data-sources')
        rendered = json.dumps(initial, ensure_ascii=False)
        personal_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'personal')

        claimed = self._request(
            '/api/local-config/data-sources/claim-personal', {
                'sourceId': personal_id,
                'expectedRevision': initial['registryRevision'],
            })

        self.assertTrue(claimed['saved'])
        self.assertEqual(
            claimed['buyerProfiles'][0]['memberId'], MEMBER_A)
        self.assertNotIn('SpreadsheetPersonal123', rendered)
        self.assertNotIn('spreadsheetToken', rendered)

    def test_admin_can_view_edit_revalidate_and_replace_team_source(self):
        self.auth.roles = ['admin']
        initial = self._request('/api/local-config/data-sources')
        team_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'team')
        renamed = self._request(
            '/api/local-config/data-sources/metadata', {
                'sourceId': team_id,
                'label': '团队采购主表',
                'enabled': True,
                'expectedRevision': initial['registryRevision'],
            })
        checked = self._request(
            '/api/local-config/data-sources/revalidate', {
                'sourceId': team_id,
            })
        replaced = self._request(
            '/api/local-config/data-sources/replace', {
                'sourceId': team_id,
                'validationId': 'validation-safe',
                'expectedRevision': renamed['registryRevision'],
            })
        rendered = json.dumps(replaced, ensure_ascii=False)

        self.assertEqual(
            next(item for item in renamed['dataSources']
                 if item['id'] == team_id)['label'],
            '团队采购主表')
        self.assertTrue(checked['valid'])
        self.assertEqual(checked['headerCount'], 43)
        self.assertTrue(replaced['saved'])
        self.assertNotIn('SpreadsheetFreshRoute123', rendered)
        self.assertNotIn('sheet_fresh_route', rendered)

    def test_non_admin_cannot_edit_team_source(self):
        initial = self._request('/api/local-config/data-sources')
        team_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'team')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self._request('/api/local-config/data-sources/metadata', {
                'sourceId': team_id,
                'label': '不允许修改',
                'enabled': True,
                'expectedRevision': initial['registryRevision'],
            })
        self.assertEqual(denied.exception.code, 403)

    def test_environment_binding_requires_admin_role(self):
        initial = self._request('/api/local-config/data-sources')
        team_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'team')
        body = {
            'memberId': MEMBER_A,
            'containerCode': 'container-001',
            'sourceId': team_id,
            'expectedRevision': initial['registryRevision'],
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self._request(
                '/api/local-config/data-sources/environment-binding', body)
        self.assertEqual(denied.exception.code, 403)

        self.auth.roles = ['admin']
        saved = self._request(
            '/api/local-config/data-sources/environment-binding', body)
        self.assertTrue(saved['saved'])
        self.assertEqual(
            saved['environmentBindings'][0]['containerCode'],
            'container-001')

    def test_current_member_can_create_personal_source_on_fresh_install(self):
        self.registry = DataSourceRegistry(
            Path(self.tempdir.name) / 'fresh-bindings-v1.json')
        main_module.STATE.data_sources = self.registry
        initial = self._request('/api/local-config/data-sources')
        inspected = self._request(
            '/api/local-config/data-sources/inspect', {
                'spreadsheetUrl': 'https://example.test/sheets/fresh',
            })
        checked = self._request(
            '/api/local-config/data-sources/validate', {
                'inspectionId': inspected['inspectionId'],
                'selectionId': inspected['sheets'][0]['selectionId'],
            })
        saved = self._request(
            '/api/local-config/data-sources/personal', {
                'validationId': checked['validationId'],
                'expectedRevision': initial['registryRevision'],
            })
        rendered = json.dumps(saved, ensure_ascii=False)

        self.assertTrue(saved['saved'])
        self.assertEqual(saved['dataSources'][0]['scope'], 'personal')
        self.assertEqual(saved['buyerProfiles'][0]['memberId'], MEMBER_A)
        self.assertNotIn('SpreadsheetFreshRoute123', rendered)
        self.assertNotIn('sheet_fresh_route', rendered)

    def test_team_source_and_environment_options_require_admin(self):
        initial = self._request('/api/local-config/data-sources')
        body = {
            'validationId': 'validation-safe',
            'setDefault': True,
            'expectedRevision': initial['registryRevision'],
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self._request('/api/local-config/data-sources/team', body)
        self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as options_denied:
            self._request(
                '/api/local-config/data-sources/environment-options')
        self.assertEqual(options_denied.exception.code, 403)

        self.auth.roles = ['admin']
        saved = self._request('/api/local-config/data-sources/team', body)
        options = self._request(
            '/api/local-config/data-sources/environment-options?query=test')
        fresh_team = next(
            item for item in saved['dataSources']
            if item['label'] == '团队采购表 · 测试工作表')

        self.assertTrue(saved['saved'])
        self.assertEqual(fresh_team['scope'], 'team')
        self.assertEqual(
            saved['teamDefaultDataSourceId'], fresh_team['id'])
        self.assertEqual(options['environments'][0]['containerCode'],
                         'container-001')

    def test_admin_can_remove_mapping_and_clear_team_default(self):
        self.auth.roles = ['admin']
        initial = self._request('/api/local-config/data-sources')
        team_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'team')
        policy = self._request(
            '/api/local-config/data-sources/team-default', {
                'sourceId': team_id,
                'expectedRevision': initial['registryRevision'],
            })
        bound = self._request(
            '/api/local-config/data-sources/environment-binding', {
                'memberId': MEMBER_A,
                'containerCode': 'container-001',
                'sourceId': team_id,
                'expectedRevision': policy['registryRevision'],
            })
        removed = self._request(
            '/api/local-config/data-sources/environment-binding/remove', {
                'memberId': MEMBER_A,
                'containerCode': 'container-001',
                'expectedRevision': bound['registryRevision'],
            })
        cleared = self._request(
            '/api/local-config/data-sources/team-default/clear', {
                'expectedRevision': removed['registryRevision'],
            })

        self.assertEqual(removed['environmentBindings'], [])
        self.assertEqual(cleared['teamDefaultDataSourceId'], '')

    def test_stale_revision_does_not_consume_validated_source(self):
        initial = self._request('/api/local-config/data-sources')
        team_id = next(
            item['id'] for item in initial['dataSources']
            if item['scope'] == 'team')
        self.registry.set_team_default(
            team_id, expected_revision=initial['registryRevision'])

        with self.assertRaises(urllib.error.HTTPError) as stale:
            self._request('/api/local-config/data-sources/personal', {
                'validationId': 'validation-safe',
                'expectedRevision': initial['registryRevision'],
            })

        payload = json.loads(stale.exception.read().decode('utf-8'))
        self.assertEqual(stale.exception.code, 409)
        self.assertEqual(payload['code'], 'config_revision_conflict')
        self.assertEqual(self.purchase_assistant.consume_count, 0)


if __name__ == '__main__':
    unittest.main()
