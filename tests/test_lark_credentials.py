# -*- coding: utf-8 -*-
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from purchase_tool.lark_credentials import (
    LarkCredentials, MacKeychainCredentialStore, MemoryCredentialStore,
    WindowsDpapiCredentialStore, public_credential_status)


class QueueRunner(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return self.responses.pop(0)


class LarkCredentialTests(unittest.TestCase):
    def test_macos_save_feeds_secret_on_stdin_not_argv(self):
        runner = QueueRunner([
            subprocess.CompletedProcess([], 0, '', ''),
        ])
        store = MacKeychainCredentialStore(
            runner=runner, security_bin='/usr/bin/security')
        store.save('cli_public_demo', 'private-secret-demo')
        argv, kwargs = runner.calls[0]
        self.assertNotIn('private-secret-demo', ' '.join(argv))
        self.assertIn('private-secret-demo', kwargs['input'])
        self.assertEqual(argv[-1], '-w')

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
