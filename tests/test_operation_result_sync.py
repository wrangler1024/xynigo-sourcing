# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from purchase_tool.cloud_auth import LocalAuthError
from purchase_tool.main import AppState
from purchase_tool.operation_result_sync import OperationResultSyncQueue


class OperationResultSyncQueueTests(unittest.TestCase):
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


class OperationResultPayloadTests(unittest.TestCase):
    def test_environment_payload_contains_only_safe_result_fields(self):
        state = AppState.__new__(AppState)
        state.env_job = SimpleNamespace(
            started_at=1000,
            finished_at=1010,
            runner=SimpleNamespace(
                site='MX', purchase_date='20260826',
                purchase_tag='希音墨西哥采购'),
            snapshot=lambda: {
                'rows': [{
                    'accountId': 'a' * 64,
                    'emailMasked': 'te***@example.test',
                    'buyer': '采购员A',
                    'envName': 'A-MX-0826-001',
                    'containerCode': 'container-safe-ref',
                    'serialNumber': 9001,
                    'state': 'done',
                    'bindingTime': '2026-08-26 10:00:00',
                }],
                'ipChecks': [{
                    'envName': 'A-MX-0826-001',
                    'ip': '198.51.100.8',
                    'country': 'MX',
                    'city': 'Synthetic',
                    'isp': 'Example',
                    'ok': True,
                    'error': '',
                }],
            },
        )
        payload = state.environment_result_payload('env_batch-safe0001')
        self.assertEqual(payload['results'][0]['accountRef'], 'a' * 64)
        self.assertEqual(payload['results'][0]['status'], 'success')
        rendered = json.dumps(payload, ensure_ascii=False).casefold()
        self.assertNotIn('password', rendered)
        self.assertNotIn('cookie', rendered)

    def test_logistics_payload_preserves_daily_tracking_result(self):
        state = AppState.__new__(AppState)
        state.orch = SimpleNamespace(
            started_at=1000,
            finished_at=1010,
            snapshot=lambda: {
                'site': 'US',
                'rows': [{
                    'serial': '9001',
                    'envName': 'A-US-0826-001',
                    'state': 'ok',
                    'orderNo': 'SYNTHETIC-ORDER-0001',
                    'tracks': ['SYNTHETIC-TRACK-0001'],
                    'pkgs': ['SYNTHETIC-PACKAGE-0001'],
                    'carrier': 'Synthetic Carrier',
                    'time': '2026-08-26 10:00:00',
                    'utcOffsetMinutes': -420,
                }],
            },
        )
        payload = state.logistics_result_payload(
            'query-safe0001', 'initial', ['9001'])
        self.assertEqual(payload['site'], 'US')
        self.assertEqual(
            payload['results'][0]['trackingNumbers'],
            ['SYNTHETIC-TRACK-0001'])
        self.assertTrue(payload['results'][0]['queriedAt'].endswith('-07:00'))


if __name__ == '__main__':
    unittest.main()
