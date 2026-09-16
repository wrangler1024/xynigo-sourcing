"""售后运行参数闭集及云端到执行器的真实任务载荷。"""
import uuid

import pytest
from pydantic import ValidationError

from xynigo_auth.operation_contract import (
    AfterSaleScanCreateBody, AfterSaleClaimRunCreateBody, AfterSaleTrackCreateBody,
)
from test_after_sale_claim import _e2e_setup, AS_CAPABILITIES, CLIENT_VERSION
from test_executor_channel import heartbeat, CSRF

CASES = [
    (AfterSaleScanCreateBody, '/v1/after-sale/scan', {'environmentSerials':['SYNTH-1']}),
    (AfterSaleClaimRunCreateBody, '/v1/operation-runs/after-sale-claim',
     {'items':[{'environmentSerial':'SYNTH-1','orderNo':'SYNTH1'}]}),
    (AfterSaleTrackCreateBody, '/v1/after-sale/track',
     {'items':[{'environmentSerial':'SYNTH-1','orderNo':'SYNTH1','refundBillId':'SYNTHBILL'}]}),
]

@pytest.mark.parametrize('model,path,scope', CASES)
def test_runtime_defaults_and_strict_bounds(model, path, scope):
    body = dict(scope, idempotencyKey='synthetic-runtime', executorId=str(uuid.uuid4()))
    parsed = model.model_validate(body)
    assert parsed.browserMode == 'headless' and parsed.concurrency == 2
    for invalid in (0,1,4,6,True,'2',2.5):
        with pytest.raises(ValidationError):
            model.model_validate(dict(body, concurrency=invalid))
    with pytest.raises(ValidationError):
        model.model_validate(dict(body, browserMode='invalid'))

@pytest.mark.parametrize('model,path,scope', CASES)
@pytest.mark.parametrize('options', [{}, {'browserMode':'visible','concurrency':3}])
def test_cloud_task_payload_keeps_runtime_options(tmp_path, model, path, scope, options):
    for web, device, ids, database in _e2e_setup(tmp_path):
        heartbeat(device, ids['credential'], capabilities=AS_CAPABILITIES, client_version=CLIENT_VERSION)
        body = dict(scope, executorId=ids['executorId'], idempotencyKey='synthetic-runtime', **options)
        response = web.post(path, json=body, headers=CSRF)
        assert response.status_code == 202, response.text
        if model is AfterSaleClaimRunCreateBody:
            # Same effective options replay; changing concurrency must conflict.
            assert web.post(path, json=body, headers=CSRF).json()['data']['runId'] == response.json()['data']['runId']
            changed = dict(body, concurrency=5)
            assert web.post(path, json=changed, headers=CSRF).status_code == 409
        leased = heartbeat(device, ids['credential'], capabilities=AS_CAPABILITIES, client_version=CLIENT_VERSION)['task']
        assert leased['payload']['browserMode'] == options.get('browserMode','headless')
        assert leased['payload']['concurrency'] == options.get('concurrency',2)
        break

@pytest.mark.parametrize('model,path,scope', CASES)
def test_older_executor_is_rejected_instead_of_ignoring_options(tmp_path, model, path, scope):
    old_caps = [x for x in AS_CAPABILITIES if x != 'after.sale.runtime-controls.v1']
    for web, device, ids, database in _e2e_setup(tmp_path):
        heartbeat(device, ids['credential'], capabilities=old_caps, client_version=CLIENT_VERSION)
        response = web.post(path, json=dict(scope, executorId=ids['executorId'],
                            idempotencyKey='synthetic-old-executor'), headers=CSRF)
        assert response.status_code == 409, response.text
        assert 'executor_after_sale_runtime_upgrade_required' in response.text
        break
