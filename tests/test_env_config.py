# -*- coding: utf-8 -*-
import json
import http.client
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import purchase_tool.main as main_module
from purchase_tool.hub_api_key import MemoryHubApiKeyStore
from purchase_tool.main import (
    AppState, Handler, default_config, effective_proxy_link, load_config,
    lark_target_link, public_config, public_executor_config, public_lark_config,
    public_envbatch_preferences, public_lark_runtime_status, save_config,
    purchase_tag_for_site,
    refreshed_lark_target_labels, resolve_submitted_lark_target,
    updated_config, updated_envbatch_preferences, updated_executor_config,
    updated_lark_config)
from purchase_tool.lark_credentials import MemoryCredentialStore
from purchase_tool.lark_links import resolve_lark_ledger_link


TEST_TAG = 'MX-Purchase'
TEST_PROXY = 'https://proxy.example.test/{region}'


class ConfigTests(unittest.TestCase):
    def test_workspace_snapshot_is_aggregated_versioned_and_non_sensitive(self):
        state = object.__new__(AppState)
        state.cfg = default_config()
        state.cfg.update({
            'purchaseSite': 'MX',
            'purchaseTags': {'MX': 'MX采购', 'US': '美国采购'},
            'proxyLink': TEST_PROXY,
        })
        state.hub_groups = lambda: ['MX采购', '美国采购', 'MX采购']
        state.hub_status = lambda force=False: (True, '')
        state.env_job = SimpleNamespace(preflight=lambda site: {
            'ready': True,
            'hubConnected': True,
            'groupFound': True,
            'proxyConfigured': True,
            'purchaseTag': state.cfg['purchaseTags'][site],
            'configuredWorkers': 5,
            'effectiveWorkers': 5,
            'message': '预检通过',
        })

        snapshot = state.workspace_snapshot()

        self.assertEqual(snapshot['schemaVersion'], 1)
        self.assertEqual(snapshot['groups'], ['MX采购', '美国采购'])
        self.assertEqual(len(snapshot['snapshotRevision']), 64)
        self.assertEqual(set(snapshot['preflight']), {'MX', 'US'})
        self.assertEqual(
            snapshot['runtimeConfig']['configRevision'],
            main_module.config_revision(public_executor_config(state.cfg)))
        self.assertEqual(snapshot['runtimeConfig']['envCreateWorkers'], 5)
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(TEST_PROXY, rendered)
        self.assertNotIn('proxyLink', rendered)

    def test_safe_parallel_mode_is_explicit_boolean_and_defaults_on(self):
        default = default_config()
        self.assertTrue(default['safeParallelTasks'])
        disabled = updated_config(default, {'safeParallelTasks': False})
        self.assertFalse(disabled['safeParallelTasks'])
        self.assertFalse(public_config(disabled)['safeParallelTasks'])
        with self.assertRaisesRegex(ValueError, '布尔值'):
            updated_config(default, {'safeParallelTasks': 'true'})

    def test_atomic_private_save_load_and_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            cfg = default_config()
            cfg.update({'purchaseTag': TEST_TAG, 'proxyLink': TEST_PROXY})
            with patch.object(main_module, 'CONFIG_PATH', config_path), \
                    patch('purchase_tool.main.os.replace', wraps=os.replace) as replace:
                save_config(cfg)
                loaded = load_config()
            replace.assert_called_once()
            self.assertEqual(loaded['purchaseTag'], TEST_TAG)
            self.assertEqual(loaded['proxyLink'], TEST_PROXY)
            if os.name != 'nt':
                self.assertEqual(Path(config_path).stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(Path(tmp).glob('.config-*.tmp')))

            before = Path(config_path).read_bytes()
            with patch.object(main_module, 'CONFIG_PATH', config_path), \
                    patch('purchase_tool.main.os.replace',
                          side_effect=OSError('simulated replacement failure')):
                with self.assertRaises(OSError):
                    save_config(cfg)
            self.assertEqual(Path(config_path).read_bytes(), before)
            self.assertFalse(list(Path(tmp).glob('.config-*.tmp')))

            cfg['unexpected'] = 'must-not-be-written'
            with patch.object(main_module, 'CONFIG_PATH', config_path):
                with self.assertRaisesRegex(ValueError, '不允许'):
                    save_config(cfg)
            self.assertNotIn(
                'unexpected', json.loads(Path(config_path).read_text('utf-8')))

    def test_blank_proxy_preserves_and_clear_is_explicit(self):
        old = default_config()
        old.update({'purchaseTag': TEST_TAG, 'proxyLink': TEST_PROXY})
        group_only = updated_config(old, {'purchaseTag': 'MX-Other'})
        self.assertEqual(group_only['purchaseTag'], 'MX-Other')
        self.assertEqual(group_only['purchaseTags']['MX'], 'MX-Other')
        self.assertEqual(group_only['proxyLink'], TEST_PROXY)
        us_group = updated_config(group_only, {
            'purchaseSite': 'US',
            'purchaseTags': {'US': 'US-Purchase'},
        })
        self.assertEqual(purchase_tag_for_site(us_group, 'MX'), 'MX-Other')
        self.assertEqual(purchase_tag_for_site(us_group, 'US'), 'US-Purchase')
        self.assertEqual(us_group['proxyLink'], TEST_PROXY)
        kept = updated_config(old, {
            'purchaseTag': TEST_TAG,
            'proxyLink': '',
            'proxyClear': False,
        })
        self.assertEqual(kept['proxyLink'], TEST_PROXY)
        cleared = updated_config(old, {
            'purchaseTag': TEST_TAG,
            'proxyLink': '',
            'proxyClear': True,
        })
        self.assertEqual(cleared['proxyLink'], '')
        with self.assertRaisesRegex(ValueError, '不允许'):
            updated_config(old, {'arbitrarySecret': 'no'})
        with self.assertRaisesRegex(ValueError, '布尔值'):
            updated_config(old, {
                'purchaseTag': TEST_TAG,
                'proxyClear': 'false',
            })
        with self.assertRaisesRegex(ValueError, 'http'):
            updated_config(old, {
                'purchaseTag': TEST_TAG,
                'proxyLink': 'file:///tmp/not-allowed',
            })
        with self.assertRaisesRegex(ValueError, '格式无效'):
            updated_config(old, {
                'purchaseTag': TEST_TAG,
                'proxyLink': '\n' + TEST_PROXY,
            })

    def test_public_config_never_returns_proxy_link(self):
        public = public_config({
            'hubPort': 6873,
            'purchaseTag': TEST_TAG,
            'proxyLink': TEST_PROXY,
        })
        rendered = json.dumps(public)
        self.assertTrue(public['proxyConfigured'])
        self.assertEqual(public['proxyMasked'],
                         'https://proxy.example.test/…')
        self.assertEqual(public['purchaseTags']['MX'], TEST_TAG)
        self.assertEqual(public['purchaseTags']['US'], '')
        self.assertNotIn('proxyLink', public)
        self.assertNotIn(TEST_PROXY, rendered)

    def test_legacy_purchase_assistant_target_migrates_to_private_team_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / 'config.json'
            config_path.write_text(json.dumps({
                'purchaseAssistantSpreadsheetToken': 'SpreadsheetTeam123',
                'purchaseAssistantSheetId': 'sheet_team',
                'purchaseAssistantCellRange': 'A1:AQ',
            }), encoding='utf-8')
            with patch.object(main_module, 'CONFIG_PATH', str(config_path)):
                loaded = load_config()
        self.assertEqual(loaded['purchaseAssistantSourceMode'], 'team')
        self.assertEqual(
            loaded['purchaseAssistantTeamSpreadsheetToken'],
            'SpreadsheetTeam123')
        self.assertEqual(loaded['purchaseAssistantTeamSheetId'], 'sheet_team')
        rendered = json.dumps(public_config(loaded), ensure_ascii=False)
        self.assertNotIn('SpreadsheetTeam123', rendered)
        self.assertNotIn('purchaseAssistant', rendered)

    def test_cloud_executor_config_contains_only_runtime_and_safety_fields(self):
        cfg = default_config()
        cfg.update({
            'purchaseSite': 'US',
            'purchaseTags': {'MX': 'MX采购', 'US': 'US采购'},
            'importBuyerPlan': '1:新刚',
            'proxyLink': TEST_PROXY,
            'hiddenQueryColumns': ['envName'],
        })
        projected = public_executor_config(cfg)
        self.assertEqual(set(projected), {
            'hubPort',
            'concurrency',
            'envCreateWorkers',
            'verifySampleCount',
            'safeParallelTasks',
        })
        rendered = json.dumps(projected, ensure_ascii=False)
        for legacy_value in ('purchaseSite', 'purchaseTags',
                             'importBuyerPlan', 'proxyLink',
                             'hiddenQueryColumns', '新刚'):
            self.assertNotIn(legacy_value, rendered)

    def test_cloud_executor_write_preserves_unrelated_legacy_preferences(self):
        old = default_config()
        old.update({
            'purchaseSite': 'US',
            'purchaseTags': {
                'MX': '历史分组名称超过十二个字符也不应影响并发修改',
                'US': '美国历史分组',
            },
            'importBuyerPlan': '历史自由格式',
            'proxyLink': TEST_PROXY,
        })
        updated = updated_executor_config(old, {
            'concurrency': 3,
            'safeParallelTasks': True,
        })
        self.assertEqual(updated['concurrency'], 3)
        self.assertTrue(updated['safeParallelTasks'])
        for key in ('purchaseSite', 'purchaseTags', 'importBuyerPlan',
                    'proxyLink'):
            self.assertEqual(updated[key], old[key])
        with self.assertRaisesRegex(ValueError, '不允许'):
            updated_executor_config(old, {'purchaseSite': 'MX'})

    def test_environment_preferences_only_expose_and_update_site_groups(self):
        old = default_config()
        old.update({
            'purchaseSite': 'MX',
            'purchaseTags': {'MX': '希音墨西哥采购', 'US': ''},
            'proxyLink': TEST_PROXY,
        })
        updated = updated_envbatch_preferences(old, {
            'purchaseSite': 'US',
            'purchaseTags': {'US': '美国采购分组'},
        })
        self.assertEqual(updated['purchaseSite'], 'US')
        self.assertEqual(updated['purchaseTags']['MX'], '希音墨西哥采购')
        self.assertEqual(updated['purchaseTags']['US'], '美国采购分组')
        public = public_envbatch_preferences(updated)
        self.assertEqual(public['purchaseSite'], 'US')
        self.assertEqual(public['purchaseTags']['US'], '美国采购分组')
        self.assertNotIn('hubPort', public)
        self.assertNotIn('proxyLink', json.dumps(public, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, '只允许'):
            updated_envbatch_preferences(old, {'hubPort': 6874})
        with self.assertRaisesRegex(ValueError, '没有可保存'):
            updated_envbatch_preferences(old, {})
        with self.assertRaisesRegex(ValueError, '美国站不能使用墨西哥'):
            updated_envbatch_preferences(old, {
                'purchaseSite': 'US',
                'purchaseTags': {'US': '希音墨西哥采购'},
            })
        with self.assertRaisesRegex(ValueError, '墨西哥站不能使用美国'):
            updated_envbatch_preferences(old, {
                'purchaseSite': 'MX',
                'purchaseTags': {'MX': '美国采购分组'},
            })

    def test_lark_config_preserves_blanks_and_never_returns_identifiers(self):
        old = default_config()
        old.update({
            'larkBuyerBaseToken': 'bascnPublicSafeExample',
            'larkBuyerTableId': 'tblPublicSafeExample',
            'larkBuyerBaseName': '公开脱敏测试 Base',
            'larkBuyerTableName': '测试数据表',
            'larkBuyerTargetVerified': True,
        })
        kept = updated_lark_config(old, {
            'appId': '', 'appSecret': '', 'ledgerUrl': '',
        })
        self.assertEqual(
            kept['larkBuyerBaseToken'], 'bascnPublicSafeExample')
        self.assertEqual(
            kept['larkBuyerTableId'], 'tblPublicSafeExample')

        ledger_url = ('https://public-safe.feishu.cn/base/'
                      'bascnAnotherSafeExample?table=tblAnotherSafeExample')
        replaced = updated_lark_config(
            old, {'ledgerUrl': ledger_url},
            resolve_lark_ledger_link(ledger_url))
        self.assertEqual(
            replaced['larkBuyerBaseToken'], 'bascnAnotherSafeExample')
        self.assertEqual(
            replaced['larkBuyerTableId'], 'tblAnotherSafeExample')
        self.assertEqual(
            replaced['larkBuyerTargetHost'], 'public-safe.feishu.cn')
        self.assertEqual(replaced['larkBuyerBaseName'], '')
        self.assertEqual(replaced['larkBuyerTableName'], '')
        self.assertFalse(replaced['larkBuyerTargetVerified'])

        cleared = updated_lark_config(old, {'clearLedgerTarget': True})
        self.assertEqual(cleared['larkBuyerBaseToken'], '')
        self.assertEqual(cleared['larkBuyerTableId'], '')
        self.assertEqual(cleared['larkBuyerTargetHost'], '')
        self.assertEqual(cleared['larkBuyerBaseName'], '')
        self.assertEqual(cleared['larkBuyerTableName'], '')
        with self.assertRaisesRegex(ValueError, '尚未完成解析'):
            updated_lark_config(old, {'ledgerUrl': ledger_url})
        with self.assertRaisesRegex(ValueError, '不能同时选择'):
            updated_lark_config(old, {
                'ledgerUrl': ledger_url, 'clearLedgerTarget': True})

        store = MemoryCredentialStore()
        store.save('cli_public_safe_example', 'sanitized-secret-value')
        public = public_lark_config(replaced, store)
        rendered = json.dumps(public)
        self.assertTrue(public['ready'])
        self.assertTrue(public['credentialConfigured'])
        self.assertTrue(public['ledgerTargetConfigured'])
        self.assertEqual(public['targetBaseName'], '')
        self.assertEqual(public['targetTableName'], '')
        self.assertFalse(public['targetVerified'])
        self.assertNotIn('bascnAnotherSafeExample', rendered)
        self.assertNotIn('tblAnotherSafeExample', rendered)
        self.assertNotIn('public-safe.feishu.cn', rendered)
        self.assertNotIn('sanitized-secret-value', rendered)

        runtime = public_lark_runtime_status(replaced, store)
        runtime_rendered = json.dumps(runtime)
        self.assertTrue(runtime['ready'])
        self.assertEqual(runtime['targetBaseName'], '')
        self.assertNotIn('credentialConfigured', runtime)
        self.assertNotIn('appIdMasked', runtime)
        self.assertNotIn('bascnAnotherSafeExample', runtime_rendered)
        self.assertNotIn('tblAnotherSafeExample', runtime_rendered)

        self.assertEqual(
            lark_target_link(replaced),
            'https://public-safe.feishu.cn/base/'
            'bascnAnotherSafeExample?table=tblAnotherSafeExample')

    def test_refreshed_lark_target_labels_exposes_names_not_identifiers(self):
        cfg = default_config()
        cfg.update({
            'larkBuyerBaseToken': 'bascnPublicSafeExample',
            'larkBuyerTableId': 'tblPublicSafeExample',
        })
        store = MemoryCredentialStore()
        store.save('cli_public_safe_example', 'sanitized-secret-value')

        class FakeClient(object):
            def get_target_metadata(self):
                return {
                    'base_name': '公开脱敏测试 Base',
                    'table_name': '买家号统一台账（测试）',
                }

        with patch('purchase_tool.main.build_buyer_ledger_service') as build:
            build.return_value.client = FakeClient()
            refreshed = refreshed_lark_target_labels(cfg, store)
        public = public_lark_config(refreshed, store)
        rendered = json.dumps(public, ensure_ascii=False)
        self.assertTrue(public['targetVerified'])
        self.assertEqual(public['targetBaseName'], '公开脱敏测试 Base')
        self.assertEqual(public['targetTableName'], '买家号统一台账（测试）')
        self.assertNotIn('bascnPublicSafeExample', rendered)
        self.assertNotIn('tblPublicSafeExample', rendered)

    def test_wiki_link_resolution_uses_stored_credentials_with_fake_client(self):
        store = MemoryCredentialStore()
        store.save('cli_public_safe_example', 'sanitized-secret-value')
        captured = {}

        class FakeClient(object):
            def __init__(self, credential_provider, base_token, table_id):
                captured['credentials'] = credential_provider()
                captured['target'] = (base_token, table_id)

            def get_wiki_node(self, node_token):
                captured['nodeToken'] = node_token
                return {
                    'obj_type': 'bitable',
                    'obj_token': 'bascnPublicSafeExample',
                }

        target = resolve_submitted_lark_target({
            'ledgerUrl': ('https://public-safe.feishu.cn/wiki/'
                          'wikcnPublicSafeExample'
                          '?table=tblPublicSafeExample'),
        }, store, client_factory=FakeClient)
        self.assertEqual(target.base_token, 'bascnPublicSafeExample')
        self.assertEqual(target.table_id, 'tblPublicSafeExample')
        self.assertEqual(captured['target'], ('', ''))
        self.assertEqual(captured['nodeToken'], 'wikcnPublicSafeExample')
        self.assertEqual(
            captured['credentials'].app_id, 'cli_public_safe_example')

    def test_buyer_roster_public_and_template_validation(self):
        public = public_config(default_config())
        self.assertEqual([b['code'] for b in public['buyers']],
                         ['XG', 'ZH', 'KD', 'YH'])
        self.assertEqual([b['name'] for b in public['buyers']],
                         ['新刚', '志恒', '康德', '宇航'])
        self.assertEqual(public['buyerDefaultSplit'], ['新刚', '志恒', '康德'])
        self.assertEqual(public['backupMaxCount'], 25)
        ok = updated_config(default_config(), {'importBuyerPlan': '2:XG,1:志恒'})
        self.assertEqual(ok['importBuyerPlan'], '2:XG,1:志恒')
        kept = updated_config(default_config(), {'concurrency': 3})
        self.assertEqual(kept['importBuyerPlan'], '1:新刚')
        with self.assertRaisesRegex(ValueError, '不在名单内'):
            updated_config(default_config(), {'importBuyerPlan': '1:Operator-A'})
        with self.assertRaises(ValueError):
            updated_config(default_config(), {'importBuyerPlan': '新刚'})

    def test_env_create_workers_setting(self):
        # 模块三建环境并发：纯 API 路径，默认 5、1-10 可调
        self.assertEqual(default_config()['envCreateWorkers'], 5)
        cfg = updated_config(default_config(), {'envCreateWorkers': 8})
        self.assertEqual(cfg['envCreateWorkers'], 8)
        for bad in (0, 11, 'x'):
            with self.assertRaisesRegex(ValueError, '1-10'):
                updated_config(default_config(), {'envCreateWorkers': bad})
        public = public_config(default_config())
        self.assertEqual(public['envCreateWorkers'], 5)

    def test_proxy_link_falls_back_to_builtin_default(self):
        # 前期写死：未配置/已清除 → 内置默认；自定义 → 覆盖默认
        from purchase_tool.env_batch import (DEFAULT_PROXY_LINK,
                                              validate_proxy_link)
        validate_proxy_link(DEFAULT_PROXY_LINK)   # 内置默认必须通过校验
        self.assertEqual(effective_proxy_link({}), DEFAULT_PROXY_LINK)
        self.assertEqual(effective_proxy_link({'proxyLink': ''}),
                         DEFAULT_PROXY_LINK)
        self.assertEqual(effective_proxy_link({'proxyLink': TEST_PROXY}),
                         TEST_PROXY)
        public = public_config({'hubPort': 6873})
        self.assertTrue(public['proxyConfigured'])
        self.assertEqual(public['proxySource'], 'default')
        self.assertNotIn('proxyLink', public)

        custom = updated_config(default_config(), {'proxyLink': TEST_PROXY})
        self.assertEqual(custom['proxyLink'], TEST_PROXY)
        cleared = updated_config(custom, {'proxyLink': '', 'proxyClear': True})
        self.assertEqual(cleared['proxyLink'], '')
        self.assertEqual(effective_proxy_link(cleared), DEFAULT_PROXY_LINK)


class ConfigRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        env_job = SimpleNamespace(preflight=lambda site='MX': {
            'ready': True,
            'hubConnected': True,
            'site': site,
            'purchaseTag': TEST_TAG,
            'proxyConfigured': True,
            'groupFound': True,
            'message': '执行前预检通过',
        })
        main_module.STATE = SimpleNamespace(
            cfg={'hubPort': 6873, 'purchaseTag': TEST_TAG,
                 'proxyLink': TEST_PROXY,
                 'larkBuyerBaseToken': 'bascnPublicSafeExample',
                 'larkBuyerTableId': 'tblPublicSafeExample'},
            config_lock=threading.RLock(),
            tasks=SimpleNamespace(
                snapshot=lambda: {'tasks': []}, running=lambda: False),
            auth=SimpleNamespace(require=lambda permission=None, role=None: {
                'roles': ['super_admin'],
                'permissions': ['system.lark_connection.manage'],
            }),
            lark_credentials=MemoryCredentialStore(),
            hub_api_key_store=MemoryHubApiKeyStore(
                'private-hub-key-1234'),
            env_job=env_job,
            reconnect_hub=lambda: True,
            hub_status=lambda: (True, ''))
        main_module.STATE.lark_credentials.save(
            'cli_public_safe_example', 'sanitized-secret-value')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        main_module.STATE = self.original_state

    def _get_json(self, path):
        url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], path)
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.read().decode('utf-8')

    def _post_json(self, path, payload):
        url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], path)
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode('utf-8'))

    def test_get_config_and_preflight_do_not_leak_proxy(self):
        config_text = self._get_json('/api/config')
        lark_config_text = self._get_json('/api/lark/config')
        preflight_text = self._get_json('/api/envbatch/preflight')
        preflight_us_text = self._get_json(
            '/api/envbatch/preflight?site=US')
        self.assertNotIn(TEST_PROXY, config_text)
        self.assertNotIn('proxyLink', config_text)
        self.assertTrue(json.loads(config_text)['proxyConfigured'])
        self.assertEqual(
            json.loads(config_text)['hubApiKeyMasked'], '••••1234')
        self.assertNotIn('private-hub-key-1234', config_text)
        self.assertRegex(
            json.loads(config_text)['configRevision'], r'^[0-9a-f]{64}$')
        self.assertTrue(json.loads(lark_config_text)['managedInCloud'])
        self.assertTrue(json.loads(lark_config_text)['legacyCredentialPresent'])
        self.assertNotIn('appIdMasked', lark_config_text)
        self.assertNotIn('bascnPublicSafeExample', lark_config_text)
        self.assertNotIn('tblPublicSafeExample', lark_config_text)
        self.assertNotIn('sanitized-secret-value', lark_config_text)
        self.assertNotIn(TEST_PROXY, preflight_text)
        self.assertTrue(json.loads(preflight_text)['ready'])
        self.assertEqual(json.loads(preflight_us_text)['site'], 'US')

    def test_environment_preferences_persist_us_site_and_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            initial = default_config()
            initial['purchaseTags'] = {
                'MX': '希音墨西哥采购',
                'US': '',
            }
            initial['purchaseTag'] = '希音墨西哥采购'
            with patch.object(main_module, 'CONFIG_PATH', config_path):
                save_config(initial)
                main_module.STATE.cfg = initial
                response = self._post_json('/api/envbatch/preferences', {
                    'purchaseSite': 'US',
                    'purchaseTags': {'US': '美国采购分组'},
                })
                reloaded = load_config()
                persisted = json.loads(
                    self._get_json('/api/envbatch/preferences'))
        self.assertTrue(response['saved'])
        self.assertEqual(response['purchaseSite'], 'US')
        self.assertEqual(response['purchaseTags']['US'], '美国采购分组')
        self.assertEqual(reloaded['purchaseSite'], 'US')
        self.assertEqual(reloaded['purchaseTags']['MX'], '希音墨西哥采购')
        self.assertEqual(reloaded['purchaseTags']['US'], '美国采购分组')
        self.assertEqual(persisted['purchaseSite'], 'US')
        self.assertNotIn('hubPort', persisted)
        self.assertNotIn(TEST_PROXY, json.dumps(persisted))

    def test_config_route_uses_expected_revision_and_reports_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            initial = default_config()
            with patch.object(main_module, 'CONFIG_PATH', config_path):
                save_config(initial)
                main_module.STATE.cfg = initial
                snapshot = json.loads(self._get_json('/api/config'))
                response = self._post_json('/api/config', {
                    'expectedRevision': snapshot['configRevision'],
                    'hubPort': 6999,
                })
                self.assertTrue(response['saved'])
                self.assertNotEqual(
                    response['configRevision'], snapshot['configRevision'])
                self.assertEqual(response['changedFields'], ['hubPort'])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json('/api/config', {
                        'expectedRevision': snapshot['configRevision'],
                        'hubPort': 7000,
                    })
                conflict = json.loads(caught.exception.read().decode('utf-8'))
                reloaded = load_config()
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(conflict['code'], 'config_revision_conflict')
        self.assertEqual(conflict['configRevision'],
                         response['configRevision'])
        self.assertEqual(reloaded['hubPort'], 6999)

    def test_lark_unified_ledger_template_is_downloadable(self):
        url = 'http://127.0.0.1:%d/api/lark/template' % (
            self.server.server_address[1])
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read()
            self.assertEqual(
                response.headers.get_content_type(),
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet')
            self.assertIn(
                'attachment; filename*=UTF-8',
                response.headers.get('Content-Disposition') or '')
        self.assertEqual(body[:2], b'PK')
        self.assertGreater(len(body), 1000)

    def test_lark_target_name_link_redirects_without_public_config_leak(self):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_address[1], timeout=3)
        try:
            connection.request('GET', '/api/lark/open-target')
            response = connection.getresponse()
            self.assertEqual(response.status, 302)
            self.assertEqual(
                response.getheader('Location'),
                'https://www.feishu.cn/base/'
                'bascnPublicSafeExample?table=tblPublicSafeExample')
            self.assertEqual(response.getheader('Referrer-Policy'), 'no-referrer')
            self.assertEqual(response.read(), b'')
        finally:
            connection.close()

    def test_post_lark_config_is_cloud_managed_and_never_mutates_local_secret(self):
        old_credentials = main_module.STATE.lark_credentials.load()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            initial = default_config()
            with patch.object(main_module, 'CONFIG_PATH', config_path):
                save_config(initial)
                main_module.STATE.cfg = initial
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self._post_json('/api/lark/config', {
                        'appId': 'cli_replacement_example',
                        'appSecret': 'replacement-secret-value',
                    })
                error_body = caught.exception.read().decode('utf-8')
                persisted = load_config()
        restored = main_module.STATE.lark_credentials.load()
        self.assertEqual(caught.exception.code, 410)
        self.assertEqual(restored, old_credentials)
        self.assertEqual(persisted, initial)
        self.assertNotIn('replacement-secret-value', error_body)

    def test_preflight_persists_target_names_before_schema_validation(self):
        fake_client = SimpleNamespace(
            get_target_metadata=lambda: {
                'base_name': '公开脱敏测试 Base',
                'table_name': '买家号统一台账（测试）',
            },
            list_fields=lambda: [])
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            with patch.object(main_module, 'CONFIG_PATH', config_path), \
                    patch('purchase_tool.main.build_buyer_ledger_service') as build, \
                    patch('purchase_tool.main.validate_unified_schema',
                          side_effect=ValueError('模拟字段契约不一致')):
                build.return_value.client = fake_client
                with self.assertRaises(urllib.error.HTTPError):
                    self._post_json('/api/lark/preflight', {})
                state = load_config()
        self.assertTrue(state['larkBuyerTargetVerified'])
        self.assertEqual(state['larkBuyerBaseName'], '公开脱敏测试 Base')
        self.assertEqual(
            state['larkBuyerTableName'], '买家号统一台账（测试）')

    def test_target_metadata_route_refreshes_names_without_field_read(self):
        fake_client = SimpleNamespace(
            get_target_metadata=lambda: {
                'base_name': '公开脱敏测试 Base',
                'table_name': '买家号统一台账（测试）',
            },
            list_fields=lambda: self.fail('名称刷新不应读取字段'))
        with tempfile.TemporaryDirectory() as tmp:
            config_path = str(Path(tmp) / 'config.json')
            with patch.object(main_module, 'CONFIG_PATH', config_path), \
                    patch('purchase_tool.main.build_buyer_ledger_service') as build:
                build.return_value.client = fake_client
                response = self._post_json(
                    '/api/lark/target-metadata', {})
        self.assertTrue(response['refreshed'])
        self.assertTrue(response['targetVerified'])
        self.assertEqual(response['targetBaseName'], '公开脱敏测试 Base')
        self.assertEqual(
            response['targetTableName'], '买家号统一台账（测试）')


if __name__ == '__main__':
    unittest.main()
