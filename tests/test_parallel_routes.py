# -*- coding: utf-8 -*-
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import purchase_tool.main as main_module
from purchase_tool.main import Handler
from purchase_tool.task_runtime import LocalTaskCoordinator


class FakeHub(object):
    def __init__(self):
        self.envs = [{
            'serialNumber': 1001,
            'containerCode': 'code-1001',
            'containerName': 'XG-MX-0824-001',
        }]

    def env_list(self, _group=None):
        return [dict(item) for item in self.envs]

    def env_by_serial(self, serial):
        return next((dict(item) for item in self.envs
                     if str(item['serialNumber']) == str(serial)), None)


class FakeOrchestrator(object):
    def __init__(self):
        self.running = False
        self.callback = None

    def start_batch(self, _serials, _index, site='MX', on_finished=None):
        self.running = True
        self.callback = on_finished

    def snapshot(self):
        return {'rows': [], 'running': self.running}

    def request_stop(self):
        pass


class FakeEnvJob(object):
    def __init__(self, resource_name='NEW-MX-0825-001'):
        self.running = False
        self.callback = None
        self.resource_name = resource_name

    def start(self, *_args, reserve_resources=None, on_finished=None,
              **_kwargs):
        if reserve_resources:
            reserve_resources({'name:' + self.resource_name.casefold()})
        self.running = True
        self.callback = on_finished
        return 1


class ParallelRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.safe_parallel = True
        self.orch = FakeOrchestrator()
        self.env_job = FakeEnvJob()
        self.state = SimpleNamespace(
            cfg={'safeParallelTasks': True},
            auth=SimpleNamespace(
                require=lambda permission=None, role=None: {
                    'authenticated': True,
                    'permission': permission,
                    'role': role,
                }),
            hub=FakeHub(),
            orch=self.orch,
            env_job=self.env_job,
            backup_job=SimpleNamespace(running=False),
            reg_job=SimpleNamespace(running=False),
        )
        self.state.tasks = LocalTaskCoordinator(
            lambda: self.safe_parallel)
        main_module.STATE = self.state
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        main_module.STATE = self.original_state

    def post(self, path, payload):
        request = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (
                self.server.server_address[1], path),
            data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode('utf-8'))

    def post_error(self, path, payload):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(path, payload)
        return caught.exception.code, json.loads(
            caught.exception.read().decode('utf-8'))

    def test_query_and_environment_creation_can_run_together(self):
        status, query = self.post('/api/query', {
            'serials': ['1001'], 'site': 'MX'})
        self.assertEqual(status, 200)
        status, env = self.post('/api/envbatch/start', {
            'planId': 'safe-test', 'assignment': '1:新刚',
            'purchaseDate': '20260825', 'confirmWrite': True})
        self.assertEqual(status, 200)
        self.assertNotEqual(query['taskId'], env['taskId'])
        self.assertEqual(len(self.state.tasks.snapshot()['tasks']), 2)
        self.orch.callback()
        self.env_job.callback()
        self.assertFalse(self.state.tasks.running())

    def test_compatibility_mode_returns_409(self):
        self.safe_parallel = False
        self.post('/api/query', {'serials': ['1001'], 'site': 'MX'})
        status, body = self.post_error('/api/envbatch/start', {
            'planId': 'safe-test', 'assignment': '1:新刚',
            'purchaseDate': '20260825', 'confirmWrite': True})
        self.assertEqual(status, 409)
        self.assertIn('订单物流查询正在进行', body['error'])
        self.orch.callback()

    def test_resource_collision_returns_409_and_releases_failed_task(self):
        self.env_job.resource_name = 'XG-MX-0824-001'
        self.post('/api/query', {'serials': ['1001'], 'site': 'MX'})
        status, body = self.post_error('/api/envbatch/start', {
            'planId': 'safe-test', 'assignment': '1:新刚',
            'purchaseDate': '20260825', 'confirmWrite': True})
        self.assertEqual(status, 409)
        self.assertIn('目标环境', body['error'])
        tasks = self.state.tasks.snapshot()['tasks']
        self.assertEqual([task['kind'] for task in tasks], ['query'])
        self.orch.callback()

    def test_runtime_config_changes_are_blocked_while_task_is_running(self):
        self.post('/api/query', {'serials': ['1001'], 'site': 'MX'})
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                main_module, 'CONFIG_PATH', tmp + '/config.json'):
            status, body = self.post_error(
                '/api/config', {'hubPort': 6999})
        self.assertEqual(status, 409)
        self.assertIn('后台任务运行中', body['error'])
        self.orch.callback()


if __name__ == '__main__':
    unittest.main()
