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
from purchase_tool.hub_api import HubApiError
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
        self.preflight_error = None
        self.preflight_calls = []
        self.start_calls = []

    def preflight_batch(self, serials, index, site='MX',
                        browser_mode='headless'):
        self.preflight_calls.append(
            (list(serials), dict(index), site, browser_mode))
        if self.preflight_error is not None:
            raise self.preflight_error
        return {'checked': True}

    def start_batch(self, serials, index, site='MX', on_finished=None,
                    browser_mode='headless'):
        self.running = True
        self.callback = on_finished
        self.start_calls.append(
            (list(serials), dict(index), site, browser_mode))

    def snapshot(self):
        return {'rows': [], 'running': self.running}

    def request_stop(self):
        pass


class FakeHubCoreRepair(object):
    def __init__(self):
        self.actor = None

    def snapshot(self):
        return {
            'state': 'required', 'running': False,
            'browserType': 'chrome', 'coreVersion': '148',
            'repairAvailable': True,
        }

    def start(self, actor=None):
        self.actor = actor
        return {
            'state': 'downloading', 'running': True,
            'browserType': 'chrome', 'coreVersion': '148',
            'repairAvailable': False,
        }


class FakeEnvJob(object):
    def __init__(self, resource_name='NEW-MX-0825-001'):
        self.running = False
        self.callback = None
        self.resource_name = resource_name
        self.stop_calls = 0
        self.retry_failed_calls = 0

    def start(self, *_args, reserve_resources=None, on_finished=None,
              **_kwargs):
        if reserve_resources:
            reserve_resources({'name:' + self.resource_name.casefold()})
        self.running = True
        self.callback = on_finished
        return 1

    def request_stop(self):
        self.stop_calls += 1
        return {'stopping': True, 'stopRequested': True}

    def retry_failed(self, reserve_resources=None, on_finished=None):
        self.retry_failed_calls += 1
        if reserve_resources:
            reserve_resources({
                'name:retry-one-mx', 'name:retry-two-mx'})
        self.running = True
        self.callback = lambda: on_finished(['account-one', 'account-two'])
        return 2


class ParallelRouteTests(unittest.TestCase):
    def setUp(self):
        self.original_state = main_module.STATE
        self.safe_parallel = True
        self.orch = FakeOrchestrator()
        self.env_job = FakeEnvJob()
        self.state = SimpleNamespace(
            cfg={'safeParallelTasks': True, 'queryBrowserMode': 'headless'},
            auth=SimpleNamespace(
                require=lambda permission=None, role=None: {
                    'authenticated': True,
                    'permission': permission,
                    'role': role,
                }),
            hub=FakeHub(),
            orch=self.orch,
            hub_core_repair=FakeHubCoreRepair(),
            env_job=self.env_job,
            backup_job=FakeEnvJob('BACKUP-MX-0825-001'),
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

    def get(self, path):
        with urllib.request.urlopen(
                'http://127.0.0.1:%d%s' % (
                    self.server.server_address[1], path), timeout=3) as response:
            return response.status, json.loads(
                response.read().decode('utf-8'))

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

    def test_hub_core_repair_status_and_start_routes(self):
        status, snapshot = self.get('/api/hub-core-repair/status')
        self.assertEqual(status, 200)
        self.assertEqual(snapshot['coreVersion'], '148')

        status, started = self.post('/api/hub-core-repair/start', {})
        self.assertEqual(status, 202)
        self.assertTrue(started['running'])
        actor = self.state.hub_core_repair.actor
        self.assertEqual(actor['permission'], 'system.integration.manage')
        self.assertEqual(actor['role'], 'super_admin')

    def test_query_rejects_missing_browser_core_before_task_is_created(self):
        self.orch.preflight_error = HubApiError(
            'HubStudio 浏览器内核不存在',
            'hubstudio_browser_core_missing', api_code=-10007)

        status, body = self.post_error('/api/query', {
            'serials': ['1001'], 'site': 'MX'})

        self.assertEqual(status, 503)
        self.assertEqual(body['code'], 'hubstudio_browser_core_missing')
        self.assertIn('内核不存在', body['error'])
        self.assertEqual(self.orch.start_calls, [])
        self.assertFalse(self.state.tasks.running())

    def test_environment_stop_routes_request_cooperative_stop(self):
        self.post('/api/envbatch/start', {
            'planId': 'safe-test', 'assignment': '1:新刚',
            'purchaseDate': '20260825', 'confirmWrite': True})
        status, body = self.post('/api/envbatch/stop', {})
        self.assertEqual(status, 202)
        self.assertTrue(body['stopRequested'])
        self.assertEqual(self.env_job.stop_calls, 1)
        self.assertEqual(len(self.state.tasks.snapshot()['tasks']), 1)
        self.env_job.callback()

        status, started = self.post('/api/envbatch/backup/start', {
            'buyer': '新刚', 'count': 1, 'type': '备用',
            'purchaseDate': '20260825', 'confirmWrite': True})
        self.assertEqual(status, 200)
        self.assertTrue(started['started'])
        status, body = self.post('/api/envbatch/backup/stop', {})
        self.assertEqual(status, 202)
        self.assertTrue(body['stopRequested'])
        self.assertEqual(self.state.backup_job.stop_calls, 1)
        self.assertEqual(len(self.state.tasks.snapshot()['tasks']), 1)
        self.state.backup_job.callback()

    def test_environment_batch_retry_route_keeps_task_until_finished(self):
        status, body = self.post('/api/envbatch/retry-failed', {})
        self.assertEqual(status, 200)
        self.assertTrue(body['started'])
        self.assertEqual(body['count'], 2)
        self.assertEqual(self.env_job.retry_failed_calls, 1)
        self.assertEqual(len(self.state.tasks.snapshot()['tasks']), 1)
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

    def test_local_config_write_is_blocked_during_cloud_config_task(self):
        task_id = self.state.tasks.begin('config')
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                main_module, 'CONFIG_PATH', tmp + '/config.json'):
            status, body = self.post_error(
                '/api/config', {'importBuyerPlan': '1:新刚'})
        self.state.tasks.finish(task_id)
        self.assertEqual(status, 409)
        self.assertIn('云端配置请求正在处理', body['error'])


if __name__ == '__main__':
    unittest.main()
