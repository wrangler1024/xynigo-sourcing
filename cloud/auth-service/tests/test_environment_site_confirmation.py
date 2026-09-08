"""Reject unreviewed mixed-cookie / filename conflicts before scheduling writes."""
import base64
import hashlib
from io import BytesIO
from datetime import timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select

from test_auth_flow import build_test_app
from test_executor_channel import login, pair, create_pairing_code, heartbeat, CSRF
from xynigo_auth.environment_plan_service import CloudEnvironmentPlanError
from xynigo_auth.models import EnvironmentAccountPlan, EnvironmentCreationRun, ExecutorTask, EnvironmentNameSequence


def mixed_source():
    workbook = Workbook()
    workbook.active.append(['buyer-review@example.test', 'synthetic-password',
        'https://vendor.example/api?orderNo=abc123',
        '[{"domain":".shein.com.mx"},{"domain":".us.shein.com"}]'])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return base64.b64encode(output.getvalue()).decode('ascii')


def upload(client, key, filename='mx-20.xlsx', site='US', group='美国采购'):
    return client.post('/v1/environment-plans/parse', headers=CSRF, json={
        'idempotencyKey':key,'filename':filename,'contentBase64':mixed_source(),
        'site':site,'environmentGroup':group})


def test_cloud_creation_requires_specific_site_and_separate_filename_acknowledgment(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    capabilities = ['config.read.v1', 'config.write.v1', 'environment.cloud-plan.v1',
                    'environment.cloud-inventory.v1', 'environment.create-bound.v1']
    with TestClient(app) as client, TestClient(app) as device:
        login(client)
        paired = pair(device, create_pairing_code(client), capabilities=capabilities)
        heartbeat(device, str(paired['deviceCredential']), capabilities=capabilities)
        parsed = upload(client, 'mixed-site-plan-0001')
        assert parsed.status_code == 201, parsed.text
        plan = parsed.json()
        assert plan['filenameSiteHints'] == ['MX']
        assert plan['filenameSiteConflict'] is True
        assert plan['siteConfirmationRequired'] is True
        assert plan['mixedSiteCookieCount'] == 1
        body = {'idempotencyKey':'mixed-site-start-0001','executorId':paired['executorId'],
                'mode':'bound','site':'US','purchaseDate':'20260908','environmentGroup':'美国采购',
                'cloudPlanId':plan['cloudPlanId'],'totalCount':1,
                'assignments':[{'purchaserLabel':'新刚','count':1}]}
        for extra in ({}, {'confirmedSite':'MX'}, {'confirmedSite':'US'},
                      {'confirmedSite':'US','confirmFilenameSiteMismatch':'true'}):
            rejected = client.post('/v1/operation-runs/environment-creation',headers=CSRF,json={**body,**extra})
            assert rejected.status_code == 422, rejected.text
            with database.session_factory() as session:
                assert session.scalar(select(func.count(ExecutorTask.id))) == 0
                assert session.scalar(select(func.count(EnvironmentCreationRun.id))) == 0
                assert session.scalar(select(func.count(EnvironmentNameSequence.id))) == 0
                stored = session.get(EnvironmentAccountPlan, uuid.UUID(plan['cloudPlanId']))
                assert stored.status == 'parsed' and stored.encrypted_payload
        preview_body={k:v for k,v in body.items() if k not in ('mode','cloudPlanId')}
        rejected_preview=client.post('/v1/environment-plans/'+plan['cloudPlanId']+'/preview',
            headers=CSRF,json=preview_body)
        assert rejected_preview.status_code == 422
        accepted = client.post('/v1/operation-runs/environment-creation',headers=CSRF,
            json={**body,'confirmedSite':'US','confirmFilenameSiteMismatch':True})
        assert accepted.status_code == 202, accepted.text
        task=heartbeat(device,str(paired['deviceCredential']),capabilities=capabilities)['task']
        assert task['payload']['confirmedSite'] == 'US'
        assert task['payload']['confirmFilenameSiteMismatch'] is True
        assert task['payload']['filename'] == 'mx-20.xlsx'
    with database.session_factory() as session:
        record=session.get(EnvironmentAccountPlan,uuid.UUID(plan['cloudPlanId']))
        assert record.status == 'submitted' and record.encrypted_payload is None
        run=session.scalar(select(EnvironmentCreationRun))
        assert run.request_summary['confirmedSite'] == 'US'
        assert run.request_summary['confirmFilenameSiteMismatch'] is True


def test_filename_changes_do_not_reuse_different_warning_context_and_group_conflicts_block(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        first=upload(client,'filename-review-0001','US-20.xlsx').json()
        assert first['filenameSiteConflict'] is False
        second=upload(client,'filename-review-0002','mx-20.xlsx').json()
        assert second['cloudPlanId'] != first['cloudPlanId']
        assert second['filenameSiteConflict'] is True
        assert second['siteConfirmationRequired'] is True
        replay=upload(client,'filename-review-0002','US-20.xlsx')
        assert replay.status_code == 409
        wrong_group=upload(client,'filename-review-0003','mx-20.xlsx',site='MX',group='美国采购')
        assert wrong_group.status_code == 422
        assert wrong_group.json()['detail']['code'] == 'environment_plan_group_site_mismatch'
        # SQLite's server-side timestamps have one-second precision.
        with database.session_factory() as session:
            previous=session.get(EnvironmentAccountPlan,uuid.UUID(first['cloudPlanId']))
            previous.created_at -= timedelta(seconds=1)
            session.commit()
        latest=client.get('/v1/environment-plans/latest',params={'site':'US','environmentGroup':'美国采购'}).json()['plan']
        assert latest['filename'] == 'mx-20.xlsx' and latest['siteConfirmationRequired'] is True
        with database.session_factory() as session:
            record=session.get(EnvironmentAccountPlan,uuid.UUID(second['cloudPlanId']))
            service=app.state.environment_plan_service
            params=dict(session=session,tenant_id=record.tenant_id,actor_user_id=record.created_by_user_id,
                        cloud_plan_id=record.id,site='US',environment_group='美国采购',
                        account_refs={hashlib.sha256(b'buyer-review@example.test').hexdigest()})
            with pytest.raises(CloudEnvironmentPlanError):
                service.load_for_takeover(**params)
            with pytest.raises(CloudEnvironmentPlanError):
                service.load_for_takeover(**params,confirmed_site='US')
            _,accounts=service.load_for_takeover(**params,confirmed_site='US',confirm_filename_site_mismatch=True)
            assert len(accounts)==1
            assert record.status=='parsed'
