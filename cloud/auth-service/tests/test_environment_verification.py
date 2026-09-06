from datetime import UTC, datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from test_auth_flow import build_test_app
from test_executor_channel import CSRF, create_pairing_code, device_headers, heartbeat, login, pair
from xynigo_auth.models import LocalExecutor


@pytest.mark.parametrize('configured', [0, 1, 3])
def test_device_count_overrides_web_count_and_live_progress_roundtrips(tmp_path, configured):
    app, database, _oauth = build_test_app(tmp_path)
    caps = ['environment.create-backup.v1']
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        paired = pair(device, create_pairing_code(web), capabilities=caps)
        credential = paired['deviceCredential']
        heartbeat(device, credential, capabilities=caps)
        with database.session_factory() as session:
            executor = session.get(LocalExecutor, uuid.UUID(paired['executorId']))
            executor.workspace_snapshot = {'runtimeConfig':{
                'configRevision':'a'*64, 'hubPort':6873, 'concurrency':2,
                'envCreateWorkers':5, 'verifySampleCount':configured,
                'safeParallelTasks':True,
            }}
            executor.workspace_snapshot_at = datetime.now(UTC)
            session.commit()
        body = {
            'executorId':paired['executorId'], 'idempotencyKey':'verification-0001',
            'mode':'backup', 'site':'MX', 'purchaseDate':'20260906',
            'environmentGroup':'合成MX', 'buyerLabel':'合成采购员',
            'totalCount':10, 'verifySampleCount':10,
        }
        created = web.post('/v1/operation-runs/environment-creation', headers=CSRF, json=body)
        assert created.status_code == 202, created.text
        run = created.json()['data']
        assert run['verifySampleCount'] == configured
        repeated = web.post('/v1/operation-runs/environment-creation', headers=CSRF, json=body)
        assert repeated.status_code == 202 and repeated.json()['data']['runId'] == run['runId']
        task = heartbeat(device, credential, capabilities=caps)['task']
        assert task['payload']['verifySampleCount'] == configured
        assert task['payload']['ipVerificationProgress'] == 1
        token = task['leaseToken']
        headers = device_headers(credential)
        task_url = '/v1/executor-channel/tasks/' + task['id']
        assert device.post(task_url+'/start', headers=headers,
                           json={'leaseToken':token}).status_code == 200
        rows = [{
            'accountRef':f'synthetic-{i:04}', 'accountLabel':'te***@example.test',
            'purchaserLabel':'合成采购员', 'environmentName':f'SYN-MX-{i:03}',
            'environmentRef':f'synthetic-env-{i}', 'environmentSerial':str(1000+i),
            'status':'success', 'completedSteps':['env_created'],
        } for i in range(10)]
        def progress(done, requested=configured, total=configured):
            return device.post(task_url+'/progress', headers=headers, json={
                'leaseToken':token, 'phase':'environment.ip_checking', 'current':10, 'total':10,
                'snapshot':{'rows':[{**row, 'ipVerified':True if i < done else None}
                                   for i,row in enumerate(rows)],
                            'ipVerification':{'requestedCount':requested, 'totalCount':total}},
            })
        assert progress(0).status_code == 200
        current = web.get('/v1/operation-runs/environment-creation/'+run['runId']).json()['data']
        assert current['ipCheckDone'] == 0 and current['ipCheckTotal'] == configured
        assert progress(0, total=configured+1).status_code == 422
        assert progress(configured).status_code == 200
        current = web.get('/v1/operation-runs/environment-creation/'+run['runId']).json()['data']
        assert current['ipCheckTotal'] == configured
        assert current['ipCheckDone'] == current['ipTotalCount'] == current['ipOkCount'] == configured


def test_legacy_unknown_progress_has_no_fabricated_total():
    from types import SimpleNamespace
    from xynigo_auth.operation_service import OperationRunService
    run = SimpleNamespace(request_summary={}, phase='environment.ip_checking',
                          status='running', ip_total_count=0)
    rows = [SimpleNamespace(status='success', ip_verified=True) for _ in range(30)]
    assert OperationRunService.environment_ip_check_total(run, rows) is None
    run.request_summary = {'verifySampleCount':100}
    rows += [SimpleNamespace(status='success', ip_verified=None) for _ in range(70)]
    assert OperationRunService.environment_ip_check_total(run, rows) == 100
