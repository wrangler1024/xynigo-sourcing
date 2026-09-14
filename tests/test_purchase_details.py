import copy
import pytest
from purchase_tool.purchase_details import validate_order
from purchase_tool.purchase_assistant import PurchaseAssistantError

URL='https://www.shein.com.mx/user/orders/detail/DEMO-ORDER-01'
MARK='a'*32

def sample():
    return dict(url=URL,marker=MARK,orderNo='DEMO-ORDER-01',amount='118.23',currency='MXN',
        isPaid='1',paidAt='1789000000000',status='10',items=[{'sku':'demo','quantity':'1'}],
        totalText='Subtotal:\n$MXN331.31\nTotal\n$MXN118.23',clip=dict(x=200,y=190,width=895,height=1500))

def test_extracts_paid_total_only():
    assert validate_order(sample(),URL,MARK)['amount']=='118.23'

@pytest.mark.parametrize('field,value', [('isPaid','0'),('amount','331.31'),('currency','USD'),('marker','different'),('orderNo',''),('amount','NaN')])
def test_rejects_mismatched_context(field,value):
    data=sample();data[field]=value
    with pytest.raises(PurchaseAssistantError):validate_order(data,URL,MARK)

def test_clipped_or_extreme_screenshots_rejected():
    data=sample();data['clip']['height']=20000
    with pytest.raises(PurchaseAssistantError):validate_order(data,URL,MARK)


def test_submission_timeout_only_reconciles_and_never_replays():
    import time
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    service=PurchaseDetailsService()
    class Page:
        _ws=SimpleNamespace(close=lambda:None)
        def _evaluate(self, script):return sample()
    service._page=lambda *a:Page()
    calls=[]
    def request(payload, **kwargs):
        calls.append(payload)
        if payload['action']=='submit':raise OSError('response lost')
        return {'ok':True,'state':'uncertain'}
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=request))
    service.entries['test']={'member':'buyer','time':time.monotonic(),
        'payload':{'action':'submit','image':'demo','requestId':'fixed'},'sent':False,'reason':'',
        'identifier':'demo','url':URL,'marker':MARK,'order':validate_order(sample(),URL,MARK)}
    body={'captureId':'test','action':'submit','confirmed':True}
    with pytest.raises(OSError):service.handle(state,'buyer',body)
    assert service.handle(state,'buyer',body)['state']=='uncertain'
    assert [c['action'] for c in calls]==['submit','status']
    with pytest.raises(PurchaseAssistantError):service.handle(state,'different-buyer',body)


def test_capture_environment_resolution_never_scans_closed_inventory():
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    calls=[]
    hub=SimpleNamespace(browser_status=lambda:[{'containerCode':'open-1','status':0},{'containerCode':'closed-2','status':3}],
        browser_lifecycle_state=lambda s:'open' if s['status']==0 else 'closed',
        env_lookup=lambda **kw:(calls.append(kw) or {'containerCode':'open-1','serialNumber':'123'}))
    with pytest.raises(PurchaseAssistantError):
        PurchaseDetailsService()._page(SimpleNamespace(hub=hub),'999',URL,MARK)
    assert calls==[{'container_code':'open-1'}]


def test_internal_route_id_can_differ_from_purchase_order_number():
    data=sample()
    data['url']='https://www.shein.com.mx/user/orders/detail/USH-DEMO-INTERNAL-123'
    data['orderNo']='GSH-DEMO-PURCHASE-456'
    assert validate_order(data,data['url'],MARK)['orderNo']=='GSH-DEMO-PURCHASE-456'


@pytest.mark.parametrize('url',[
    'https://www.shein.com/user/orders/detail/USH-DEMO-INTERNAL-123',
    'https://www.shein.com.mx.example.test/user/orders/detail/USH-DEMO-INTERNAL-123',
    'https://www.shein.com.mx/user/orders/list',
])
def test_internal_id_support_does_not_widen_site_boundary(url):
    data=sample();data['url']=url
    with pytest.raises(PurchaseAssistantError):validate_order(data,url,MARK)


def test_changed_purchase_order_on_same_url_blocks_submit():
    import time
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    service=PurchaseDetailsService()
    original=sample()
    original['url']='https://www.shein.com.mx/user/orders/detail/USH-DEMO-INTERNAL-123'
    original['orderNo']='GSH-DEMO-PURCHASE-456'
    changed={**original,'orderNo':'GSH-DEMO-PURCHASE-789'}
    page=SimpleNamespace(_ws=SimpleNamespace(close=lambda:None),_evaluate=lambda script:changed)
    service._page=lambda *args:page
    calls=[]
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=lambda body, **kwargs:calls.append(body)))
    service.entries['test']={'member':'buyer','time':time.monotonic(),'payload':{'action':'submit'},
        'sent':False,'reason':'','identifier':'demo','url':original['url'],'marker':MARK,
        'order':validate_order(original,original['url'],MARK)}
    with pytest.raises(PurchaseAssistantError,match='变化'):
        service.handle(state,'buyer',{'captureId':'test','action':'submit','confirmed':True})
    assert calls==[]


