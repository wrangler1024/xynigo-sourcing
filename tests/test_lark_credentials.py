# -*- coding: utf-8 -*-
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from purchase_tool.lark_credentials import (
    LarkCredentialError, LarkCredentials, MacKeychainCredentialStore,
    MemoryCredentialStore,
    WindowsDpapiCredentialStore, public_credential_status)


class QueueRunner(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.responses.pop(0)


class LarkCredentialTests(unittest.TestCase):
    def test_macos_save_uses_noninteractive_hex_stdin_not_argv(self):
        runner = QueueRunner([
            subprocess.CompletedProcess([], 0, '', ''),
        ])
        store = MacKeychainCredentialStore(
            runner=runner, security_bin='/usr/bin/security')
        store.save('cli_public_demo', 'private-secret-demo')
        argv, kwargs = runner.calls[0]
        self.assertNotIn('private-secret-demo', ' '.join(argv))
        self.assertEqual(argv, ['/usr/bin/security', '-i'])
        payload = json.dumps({
            'app_id': 'cli_public_demo',
            'app_secret': 'private-secret-demo',
        }, ensure_ascii=False, separators=(',', ':'))
        self.assertNotIn(payload, kwargs['input'])
        self.assertNotIn('private-secret-demo', kwargs['input'])
        self.assertIn(payload.encode('utf-8').hex(), kwargs['input'])
        self.assertIn('add-generic-password', kwargs['input'])
        self.assertIn('-X', kwargs['input'])
        self.assertEqual(kwargs['timeout'], 15)

    def test_macos_save_reports_keychain_timeout_without_secret(self):
        def timeout_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))

        store = MacKeychainCredentialStore(runner=timeout_runner)
        with self.assertRaisesRegex(
                LarkCredentialError,
                '保存飞书应用凭证到 macOS 钥匙串超时'):
            store.save('cli_public_demo', 'private-secret-demo')

    def test_macos_load_and_public_status_never_return_secret(self):
        payload = json.dumps({
            'app_id': 'cli_public_demo',
            'app_secret': 'private-secret-demo',
        })
        runner = QueueRunner([
            subprocess.CompletedProcess([], 0, payload, ''),
        ])
        store = MacKeychainCredentialStore(runner=runner)
        status = public_credential_status(store)
        rendered = json.dumps(status)
        self.assertTrue(status['credentialConfigured'])
        self.assertNotIn('private-secret-demo', rendered)
        self.assertNotIn('cli_public_demo', rendered)
        self.assertIsNotNone(store.load())
        self.assertEqual(len(runner.calls), 1)

    def test_macos_denied_keychain_read_is_cached_for_process_lifetime(self):
        runner = QueueRunner([
            subprocess.CompletedProcess([], 44, '', 'denied'),
        ])
        store = MacKeychainCredentialStore(runner=runner)
        self.assertIsNone(store.load())
        self.assertIsNone(store.load())
        self.assertEqual(len(runner.calls), 1)

    def test_windows_store_uses_injected_dpapi_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'feishu.bin'
            store = WindowsDpapiCredentialStore(
                path=path,
                protect_fn=lambda value: b'encrypted:' + value,
                unprotect_fn=lambda value: value.removeprefix(b'encrypted:'))
            store.save('cli_public_demo', 'private-secret-demo')
            self.assertTrue(path.read_bytes().startswith(b'encrypted:'))
            loaded = store.load()
            self.assertEqual(loaded.app_id, 'cli_public_demo')
            self.assertEqual(loaded.app_secret, 'private-secret-demo')
            store.clear()
            self.assertFalse(path.exists())

    def test_memory_store_validates_and_clears(self):
        store = MemoryCredentialStore()
        store.save('cli_public_demo', 'private-secret-demo')
        self.assertIsInstance(store.load(), LarkCredentials)
        store.clear()
        self.assertIsNone(store.load())


if __name__ == '__main__':
    unittest.main()
