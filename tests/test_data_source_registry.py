# -*- coding: utf-8 -*-
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import purchase_tool.main as main_module

from purchase_tool.data_source_registry import (
    DataSourceMappingRequired,
    DataSourceRegistry,
    DataSourceRegistryError,
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


class DataSourceRegistryRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.tempdir = tempfile.TemporaryDirectory()
        self.registry = DataSourceRegistry(
            Path(self.tempdir.name) / 'local-bindings-v1.json')
        self.registry.migrate_legacy(legacy_config())
        self.auth = FakeAuth()
        main_module.STATE = type('State', (), {
            'auth': self.auth,
            'data_sources': self.registry,
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


if __name__ == '__main__':
    unittest.main()
