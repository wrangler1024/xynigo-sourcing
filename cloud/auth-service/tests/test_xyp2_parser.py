"""Authenticated read-only decoder, independent of Feishu import setup."""
import json

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth_flow import build_test_app
from test_procurement_import_cloud import login
from test_admin_api import build_admin_app, MEMBER_TOKEN
from xynigo_auth.models import AuditEvent, ProcurementImportJob, ProcurementImportPlan, PurchaseOrder


REMARK = '[XYP2]' + json.dumps({'d':'mx', 'c':'MXN', 'i':[
    ['SYNTH-READONLY', '123456789', 'SYNTH-SKU', '27_447', 'Black', 'M', 20, .5, 10.25, 2]
]}) + '[/XYP2]'


def test_xyp2_auth_csrf_and_parse_without_feishu_setup_or_business_writes(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    assert app.state.procurement_import_service is None
    headers = {'X-Xynigo-Web-CSRF':'same-origin'}
    with TestClient(app) as client:
        denied = client.post('/v1/assistant/xyp2/parse', headers=headers, json={'remark':REMARK})
        assert denied.status_code == 401
        login(client)
        denied_csrf = client.post('/v1/assistant/xyp2/parse', json={'remark':REMARK})
        assert denied_csrf.status_code == 403
        result = client.post('/v1/assistant/xyp2/parse', headers=headers, json={'remark':REMARK})
        assert result.status_code == 200, result.text
        assert result.json()['guideTotal'] == 20.5
        assert result.json()['quantityCount'] == 2
        assert result.json()['items'][0]['secondarySpec'] == 'M'
        for remark in ('[XYP2]{broken}[/XYP2]', REMARK + '[XYP2]{', 'x' * 20001, ''):
            invalid = client.post('/v1/assistant/xyp2/parse', headers=headers, json={'remark':remark})
            assert invalid.status_code == 422, invalid.text
        wrong_type = client.post('/v1/assistant/xyp2/parse', headers=headers, json={'remark':123})
        assert wrong_type.status_code == 422
        with database.session_factory() as session:
            assert session.scalar(select(ProcurementImportPlan)) is None
            assert session.scalar(select(ProcurementImportJob)) is None
            assert session.scalar(select(PurchaseOrder)) is None
            events = session.scalars(select(AuditEvent)).all()
            assert 'SYNTH-READONLY' not in json.dumps([event.details for event in events], default=str)


def test_xyp2_requires_assistant_permission(tmp_path):
    app, database, _ = build_admin_app(tmp_path)
    with TestClient(app) as client:
        denied = client.post('/v1/assistant/xyp2/parse',
                            headers={'Authorization':'Bearer ' + MEMBER_TOKEN}, json={'remark':REMARK})
        assert denied.status_code == 403, denied.text
        assert denied.json()['detail']['code'] == 'permission_denied'
