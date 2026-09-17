"""Synthetic history resolution: no browser and no real refund writes."""
import copy
import uuid

import pytest
from sqlalchemy import func, select

from xynigo_auth import after_sale_track_resolver as resolver
from xynigo_auth.models import (
    AfterSaleClaimResult, AfterSaleClaimRun, AfterSaleRefundTracking,
    ExecutorTask, LocalExecutor, Tenant, User,
)
from test_after_sale_claim import _e2e_setup, AS_CAPABILITIES, CLIENT_VERSION
from test_executor_channel import CSRF, heartbeat


@pytest.fixture
def context(tmp_path):
    yield from _e2e_setup(tmp_path)


def identity(session, ids):
    tenant = session.get(LocalExecutor, uuid.UUID(ids['executorId'])).tenant_id
    user = session.scalar(select(User.id).where(User.tenant_id == tenant))
    return tenant, user


def claim(session, tenant, user, serial, order, bill='', refunds=None):
    run = AfterSaleClaimRun(tenant_id=tenant, actor_user_id=user,
        source_run_key=str(uuid.uuid4()), payload_hash='a' * 64,
        status='completed', total_count=1, success_count=1, failed_count=0,
        source='synthetic')
    session.add(run)
    session.flush()
    row = AfterSaleClaimResult(run_id=run.id, tenant_id=tenant,
        environment_serial=serial, order_no=order, status='ok',
        refund_bill_id=bill, refunds=refunds or [], store_name='Synthetic')
    session.add(row)
    session.flush()
    return row


def test_resolves_all_batches_packages_and_tracking_in_input_order_without_writes(context):
    web, device, ids, database = context
    with database.session_factory() as session:
        tenant, user = identity(session, ids)
        row = claim(session, tenant, user, '900002', 'SYNTH-B', 'B2',
                    [{'refundBillId':'B1'}, {'refundBillId':'B2'}])
        claim(session, tenant, user, '900002', 'SYNTH-B', 'B2')
        claim(session, tenant, user, '900001', 'SYNTH-A', 'A1')
        # A later/older empty attempt cannot hide a valid earlier receipt.
        claim(session, tenant, user, '900001', 'SYNTH-A')
        claim(session, tenant, user, '900004', 'SYNTH-NO-BILL')
        session.add(AfterSaleRefundTracking(tenant_id=tenant, environment_serial='900003',
            order_no='synth-c', refund_bill_id='C1', phase='refunded', last_status='ok'))
        other = Tenant(feishu_tenant_key='synthetic-resolver-other')
        session.add(other); session.flush()
        claim(session, other.id, user, '900001', 'FOREIGN', 'FOREIGN-BILL')
        # Same bill in another tenant never creates a local identity conflict.
        session.add(AfterSaleRefundTracking(tenant_id=other.id, environment_serial='OTHER',
            order_no='FOREIGN', refund_bill_id='A1'))
        session.commit()
        original = copy.deepcopy(row.refunds)
        row_id = row.id
        before = session.scalar(select(func.count()).select_from(ExecutorTask))
    response = web.post('/v1/after-sale/track/resolve', headers=CSRF,
        json={'environmentSerials':['900003','900002','900001','900004','900005','900002']})
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert [r['refundBillId'] for r in data['items']] == ['C1','B1','B2','A1']
    assert data['items'][0]['orderNo'] == 'synth-c', 'preserve the established tracking identity exactly'
    assert data['billCount'] == 4 and data['environmentCount'] == 5
    assert data['matchedEnvironmentCount'] == 3
    assert [e['status'] for e in data['environments']] == ['matched']*3+['unmatched']*2
    assert '缺少' in data['environments'][3]['note']
    assert '不代表平台没有退款' in data['environments'][4]['note']
    assert 'FOREIGN' not in response.text
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExecutorTask)) == before
        assert session.get(AfterSaleClaimResult, row_id).refunds == original
    heartbeat(device, ids['credential'], capabilities=AS_CAPABILITIES, client_version=CLIENT_VERSION)
    created = web.post('/v1/after-sale/track', headers=CSRF, json={
        'executorId':ids['executorId'], 'idempotencyKey':'synthetic-resolved-tracking',
        'items':data['items'], 'browserMode':'headless', 'concurrency':3})
    assert created.status_code == 202, created.text
    task = heartbeat(device, ids['credential'], capabilities=AS_CAPABILITIES,
                     client_version=CLIENT_VERSION)['task']
    assert task['type'] == 'after.sale.track.v1'
    assert task['payload']['items'] == data['items']
    assert task['payload']['concurrency'] == 3


