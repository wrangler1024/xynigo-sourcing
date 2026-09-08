"""Account-scoped destination history and durable import options (offline)."""

import base64
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_procurement_import_cloud import FakeCloudSheetGateway, login, source_workbook
from xynigo_auth.models import User, WorkspaceViewPreference, ProcurementImportJob
from xynigo_auth.procurement_import_preferences import remember_target, read_preferences, VIEW_KEY

HEADERS = {'X-Xynigo-Web-CSRF': 'same-origin'}


def test_validated_history_color_preference_and_durable_disabled_color(tmp_path):
    gateway = FakeCloudSheetGateway()
    app, db, _oauth = build_test_app(tmp_path, procurement_import_enabled=True,
                                    procurement_import_gateway=gateway)
    with TestClient(app) as client:
        login(client)
        endpoint = '/v1/assistant/procurement-import/'
        assert client.get(endpoint + 'preferences').json() == {'targets': []}
        source = base64.b64encode(source_workbook()).decode()
        parsed = client.post(endpoint + 'parse', headers=HEADERS,
                             json={'filename':'synthetic.xlsx','contentBase64':source}).json()
        target = {'planId':parsed['planId'], 'spreadsheetUrl':'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetId':'sheetA'}
        bad = client.post(endpoint + 'target/validate', headers=HEADERS,
                          json={**target, 'sheetId':'deleted'})
        assert bad.status_code == 422
        assert client.get(endpoint + 'preferences').json() == {'targets': []}
        valid = client.post(endpoint + 'target/validate', headers=HEADERS, json=target)
        assert valid.status_code == 200, valid.text
        saved = valid.json()['preferences']['targets'][0]
        assert saved['sheetId'] == 'sheetA'
        assert saved['fillOrderBackground'] is True
        color = {'action':'color', 'spreadsheetUrl':saved['spreadsheetUrl'],
                 'sheetId':'sheetA', 'fillOrderBackground':False}
        assert client.post(endpoint + 'preferences', json=color).status_code == 403
        result = client.post(endpoint + 'preferences', headers=HEADERS, json=color)
        assert result.status_code == 200, result.text
        assert result.json()['targets'][0]['fillOrderBackground'] is False
        # Reloading from another request/database session restores the account preference.
        assert client.get(endpoint + 'preferences').json()['targets'][0]['fillOrderBackground'] is False
        revalidated = client.post(endpoint + 'target/validate', headers=HEADERS, json=target)
        assert revalidated.json()['preferences']['targets'][0]['fillOrderBackground'] is False
        assert len(revalidated.json()['preferences']['targets']) == 1
        assert client.post(endpoint + 'sheet-sync', headers=HEADERS, json={
            'planId':parsed['planId'],'confirmWrite':True,'fillOrderBackground':'false'}).status_code == 422
        started = client.post(endpoint + 'sheet-sync', headers=HEADERS, json={
            'planId':parsed['planId'],'confirmWrite':True,'fillOrderBackground':False})
        assert started.status_code == 202, started.text
        assert started.json()['fillOrderBackground'] is False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(endpoint + 'sheet-sync/status', params={'jobId':started.json()['jobId']}).json()
            if status['state'] in {'completed','partial','failed'}:
                break
            time.sleep(.05)
        assert status['state'] == 'completed', status
        assert status['rowsWritten'] == 1
        assert status['fillOrderBackground'] is False
        assert status['rowsStyled'] == 0
        assert gateway.backgrounds == {}
        assert len(gateway.links) == 1
        with db.session_factory() as session:
            stored = session.scalar(select(ProcurementImportJob))
            assert stored.progress['fillOrderBackground'] is False
            assert stored.progress['backgroundPlanIndices'] == [0]
        # Simulate recovery after a process stopped during formatting. Options
        # must survive persistence, recovery and construction of the core job.
        worker = app.state.procurement_import_worker
        worker.stop()
        with db.session_factory() as session:
            stored = session.scalar(select(ProcurementImportJob))
            stored.state = 'formatting_rows'
            stored.progress = {**stored.progress, 'state':'formatting_rows'}
            session.commit()
        worker._recover()
        recovered_id = worker._claim()
        assert recovered_id is not None
        worker._run(recovered_id)
        recovered = client.get(endpoint + 'sheet-sync/status', params={'jobId':str(recovered_id)}).json()
        assert recovered['state'] == 'completed', recovered
        assert recovered['fillOrderBackground'] is False
        assert recovered['backgroundPlanIndices'] == [0]
        assert recovered['rowsWritten'] == recovered['rowsStyled'] == 0
        assert len(gateway.rows) == 1
        removed = client.post(endpoint + 'preferences', headers=HEADERS,
                              json={**color,'action':'remove'})
        assert removed.json() == {'targets': []}
        # Client cannot insert unvalidated destinations through the preference API.
        assert client.post(endpoint + 'preferences', headers=HEADERS, json=color).json() == {'targets': []}


def test_recent_targets_are_bounded_deduplicated_and_scoped(tmp_path):
    app, db, _oauth = build_test_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        with db.session_factory() as session:
            user = session.scalar(select(User))
            tenant_id, user_id = user.tenant_id, user.id
            for index in range(7):
                remember_target(session, tenant_id, user_id, {
                    'spreadsheetUrl':'https://tenant.feishu.cn/sheets/SyntheticToken',
                    'spreadsheetName':'合成工作簿','sheetId':'sheet%d' % index,'sheetName':'合成工作表'})
            items = read_preferences(session, tenant_id, user_id)['targets']
            assert [item['sheetId'] for item in items] == ['sheet6','sheet5','sheet4','sheet3','sheet2']
            remember_target(session, tenant_id, user_id, {**items[-1],'sheetName':'已改名'})
            assert read_preferences(session, tenant_id, user_id)['targets'][0]['sheetName'] == '已改名'
            assert len(read_preferences(session, tenant_id, user_id)['targets']) == 5
            # The tenant/user components are both part of the persisted primary key.
            import uuid
            assert read_preferences(session, tenant_id, uuid.uuid4()) == {'targets': []}
            assert read_preferences(session, uuid.uuid4(), user_id) == {'targets': []}
            session.commit()
        with db.session_factory() as session:
            record = session.get(WorkspaceViewPreference, (tenant_id, user_id, VIEW_KEY))
            assert record.settings['targets'][0]['sheetId'] == 'sheet2'
