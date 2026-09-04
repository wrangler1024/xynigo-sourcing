# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from purchase_tool import main as main_module
from purchase_tool.cloud_auth import (
    LocalAuthError, MemoryAuthSessionStore,
)
from purchase_tool.operation_result_sync import OperationResultSyncQueue


class OperationResultSyncQueueTests(unittest.TestCase):
    def test_app_state_loads_device_proof_only_when_sending_logistics(self):
        calls = []
        state = main_module.AppState.__new__(main_module.AppState)
        state.executor_credential_store = MemoryAuthSessionStore(
            'device-credential-' + ('x' * 40))
        state.auth = SimpleNamespace(
            operation_result_request=lambda *args, **kwargs:
                calls.append((args, kwargs)) or {'ok': True, 'data': {}})
        payload = {
            'runKey': 'query-synthetic-device-proof-0001',
            'results': [],
        }

        state._send_operation_result(
            '/v1/operations/logistics-query-runs',
            payload,
            'fulfillment.order.read',
        )

        self.assertEqual(calls[0][0], (
            '/v1/operations/logistics-query-runs',
            payload,
            'fulfillment.order.read',
        ))
        self.assertEqual(
            calls[0][1]['executor_credential'],
            'device-credential-' + ('x' * 40),
        )
        self.assertNotIn('deviceCredential', payload)

    def test_failed_upload_is_persisted_and_retried_idempotently(self):
        now = [1000.0]
        calls = []

        def failing_sender(endpoint, payload, permission):
            calls.append((endpoint, payload['runKey'], permission))
            raise LocalAuthError('cloud_unreachable', status=503)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'operation-outbox.json'
            queue = OperationResultSyncQueue(
                failing_sender, path=path, start_worker=False,
                clock=lambda: now[0])
            payload = {
                'source': 'local_executor',
                'runKey': 'query-synthetic0001',
                'site': 'US',
                'queryMode': 'initial',
                'completedAt': '2026-08-26T10:00:00+08:00',
                'results': [{
                    'environmentSerial': '9001',
                    'status': 'ok',
                    'trackingNumbers': ['SYNTHETIC-TRACK-0001'],
                }],
            }
            self.assertTrue(queue.enqueue(
                '/v1/operations/logistics-query-runs',
                'fulfillment.order.read', payload))
            self.assertEqual(queue.snapshot()['pending'], 1)
            self.assertEqual(len(calls), 1)
            stored = path.read_text(encoding='utf-8')
            self.assertIn('SYNTHETIC-TRACK-0001', stored)
            self.assertNotIn('password', stored.casefold())
            queue.close()

            now[0] = 2000.0
            delivered = []
            recovered = OperationResultSyncQueue(
                lambda endpoint, body, permission: delivered.append(
                    (endpoint, body['runKey'], permission)),
                path=path, start_worker=False, clock=lambda: now[0])
            self.assertEqual(recovered.flush(), 1)
            self.assertEqual(recovered.snapshot()['pending'], 0)
            self.assertEqual(delivered, [(
                '/v1/operations/logistics-query-runs',
                'query-synthetic0001',
                'fulfillment.order.read',
            )])
            recovered.close()

    def test_queue_rejects_credential_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = OperationResultSyncQueue(
                lambda *_args: None,
                path=Path(tmp) / 'operation-outbox.json',
                start_worker=False)
            with self.assertRaisesRegex(ValueError, '凭证字段'):
                queue.enqueue(
                    '/v1/operations/environment-creation-runs',
                    'resource.environment.create',
                    {
                        'runKey': 'env_batch-synthetic0001',
                        'results': [{'password': 'synthetic-secret'}],
                    })
            self.assertFalse(queue.path.exists())
            queue.close()

    def test_same_run_key_with_different_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = OperationResultSyncQueue(
                lambda *_args: (_ for _ in ()).throw(
                    LocalAuthError('cloud_unreachable', status=503)),
                path=Path(tmp) / 'operation-outbox.json',
                start_worker=False)
            base = {'runKey': 'env_batch-synthetic0001', 'results': []}
            queue.enqueue(
                '/v1/operations/environment-creation-runs',
                'resource.environment.create', base)
            changed = json.loads(json.dumps(base))
            changed['results'] = [{'status': 'failed'}]
            with self.assertRaisesRegex(ValueError, '结果不一致'):
                queue.enqueue(
                    '/v1/operations/environment-creation-runs',
                    'resource.environment.create', changed)
            queue.close()


if __name__ == '__main__':
    unittest.main()
