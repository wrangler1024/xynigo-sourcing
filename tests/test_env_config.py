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
    Handler, default_config, load_config, public_config, save_config,
    updated_config)


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
        self.assertNotIn('proxyLink', public)
        self.assertNotIn(TEST_PROXY, rendered)


class ConfigRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        env_job = SimpleNamespace(preflight=lambda: {
            'ready': True,
            'hubConnected': True,
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
        self.assertNotIn(TEST_PROXY, config_text)
        self.assertNotIn('proxyLink', config_text)
        self.assertTrue(json.loads(config_text)['proxyConfigured'])
        self.assertNotIn(TEST_PROXY, preflight_text)
        self.assertTrue(json.loads(preflight_text)['ready'])


if __name__ == '__main__':
    unittest.main()
