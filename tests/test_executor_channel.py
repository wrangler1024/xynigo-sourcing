# -*- coding: utf-8 -*-
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from purchase_tool.cloud_auth import MemoryAuthSessionStore
from purchase_tool.executor_channel import (
    CHANNEL_STATE_FIELDS,
    ExecutorChannelStateStore,
    ExecutorChannelWorker,
    config_revision,
    pair_executor,
)
from purchase_tool.task_runtime import LocalTaskCoordinator


DEVICE_CREDENTIAL = 'device-credential-' + ('x' * 40)
LEASE_TOKEN = 'lease-token-' + ('y' * 40)


class FakeExecutorClient(object):
    def __init__(self):
        self.started = []
        self.progress_events = []
        self.finishes = []
        self.renewals = []
        self.pair_calls = []
        self.poll_calls = []
        self.poll_callback = None

    def pair(self, code, name, system, architecture):
        self.pair_calls.append((code, name, system, architecture))
        return {
            'executorId': '00000000-0000-0000-0000-000000000001',
            'deviceCredential': DEVICE_CREDENTIAL,
        }

    def poll(self, credential, revision, hub_status, wait_seconds=25):
        self.poll_calls.append(
            (credential, revision, hub_status, wait_seconds))
        if self.poll_callback:
            self.poll_callback()
        return {'task': None}

    def start(self, credential, task_id, lease_token):
        self.started.append((credential, task_id, lease_token))

    def renew(self, credential, task_id, lease_token):
        self.renewals.append((credential, task_id, lease_token))

    def progress(self, credential, task_id, lease_token, phase,
                 current=None, total=None, stable_code=None):
        self.progress_events.append({
            'credential': credential,
            'taskId': task_id,
            'leaseToken': lease_token,
            'phase': phase,
            'current': current,
            'total': total,
            'stableCode': stable_code,
        })

    def finish(self, credential, task_id, lease_token, outcome,
               result_code, result_summary):
        self.finishes.append({
            'credential': credential,
            'taskId': task_id,
            'leaseToken': lease_token,
            'outcome': outcome,
            'resultCode': result_code,
            'resultSummary': result_summary,
        })


class ExecutorChannelStateTests(unittest.TestCase):
    def test_state_file_is_atomic_private_and_rejects_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'runtime', 'executor-channel.json')
            store = ExecutorChannelStateStore(path)
            store.save({
                'executorId': 'executor-1',
                'displayName': '采购电脑 A',
                'platform': 'macos',
                'architecture': 'arm64',
                'pairedAt': '2026-08-27T00:00:00+00:00',
                'lastPollAt': None,
                'lastErrorCode': '',
                'status': 'paired',
                'configRevision': None,
            })
            loaded = store.load()
            self.assertEqual(set(loaded), CHANNEL_STATE_FIELDS)
            self.assertNotIn(DEVICE_CREDENTIAL, json.dumps(loaded))
            if os.name != 'nt':
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode & 0o077, 0)
            with self.assertRaises(ValueError):
                store.save({'deviceCredential': DEVICE_CREDENTIAL})
            with self.assertRaises(ValueError):
                store.save({'leaseToken': LEASE_TOKEN})

    def test_config_revision_is_deterministic_and_covers_private_values(self):
        left = {'concurrency': 2, 'proxyLink': 'private-value',
                'purchaseTags': {'US': 'us', 'MX': 'mx'}}
        reordered = {'purchaseTags': {'MX': 'mx', 'US': 'us'},
                     'proxyLink': 'private-value', 'concurrency': 2}
        changed_private = dict(left, proxyLink='another-private-value')
        self.assertEqual(config_revision(left), config_revision(reordered))
        self.assertNotEqual(config_revision(left),
                            config_revision(changed_private))
        self.assertNotIn('private-value', config_revision(left))


