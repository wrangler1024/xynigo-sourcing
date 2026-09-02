# -*- coding: utf-8 -*-
import json
import os
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from purchase_tool.cloud_auth import LocalAuthError, MemoryAuthSessionStore
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
        self.renew_callback = None
        self.user_sessions = []

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

    def issue_user_session(self, credential):
        self.user_sessions.append(credential)
        return {'sessionToken': 'user-session-' + ('z' * 40)}

    def start(self, credential, task_id, lease_token):
        self.started.append((credential, task_id, lease_token))

    def renew(self, credential, task_id, lease_token):
        self.renewals.append((credential, task_id, lease_token))
        if self.renew_callback:
            return self.renew_callback()
        return {'task': {'cancellationRequested': False}}

    def progress(self, credential, task_id, lease_token, phase,
                 current=None, total=None, stable_code=None, snapshot=None):
        self.progress_events.append({
            'credential': credential,
            'taskId': task_id,
            'leaseToken': lease_token,
            'phase': phase,
            'current': current,
            'total': total,
            'stableCode': stable_code,
            'snapshot': snapshot,
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
                'connectionPhase': '',
                'connectionAttempt': 0,
                'nextRetryAt': None,
                'connectedAt': None,
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
            return {key: cfg[key] for key in (
                'concurrency', 'safeParallelTasks') if key in cfg}

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
            workspace_rpc_executor=lambda payload: {
                'httpStatus': 200,
                'responseType': 'json',
                'contentType': 'application/json',
                'body': {'echo': payload},
            },
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

    def test_legacy_business_preferences_do_not_change_device_revision(self):
        worker, _client, holder, _coordinator = self.build_worker({
            'concurrency': 2,
            'safeParallelTasks': False,
            'purchaseSite': 'MX',
            'purchaseTags': {'MX': 'MX采购', 'US': 'US采购'},
            'importBuyerPlan': '1:新刚',
        })
        first = worker._apply_task('config.read.v1', {})[2]
        holder['config']['purchaseSite'] = 'US'
        holder['config']['purchaseTags']['US'] = '美国采购'
        holder['config']['importBuyerPlan'] = '2:志恒'
        second = worker._apply_task('config.read.v1', {})[2]
        self.assertEqual(first['configRevision'], second['configRevision'])
        self.assertEqual(first['config'], second['config'])
        self.assertNotIn('purchaseSite', second['config'])

    def test_write_checks_revision_and_applies_under_local_gate(self):
        initial = {
            'concurrency': 2,
            'safeParallelTasks': False,
            'proxyLink': 'https://private.invalid/secret',
        }
        worker, client, holder, coordinator = self.build_worker(initial)
        public_initial = {
            'concurrency': 2,
            'safeParallelTasks': False,
        }
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-write',
            'type': 'config.write.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'expectedRevision': config_revision(public_initial),
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
        self.assertNotIn(
            'proxyLink', client.finishes[0]['resultSummary']['config'])
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

    def test_workspace_rpc_runs_without_claiming_config_gate(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2, 'safeParallelTasks': True,
        })
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-workspace',
            'type': 'workspace.rpc.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'method': 'GET',
                'path': '/api/progress',
                'body': None,
            },
        })
        self.assertEqual(client.finishes[0]['outcome'], 'succeeded')
        self.assertEqual(client.finishes[0]['resultCode'],
                         'workspace_rpc_completed')
        self.assertEqual(
            client.finishes[0]['resultSummary']['body']['echo']['path'],
            '/api/progress')
        self.assertFalse(coordinator.running())

    def test_formal_operation_task_streams_snapshot_and_finishes(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2, 'safeParallelTasks': True,
        })
        received = []

        def execute_operation(task_type, payload, report,
                              cancellation_event=None):
            received.append((task_type, payload, cancellation_event))
            report(
                'logistics.running', 1, 1,
                snapshot={'rows': [{
                    'environmentSerial': '101',
                    'environmentName': 'XG-MX-001',
                    'status': 'ok',
                    'currentStep': 'ok',
                    'completedSteps': ['query_completed'],
                }]})
            return 'succeeded', 'logistics_completed', {
                'runStatus': 'completed',
                'phase': 'logistics.completed',
                'progressCompleted': 1,
                'progressTotal': 1,
                'totalCount': 1,
                'successCount': 1,
                'failedCount': 0,
                'stoppedCount': 0,
            }

        worker.operation_task_executor = execute_operation
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-operation',
            'type': 'logistics.query.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'runKey': 'logistics-run-0001',
                'site': 'MX',
                'queryMode': 'initial',
                'environmentSerials': ['101'],
            },
        })

        self.assertEqual(received[0][0], 'logistics.query.v1')
        self.assertEqual(client.progress_events[0]['snapshot']['rows'][0][
            'environmentSerial'], '101')
        self.assertEqual(client.finishes[0]['resultCode'],
                         'logistics_completed')
        self.assertEqual(client.finishes[0]['resultSummary']['runStatus'],
                         'completed')
        self.assertFalse(coordinator.running())

    def test_formal_task_retries_one_transient_lease_renewal_failure(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2, 'safeParallelTasks': True,
        })
        renewal_recovered = threading.Event()

        def renew():
            if len(client.renewals) == 1:
                raise LocalAuthError('cloud_unreachable')
            renewal_recovered.set()
            return {'task': {'cancellationRequested': False}}

        def execute_operation(_task_type, _payload, _report,
                              cancellation_event=None):
            self.assertTrue(renewal_recovered.wait(1.0))
            self.assertFalse(cancellation_event.is_set())
            return 'succeeded', 'logistics_completed', {
                'runStatus': 'completed',
                'phase': 'logistics.completed',
                'progressCompleted': 1,
                'progressTotal': 1,
                'totalCount': 1,
                'successCount': 1,
                'failedCount': 0,
                'stoppedCount': 0,
            }

        client.renew_callback = renew
        worker.operation_task_executor = execute_operation
        with patch(
                'purchase_tool.executor_channel.LEASE_RENEW_INTERVAL_SECONDS',
                0.01), patch(
                'purchase_tool.executor_channel.LEASE_RENEW_RETRY_SECONDS',
                0.01):
            worker._execute_task(DEVICE_CREDENTIAL, {
                'id': 'task-renew-retry',
                'type': 'logistics.query.v1',
                'leaseToken': LEASE_TOKEN,
                'payload': {
                    'runKey': 'logistics-run-renew-retry',
                    'site': 'MX',
                    'queryMode': 'initial',
                    'environmentSerials': ['101'],
                },
            })

        self.assertGreaterEqual(len(client.renewals), 2)
        self.assertEqual(client.finishes[0]['outcome'], 'succeeded')
        self.assertNotEqual(
            worker.state_store.load().get('lastErrorCode'),
            'executor_lease_renew_failed')
        self.assertFalse(coordinator.running())

    def test_dedicated_environment_parse_task_uses_local_parser(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2, 'safeParallelTasks': True,
        })
        received = []

        def parse_rpc(payload):
            received.append(payload)
            return {
                'httpStatus': 200,
                'responseType': 'json',
                'contentType': 'application/json',
                'body': {
                    'planId': 'plan-local-0001',
                    'site': 'MX',
                    'count': 2,
                    'preview': [],
                },
            }

        worker.workspace_rpc_executor = parse_rpc
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-environment-parse',
            'type': 'environment.parse.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {
                'filename': 'buyers.xlsx',
                'contentBase64': 'UEsDB-synthetic',
                'site': 'MX',
            },
        })

        self.assertEqual(received[0]['path'], '/api/envbatch/parse')
        self.assertEqual(client.finishes[0]['outcome'], 'succeeded')
        self.assertEqual(client.finishes[0]['resultCode'],
                         'environment_parse_completed')
        self.assertEqual(client.finishes[0]['resultSummary']['planId'],
                         'plan-local-0001')
        self.assertFalse(coordinator.running())

    def test_workspace_snapshot_task_uses_one_aggregated_local_read(self):
        worker, client, _holder, coordinator = self.build_worker({
            'concurrency': 2, 'safeParallelTasks': True,
        })
        received = []
        snapshot = {
            'schemaVersion': 1,
            'snapshotRevision': 'c' * 64,
            'capturedAt': '2026-09-01T10:00:00+00:00',
            'preferences': {},
            'groups': ['MX采购'],
            'preflight': {},
        }

        def snapshot_rpc(payload):
            received.append(payload)
            return {
                'httpStatus': 200,
                'responseType': 'json',
                'contentType': 'application/json',
                'body': snapshot,
            }

        worker.workspace_rpc_executor = snapshot_rpc
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-workspace-snapshot',
            'type': 'workspace.snapshot.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {},
        })

        self.assertEqual(received, [{
            'method': 'GET',
            'path': '/api/workspace/snapshot',
            'body': None,
        }])
        self.assertEqual(client.finishes[0]['resultCode'],
                         'workspace_snapshot_completed')
        self.assertEqual(client.finishes[0]['resultSummary'], snapshot)
        self.assertFalse(coordinator.running())


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

    def test_first_poll_is_fast_handshake_then_enters_long_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            client.poll_callback = lambda: (
                worker.stop_event.set()
                if len(client.poll_calls) >= 2 else None)
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(
                [call[3] for call in client.poll_calls], [0, 25])
            state = state_store.load()
            self.assertEqual(state['status'], 'online')
            self.assertEqual(state['connectionPhase'], 'listening')
            self.assertEqual(state['connectionAttempt'], 0)
            self.assertIsNone(state['nextRetryAt'])
            self.assertTrue(state['connectedAt'])

    def test_worker_installs_owner_session_before_first_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            installed = []
            worker = ExecutorChannelWorker(
                client=client,
                credential_store=MemoryAuthSessionStore(DEVICE_CREDENTIAL),
                state_store=state_store,
                config_getter=lambda: {'concurrency': 2},
                public_config_getter=lambda config: dict(config),
                config_writer=lambda config: dict(config),
                task_coordinator=LocalTaskCoordinator(lambda: True),
                hub_status_getter=lambda _force=False: (True, ''),
                user_session_installer=installed.append,
            )
            client.poll_callback = worker.stop_event.set
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(client.user_sessions, [DEVICE_CREDENTIAL])
            self.assertEqual(installed, ['user-session-' + ('z' * 40)])

    def test_two_transient_poll_failures_stay_reconnecting_before_recovery(self):
        class RecordingStateStore(ExecutorChannelStateStore):
            def __init__(self, path):
                super().__init__(path)
                self.statuses = []

            def update(self, **changes):
                if 'status' in changes:
                    self.statuses.append(changes['status'])
                return super().update(**changes)

        class FlakyClient(FakeExecutorClient):
            def poll(self, credential, revision, hub_status, wait_seconds=25):
                self.poll_calls.append(
                    (credential, revision, hub_status, wait_seconds))
                if len(self.poll_calls) <= 2:
                    raise LocalAuthError('cloud_unreachable', status=503)
                worker.stop_event.set()
                return {'task': None}

        with tempfile.TemporaryDirectory() as directory:
            state_store = RecordingStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FlakyClient()
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            worker._wait = lambda _seconds: None
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(len(client.poll_calls), 3)
            self.assertEqual(
                [call[3] for call in client.poll_calls], [0, 0, 0])
            self.assertEqual(state_store.load()['status'], 'online')
            self.assertGreaterEqual(state_store.statuses.count('reconnecting'), 2)
            self.assertNotIn('offline', state_store.statuses)

    def test_third_consecutive_poll_failure_reports_offline(self):
        class FlakyClient(FakeExecutorClient):
            def poll(self, credential, revision, hub_status, wait_seconds=25):
                self.poll_calls.append(
                    (credential, revision, hub_status, wait_seconds))
                if len(self.poll_calls) >= 3:
                    worker.stop_event.set()
                raise LocalAuthError('cloud_unreachable', status=503)

        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FlakyClient()
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            worker._wait = lambda _seconds: None
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            state = state_store.load()
            self.assertEqual(state['status'], 'offline')
            self.assertEqual(state['connectionPhase'], 'retry_wait')
            self.assertEqual(state['connectionAttempt'], 3)
            self.assertTrue(state['nextRetryAt'])


if __name__ == '__main__':
    unittest.main()
