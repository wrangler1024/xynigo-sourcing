# -*- coding: utf-8 -*-
import json
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
from purchase_tool.main import (
    Handler, default_config, effective_proxy_link, load_config,
    public_config, save_config, purchase_tag_for_site, updated_config)


TEST_TAG = 'MX-Purchase'
TEST_PROXY = 'https://proxy.example.test/{region}'


class ConfigTests(unittest.TestCase):
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
        self.assertEqual(public['purchaseTags']['MX'], TEST_TAG)
        self.assertEqual(public['purchaseTags']['US'], '')
        self.assertNotIn('proxyLink', public)
        self.assertNotIn(TEST_PROXY, rendered)

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
                 'proxyLink': TEST_PROXY},
            env_job=env_job)
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

    def test_get_config_and_preflight_do_not_leak_proxy(self):
        config_text = self._get_json('/api/config')
        preflight_text = self._get_json('/api/envbatch/preflight')
        preflight_us_text = self._get_json(
            '/api/envbatch/preflight?site=US')
        self.assertNotIn(TEST_PROXY, config_text)
        self.assertNotIn('proxyLink', config_text)
        self.assertTrue(json.loads(config_text)['proxyConfigured'])
        self.assertNotIn(TEST_PROXY, preflight_text)
        self.assertTrue(json.loads(preflight_text)['ready'])
        self.assertEqual(json.loads(preflight_us_text)['site'], 'US')


if __name__ == '__main__':
    unittest.main()
