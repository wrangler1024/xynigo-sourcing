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
    COMPATIBLE_CAPABILITIES,
    CloudExecutorClient,
    ExecutorChannelStateStore,
    ExecutorChannelWorker,
    MODERN_ONLY_CAPABILITIES,
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
        self.poll_summaries = []
        self.poll_callback = None
        self.renew_callback = None
        self.user_sessions = []

    def pair(self, code, name, system, architecture):
        self.pair_calls.append((code, name, system, architecture))
        return {
            'executorId': '00000000-0000-0000-0000-000000000001',
            'deviceCredential': DEVICE_CREDENTIAL,
        }

    def poll(self, credential, revision, hub_status, wait_seconds=25,
             config_summary=None):
        self.poll_summaries.append(config_summary)
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


class ExecutorProtocolCompatibilityTests(unittest.TestCase):
    class RejectModernContractClient(object):
        def __init__(self):
            self.calls = []

        def _request(self, path, **kwargs):
            payload = dict(kwargs.get('payload') or {})
            self.calls.append((path, payload))
            if set(payload.get('capabilities') or ()) & MODERN_ONLY_CAPABILITIES:
                raise LocalAuthError('validation_failed', status=422)
            if path.endswith('/pair'):
                return {
                    'executorId': '00000000-0000-0000-0000-000000000001',
                    'deviceCredential': DEVICE_CREDENTIAL,
                    'credentialType': 'Bearer',
                }
            return {'task': None}

    def test_poll_falls_back_once_without_sending_local_config_summary(self):
        wire = self.RejectModernContractClient()
        client = CloudExecutorClient(client=wire)
        summary = {'schemaVersion': 2, 'configRevision': 'a' * 64}

        self.assertEqual(
            client.poll(DEVICE_CREDENTIAL, 'a' * 64, 'ready', 0, summary),
            {'task': None},
        )
        self.assertEqual(
            client.poll(DEVICE_CREDENTIAL, 'b' * 64, 'ready', 0, summary),
            {'task': None},
        )

        self.assertEqual(len(wire.calls), 3)
        self.assertIn('configSummary', wire.calls[0][1])
        for _path, payload in wire.calls[1:]:
            self.assertEqual(
                payload['capabilities'], list(COMPATIBLE_CAPABILITIES))
            self.assertNotIn('configSummary', payload)
        self.assertTrue(client.compatibility_mode)

    def test_pairing_uses_the_same_bounded_compatibility_retry(self):
        wire = self.RejectModernContractClient()
        client = CloudExecutorClient(client=wire)
        result = client.pair('ABCD-EFGH', '采购电脑 A', 'macos', 'arm64')

        self.assertEqual(result['executorId'],
                         '00000000-0000-0000-0000-000000000001')
        self.assertEqual(len(wire.calls), 2)
        self.assertEqual(
            wire.calls[1][1]['capabilities'],
            list(COMPATIBLE_CAPABILITIES),
        )

    def test_non_validation_error_never_downgrades_the_contract(self):
        class OfflineClient(object):
            def _request(self, _path, **_kwargs):
                raise LocalAuthError('cloud_unreachable', status=503)

        client = CloudExecutorClient(client=OfflineClient())
        with self.assertRaises(LocalAuthError) as caught:
            client.poll(DEVICE_CREDENTIAL, 'a' * 64, 'ready', 0, {})
        self.assertEqual(caught.exception.code, 'cloud_unreachable')
        self.assertFalse(client.compatibility_mode)


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
    def build_worker(self, config, operation_task_executor=None):
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
            operation_task_executor=operation_task_executor,
        )
        return worker, client, holder, coordinator

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_config_read_is_rejected_by_new_executor(self):
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
        self.assertEqual(client.finishes[0]['outcome'], 'failed')
        self.assertEqual(
            client.finishes[0]['resultCode'], 'local_config_desktop_only')
        self.assertNotIn('private.invalid', json.dumps(client.finishes[0]))
        self.assertFalse(coordinator.running())

    def test_workspace_rpc_cannot_bypass_desktop_config_boundary(self):
        worker, _client, _holder, _coordinator = self.build_worker({
            'concurrency': 2,
            'safeParallelTasks': False,
        })
        with self.assertRaises(LocalAuthError) as blocked:
            worker._apply_task('workspace.rpc.v1', {
                'method': 'POST',
                'path': '/api/local-config/data-sources/team-default',
                'body': {'sourceId': 'ds_' + '1' * 24},
            })
        self.assertEqual(blocked.exception.code, 'local_config_desktop_only')
        self.assertEqual(blocked.exception.status, 410)

    def test_legacy_config_write_is_rejected_without_mutation(self):
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
        self.assertEqual(holder['config'], initial)
        self.assertEqual(client.finishes[0]['resultCode'],
                         'local_config_desktop_only')
        self.assertFalse(coordinator.running())

    def test_desktop_only_rejection_precedes_legacy_revision_handling(self):
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
                         'local_config_desktop_only')
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

    def test_unexpected_operation_failure_is_not_mislabeled_as_config(self):
        def fail_operation(_task_type, _payload, _report,
                           cancellation_event=None):
            del cancellation_event
            raise RuntimeError('synthetic operation failure')

        worker, client, _holder, _coordinator = self.build_worker(
            {'concurrency': 2, 'safeParallelTasks': True},
            operation_task_executor=fail_operation)
        worker._execute_task(DEVICE_CREDENTIAL, {
            'id': 'task-operation-failure',
            'type': 'environment.create-bound.v1',
            'leaseToken': LEASE_TOKEN,
            'payload': {},
        })
        finish = client.finishes[0]
        self.assertEqual(finish['outcome'], 'failed')
        self.assertEqual(finish['resultCode'], 'operation_task_failed')
        self.assertEqual(
            finish['resultSummary']['errorCode'], 'operation_task_failed')
        self.assertIn(
            'synthetic operation failure',
            finish['resultSummary']['errorSummary'])

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

    def test_worker_attaches_strict_config_summary_to_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            summary = {
                'schemaVersion': 2,
                'configRevision': 'a' * 64,
                'capturedAt': '2026-09-02T05:00:00+00:00',
                'runtimeConfig': {'concurrency': 2},
            }
            worker.config_summary_getter = lambda: dict(summary)
            client.poll_callback = worker.stop_event.set
            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(client.poll_summaries, [summary])

    def test_each_heartbeat_forces_a_fresh_local_hubstudio_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            force_arguments = []
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            worker.hub_status_getter = lambda force=False: (
                force_arguments.append(force) or False, '')
            client.poll_callback = worker.stop_event.set

            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)

            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(force_arguments, [True])
            self.assertEqual(client.poll_calls[0][2], 'offline')

    def test_running_hubstudio_with_unready_api_reports_limited(self):
        with tempfile.TemporaryDirectory() as directory:
            state_store = ExecutorChannelStateStore(
                os.path.join(directory, 'executor-channel.json'))
            client = FakeExecutorClient()
            worker = self.build_worker(
                MemoryAuthSessionStore(DEVICE_CREDENTIAL), state_store, client)
            worker.hub_status_getter = lambda _force=False: {
                'available': False,
                'clientRunning': True,
                'reasonCode': 'hubstudio_local_api_disabled',
            }
            client.poll_callback = worker.stop_event.set

            self.assertTrue(worker.start())
            worker.thread.join(timeout=2)

            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(client.poll_calls[0][2], 'limited')

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
            def poll(self, credential, revision, hub_status, wait_seconds=25,
                     config_summary=None):
                del config_summary
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
            def poll(self, credential, revision, hub_status, wait_seconds=25,
                     config_summary=None):
                del config_summary
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
