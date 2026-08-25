# -*- coding: utf-8 -*-
import json
import threading
import time
import unittest

from purchase_tool.hub_api import HubStudioApi
from purchase_tool.task_runtime import (
    HubRuntimeGate, LocalTaskCoordinator, TaskConflict,
    environment_resources)


class TaskCoordinatorTests(unittest.TestCase):
    def test_compatibility_mode_preserves_serial_execution(self):
        coordinator = LocalTaskCoordinator(lambda: False)
        query_id = coordinator.begin('query', {'code:100'})
        with self.assertRaisesRegex(TaskConflict, '订单物流查询正在进行'):
            coordinator.begin('env_batch', {'name:new'})
        coordinator.finish(query_id)
        self.assertFalse(coordinator.running())

    def test_safe_mode_allows_query_with_one_environment_job(self):
        coordinator = LocalTaskCoordinator(lambda: True)
        query_id = coordinator.begin('query', {'code:100'})
        env_id = coordinator.begin('env_batch', {'name:new'})
        snap = coordinator.snapshot()
        self.assertEqual(len(snap['tasks']), 2)
        self.assertTrue(snap['safeParallel'])
        with self.assertRaisesRegex(TaskConflict, '买家号建环境正在进行'):
            coordinator.begin('backup_env', {'name:backup'})
        coordinator.finish(query_id)
        coordinator.finish(env_id)

    def test_same_environment_is_rejected_even_for_compatible_tasks(self):
        coordinator = LocalTaskCoordinator(lambda: True)
        query_id = coordinator.begin(
            'query', {'code:100', 'name:xg-mx-0825-001'})
        with self.assertRaisesRegex(TaskConflict, '目标环境'):
            coordinator.begin('env_batch', {'name:xg-mx-0825-001'})
        env_id = coordinator.begin('env_batch')
        with self.assertRaisesRegex(TaskConflict, '目标环境'):
            coordinator.reserve(env_id, {'code:100'})
        coordinator.finish(query_id)
        coordinator.reserve(env_id, {'code:100'})
        coordinator.finish(env_id)

    def test_registration_remains_exclusive(self):
        coordinator = LocalTaskCoordinator(lambda: True)
        query_id = coordinator.begin('query')
        with self.assertRaisesRegex(TaskConflict, '订单物流查询正在进行'):
            coordinator.begin('register')
        coordinator.finish(query_id)

    def test_environment_resources_are_normalized_without_secrets(self):
        resources = environment_resources([{
            'containerName': 'XG-MX-0825-001',
            'containerCode': 100,
            'password': 'must-not-appear',
        }])
        self.assertEqual(resources, {
            'name:xg-mx-0825-001', 'code:100'})
        self.assertNotIn('must-not-appear', json.dumps(sorted(resources)))


class HubRuntimeGateTests(unittest.TestCase):
    def test_browser_control_calls_are_submitted_serially(self):
        hub = HubStudioApi(runtime_gate=HubRuntimeGate(max_requests=4))
        state = {'active': 0, 'maximum': 0}
        lock = threading.Lock()

        def fake_post(_path, _body):
            with lock:
                state['active'] += 1
                state['maximum'] = max(state['maximum'], state['active'])
            time.sleep(0.03)
            with lock:
                state['active'] -= 1
            return {}

        hub._post = fake_post
        threads = [threading.Thread(target=hub.browser_start, args=(i,))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(state['maximum'], 1)

    def test_request_slots_bound_total_local_api_concurrency(self):
        gate = HubRuntimeGate(max_requests=2)
        state = {'active': 0, 'maximum': 0}
        lock = threading.Lock()

        def worker():
            with gate.request():
                with lock:
                    state['active'] += 1
                    state['maximum'] = max(
                        state['maximum'], state['active'])
                time.sleep(0.03)
                with lock:
                    state['active'] -= 1

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(state['maximum'], 2)


if __name__ == '__main__':
    unittest.main()
