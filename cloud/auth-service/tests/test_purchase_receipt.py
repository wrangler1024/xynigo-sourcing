import base64
import copy
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from xynigo_auth.models import Tenant, User, PurchaseReceipt, PurchaseReceiptSlot, PurchaseReceiptOrder
from xynigo_auth.purchase_receipt import ReceiptBody, ReceiptError, execute, key_for
from xynigo_auth.procurement_import_sheet import SheetTable, LarkSheetSyncError, _column_index


class Gateway:
    headers = ('系统订单键','销售订单号','店铺','包裹号','采购员','主规格','需求数量','采购订单号','实际付款','下单截图')
    def __init__(self):
        self.rows = [['origin','SALE-1','Demo shop','PACKAGE-1','Demo buyer','L',1,'','',''],
                     ['origin','SALE-1','Demo shop','PACKAGE-1','Demo buyer','M',1,'','','']]
        self.writes = 0
        self.image_writes = 0
        self.fail_image = None
        self.fail_text = False
    def read_table(self, *args):
        return SheetTable(self.headers, tuple((i+2, tuple(r)) for i,r in enumerate(self.rows)))
    def _write_value_ranges(self, url, ranges):
        import re
        self.writes += 1
        if self.fail_text:
            raise OSError('network lost')
        for item in ranges:
            c, r = re.search(r'!([A-Z]+)(\d+):',item['range']).groups()
            self.rows[int(r)-2][_column_index(c)-1] = item['values'][0][0]
    def set_image(self, url, sheet, row, data, mime, column):
        self.image_writes += 1
        if self.fail_image:
            raise self.fail_image
        self.rows[row-2][_column_index(column)-1] = {'type':'image','file_token':'demo-image-'+str(self.image_writes)}
    def _read_range(self, url, sheet, cell_range, *, raw=False):
        assert raw is True, "evidence verification requires original image tokens"
        import re
        c, r = re.match(r'([A-Z]+)([0-9]+)',cell_range).groups()
        return {'valueRange':{'values':[[self.rows[int(r)-2][_column_index(c)-1]]]}}


@pytest.fixture
def ctx():
    e = create_engine('sqlite://')
    for model in (Tenant,User,PurchaseReceipt,PurchaseReceiptSlot,PurchaseReceiptOrder):
        model.__table__.create(e)
    with Session(e) as s:
        tenant = Tenant(id=uuid.uuid4(),feishu_tenant_key='demo-tenant',name='Demo')
        s.add(tenant); s.flush()
        user = User(id=uuid.uuid4(),tenant_id=tenant.id,feishu_open_id='demo-user',display_name='Demo buyer')
        s.add(user);s.commit()
        g = Gateway()
        target = dict(spreadsheetToken='DemoSpreadsheet123',sheetId='DemoSheet',label='Demo team',sheetName='Execution')
        def run(body):return execute(s, ReceiptBody(**body), user=user, target=target, gateway=g)
        key=key_for(dict(zip(g.headers,g.rows[0])))
        body=dict(action='preview',sourceId='demo-source',taskKey=key)
        preview=run(body)
        body.update(action='submit',expectedRevision=preview['expectedRevision'],fingerprint=preview['fingerprint'],
            orderNo='DEMO-MX-0001',amount='110.02',image=base64.b64encode(b'\xff\xd8\xff'+b'x'*200).decode(),requestId=str(uuid.uuid4()))
        yield s,user,g,run,body


def test_multiple_goods_count_payment_once_and_idempotent(ctx):
    s,u,g,run,b=ctx
    result=run(b)
    assert result['state']=='complete'
    assert [r[7] for r in g.rows]==['DEMO-MX-0001']*2
    assert [r[8] for r in g.rows]==[110.02,'']
    assert run(b)['receiptId']==result['receiptId']
    assert g.writes==g.image_writes==1
    assert s.scalar(select(PurchaseReceipt)).image.startswith(b'\xff\xd8\xff')


def test_exact_split_suffix_and_ownership(ctx):
    s,u,g,run,b=ctx
    g.rows[0][1]='SALE-1-1';g.rows[1][1]='SALE-1-2'
    with pytest.raises(ReceiptError):run(b)
    assert g.writes==0
    g.rows[0][1]=g.rows[1][1]='SALE-1'
    g.rows[0][4]='Other buyer'
    with pytest.raises(ReceiptError,match='采购员'):run(b)


