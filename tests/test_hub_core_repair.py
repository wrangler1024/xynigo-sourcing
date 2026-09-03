# -*- coding: utf-8 -*-
import json
import os
import tempfile
import time
import unittest

from purchase_tool.hub_core_repair import (
    HubCoreRepairCoordinator, HubCoreRepairError)
from purchase_tool.task_runtime import LocalTaskCoordinator


class ReadyHub(object):
    def __init__(self):
        self.calls = []
        self.requirement = {
            'known': True,
            'browserType': 'chrome',
            'version': '148',
            'containerCode': 'container-test-1',
        }

    def core_requirement_snapshot(self):
        return dict(self.requirement)

    def download_core(self, browser_type, version):
        self.calls.append(('download', browser_type, version))

    def browser_start(self, container_code, headless=False):
        self.calls.append(('start', container_code, headless))
        return {'debuggingPort': '9222'}

    def browser_stop(self, container_code):
        self.calls.append(('stop', container_code))

    def clear_runtime_failure(self):
        self.calls.append(('clear-runtime',))

    def clear_core_requirement(self):
        self.calls.append(('clear-requirement',))
        self.requirement = {'known': False}


class FakeCdp(object):
    def __init__(self, port):
        self.port = port

    def version_info(self):
        return {
            'browser': 'HeadlessChrome/148.0.0.0',
            'protocolVersion': '1.3',
        }


class HubCoreRepairTests(unittest.TestCase):
    def test_download_verifies_runtime_and_writes_redacted_audit(self):
        hub = ReadyHub()
        tasks = LocalTaskCoordinator()
        with tempfile.TemporaryDirectory() as directory:
            audit_path = os.path.join(directory, 'audit.jsonl')
            coordinator = HubCoreRepairCoordinator(
                lambda: hub, tasks, audit_path,
                cdp_factory=FakeCdp, verify_interval=0.01,
                device_info_getter=lambda: {
                    'displayName': '测试电脑',
                    'platform': 'windows',
                    'clientVersion': 'test-version',
                })

            accepted = coordinator.start(actor={
                'user': {'id': 'member-test', 'name': '测试管理员'}})
            self.assertIn(accepted['state'], {'downloading', 'verifying', 'ready'})
            deadline = time.time() + 2
            while coordinator.snapshot()['running'] and time.time() < deadline:
                time.sleep(0.01)
            result = coordinator.snapshot()
            with open(audit_path, encoding='utf-8') as handle:
                records = [json.loads(line) for line in handle if line.strip()]

        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['auditState'], 'recorded')
        self.assertFalse(tasks.running())
        self.assertEqual(hub.calls, [
            ('download', 'chrome', '148'),
            ('start', 'container-test-1', True),
            ('stop', 'container-test-1'),
            ('clear-runtime',),
            ('clear-requirement',),
        ])
        self.assertEqual([item['event'] for item in records], [
            'hub_core_repair_requested', 'hub_core_repair_completed'])
        self.assertEqual(records[-1]['resultCode'], 'ok')
        self.assertEqual(records[-1]['deviceName'], '测试电脑')
        self.assertEqual(records[-1]['platform'], 'windows')
        self.assertNotIn('containerCode', json.dumps(records))

    def test_download_is_blocked_when_audit_cannot_be_started(self):
        hub = ReadyHub()
        tasks = LocalTaskCoordinator()
        with tempfile.TemporaryDirectory() as directory:
            blocker = os.path.join(directory, 'not-a-directory')
            with open(blocker, 'w', encoding='utf-8') as handle:
                handle.write('block')
            coordinator = HubCoreRepairCoordinator(
                lambda: hub, tasks, os.path.join(blocker, 'audit.jsonl'),
                cdp_factory=FakeCdp)

            with self.assertRaises(HubCoreRepairError) as caught:
                coordinator.start(actor={'user': {'id': 'member-test'}})

        self.assertEqual(
            caught.exception.code, 'hubstudio_core_audit_unavailable')
        self.assertFalse(tasks.running())
        self.assertEqual(hub.calls, [])

    def test_unknown_core_requirement_is_not_downloaded(self):
        hub = ReadyHub()
        hub.requirement = {'known': False}
        coordinator = HubCoreRepairCoordinator(
            lambda: hub, LocalTaskCoordinator(), '/unused/audit.jsonl')

        with self.assertRaises(HubCoreRepairError) as caught:
            coordinator.start()

        self.assertEqual(
            caught.exception.code, 'hubstudio_core_requirement_unknown')


if __name__ == '__main__':
    unittest.main()