def test_async_submit_returns_processing_and_never_replays():
    import threading,time
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    service=PurchaseDetailsService();started=threading.Event();release=threading.Event();calls=[]
    page=SimpleNamespace(_ws=SimpleNamespace(close=lambda:None),_evaluate=lambda script:sample())
    service._page=lambda *args:page
    def request(payload, **kwargs):
        assert kwargs['expected_member']=='buyer'
        calls.append(dict(payload));started.set();assert release.wait(2)
        return {'ok':True,'state':'complete','color':{'state':'complete','selected':'#E2F0D9'}}
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=request))
    service.entries['capture']={'member':'buyer','time':time.monotonic(),
        'payload':{'action':'submit','image':'demo','requestId':'fixed'},'sent':False,'reason':'',
        'identifier':'demo','url':URL,'marker':MARK,'order':validate_order(sample(),URL,MARK),
        'features':{'fillColorV1':True}}
    body={'action':'submit','captureId':'capture','confirmed':True,'async':True,'fillColor':'#E2F0D9'}
    try:
        assert service.handle(state,'buyer',body)['state']=='processing'
        assert started.wait(1)
        assert service.handle(state,'buyer',body)['state']=='processing'
        assert service.handle(state,'buyer',{'action':'status','captureId':'capture'})['state']=='processing'
        with pytest.raises(PurchaseAssistantError):
            service.handle(state,'another-buyer',body)
        assert len(calls)==1 and calls[0]['fillColor']=='#E2F0D9'
    finally:release.set()
    assert service.entries['capture']['job']['done'].wait(2)
    assert service.handle(state,'buyer',{'action':'status','captureId':'capture'})['state']=='complete'
    assert len(calls)==1


def test_capture_failure_always_restores_panel():
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService,HIDE_PANEL,RESTORE_PANEL,READ_IDENTITY
    commands=[]
    class Page:
        _ws=SimpleNamespace(close=lambda:None)
        def _evaluate(self,script):return sample()
        def _send(self,method,params):
            commands.append((method,params))
            if method=='Page.captureScreenshot':raise OSError('capture failed')
            return {'result':{'value':False}}
    service=PurchaseDetailsService();service._page=lambda *args:Page()
    state=SimpleNamespace(purchase_assistant_for_member=lambda m:(None,{'scope':'team','dataSourceId':'demo'}),
        auth=SimpleNamespace(purchase_receipt_request=lambda p,**kw:{'expectedRevision':0,'fingerprint':'demo'}))
    with pytest.raises(OSError):
        service.handle(state,'buyer',{'action':'read','taskKey':'PT1-'+'a'*64,'identifier':'demo','pageUrl':URL,'marker':MARK})
    assert '__CAPTURE_TOKEN__' not in commands[0][1]['expression']
    assert 'state.token' in commands[-1][1]['expression']
    assert commands[0][1]['awaitPromise'] is True
    assert 'const includeClip=false;' in READ_IDENTITY


def test_revision_color_retry_uses_new_request_not_previous_receipt():
    import time
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    service=PurchaseDetailsService();calls=[]
    service._page=lambda *args:SimpleNamespace(_ws=SimpleNamespace(close=lambda:None),_evaluate=lambda script:sample())
    def request(body, **kw):
        calls.append(dict(body))
        return {'ok':True,'state':'complete','requestId':body['requestId'],
                'color':{'state':'failed' if body['action']=='submit' else 'complete'}}
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=request))
    service.entries['capture']={'member':'buyer','time':time.monotonic(),
        'payload':{'requestId':'new-revision','image':'demo'},'sent':False,'reason':'',
        'identifier':'demo','url':URL,'marker':MARK,'order':validate_order(sample(),URL,MARK),
        'retryColorRequestId':'previous-revision','features':{'fillColorV1':True}}
    service.handle(state,'buyer',{'action':'submit','captureId':'capture','confirmed':True,'fillColor':'#FFF2CC','reason':'fix'})
    service.handle(state,'buyer',{'action':'retry-color','captureId':'capture'})
    assert [c['requestId'] for c in calls]==['new-revision','new-revision']
    assert calls[1]['image']==''


def test_restored_color_retry_timeout_queries_original_request_id():
    import time
    from types import SimpleNamespace
    from purchase_tool.purchase_details import PurchaseDetailsService
    service=PurchaseDetailsService();calls=[]
    def request(body, **kw):
        calls.append(dict(body))
        if body['action']=='retry-color':raise OSError('response lost')
        return {'ok':True,'state':'complete','requestId':body['requestId']}
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=request))
    service.entries['capture']={'member':'buyer','time':time.monotonic(),
        'payload':{'requestId':'new-preview','image':'demo'},'sent':False,'reason':'',
        'retryColorRequestId':'original-receipt'}
    with pytest.raises(OSError):
        service.handle(state,'buyer',{'action':'retry-color','captureId':'capture'})
    service.handle(state,'buyer',{'action':'status','captureId':'capture'})
    assert [(c['action'],c['requestId']) for c in calls]==[
        ('retry-color','original-receipt'),('status','original-receipt')]


def test_auth_member_snapshot_rejects_identity_switch_before_network():
    import threading
    from types import SimpleNamespace
    from purchase_tool.cloud_auth import LocalAuthService,LocalAuthError
    auth=object.__new__(LocalAuthService);auth.lock=threading.RLock();auth.session_token='synthetic-token'
    auth.require=lambda permission:{'user':{'id':'current-user'}}
    calls=[];auth.client=SimpleNamespace(purchase_receipt_request=lambda token,body:calls.append(token))
    with pytest.raises(LocalAuthError):auth.purchase_receipt_request({},expected_member='old-user')
    assert calls==[]
    auth.purchase_receipt_request({},expected_member='current-user')
    assert calls==['synthetic-token']