def test_manual_values_are_not_overwritten(ctx):
    s,u,g,run,b=ctx
    g.rows[0][8]=77
    with pytest.raises(ReceiptError,match='已有'):run(b)
    assert g.writes==0


def test_revision_preserves_prior_evidence(ctx):
    s,u,g,run,b=ctx
    first=run(b)
    preview=run(dict(action='preview',sourceId=b['sourceId'],taskKey=b['taskKey']))
    revised={**b,'requestId':str(uuid.uuid4()),'orderNo':'DEMO-MX-0002','amount':'119.00',
             'expectedRevision':preview['expectedRevision'],'reason':'原订单取消后重新下单'}
    assert run(revised)['state']=='complete'
    records=list(s.scalars(select(PurchaseReceipt).order_by(PurchaseReceipt.revision)))
    assert len(records)==2 and str(records[1].previous_id)==first['receiptId']
    assert records[0].amount=='110.02' and records[1].amount=='119.00'
    with pytest.raises(ReceiptError):run({**b,'requestId':str(uuid.uuid4())})


def test_rejected_image_retries_only_image(ctx):
    s,u,g,run,b=ctx
    g.fail_image=LarkSheetSyncError('permission denied',code=90218)
    assert run(b)['state']=='image_failed'
    g.fail_image=None
    assert run({**b,'action':'retry-image','image':''})['state']=='complete'
    assert g.writes==1 and g.image_writes==2


def test_unknown_result_never_replays(ctx):
    s,u,g,run,b=ctx
    g.fail_text=True
    assert run(b)['state']=='uncertain'
    assert run(b)['state']=='uncertain'
    assert run({**b,'action':'retry-image','image':''})['state']=='uncertain'
    assert g.writes==1 and g.image_writes==0


def test_image_timeout_blocks_retry(ctx):
    s,u,g,run,b=ctx
    g.fail_image=OSError('timeout')
    assert run(b)['state']=='uncertain'
    assert run({**b,'action':'retry-image','image':''})['state']=='uncertain'
    assert g.image_writes==1


def test_duplicate_order_cannot_bind_other_task(ctx):
    s,u,g,run,b=ctx
    run(b)
    g.rows=[['another','SALE-2','Demo shop','PACKAGE-2','Demo buyer','L',1,'','','']]
    key=key_for(dict(zip(g.headers,g.rows[0])))
    preview=run(dict(action='preview',sourceId=b['sourceId'],taskKey=key))
    with pytest.raises(ReceiptError,match='其他任务'):
        run({**b,'taskKey':key,'requestId':str(uuid.uuid4()),'fingerprint':preview['fingerprint']})


def test_invalid_money_and_missing_header(ctx):
    s,u,g,run,b=ctx
    for amount in ['NaN','Infinity','-1','1.001']:
        with pytest.raises(ReceiptError):run({**b,'amount':amount})
    g.headers=tuple('renamed' if h=='实际付款' else h for h in g.headers)
    with pytest.raises(ReceiptError,match='表头'):run(b)
    assert g.writes==0


def test_preexisting_real_image_cell_is_not_overwritten(ctx):
    s,u,g,run,b=ctx
    g.rows[0][9]={'type':'image','file_token':'manual-evidence'}
    with pytest.raises(ReceiptError,match='截图'):run(b)
    assert g.image_writes==0


def test_api_auth_target_restrictions_and_complete_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_auth_flow import build_test_app
    from test_executor_channel import login, CSRF
    from xynigo_auth.tenant_feishu import TenantFeishuService
    import xynigo_auth.main as main
    app, db, _ = build_test_app(tmp_path)
    target=dict(id='demo-source',scope='team',enabled=True,migrationState='ready',
                spreadsheetToken='DemoSpreadsheet123',sheetId='DemoSheet',label='Demo',sheetName='Execution')
    monkeypatch.setattr(app.state.data_source_registry_service,'read',lambda *a,**kw:{'registry':{'dataSources':[target]}})
    monkeypatch.setattr(TenantFeishuService,'resolve',lambda *a:SimpleNamespace(app_id='demo',app_secret='synthetic'))
    g=Gateway()
    monkeypatch.setattr(main,'FeishuSheetsGateway',lambda **kw:g)
    key=key_for(dict(zip(g.headers,g.rows[0])))
    endpoint='/v1/assistant/purchase-receipts'
    with TestClient(app) as client:
        body=dict(action='preview',sourceId='demo-source',taskKey=key)
        assert client.post(endpoint,json=body,headers=CSRF).status_code==401
        login(client)
        assert client.post(endpoint,json={**body,'sourceId':'another-tenant-source'},headers=CSRF).status_code==403
        target['scope']='personal'
        assert client.post(endpoint,json=body,headers=CSRF).status_code==403
        target['scope']='team'
        result=client.post(endpoint,json=body,headers=CSRF)
        assert result.status_code==200,result.text
        preview=result.json()
        result=client.post(endpoint,json={**body,'action':'submit','expectedRevision':0,
            'fingerprint':preview['fingerprint'],'orderNo':'DEMO-ORDER-007','amount':'118.23',
            'requestId':str(uuid.uuid4()),'image':base64.b64encode(b'\xff\xd8\xff'+b'x'*200).decode()},headers=CSRF)
        assert result.status_code==200,result.text
        assert result.json()['state']=='complete'


