"""Observe the platform's own submit request. Never issue or replay a write."""
import json


INSTALL_OBSERVER = r'''(() => {
  const target = __TARGET__;
  window.__xynigoRefundAttempt?.restore?.();
  const state = {response:null, count:0, closed:false};
  const fetchOriginal = window.fetch, openOriginal = XMLHttpRequest.prototype.open;
  const sendOriginal = XMLHttpRequest.prototype.send, requests = new WeakMap();
  const endpoint = (method, url) => {
    try { const u=new URL(url, location.href);
      return String(method || 'GET').toUpperCase()==='POST' && u.origin===location.origin
        && u.pathname==='/bff-api/trade-api/refund_only/create';
    } catch { return false; }
  };
  const record = (body, response, status) => {
    if (state.closed) return;
    try {
      const request = typeof body==='string' ? JSON.parse(body) : body;
      const packages=request?.package_info_list;
      if (!Array.isArray(packages) || packages.length!==1) return;
      const pkg=packages[0], orders=pkg.order_info_list;
      if (String(pkg.package_no)!==target.packageNo || !Array.isArray(orders)
          || !orders.some(o=>o.billno===target.orderNo && Number(o.path)===3 && Number(o.reason)===83)) return;
      state.count++;
      const matches=(response?.info?.package_info_list || [])
        .filter(p=>String(p.package_no)===target.packageNo)
        .flatMap(p=>p.success_order_info_list || [])
        .filter(o=>o.billno===target.orderNo);
      const failed=(response?.info?.package_info_list || [])
        .filter(p=>String(p.package_no)===target.packageNo)
        .some(p=>(p.fail_order_info_list || []).some(o=>o.billno===target.orderNo));
      const bills=[...new Set(matches.map(o=>typeof o.refund_bill_id==='string' ? o.refund_bill_id :
        Number.isSafeInteger(o.refund_bill_id) ? String(o.refund_bill_id) : ''))]
        .filter(id=>/^[1-9][0-9]{0,31}$/.test(id));
      state.response={orderNo:target.orderNo, packageNo:target.packageNo,
        accepted:state.count===1 && status>=200 && status<300 && String(response?.code)==='0'
          && !failed && matches.length===1 && bills.length===1,
        refundBillId:state.count===1 && bills.length===1 ? bills[0] : '',
        httpStatus:status, code:String(response?.code ?? '').slice(0,32)};
    } catch { /* Invalid or unknown response is not evidence of acceptance. */ }
  };
  const wrappedFetch=function(input, options) {
    const url=typeof input==='string' ? input : input?.url;
    const method=options?.method || input?.method || 'GET';
    let body=null;
    if (endpoint(method,url)) {
      try { body=Promise.resolve(options?.body ?? (input?.clone ? input.clone().text() : null)); }
      catch { body=null; }
    }
    const result=fetchOriginal.apply(this,arguments);
    if (body) result.then(response=>Promise.all([body,response.clone().json()])
      .then(([request,data])=>record(request,data,response.status)).catch(()=>{}),()=>{});
    return result;
  };
  const wrappedOpen=function(method,url) {
    requests.set(this,{method,url}); return openOriginal.apply(this,arguments);
  };
  const wrappedSend=function(body) {
    const request=requests.get(this);
    if (request && endpoint(request.method,request.url)) this.addEventListener('load',()=>{
      try { record(body,this.responseType==='json'?this.response:JSON.parse(this.responseText),this.status); }
      catch { /* Preserve the platform's own response handling. */ }
    },{once:true});
    return sendOriginal.apply(this,arguments);
  };
  state.restore=()=>{
    state.closed=true;
    if(window.fetch===wrappedFetch)window.fetch=fetchOriginal;
    if(XMLHttpRequest.prototype.open===wrappedOpen)XMLHttpRequest.prototype.open=openOriginal;
    if(XMLHttpRequest.prototype.send===wrappedSend)XMLHttpRequest.prototype.send=sendOriginal;
  };
  window.fetch=wrappedFetch;
  XMLHttpRequest.prototype.open=wrappedOpen;
  XMLHttpRequest.prototype.send=wrappedSend;
  window.__xynigoRefundAttempt=state;
  return true;
})()'''

READ_OBSERVER = 'window.__xynigoRefundAttempt?.response || null'
REMOVE_OBSERVER = 'window.__xynigoRefundAttempt?.restore?.()'


def install_observer(page, order_no, package_no):
    target = json.dumps({'orderNo': order_no, 'packageNo': package_no})
    return page.js_evaluate(INSTALL_OBSERVER.replace('__TARGET__', target)) is True


def read_observer(page, order_no, package_no):
    value = page.js_evaluate(READ_OBSERVER)
    if not isinstance(value, dict) or value.get('accepted') is not True:
        return None
    if value.get('orderNo') != order_no or value.get('packageNo') != package_no:
        return None
    bill = value.get('refundBillId')
    if not isinstance(bill, str) or not bill.isascii() or not bill.isdecimal() or not 1 <= len(bill) <= 32:
        return None
    return {'refundBillId': bill, 'packageNo': package_no, 'source': 'submit_response'}