class ExecutorTaskApplicationTests(unittest.TestCase):
    def build_worker(self, config):
        client = FakeExecutorClient()
        state_store = ExecutorChannelStateStore(
            os.path.join(self.tempdir.name, 'state.json'))
        coordinator = LocalTaskCoordinator(lambda: True)
        holder = {'config': dict(config)}

        def public_config(cfg):
            return {key: value for key, value in cfg.items()
                    if key != 'proxyLink'}

        def write_config(submitted):
            allowed = {'concurrency', 'safeParallelTasks'}
            if set(submitted) - allowed:
                raise ValueError('unsupported field')
            holder['config'].update(submitted)
            return dict(holder['config'])

        worker = ExecutorChannelWorker(
            client=client,
            credential_store=MemoryAuthSessionStore(DEVICE_CREDENTIAL),
            state_store=state_store,
            config_getter=lambda: dict(holder['config']),
            public_config_getter=public_config,
            config_writer=write_config,
            task_coordinator=coordinator,
            hub_status_getter=lambda _force=False: (True, ''),
        )
        return worker, client, holder, coordinator

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_read_returns_public_config_and_never_returns_private_link(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2,
            'safeParallelTasks': True,
            'proxyLink': 'https://private.invalid/secret',
        })
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-read',
            'type': 'config.read.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {},
        })
        self.assertEqual(client.started[0][1], 'task-read')
        self.assertEqual(client.finishes[0]['outcome'], 'succeeded')
        summary = client.finishes[0]['resultSummary']
        self.assertEqual(summary['config']['concurrency'], 2)
        self.assertNotIn('proxyLink', summary['config'])
        self.assertNotIn('private.invalid', json.dumps(summary))
        self.assertFalse(coordinator.running())

    def test_write_checks_revision_and_applies_under_local_gate(self):
        initial = {
            'concurrency': 2,
            'safeParallelTasks': False,
            'proxyLink': 'https://private.invalid/secret',
        }
        worker, client, holder, coordinator = self.build_worker(initial)
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-write',
            'type': 'config.write.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'expectedRevision': config_revision(initial),
                'config': {
                    'concurrency': 3,
                    'safeParallelTasks': True,
                },
            },
        })
        self.assertEqual(holder['config']['concurrency'], 3)
        self.assertTrue(holder['config']['safeParallelTasks'])
        self.assertEqual(client.finishes[0]['resultCode'],
                         'config_write_succeeded')
        self.assertFalse(coordinator.running())

    def test_revision_conflict_keeps_original_config(self):
        initial = {'concurrency': 2, 'safeParallelTasks': False}
        worker, client, holder, coordinator = self.build_worker(initial)
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-conflict',
            'type': 'config.write.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'expectedRevision': '0' * 64,
                'config': {'concurrency': 5},
            },
        })
        self.assertEqual(holder['config'], initial)
        self.assertEqual(client.finishes[0]['outcome'], 'failed')
        self.assertEqual(client.finishes[0]['resultCode'],
                         'config_revision_conflict')
        self.assertFalse(coordinator.running())

    def test_running_business_task_blocks_config_write(self):
        initial = {'concurrency': 2, 'safeParallelTasks': True}
        worker, client, holder, coordinator = self.build_worker(initial)
        query_id = coordinator.begin('query')
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-busy',
            'type': 'config.write.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'expectedRevision': config_revision(initial),
                'config': {'concurrency': 4},
            },
        })
        coordinator.finish(query_id)
        self.assertEqual(holder['config'], initial)
        self.assertEqual(client.finishes[0]['outcome'], 'failed')
        self.assertEqual(client.finishes[0]['resultCode'],
                         'config_task_failed')


class PairingTests(unittest.TestCase):
    def test_pair_saves_credential_only_in_secure_store(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeExecutorClient()
            credential_store = MemoryAuthSessionStore()
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            with patch(
                    'purchase_tool.executor_channel.local_platform',
                    return_value=('macos', 'arm64')):
                result = pair_executor(
                    'ABCD-EFGH', '采购电脑 A', client=client,
                    credential_store=credential_store,
                    state_store=state_store)
            self.assertEqual(result['executorId'],
                             '00000000-0000-0000-0000-000000000001')
            self.assertNotIn('deviceCredential', result)
            self.assertEqual(credential_store.load(), DEVICE_CREDENTIAL)
            with open(state_store.path, encoding='utf-8') as handle:
                raw_state = handle.read()
            self.assertNotIn(DEVICE_CREDENTIAL, raw_state)
            self.assertNotIn('deviceCredential', raw_state)

    def test_both_green_packages_include_a_managed_pairing_entry(self):
        root = Path(__file__).resolve().parents[1]
        mac_script = (root / '组装macOS绿色包.sh').read_text(encoding='utf-8')
        windows_script = (root / '组装Windows绿色包.sh').read_text(encoding='utf-8')
        mac_entry = (root / 'packaging/macos/entry.py').read_text(encoding='utf-8')
        updater = (root / 'src/purchase_tool/updater.py').read_text(encoding='utf-8')
        self.assertIn('配对本地执行器-Mac.command', mac_script)
        self.assertIn('配对本地执行器.bat', windows_script)
        self.assertIn("sys.argv[1] == 'pair'", mac_entry)
        self.assertIn("sys.argv[1] == 'pair'", windows_script)
        self.assertIn('配对本地执行器-Mac.command', updater)
        self.assertIn('配对本地执行器.bat', updater)


class ExecutorChannelLifecycleTests(unittest.TestCase):
    def build_worker(self, credential_store, state_store, client):
        return ExecutorChannelWorker(
            client=client,
            credential_store=credential_store,
            state_store=state_store,
            config_getter=lambda: {'concurrency': 2},
            public_config_getter=lambda config: dict(config),
            config_writer=lambda config: dict(config),
            task_coordinator=LocalTaskCoordinator(lambda: True),
            hub_status_getter=lambda _force=False: (True, ''),
        )

    def test_running_unpaired_worker_detects_later_pairing(self):
        class DelayedCredentialStore(object):
            def __init__(self):
                self.loads = 0

            def load(self):
                self.loads += 1
                return DEVICE_CREDENTIAL if self.loads >= 2 else None

            def clear(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = DelayedCredentialStore()
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            worker = self.build_worker(store, state_store, client)
            worker._wait = lambda _seconds: None
            client.poll_callback = worker.stop_event.set
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(store.loads, 2)
            self.assertEqual(len(client.poll_calls), 1)
            self.assertEqual(client.poll_calls[0][0], DEVICE_CREDENTIAL)
            self.assertEqual(state_store.load()['status'], 'online')

    def test_worker_reuses_loaded_credential_between_polls(self):
        class CountingCredentialStore(MemoryAuthSessionStore):
            def __init__(self):
                super().__init__(DEVICE_CREDENTIAL)
                self.loads = 0

            def load(self):
                self.loads += 1
                return super().load()

        with tempfile.TemporaryDirectory() as directory:
            store = CountingCredentialStore()
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            worker = self.build_worker(store, state_store, client)
            client.poll_callback = lambda: (
                worker.stop_event.set()
                if len(client.poll_calls) >= 2 else None)
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(len(client.poll_calls), 2)
            self.assertEqual(store.loads, 1)


if __name__ == '__main__':
    unittest.main()
