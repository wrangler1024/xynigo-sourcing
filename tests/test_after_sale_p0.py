"""P0 regression: immutable product facts and receipt-based completion, no platform writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from purchase_tool import after_sale_claim as module
from purchase_tool.after_sale_receipts import _DETAIL_FACTS, read_current_receipt
from purchase_tool.after_sale_submit_evidence import INSTALL_OBSERVER, read_observer
from purchase_tool.operation_executor import LocalOperationExecutor
from test_operation_executor import FakeRpc


def test_observer_binds_platform_success_to_exact_order_and_package_for_fetch_and_xhr():
    observer = INSTALL_OBSERVER.replace('__TARGET__', json.dumps({'orderNo': 'SYNTH', 'packageNo': 'PKG'}))
    program = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
for(const transport of ['fetch','xhr'])for(const variant of ['ok','wrong_order','wrong_package','business_error','partial_failure','duplicate']) {
 const requests=[];
 class XHR {constructor(){this.events={};this.status=200;this.responseType='json';}
   open(method,url){this.method=method;this.url=url;}
   addEventListener(name,fn){this.events[name]=fn;}
   send(body){requests.push(body);this.response=response;this.events.load?.();}
 }
 const originalFetch=()=>{requests.push('fetch');return Promise.resolve({status:200,clone(){return {json:async()=>response}}})};
 const ctx=vm.createContext({window:{fetch:originalFetch},XMLHttpRequest:XHR,URL,location:{href:'https://www.shein.com.mx/orders/refundApplication',origin:'https://www.shein.com.mx'}});
 const body={package_info_list:[{package_no:'PKG',order_info_list:[{billno:'SYNTH',path:3,reason:83}]}]};
 const response={code:variant==='business_error'?7:0,info:{package_info_list:[{package_no:variant==='wrong_package'?'OTHER':'PKG',
   success_order_info_list:variant==='partial_failure'?[]:[{billno:variant==='wrong_order'?'OTHER':'SYNTH',refund_bill_id:'12345'}],
   fail_order_info_list:variant==='partial_failure'?[{billno:'SYNTH'}]:[]}]}};
 vm.runInContext(OBSERVER,ctx);
 assert.equal(requests.length,0,'install must never create a network request');
 const send=async()=>{
   if(transport==='fetch')await ctx.window.fetch('/bff-api/trade-api/refund_only/create',{method:'POST',body:JSON.stringify(body)});
   else {const x=new XHR();x.open('POST','/bff-api/trade-api/refund_only/create');x.send(JSON.stringify(body));}
   await new Promise(resolve=>setImmediate(resolve));
 };
 await send();if(variant==='duplicate')await send();
 assert.equal(ctx.window.__xynigoRefundAttempt.response.accepted,variant==='ok',transport+'/'+variant);
 ctx.window.__xynigoRefundAttempt.restore();assert.equal(ctx.window.fetch,originalFetch);
}
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('OBSERVER', json.dumps(observer))
    result = subprocess.run(['node', '-e', program], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_late_response_and_repeated_cleanup_cannot_pollute_next_attempt():
    program=r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
(async()=>{
 for(const transport of ['fetch','xhr']) {
  const pending=[];
  class XHR {open(){} addEventListener(_,fn){this.load=fn;}send(){pending.push(this);} }
  const fetchOriginal=()=>new Promise(resolve=>pending.push(resolve));
  const ctx=vm.createContext({window:{fetch:fetchOriginal},XMLHttpRequest:XHR,URL,
    location:{href:'https://www.shein.com.mx/orders/refundApplication',origin:'https://www.shein.com.mx'}});
  const install=pkg=>vm.runInContext(INSTALL.replace('__TARGET__',JSON.stringify({orderNo:'SYNTH',packageNo:pkg})),ctx);
  const send=pkg=>{const body=JSON.stringify({package_info_list:[{package_no:pkg,order_info_list:[{billno:'SYNTH',path:3,reason:83}]}]});
   if(transport==='fetch')ctx.window.fetch('/bff-api/trade-api/refund_only/create',{method:'POST',body});
   else {const xhr=new XHR();xhr.open('POST','/bff-api/trade-api/refund_only/create');xhr.send(body);}}
  const finish=(index,pkg)=>{const response={code:0,info:{package_info_list:[{package_no:pkg,success_order_info_list:[{billno:'SYNTH',refund_bill_id:'12345'}]}]}};
   if(transport==='fetch')pending[index]({status:200,clone:()=>({json:async()=>response})});
   else Object.assign(pending[index],{status:200,responseType:'json',response}).load();};
  install('P1');const old=ctx.window.__xynigoRefundAttempt;send('P1');install('P2');send('P2');
  old.restore();finish(0,'P1');await new Promise(r=>setImmediate(r));
  assert.equal(ctx.window.__xynigoRefundAttempt.response,null);
  finish(1,'P2');await new Promise(r=>setImmediate(r));assert.equal(ctx.window.__xynigoRefundAttempt.response.packageNo,'P2');
  assert.equal(ctx.window.__xynigoRefundAttempt.response.accepted,true);
  const external=()=>{};ctx.window.fetch=external;
  ctx.window.__xynigoRefundAttempt.restore();ctx.window.__xynigoRefundAttempt.restore();
  assert.equal(ctx.window.fetch,external);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('INSTALL',json.dumps(INSTALL_OBSERVER))
    result=subprocess.run(['node','-e',program],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr


def test_receipt_details_extract_masked_card_actual_paths_package_and_platform_time():
    setup = r'''
const assert=require('node:assert/strict');
const location={pathname:'/orders/refundLabel/SYNTH'};
const window={GB_OrderReturnLabel:{billno:'SYNTH',refund_page_info:{cur_refund_order:{
 refund_bill_id:'12345',add_time:'1757505600',reason:'83',
 refund_bill_goods_list:[{package_no:'PKG'}],
 refund_record_list:[{refund_path:'3',refund_amount_without_symbol:'10.00'},{refund_path:'2',refund_amount_without_symbol:'0.00'}]}}}};
const document={body:{innerText:'Código del Reembolso:12345\nPlazo de la solicitud:10 Sep 2025 10:00:00\nestá en revisión'},
 querySelector:s=>s==='.refundAccount-info .tip'?{innerText:'****1234'}:{innerText:'Error'}};
const facts=FACTS;
assert.equal(facts.refundAccount,'****1234');assert.equal(facts.refundPath,'Cuenta original de pago');
assert.deepEqual(facts.packageNos,['PKG']);assert.equal(facts.reasonId,'83');assert.match(facts.applicationAt,/Z$/);
window.GB_OrderReturnLabel.billno='OTHER';const wrong=FACTS;assert.equal(wrong.refundPath,'');assert.deepEqual(wrong.packageNos,[]);
'''.replace('FACTS', _DETAIL_FACTS)
    result = subprocess.run(['node', '-e', setup], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def claimer():
    c = module.AfterSaleClaimer(None)
    c._claim_rows = {'SYNTH': {'orderNo': 'SYNTH', 'environmentSerial': 'ENV', 'status': 'running'}}
    c._capture_screenshot = lambda *a: None
    return c


def test_accepted_response_without_toast_redirect_is_success_and_reads_details(monkeypatch):
    c = claimer(); c._click = lambda *a: True
    c._select_refund_path = lambda *a: (True, '')
    c._pre_info = lambda *a: {'eligible': []}
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(module, 'install_observer', lambda *a: True)
    monkeypatch.setattr(module, 'read_observer', lambda *a: {'refundBillId': '12345', 'packageNo': 'PKG', 'source': 'submit_response'})
    monkeypatch.setattr(module, 'read_current_receipt', lambda *a: {'refundBillId': '12345', 'refundAccount': '****1234',
        'refundPath': 'Cuenta original de pago', 'phase': 'reviewing', 'phaseLabel': '审核中'})
    page = Mock(); page.url = 'https://www.shein.com.mx/orders/refundApplication?billno=SYNTH'
    page.wait_for.return_value = True
    page.js_evaluate.side_effect = lambda js: [{'x': 1, 'y': 1}] if js == module._JS_PICK_POINTS else ['PKG'] if js == module._JS_CHECKED_PACKAGES else ''
    result = c._submit_package(page, 'SYNTH')
    assert result['ok'] and result['source'] == 'submit_response'
    assert result['refundAccount'] == '****1234' and result['phase'] == 'reviewing'
    page.click_selector.assert_called_once()
    assert '/orders/refundLabel/SYNTH?refund_bill_id=12345' in page.goto.call_args.args[0]


@pytest.mark.parametrize('case', ['current', 'old', 'other_package', 'partial', 'rejected', 'incomplete', 'still_eligible'])
def test_recovery_checks_target_coverage_and_keeps_original_error(monkeypatch, case):
    c = claimer(); now = datetime.now(timezone.utc)
    c._claim_rows['SYNTH'].update(_attemptPackage='PKG',_targetPackages=['PKG','P2'] if case=='partial' else ['PKG'],
        _attemptStartedAt=(now-timedelta(seconds=10)).isoformat(), _writeAttempted=True)
    record = {'refundBillId':'12345','packageNos':['OTHER' if case=='other_package' else 'PKG'],
        'applicationAt':(now-timedelta(days=1) if case=='old' else now-timedelta(seconds=5)).isoformat(),
        'reasonId':'83','phase':'rejected' if case=='rejected' else 'reviewing','phaseLabel':'审核中',
        'refundPath':'Cuenta original de pago','refundAccount':'****1234'}
    def read(*a,**kw):
        kw['on_record'](record)
        if case=='incomplete': raise RuntimeError('incomplete receipt list')
        return [record]
    monkeypatch.setattr(module,'read_refund_receipts',read)
    c._pre_info=lambda *a: {'eligible':[{'packageNo':'PKG'}] if case=='still_eligible' else []}
    c._uncertain_claim(Mock(),'SYNTH','synthetic missing redirect')
    row=c._claim_rows['SYNTH']
    assert row['status']==('ok' if case=='current' else 'uncertain')
    assert row['submissionError']=='synthetic missing redirect'
    assert row['refundAccount']=='****1234'
    assert row['refunds'][0]['source']==('recovered_verified' if case in ('current','partial') else 'recovered')
    if case=='incomplete': assert row['recoveryError']=='incomplete receipt list'


def test_product_snapshot_survives_local_queue_but_is_not_repeated_in_progress():
    item={'environmentSerial':'ENV','orderNo':'SYNTH','goodsImg':'a','goodsImages':['a','b'],
          'itemCount':2,'goodsItems':[{'goodsImg':'a','quantity':1},{'goodsImg':'b','quantity':1}]}
    c=claimer();c._start=Mock(return_value={})
    c.start_submit([item]);clean=c._start.call_args.args[1][0]
    assert clean['goodsImages']==['a','b'] and clean['itemCount']==2
    rpc=FakeRpc('/api/after-sale/progress',[{'running':False,'claimRows':[{**item,'status':'ok','operationCompletedAt':'2026-09-01T01:02:03+00:00'}]}])
    reports=[];executor=LocalOperationExecutor(rpc,sleep_fn=lambda _:None)
    _,_,summary=executor.execute('after.sale.claim.v1',{'items':[item]},lambda **event:reports.append(event))
    assert summary['rows'][0]['operationCompletedAt']=='2026-09-01T01:02:03+00:00'
    for row in [summary['rows'][0],reports[0]['snapshot']['rows'][0]]:
        assert not {'goodsImages','goodsItems','itemCount'}.intersection(row)


@pytest.mark.parametrize('status',['ok','uncertain','blocked','login','fail','stopped'])
def test_operation_completion_is_recorded_for_every_finished_attempt(status):
    c=claimer();c._open_env=lambda *a:(Mock(),False)
    def run(*args):
        c._claim_rows['SYNTH'].update(status=status,_startedAt=module.time.time()-1)
    c._claim_one=run
    c._claim_env('ENV',{},[{'orderNo':'SYNTH'}],False)
    completed=c._claim_rows['SYNTH']['operationCompletedAt']
    assert datetime.fromisoformat(completed).tzinfo
    c._claim_env('ENV',{},[{'orderNo':'SYNTH'}],False)
    assert c._claim_rows['SYNTH']['operationCompletedAt']==completed


def test_large_product_batch_has_small_receipt_frames():
    rows=[{'orderNo':'SYNTH'+str(i),'environmentSerial':'ENV','status':'ok',
           'goodsImages':['https://img.ltwebstatic.com/'+('a'*250)]*100,
           'goodsItems':[{'name':'合成商品'*30,'specification':'合成规格'*30,'quantity':1}]*100}
          for i in range(500)]
    projected=LocalOperationExecutor._after_sale_rows(rows,claim=True)
    assert len(json.dumps({'rows':projected},ensure_ascii=False).encode())<100_000


def test_single_package_accepted_response_survives_later_read_failure():
    c=claimer();c._login_required=lambda _:False
    c._pre_info=lambda *a:{'eligible':[{'packageNo':'PKG'}]}
    c._submit_package=lambda *a:{'ok':True,'packageNo':'PKG','refundBillId':'12345',
        'source':'submit_response','verificationError':'synthetic later read failure'}
    page=Mock();page.wait_for.return_value=True;page.js_evaluate.return_value=''
    c._claim_one(page,'ENV',{'orderNo':'SYNTH'})
    row=c._claim_rows['SYNTH']
    assert row['status']=='ok' and row['refunds'][0]['source']=='submit_response'
    assert row['recoveryError']=='synthetic later read failure'
    assert '正在核对' not in row['note']


def test_success_completion_removes_inflight_verification_message():
    c=claimer();c._login_required=lambda _:False
    c._pre_info=lambda *a:{'eligible':[{'packageNo':'PKG'}]}
    def submit(*a):
        c._claim_rows['SYNTH']['note']='提交已开始，正在核对平台退款凭证；请勿重复提交'
        return {'ok':True,'packageNo':'PKG','refundBillId':'12345','source':'submit_response','remaining':[]}
    c._submit_package=submit
    page=Mock();page.wait_for.return_value=True;page.js_evaluate.return_value=''
    c._claim_one(page,'ENV',{'orderNo':'SYNTH'})
    assert c._claim_rows['SYNTH']['status']=='ok'
    assert '正在核对' not in c._claim_rows['SYNTH']['note']


def test_missing_second_target_never_becomes_success_when_eligibility_disappears(monkeypatch):
    c=claimer();c._login_required=lambda _:False
    c._pre_info=lambda *a:{'eligible':[{'packageNo':'P1'},{'packageNo':'P2'}]}
    c._submit_package=lambda *a:{'ok':True,'packageNo':'P1','refundBillId':'12345','source':'submit_response','remaining':[]}
    monkeypatch.setattr(module,'read_refund_receipts',lambda *a,**kw:[])
    page=Mock();page.wait_for.return_value=True;page.js_evaluate.return_value=''
    c._claim_one(page,'ENV',{'orderNo':'SYNTH'})
    row=c._claim_rows['SYNTH']
    assert row['status']=='uncertain'
    assert '目标包裹' in row['submissionError']


def test_url_only_receipt_for_other_package_is_never_accepted(monkeypatch):
    c=claimer();c._click=lambda *a:True;c._select_refund_path=lambda *a:(True,'')
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    monkeypatch.setattr(module,'install_observer',lambda *a:True)
    monkeypatch.setattr(module,'read_observer',lambda *a:None)
    monkeypatch.setattr(module,'read_current_receipt',lambda *a:{'refundBillId':'12345',
        'packageNos':['OTHER'],'reasonId':'83','phase':'reviewing','applicationAt':datetime.now(timezone.utc).isoformat()})
    page=Mock();page.wait_for.return_value=True
    page.url='https://www.shein.com.mx/orders/refundLabel/SYNTH?refund_bill_id=12345'
    page.js_evaluate.side_effect=lambda js:[{'x':1,'y':1}] if js==module._JS_PICK_POINTS else ['PKG'] if js==module._JS_CHECKED_PACKAGES else ''
    result=c._submit_package(page,'SYNTH')
    assert result['uncertain'] and not result['ok']
    assert not c._claim_rows['SYNTH'].get('refunds')


def test_missing_detail_warning_clears_after_fields_are_read():
    c=claimer()
    c._record_refund('SYNTH',{'refundBillId':'12345','source':'recovered'})
    assert '退款账户未读取' in c._claim_rows['SYNTH']['refunds'][0]['detailsNote']
    c._record_refund('SYNTH',{'refundBillId':'12345','source':'recovered',
        'refundAccount':'****1234','refundPath':'Cuenta original de pago','detailsNote':''})
    assert c._claim_rows['SYNTH']['refunds'][0]['detailsNote']==''


def test_web_product_rows_keep_all_images_quantities_and_action_time():
    html=Path('src/purchase_tool/web/index.html').read_text(encoding='utf-8')
    names=['asOrderIdentity','asSubmissionForRow','asSubmissionProtections','asSubmissionHasEvidence','asOrderRefundRows','asSubmissionBlocksSelection','asCanSelectScanRow',
           'asItemCount','asGoodsImages','asScanGoodsHtml','asSelectedItems','asClaimRowHtml',
           'asClaimNeedsReconciliation','asClaimPill','asClaimReasonHtml','asRecoverableClaimRows']
    functions=[]
    for name in names:
        a=html.index('function '+name+'(');functions.append(html[a:html.index('\n}',a)+2])
    script=r'''
const assert=require('node:assert/strict');
const esc=s=>String(s).replaceAll('<','&lt;'),safeProcurementImageUrl=s=>s||'',AFTER_SALE_TYPE='丢件退款';
const AS_CLAIM_PILL={queued:['warn','等待'],ok:['ok','已受理']},AS_RECOVERABLE_CLAIM_STATUS=['fail','stopped'];
const row={orderNo:'SYNTH',environmentSerial:'ENV',status:'ok',claimable:true,
 itemCount:2,goodsImages:['a','b'],goodsImg:'b',goodsItems:[{name:'A',quantity:2}]};
const AS_STATE={rows:[row],selected:new Set(['SYNTH'])},asFilteredRows=()=>AS_STATE.rows;
FUNCTIONS
const selected=asSelectedItems()[0];assert.deepEqual(selected.goodsImages,['a','b']);assert.equal(selected.itemCount,2);
let rendered=asClaimRowHtml({...selected,status:'queued'});
assert.equal((rendered.match(/<img /g)||[]).length,2);assert.match(rendered,/共 2 件/);
const retry=asRecoverableClaimRows([{...selected,status:'fail'}])[0];assert.deepEqual(retry.goodsItems,row.goodsItems);
rendered=asClaimRowHtml({...selected,status:'ok',operationCompletedAt:'2026-09-01T00:00:20Z',
 refunds:[{refundBillId:'12345',source:'submit_response',phaseLabel:'已退款'}]});
assert.match(rendered,/已退款/);assert.match(rendered,/2026/);assert.match(rendered,/查看图片/);
assert.match(asClaimReasonHtml({status:'uncertain',submissionError:'lost response',recoveryError:'read failed'}),/执行诊断/);
'''.replace('FUNCTIONS','\n'.join(functions))
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
