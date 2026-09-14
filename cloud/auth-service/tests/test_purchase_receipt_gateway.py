import copy
import re
from urllib.parse import unquote

import httpx

from xynigo_auth.purchase_receipt_gateway import ReceiptGatewayFactory
from xynigo_auth.procurement_import_sheet import FeishuSheetsGateway, _column_index
from xynigo_auth.purchase_receipt import execute, ReceiptBody
from test_purchase_receipt import ctx


class SheetsTransport:
    def __init__(self, gateway):
        self.gateway = gateway
        self.calls = []

    def __call__(self, request):
        import json
        path=unquote(request.url.path)
        self.calls.append((request.method,path))
        g=self.gateway
        if path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200,json={'code':0,'tenant_access_token':'synthetic-token','expire':7200})
        if path.endswith('/sheets/query'):
            data={'sheets':[{'resource_type':'sheet','sheet_id':'DemoSheet','title':'Execution','grid_properties':{'row_count':len(g.rows)+1}}]}
        elif '/sheets/v3/spreadsheets/' in path:
            data={'spreadsheet':{'title':'Demo'}}
        elif '/values/' in path:
            cell=path.rsplit('/',1)[-1].split('!')[1]
            if cell.startswith('A1:AZ'):
                values=[list(g.headers)]+copy.deepcopy(g.rows)
                if request.url.params.get('valueRenderOption')=='ToString':
                    values=[[str(x) if isinstance(x,(int,float)) else x for x in row] for row in values]
            else:
                c,r=re.match(r'([A-Z]+)(\d+)',cell).groups()
                last=int(re.search(r':(?:[A-Z]+)(\d+)$',cell).group(1))
                values=[[g.rows[number-2][_column_index(c)-1]] for number in range(int(r),last+1)]
            data={'valueRange':{'values':values}}
        elif path.endswith('/values_batch_update'):
            body=json.loads(request.content)
            g._write_value_ranges('',body['valueRanges']);data={}
        elif path.endswith('/values_image'):
            body=json.loads(request.content)
            c,r=re.search(r'!([A-Z]+)(\d+):',body['range']).groups()
            g.set_image('','',int(r),b'demo','image/jpeg',c);data={}
        elif path.endswith('/styles_batch_update'):
            data={}
        else:
            raise AssertionError(path)
        return httpx.Response(200,json={'code':0,'data':data})


def perform(ctx, optimized, *, single=True, color=""):
    s,u,g,run,body=ctx
    # One matched row gives a reproducible baseline; no real order data.
    if single:g.rows=g.rows[:1]
    transport=SheetsTransport(g)
    factory=ReceiptGatewayFactory(httpx.MockTransport(transport))
    def gateway():
        if optimized:return factory.create(u.tenant_id,'demo','synthetic')
        return FeishuSheetsGateway(app_id='demo',app_secret='synthetic',transport=httpx.MockTransport(transport))
    target=dict(spreadsheetToken='DemoSpreadsheet123',sheetId='DemoSheet',label='Demo',sheetName='Execution')
    try:
        preview=execute(s,ReceiptBody(**{**body,'action':'preview'}),user=u,target=target,gateway=gateway())
        result=execute(s,ReceiptBody(**{**body,'fingerprint':preview['fingerprint'],'fillColor':color}),user=u,target=target,gateway=gateway())
        assert result['state']=='complete'
        return len(transport.calls)
    finally:factory.close()


def test_legacy_single_row_baseline_has_31_provider_calls(ctx):
    assert perform(ctx,False)==31


def test_optimized_single_row_flow_has_18_provider_calls(ctx):
    assert perform(ctx,True)==18


def test_token_cache_is_tenant_and_credential_scoped():
    tokens=[]
    def provider(request):
        tokens.append(request)
        return httpx.Response(200,json={'code':0,'tenant_access_token':'synthetic-token','expire':7200})
    pool=ReceiptGatewayFactory(httpx.MockTransport(provider))
    try:
        for tenant,secret in [('tenant-a','secret-a'),('tenant-a','secret-a'),('tenant-b','secret-a'),('tenant-a','secret-b')]:
            pool.create(tenant,'demo',secret)._tenant_token()
        assert len(tokens)==3
        assert len(pool.tokens)==3
    finally:pool.close()


def test_formatted_identity_and_raw_money_match_legacy_fingerprint(ctx):
    from xynigo_auth.purchase_receipt import locate,key_for
    s,u,g,run,b=ctx
    g.rows=g.rows[:1]
    g.rows[0][1]=123;g.rows[0][3]=7;g.rows[0][5]=1;g.rows[0][6]=1;g.rows[0][8]=1118.23
    base=SheetsTransport(g)
    def provider(request):
        response=base(request)
        if '/values/' in request.url.path and request.url.params.get('valueRenderOption')=='ToString':
            payload=response.json();values=payload['data']['valueRange']['values']
            values[1][1]='000123';values[1][3]='000007';values[1][5]='1.00';values[1][6]='1.00';values[1][8]='$MXN1,118.23'
            return httpx.Response(200,json=payload)
        return response
    transport=httpx.MockTransport(provider)
    pool=ReceiptGatewayFactory(transport)
    old=FeishuSheetsGateway(app_id='demo',app_secret='synthetic',transport=transport)
    new=pool.create(u.tenant_id,'demo','synthetic')
    target=dict(spreadsheetToken='DemoSpreadsheet123',sheetId='DemoSheet')
    try:
        old_table=old.read_table('https://feishu.cn/sheets/DemoSpreadsheet123','DemoSheet')
        new_table=new.read_table('https://feishu.cn/sheets/DemoSpreadsheet123','DemoSheet')
        old_key=key_for(dict(zip(old_table.headers,old_table.rows[0][1])))
        assert old_key==key_for(dict(zip(new_table.headers,new_table.rows[0][1])))
        _,old_rows,old_fp=locate(old,target,old_key,u,False)
        _,new_rows,new_fp=locate(new,target,old_key,u,False)
        assert old_fp==new_fp
        assert old_rows[0][1]['实际付款']==new_rows[0][1]['实际付款']==1118.23
    finally:pool.close()


def test_multiple_goods_use_same_batched_request_budget(ctx):
    assert perform(ctx,True,single=False)==18


def test_coloring_adds_only_fresh_verification_and_one_style_request(ctx):
    assert perform(ctx,True,color='#E2F0D9')==22
