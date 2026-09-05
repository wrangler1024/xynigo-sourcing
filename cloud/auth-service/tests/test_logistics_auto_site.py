import pytest

from fastapi.testclient import TestClient

from test_auth_flow import build_test_app
from test_executor_channel import (
    CSRF, create_pairing_code, device_headers, heartbeat, login, pair,
)


@pytest.mark.parametrize("legacy_site", [None, "MX"])
def test_auto_query_contract_dispatch_and_retry_without_site_selection(tmp_path, legacy_site):
    app, _database, _oauth = build_test_app(tmp_path)
    caps = ['logistics.query.v1', 'logistics.auto-site.v1']
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        paired = pair(device, create_pairing_code(web), capabilities=caps)
        credential = paired['deviceCredential']
        heartbeat(device, credential, capabilities=caps)
        response = web.post('/v1/operation-runs/logistics-query', headers=CSRF, json={
            'executorId': paired['executorId'], 'idempotencyKey': 'auto-mixed-0001',
            'environmentSerials': ['9001', '9002'],
            **({'site': legacy_site} if legacy_site else {}),
        })
        assert response.status_code == 202, response.text
        run = response.json()['data']
        assert run['site'] == (legacy_site or 'AUTO')
        task = heartbeat(device, credential, capabilities=caps)['task']
        assert task['payload']['site'] == (legacy_site or 'AUTO')
        assert task['payload']['environmentSerials'] == ['9001', '9002']
        task_id = run['executorTaskId']
        headers = device_headers(credential)
        token = task['leaseToken']
        assert device.post(f'/v1/executor-channel/tasks/{task_id}/start',
                           headers=headers, json={'leaseToken': token}).status_code == 200
        response = device.post(f'/v1/executor-channel/tasks/{task_id}/progress', headers=headers, json={
            'leaseToken': token, 'phase': 'logistics.querying', 'current': 2, 'total': 2,
            'snapshot': {'rows': [
                {'environmentSerial': '9001', 'environmentName': 'SYN-US-001', 'status': 'ok'},
                {'environmentSerial': '9002', 'environmentName': 'SYN-MX-001', 'status': 'fail'},
            ]},
        })
        assert response.status_code == 200, response.text
        response = device.post(f'/v1/executor-channel/tasks/{task_id}/finish', headers=headers, json={
            'leaseToken': token, 'outcome': 'succeeded', 'resultCode': 'logistics_completed',
            'resultSummary': {'runStatus': 'partial_failure', 'phase': 'logistics.completed',
                              'progressCompleted': 2, 'progressTotal': 2,
                              'successCount': 1, 'failedCount': 1},
        })
        assert response.status_code == 200, response.text
        retry = web.post('/v1/operation-runs/logistics-query', headers=CSRF, json={
            'executorId': paired['executorId'], 'idempotencyKey': 'auto-retry-0001',
            'queryMode': 'failed_retry', 'parentRunId': run['runId'],
            'environmentSerials': ['9002'],
        })
        assert retry.status_code == 202, retry.text
        assert retry.json()['data']['site'] == 'AUTO'


def test_auto_query_rejects_old_executor_before_dispatch(tmp_path):
    app, _database, _oauth = build_test_app(tmp_path)
    caps = ['logistics.query.v1']
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        paired = pair(device, create_pairing_code(web), capabilities=caps)
        heartbeat(device, paired['deviceCredential'], capabilities=caps)
        response = web.post('/v1/operation-runs/logistics-query', headers=CSRF, json={
            'executorId': paired['executorId'], 'idempotencyKey': 'auto-old-0001',
            'environmentSerials': ['9001'],
        })
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'executor_auto_site_required'