def test_conflicting_bill_identity_is_excluded_even_when_tracking_env_not_selected(context):
    web, _, ids, database = context
    with database.session_factory() as session:
        tenant, user = identity(session, ids)
        claim(session, tenant, user, '900001', 'SYNTH-A', 'CONFLICT')
        claim(session, tenant, user, '900002', 'SYNTH-B', 'CONFLICT')
        claim(session, tenant, user, '900001', 'SYNTH-C', 'BOUND-ELSEWHERE')
        claim(session, tenant, user, '900001', 'SYNTH-OK', 'SAFE')
        session.add(AfterSaleRefundTracking(tenant_id=tenant, refund_bill_id='BOUND-ELSEWHERE',
            environment_serial='900099', order_no='SYNTH-C'))
        session.commit()
    response = web.post('/v1/after-sale/track/resolve', headers=CSRF,
        json={'environmentSerials':['900001','900002']})
    assert response.status_code == 200
    data = response.json()['data']
    assert [item['refundBillId'] for item in data['items']] == ['SAFE']
    assert [e['status'] for e in data['environments']] == ['partial','unmatched']
    assert all('归属不一致' in e['note'] for e in data['environments'])
    for serial in ['900001', '900002']:
        single = web.post('/v1/after-sale/track/resolve', headers=CSRF,
                         json={'environmentSerials':[serial]}).json()['data']
        assert all(item['refundBillId'] != 'CONFLICT' for item in single['items'])
        assert '归属不一致' in single['environments'][0]['note']


def test_package_identity_projection_and_conflicts_outside_selected_environment(context):
    web, _, ids, database = context
    with database.session_factory() as session:
        tenant, user = identity(session, ids)
        claim(session, tenant, user, '900001', 'SYNTH-A', refunds=[
            {'refundBillId':'ARRAY-CONFLICT', 'detailsNote':'x'*1_000_000,
             'refundAccount':'unused-synthetic-account'}])
        claim(session, tenant, user, '900002', 'SYNTH-B', refunds=[
            {'refundBillId':'ARRAY-CONFLICT'}])
        session.commit()
        # Database projection never returns the receipt JSON/account/diagnostics.
        rows = session.execute(select(resolver._claim_identities(session, tenant, ['900001']))).all()
        assert rows and all(len(row) == 4 for row in rows)
        assert max(len(str(value)) for row in rows for value in row) < 128
    response = web.post('/v1/after-sale/track/resolve', headers=CSRF,
                        json={'environmentSerials':['900001']})
    assert response.status_code == 200
    assert response.json()['data']['items'] == []
    assert '归属不一致' in response.json()['data']['environments'][0]['note']
    assert 'unused-synthetic-account' not in response.text


def test_validation_auth_and_limits_do_not_create_partial_tasks(context, monkeypatch):
    web, device, ids, database = context
    path = '/v1/after-sale/track/resolve'
    assert device.post(path, json={'environmentSerials':['900001']}).status_code == 401
    assert web.post(path, json={'environmentSerials':['900001']}).status_code == 403
    for serials in [[], [''], ['  '], ['ENV ORDER BILL'], ['abc'], [True],
                    ['9'*65], ['900001']*301]:
        assert web.post(path, headers=CSRF, json={'environmentSerials':serials}).status_code == 422
    assert web.post(path, headers=CSRF,
                    json={'environmentSerials':['900001'],'tenantId':'wrong'}).status_code == 422
    with database.session_factory() as session:
        tenant, user = identity(session, ids)
        claim(session, tenant, user, '900001', 'SYNTH-A', 'B1', [{'refundBillId':'B2'}])
        session.commit()
    monkeypatch.setattr(resolver, 'MAX_TRACK_ITEMS', 1)
    too_many = web.post(path, headers=CSRF, json={'environmentSerials':['900001']})
    assert too_many.status_code == 422
    monkeypatch.setattr(resolver, 'MAX_TRACK_ITEMS', 500)
    monkeypatch.setattr(resolver, 'MAX_HISTORY_ROWS', 0)
    assert web.post(path, headers=CSRF, json={'environmentSerials':['900001']}).status_code == 422
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExecutorTask)) == 0
