# -*- coding: utf-8 -*-
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import purchase_tool.main as main_module
from purchase_tool.main import Handler


LAUNCHER_TOKEN = 'launcher-token-' + ('x' * 40)


class FakeUpdates:
    def __init__(self):
        self.state = 'current'
        self.checked = 0
        self.installed = 0

    def snapshot(self):
        return {
            'enabled': True,
            'state': self.state,
            'installMode': 'standard',
            'currentVersion': '0.12.9',
            'latestVersion': '0.12.10',
            'message': '测试更新状态',
        }

    def check_async(self, force=False):
        self.checked += int(bool(force))
        self.state = 'checking'
        return True

    def prompt_async(self):
        if self.state != 'available':
            return False
        self.installed += 1
        self.state = 'downloading'
        return True


class LauncherUpdateControlTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.updates = FakeUpdates()
        self.tasks = []
        main_module.STATE = SimpleNamespace(
            updates=self.updates,
            tasks=SimpleNamespace(snapshot=lambda: {'tasks': list(self.tasks)}),
        )
        self.token_patch = patch.dict(
            os.environ, {'XYNIGO_LAUNCHER_TOKEN': LAUNCHER_TOKEN})
        self.token_patch.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.token_patch.stop()
        main_module.STATE = self.original_state

    def _post(self, path, token=LAUNCHER_TOKEN):
        request = Request(
            'http://127.0.0.1:%d%s' % (
                self.server.server_address[1], path),
            data=b'',
            method='POST',
            headers={'X-Xynigo-Launcher': token},
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode('utf-8'))

    def test_launcher_token_is_required(self):
        with self.assertRaises(HTTPError) as caught:
            self._post('/executor-control/update/check', token='wrong-token')
        self.assertEqual(caught.exception.code, 403)
        payload = json.loads(caught.exception.read().decode('utf-8'))
        self.assertEqual(payload['code'], 'launcher_control_forbidden')
        self.assertEqual(self.updates.checked, 0)

    def test_check_update_is_local_and_asynchronous(self):
        status, payload = self._post('/executor-control/update/check')
        self.assertEqual(status, 202)
        self.assertTrue(payload['started'])
        self.assertEqual(payload['state'], 'checking')
        self.assertEqual(self.updates.checked, 1)

    def test_install_rejects_active_tasks_then_accepts_idle_executor(self):
        self.updates.state = 'available'
        self.tasks.append({'kind': 'query'})
        with self.assertRaises(HTTPError) as caught:
            self._post('/executor-control/update/install')
        self.assertEqual(caught.exception.code, 409)
        payload = json.loads(caught.exception.read().decode('utf-8'))
        self.assertEqual(payload['code'], 'executor_tasks_active')
        self.assertEqual(self.updates.installed, 0)

        self.tasks.clear()
        status, payload = self._post('/executor-control/update/install')
        self.assertEqual(status, 202)
        self.assertTrue(payload['accepted'])
        self.assertEqual(payload['state'], 'downloading')
        self.assertEqual(self.updates.installed, 1)


if __name__ == '__main__':
    unittest.main()
