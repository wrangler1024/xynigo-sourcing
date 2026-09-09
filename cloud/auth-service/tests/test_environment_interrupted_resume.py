"""Recover interrupted batches without releasing duplicate-account guards."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_executor_channel import CSRF, login, pair, create_pairing_code, heartbeat, environment_workbook
from xynigo_auth.models import (Tenant, User, EnvironmentCreationRun, EnvironmentCreationResult,
    HubEnvironmentInventory, EnvironmentAccountRunGuard, ExecutorTask, EnvironmentAccountPlan, LocalExecutor)

CAPS = ['environment.cloud-plan.v1', 'environment.cloud-inventory.v1',
        'environment.create-bound.v1', 'environment.resume.v1']


@pytest.fixture
def interrupted(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    with TestClient(app) as web, TestClient(app) as device:
        login(web)
        paired = pair(device, create_pairing_code(web), capabilities=CAPS)
        credential, executor = str(paired['deviceCredential']), str(paired['executorId'])
        heartbeat(device, credential, capabilities=CAPS)
        refs = [hashlib.sha256(f'buyer{i}@example.test'.encode()).hexdigest() for i in range(1, 4)]
        now = datetime.now(UTC)
        with database.session_factory() as session:
            tenant, actor = session.scalar(select(Tenant)), session.scalar(select(User))
            run = EnvironmentCreationRun(id=uuid.uuid4(), tenant_id=tenant.id, actor_user_id=actor.id,
                executor_id=uuid.UUID(executor), source_run_key='synthetic-interrupted-batch', payload_hash='a'*64,
                run_mode='bound', site='MX', purchase_date='20260909', environment_group='MX采购',
                status='uncertain', phase='uncertain', total_count=3, success_count=1, failed_count=0,
                progress_total=3, progress_completed=1, ip_ok_count=0, ip_total_count=0,
                request_summary={'accountRefs': refs, 'assignments': [{'purchaserLabel':'新刚','count':3}]},
                source='cloud_web', created_at=now-timedelta(minutes=2), updated_at=now)
            session.add(run)
            session.flush()
            for i, ref in enumerate(refs, 1):
                name = f'XG-MX-260909-{i:03d}-ABCD'
                session.add(HubEnvironmentInventory(id=uuid.uuid4(), tenant_id=tenant.id, account_ref=ref,
                    source_order_ref='sha256:' + hashlib.sha256(f'{i:08x}'.encode()).hexdigest(),
                    environment_name=name, environment_ref=f'800{i}' if i != 2 else None,
                    environment_serial=str(100+i) if i != 2 else None, site='MX',
                    environment_group='MX采购', purchaser_label='新刚', state='uncertain', source_run_id=run.id,
                    created_at=now, updated_at=now))
                session.add(EnvironmentAccountRunGuard(id=uuid.uuid4(), tenant_id=tenant.id,
                    account_ref=ref, run_id=run.id, state='cleanup_failed', created_at=now, updated_at=now))
                if i == 1:
                    session.add(EnvironmentCreationResult(id=uuid.uuid4(), run_id=run.id, tenant_id=tenant.id,
                        account_ref=ref, account_label='bu***@example.test', purchaser_label='新刚',
                        environment_name=name, environment_ref='8001', status='success', current_step='done',
                        completed_steps=['env_created','cookie_imported','account_bound','remarked','done'],
                        created_in_run=True, cleanup_status='not_required', created_at=now, updated_at=now))
            parent = str(run.id)
            session.commit()
        def parse(key, count=3, site='MX'):
            result = web.post('/v1/environment-plans/parse', headers=CSRF, json={
                'idempotencyKey': 'synthetic-plan-' + key, 'filename': 'buyers.xlsx',
                'contentBase64': environment_workbook(count, site=site), 'site':site, 'environmentGroup':site+'采购'})
            assert result.status_code == 201, result.text
            return result.json()['cloudPlanId']
        payload = {'idempotencyKey':'synthetic-resume-request', 'retryMode':'interrupted',
            'accountRefs':refs[1:], 'executorId':executor, 'cloudPlanId':parse('valid'), 'confirmOriginalStopped':True}
        yield web, device, database, credential, parent, refs, payload, parse


def test_resume_keeps_identity_and_recovers_rows_without_first_progress(interrupted):
    web, device, database, credential, parent, refs, payload, _ = interrupted
    snapshot = web.get('/v1/operation-runs/environment-creation/' + parent).json()['data']
    assert len(snapshot['recoveryRows']) == 3
    endpoint = '/v1/operation-runs/environment-creation/' + parent + '/retry'
    resumed = web.post(endpoint, headers=CSRF, json=payload)
    assert resumed.status_code == 202, resumed.text
    child = resumed.json()['data']
    assert child['rootRunId'] == parent
    replay = web.post(endpoint, headers=CSRF, json=payload)
    assert replay.status_code == 202, replay.text
    assert replay.json()['data']['runId'] == child['runId']
    assert replay.json()['data']['unchanged']
    conflicting = web.post(endpoint, headers=CSRF, json={**payload, 'idempotencyKey':'synthetic-overlap-resume'})
    assert conflicting.status_code == 409
    leased = heartbeat(device, credential, capabilities=CAPS)['task']
    assert leased['type'] == 'environment.create-bound.v1'
    body = leased['payload']
    assert body['inventoryCacheFresh'] is False
    assert body['cleanupBlockedAccountRefs'] == []
    assert len(body['planAccounts']) == 2
    assert body['assignments'] == [{'purchaserLabel':'新刚','count':2}]
    assert {r['accountRef'] for r in body['resumeContext']['rows']} == set(refs[1:])
    third = next(r for r in body['resumeContext']['rows'] if r['accountRef'] == refs[2])
    assert third['environmentRef'] == '8003', 'inventory evidence must survive transfer to the child run'
    assert body['resumeContext']['originalAssignments'] == [{'purchaserLabel':'新刚','count':3}]
    assert {r['environmentName'] for r in body['plannedEnvironmentNames']} == {
        'XG-MX-260909-002-ABCD', 'XG-MX-260909-003-ABCD'}
    with database.session_factory() as session:
        assert len(list(session.scalars(select(ExecutorTask)))) == 1
        for guard in session.scalars(select(EnvironmentAccountRunGuard)):
            assert str(guard.run_id) == (parent if guard.account_ref == refs[0] else child['runId'])
        plan = session.get(EnvironmentAccountPlan, uuid.UUID(payload['cloudPlanId']))
        assert plan.encrypted_payload is None
        assert 'synthetic-password' not in json.dumps(session.scalar(select(ExecutorTask)).payload_envelope)


def test_resume_rejects_wrong_proof_file_device_and_old_client(interrupted):
    web, _device, database, _credential, parent, refs, payload, parse = interrupted
    endpoint = '/v1/operation-runs/environment-creation/' + parent + '/retry'
    invalid = [({'confirmOriginalStopped':False}, 422), ({'confirmOriginalStopped':'true'},422),
        ({'accountRefs':refs},409), ({'accountRefs':[refs[1]]},409),
        ({'executorId':str(uuid.uuid4())},409),
        ({'cloudPlanId':parse('missing', count=1)},409),
        ({'cloudPlanId':parse('wrong-site', site='US')},409)]
    for fields, status in invalid:
        result = web.post(endpoint, headers=CSRF, json={**payload, **fields})
        assert result.status_code == status, result.text
    with database.session_factory() as session:
        executor = session.get(LocalExecutor, uuid.UUID(payload['executorId']))
        executor.capabilities = [c for c in CAPS if c != 'environment.resume.v1']
        session.commit()
    result = web.post(endpoint, headers=CSRF, json=payload)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'executor_capability_missing'
    with database.session_factory() as session:
        assert not list(session.scalars(select(ExecutorTask)))
        assert {str(g.run_id) for g in session.scalars(select(EnvironmentAccountRunGuard))} == {parent}


def test_expired_environment_lease_records_actionable_reason_and_preserves_batch(interrupted):
    web, device, database, credential, parent, refs, _payload, _parse = interrupted
    with database.session_factory() as session:
        run = session.get(EnvironmentCreationRun, uuid.UUID(parent))
        run.status = 'running'
        task = ExecutorTask(id=uuid.uuid4(), tenant_id=run.tenant_id, executor_id=run.executor_id,
            created_by_user_id=run.actor_user_id, task_type='environment.create-bound.v1',
            status='running', idempotency_key='synthetic-expired-lease', attempt=1,
            lease_until=datetime.now(UTC)-timedelta(seconds=1), started_at=datetime.now(UTC)-timedelta(minutes=2))
        session.add(task)
        session.flush()
        run.executor_task_id = task.id
        session.commit()
    assert heartbeat(device, credential, capabilities=CAPS)['task'] is None
    snapshot = web.get('/v1/operation-runs/environment-creation/' + parent).json()['data']
    assert snapshot['status'] == 'uncertain'
    assert snapshot['errorCode'] == 'lease_expired_after_start'
    assert '原批次核对续跑' in snapshot['errorSummary']
    assert snapshot['successCount'] == 1
    assert {r['accountRef'] for r in snapshot['recoveryRows']} == set(refs)


def test_only_original_creator_can_resume(interrupted):
    web, _device, database, _credential, parent, _refs, payload, _parse = interrupted
    with database.session_factory() as session:
        run = session.get(EnvironmentCreationRun, uuid.UUID(parent))
        other = User(tenant_id=run.tenant_id, feishu_open_id='synthetic-other-creator', display_name='合成用户')
        session.add(other)
        session.flush()
        run.actor_user_id = other.id
        session.commit()
    result = web.post('/v1/operation-runs/environment-creation/'+parent+'/retry', json=payload, headers=CSRF)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'environment_resume_not_allowed'
