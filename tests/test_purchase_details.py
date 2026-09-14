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
    def request(payload):
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
    state=SimpleNamespace(auth=SimpleNamespace(purchase_receipt_request=lambda body:calls.append(body)))
    service.entries['test']={'member':'buyer','time':time.monotonic(),'payload':{'action':'submit'},
        'sent':False,'reason':'','identifier':'demo','url':original['url'],'marker':MARK,
        'order':validate_order(original,original['url'],MARK)}
    with pytest.raises(PurchaseAssistantError,match='变化'):
        service.handle(state,'buyer',{'captureId':'test','action':'submit','confirmed':True})
    assert calls==[]