def test_duplicate_purchaser_names_require_explicit_user_id(ctx):
    s,u,g,run,b=ctx
    s.add(User(id=uuid.uuid4(),tenant_id=u.tenant_id,feishu_open_id='other-user',display_name=u.display_name))
    s.commit()
    with pytest.raises(ReceiptError,match='采购员'):run(b)
    assert g.writes==0


def test_manual_image_change_blocks_retry(ctx):
    s,u,g,run,b=ctx
    g.fail_image=LarkSheetSyncError('denied',code=90218)
    assert run(b)['state']=='image_failed'
    g.fail_image=None
    g.rows[0][9]={'type':'image','file_token':'manual-image'}
    assert run({**b,'action':'retry-image','image':''})['state']=='uncertain'
    assert g.image_writes==1


def test_migration_creates_and_removes_only_receipt_tables():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    path=Path(__file__).parents[1]/'migrations/versions/0035_purchase_receipts.py'
    spec=importlib.util.spec_from_file_location('receipt_migration',path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    e=create_engine('sqlite://')
    Tenant.__table__.create(e);User.__table__.create(e)
    with e.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            names=inspect(connection).get_table_names()
            assert all(n in names for n in ['purchase_receipts','purchase_receipt_slots','purchase_receipt_orders'])
            assert {c['name'] for c in inspect(connection).get_columns('purchase_receipts')}==set(PurchaseReceipt.__table__.columns.keys())
            migration.downgrade()
            assert set(inspect(connection).get_table_names())=={'tenants','users'}


def test_target_rebinding_invalidates_same_rows_preview(ctx):
    from xynigo_auth.purchase_receipt import locate
    s,u,g,run,b=ctx
    target={'spreadsheetToken':'DemoSheetA','sheetId':'DemoTabA'}
    first=locate(g,target,b['taskKey'],u,False)[2]
    second=locate(g,{**target,'spreadsheetToken':'DemoSheetB'},b['taskKey'],u,False)[2]
    assert first != second


def test_duplicate_names_never_authorize_unassigned_rows(ctx):
    s,u,g,run,b=ctx
    s.add(User(id=uuid.uuid4(),tenant_id=u.tenant_id,feishu_open_id='duplicate-name',display_name=u.display_name))
    s.commit()
    g.rows[0][4]=g.rows[1][4]=''
    with pytest.raises(ReceiptError,match='采购员'):run(b)
    assert g.writes==0


def test_image_reads_use_gateway_original_value_request():
    from xynigo_auth.procurement_import_sheet import FeishuSheetsGateway
    from xynigo_auth.purchase_receipt import read_images
    gateway=FeishuSheetsGateway(app_id='synthetic-app',app_secret='synthetic-secret')
    calls=[]
    def request(method,path,**kwargs):
        calls.append((method,path,kwargs))
        return {'valueRange':{'values':[[{'type':'image','file_token':'original-demo-token'}]]}}
    gateway._request=request
    assert read_images(gateway,{'spreadsheetToken':'DemoSpreadsheet123','sheetId':'DemoTab'},
        ['下单截图'],[(2,{})])[0]['file_token']=='original-demo-token'
    assert calls[0][0]=='GET' and calls[0][2]['params'] is None


def test_formatted_money_does_not_break_readback(ctx):
    s,u,g,run,b=ctx
    old=g.read_table
    def formatted(*args):
        t=old(*args)
        return SheetTable(t.headers,tuple((n,tuple('$MXN1,234.50' if i==8 and v else v for i,v in enumerate(values))) for n,values in t.rows))
    g.read_table=formatted
    assert run({**b,'amount':'1234.50'})['state']=='complete'
