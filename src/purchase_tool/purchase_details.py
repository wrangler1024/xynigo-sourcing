"""Read the caller's exact SHEIN tab and retain short-lived evidence in memory."""
import re
import secrets
import threading
import time
import uuid
import weakref
from decimal import Decimal, InvalidOperation

from .cdp import CdpClient, PageTarget
from .purchase_assistant import PurchaseAssistantError

# Executed only in the tab carrying the extension's random per-document marker.
READ_ORDER = r'''(() => {
 const includeClip=true;
 const o=window.gbRawData?.orderInfo;
 const h=document.querySelector('h1.sui-title__text');
 const total=document.querySelector('.order-total');
 if(!o||!h||!total) return null;
 let clip;
 if(includeClip){
 const a=h.getBoundingClientRect(),z=total.getBoundingClientRect();
 // Heading/total boxes can be narrower than their overflowing table/text.
 let region=h;
 while(region.parentElement&&!region.contains(total)) region=region.parentElement;
 const boxes=[a,z];
 for(const e of [region,...region.querySelectorAll('*')]){
   if(e.closest('#xynigo-purchase-assistant-host')||!e.getClientRects().length) continue;
   const r=e.getBoundingClientRect();
   if(r.bottom<a.top||r.top>z.bottom) continue;
   boxes.push(r);
   for(const node of e.childNodes){
     if(node.nodeType!==Node.TEXT_NODE||!node.textContent.trim()) continue;
     const range=document.createRange();range.selectNodeContents(node);
     boxes.push(...range.getClientRects());
   }
 }
 const left=Math.max(0,Math.floor(Math.min(...boxes.map(r=>r.left))+scrollX)-8);
 const top=Math.max(0,Math.floor(a.top+scrollY)-8);
 const right=Math.ceil(Math.max(...boxes.map(r=>r.right))+scrollX)+8;
 const bottom=Math.ceil(z.bottom+scrollY)+16;
 clip={x:left,y:top,width:right-left,height:bottom-top,scale:1};
 }
 const items=(o.orderGoodsList||[]).map(x=>({sku:String(x.display_goods_sn||x.sku_code||''),quantity:String(x.quantity||'')}));
 return {orderNo:String(o.billno||''),amount:String(o.totalPrice?.amount||''),
 currency:String(o.currency_code||''),isPaid:String(o.isPaid||''),paidAt:String(o.paymentTime||''),
 status:String(o.orderStatus||''),items,
 totalText:total.innerText,
 url:location.href,marker:document.documentElement.getAttribute('data-xynigo-receipt-tab'),
 clip};
})()'''


READ_IDENTITY = READ_ORDER.replace('const includeClip=true;', 'const includeClip=false;')
FILL_COLORS = ('', '#E2F0D9', '#DDEBF7', '#FFF2CC', '#FCE4D6', '#F4DCE6', '#E4DFEC', '#DDF2EF')

def validate_order(data, url, marker, *, check_clip=True):
    if not isinstance(data, dict) or data.get('url') != url or data.get('marker') != marker:
        raise PurchaseAssistantError('订单页面已变化，请重新读取')
    match = re.fullmatch(r'https://www\.shein\.com\.mx/user/orders/detail/([A-Za-z0-9-]{6,64})', url)
    if not match:
        raise PurchaseAssistantError('当前网址不是受支持的墨西哥站订单详情页')
    # The route identifies an internal order (e.g. USH...), whereas billno is
    # the displayed purchase order number (e.g. GSH...). They are not equal.
    if not re.fullmatch(r'[A-Za-z0-9-]{6,64}', str(data.get('orderNo') or '')):
        raise PurchaseAssistantError('未能读取有效采购订单号，请刷新详情页')
    if data.get('currency') != 'MXN':
        raise PurchaseAssistantError('当前订单币种不是 MXN，暂不支持回传')
    if data.get('isPaid') != '1':
        raise PurchaseAssistantError('当前订单未确认付款，不能回传采购凭证')
    try:
        amount = Decimal(data.get('amount', ''))
        if not amount.is_finite() or amount < 0 or amount > 10_000_000 or amount.as_tuple().exponent < -2:
            raise InvalidOperation()
    except (InvalidOperation, ValueError):
        raise PurchaseAssistantError('订单付款金额无效') from None
    # Match the rendered Total, not any discount/product number on the page.
    total = re.search(r'(?:^|\n)Total\s*\n?\s*\$MXN\s*([\d,]+\.\d{2})(?:\s|$)', data.get('totalText', ''))
    if not total or Decimal(total.group(1).replace(',', '')) != amount:
        raise PurchaseAssistantError('页面总额与订单数据不一致，请刷新详情页')
    clip = data.get('clip', {})
    if check_clip and (clip.get('width', 0) < 200 or clip.get('width', 0) > 3000 or
            clip.get('height', 0) < 100 or clip.get('height', 0) > 12000 or
            clip.get('x', -1) < 0 or clip.get('y', -1) < 0):
        raise PurchaseAssistantError('订单区域过长或布局异常，请人工核对截图')
    return {k: data[k] for k in ('orderNo', 'amount', 'currency', 'paidAt', 'status', 'items')}


# Animation only surrounds the actual capture; a watchdog restores the panel
# even if the debugging connection disappears. An expired capture is rejected.
HIDE_PANEL = r"""(async () => {
 const e=document.querySelector('#xynigo-purchase-assistant-host');
 if(!e)return null;
 const key='__xynigoReceiptPanel';
 if(window[key]) window[key].restore();
 const original={value:e.style.getPropertyValue('visibility'),priority:e.style.getPropertyPriority('visibility')};
 const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
 const state={expired:false,restored:false,original,token:"__CAPTURE_TOKEN__"};
 state.restore=()=>{
   clearTimeout(state.timer);
   if(state.revealAnimation)state.revealAnimation.cancel();
   if(window[key]!==state||state.restored)return;
   state.restored=true;
   if(state.animation)state.animation.cancel();
   if(original.value)e.style.setProperty('visibility',original.value,original.priority);
   else e.style.removeProperty('visibility');
 };
 window[key]=state;
 state.timer=setTimeout(()=>{state.expired=true;state.restore();},10000);
 if(!reduce&&e.animate){
   const animation=e.animate([{opacity:getComputedStyle(e).opacity},{opacity:0}],{duration:120,fill:'forwards',easing:'ease-out'});
   state.animation=animation;
   try {await animation.finished;} catch(_) {}
   if(state.expired||state.restored||window[key]!==state){animation.cancel();return null;}
   e.style.setProperty('visibility','hidden','important');animation.cancel();
 } else e.style.setProperty('visibility','hidden','important');
 return true;
})()"""

RESTORE_PANEL = r"""(() => {
 const state=window.__xynigoReceiptPanel;
 if(!state)return true;
 if(state.token!=="__CAPTURE_TOKEN__")return true;
 const invalid=state.expired||state.restored;
 state.restore();
 const e=document.querySelector('#xynigo-purchase-assistant-host');
 if(e&&!invalid&&!matchMedia('(prefers-reduced-motion: reduce)').matches&&e.animate)
   state.revealAnimation=e.animate([{opacity:0},{opacity:getComputedStyle(e).opacity}],{duration:140,easing:'ease-in'});
 return invalid;
})()"""


def evaluate_capture_animation(page, expression, token):
    expression = expression.replace("__CAPTURE_TOKEN__", token)
    result = page._send('Runtime.evaluate', {'expression': expression,
        'awaitPromise': True, 'returnByValue': True})
    if result.get('exceptionDetails'):
        raise PurchaseAssistantError('截图面板准备失败，请重新读取')
    return (result.get('result') or {}).get('value')


class PurchaseDetailsService:
    def __init__(self):
        self.entries = {}
        self.lock = threading.RLock()
        self.operation_locks = weakref.WeakValueDictionary()
        self.workers = threading.BoundedSemaphore(4)
        self.page_cache = {}

    def _page(self, state, identifier, url, marker):
        if not re.fullmatch(r'[A-Za-z0-9-]{20,100}', marker):
            raise PurchaseAssistantError('页面会话标识无效')
        if not re.fullmatch(r'https://www\.shein\.com\.mx/user/orders/detail/[A-Za-z0-9-]{6,64}', url):
            raise PurchaseAssistantError('首版仅支持墨西哥站订单详情')
        cache_key = (str(identifier), url, marker)
        with self.lock:
            cached = self.page_cache.get(cache_key)
        if cached and time.monotonic()-cached[0] < 60:
            try:
                return self._attach_page(cached[1], url, marker)
            except Exception:
                with self.lock:
                    self.page_cache.pop(cache_key, None)
        # Only opened environments are eligible. Do not scan the team's entire
        # environment inventory for every capture (it can contain thousands).
        matches = []
        for status in state.hub.browser_status():
            if state.hub.browser_lifecycle_state(status) != 'open':
                continue
            env = state.hub.env_lookup(container_code=str(status.get('containerCode') or ''))
            if env and str(identifier).strip() in {
                    str(env.get('containerCode') or ''), str(env.get('serialNumber') or '')}:
                matches.append(env)
        if len(matches) != 1:
            raise PurchaseAssistantError('请核对当前已打开的环境序号或 containerCode')
        code = str(matches[0].get('containerCode') or '')
        if state.hub.browser_lifecycle_status(code).get('state') != 'open':
            raise PurchaseAssistantError('请先打开指定 HubStudio 环境')
        port = int(state.hub.browser_start(code, headless=False)['debuggingPort'])
        page = self._attach_page(port, url, marker)
        with self.lock:
            if len(self.page_cache) >= 32:
                self.page_cache.clear()
            self.page_cache[cache_key] = (time.monotonic(), port)
        return page

    def _attach_page(self, port, url, marker):
        client = CdpClient(port)
        found = []
        for target in client.list_pages():
            if target.get('url') != url:
                continue
            page = PageTarget(client, target['id'], target['webSocketDebuggerUrl'])
            if page._evaluate("document.documentElement.getAttribute('data-xynigo-receipt-tab')") == marker:
                found.append(page)
            else:
                page._ws.close()
        if len(found) != 1:
            for page in found:
                page._ws.close()
            raise PurchaseAssistantError('环境与当前标签页不匹配，请核对环境序号')
        return found[0]

    def _operation_lock(self, member, body):
        key = (member, str(body.get('captureId') or body.get('marker') or ''))
        with self.lock:
            lock = self.operation_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self.operation_locks[key] = lock
            return lock

    def handle(self, state, member, body):
        action = body.get('action')
        with self.lock:
            entry = self.entries.get(str(body.get('captureId') or ''))
            job = entry.get('job') if entry and entry['member'] == member else None
            if job:
                if not job['done'].is_set():
                    return {'ok': True, 'state': 'processing', 'message': '正在处理采购凭证'}
                if action == 'status':
                    entry.pop('job', None)
                    return job['result']
            if body.get('async') is True and action in {'submit', 'retry-image', 'retry-color'}:
                if not entry or entry['member'] != member:
                    raise PurchaseAssistantError('凭证会话已过期，请重新读取')
                if not self.workers.acquire(blocking=False):
                    raise PurchaseAssistantError('正在处理的任务较多，请稍后重试')
                job = {'done': threading.Event()}
                entry['job'] = job
                job_body = dict(body)
                def work():
                    try:
                        job['result'] = self._handle_sync(state, member, job_body)
                    except Exception:
                        job['result'] = {'ok': False,
                            'code': 'receipt_uncertain' if entry['sent'] else 'receipt_conflict',
                            'error': '结果暂时无法确认，请查询回传结果' if entry['sent'] else '提交未完成，请重新读取并核对'}
                    finally:
                        job['done'].set()
                        self.workers.release()
                threading.Thread(target=work, name='purchase-receipt', daemon=True).start()
                return {'ok': True, 'state': 'processing', 'message': '正在处理采购凭证'}
        return self._handle_sync(state, member, body)

    def _handle_sync(self, state, member, body):
        action = body.get('action')
        if action not in {'read', 'submit', 'retry-image', 'retry-color', 'status'}:
            raise PurchaseAssistantError('采购详情操作无效')
        # Only the same capture/document is serialized; other tasks can progress.
        started = time.perf_counter()
        with self._operation_lock(member, body):
            with self.lock:
                self.entries = {k: v for k, v in self.entries.items() if time.monotonic()-v['time'] < 1800 or (v.get('job') and not v['job']['done'].is_set())}
            if action == 'read':
                with self.lock:
                    pending_count = sum(1 for v in self.entries.values() if (v.get('result') or {}).get('state') != 'complete')
                if pending_count >= 20:
                    raise PurchaseAssistantError('待处理凭证过多，请先完成当前任务')
                service, source = state.purchase_assistant_for_member(member)
                if source.get('scope') != 'team':
                    raise PurchaseAssistantError('请在桌面客户端切换到团队执行协作表；个人速填表不能回传')
                task_key = str(body.get('taskKey') or '')
                if not re.fullmatch(r'PT1-[0-9a-f]{64}', task_key):
                    raise PurchaseAssistantError('请先选择具体采购任务')
                # Cloud checks registered team source and fresh task identity.
                preview_start = time.perf_counter()
                preview = state.auth.purchase_receipt_request({'action': 'preview',
                    'sourceId': source['dataSourceId'], 'taskKey': task_key}, expected_member=member)
                preview_ms = round((time.perf_counter()-preview_start)*1000, 2)
                page_start = time.perf_counter()
                url, marker = str(body.get('pageUrl') or ''), str(body.get('marker') or '')
                page = self._page(state, str(body.get('identifier') or ''), url, marker)
                page_ms = round((time.perf_counter()-page_start)*1000, 2)
                try:
                    raw = page._evaluate(READ_ORDER)
                    order = validate_order(raw, url, marker)
                    capture_start = time.perf_counter()
                    expired = False
                    animation_token = secrets.token_hex(16)
                    try:
                        evaluate_capture_animation(page, HIDE_PANEL, animation_token)
                        image = page._send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 85,
                            'captureBeyondViewport': True, 'fromSurface': True, 'clip': raw['clip']})['data']
                    finally:
                        expired = evaluate_capture_animation(page, RESTORE_PANEL, animation_token)
                    if expired:
                        raise PurchaseAssistantError('截图会话已变化或耗时过长，请重新读取')
                    capture_ms = round((time.perf_counter()-capture_start)*1000, 2)
                    after = validate_order(page._evaluate(READ_IDENTITY), url, marker, check_clip=False)
                    if order != after or len(image) > 4_000_000:
                        raise PurchaseAssistantError('截图期间订单变化或图片过大，请重新读取')
                finally:
                    page._ws.close()
                capture = secrets.token_urlsafe(24)
                payload = {k: order[k] for k in ('orderNo', 'amount', 'currency', 'paidAt')}
                payload.update(action='submit', sourceId=source['dataSourceId'], taskKey=task_key,
                    requestId=str(uuid.uuid4()), expectedRevision=preview['expectedRevision'],
                    fingerprint=preview['fingerprint'], image=image)
                existing = preview.get('existing') or {}
                resume = existing.get('state') and existing['state'] != 'complete'
                if resume:
                    payload['requestId'] = existing['requestId']
                with self.lock:
                    self.entries[capture] = {'member': member, 'time': time.monotonic(), 'payload': payload,
                        'sent': bool(resume), 'reason': '', 'url': url, 'marker': marker, 'identifier': str(body.get('identifier') or ''), 'order': order,
                        'features': preview.get('features') or {}, 'retryColorRequestId': existing.get('requestId') or ''}
                return {'ok': True, 'captureId': capture, 'order': order, 'image': image,
                        'target': preview, 'requestId': payload['requestId'],
                        'features': {**(preview.get('features') or {}), 'asyncSubmitV1': True},
                        'performance': {'cloudPreviewMs': preview_ms, 'browserConnectMs': page_ms, 'captureMs': capture_ms, 'readMs': round((time.perf_counter()-started)*1000, 2), 'imageBytes': len(image)*3//4}}
            entry = self.entries.get(str(body.get('captureId') or ''))
            if not entry or entry['member'] != member:
                raise PurchaseAssistantError('凭证会话已过期或登录身份变化，请重新读取')
            payload = dict(entry['payload'])
            if action == 'submit':
                if body.get('confirmed') is not True:
                    raise PurchaseAssistantError('请核对订单、任务和截图后确认回传')
                if entry['sent']:
                    action = 'status'  # Never replay after an ambiguous HTTP result.
                else:
                    page = self._page(state, entry['identifier'], entry['url'], entry['marker'])
                    try:
                        if validate_order(page._evaluate(READ_IDENTITY), entry['url'], entry['marker'], check_clip=False) != entry['order']:
                            raise PurchaseAssistantError('订单已变化，请重新读取截图')
                    finally:
                        page._ws.close()
                    color = body.get('fillColor') or ''
                    if color not in FILL_COLORS:
                        raise PurchaseAssistantError('请选择预设浅色或不填色')
                    if color and not entry.get('features', {}).get('fillColorV1'):
                        raise PurchaseAssistantError('云端尚未支持填色，请先升级')
                    if entry.get('features', {}).get('fillColorV1'):
                        payload['fillColor'] = color
                        entry['payload']['fillColor'] = color
                    entry['reason'] = str(body.get('reason') or '').strip()[:300]
                    entry['retryColorRequestId'] = payload['requestId']
                    entry['sent'] = True
            if action == 'retry-color' and entry.get('retryColorRequestId'):
                payload['requestId'] = entry['retryColorRequestId']
                entry['payload']['requestId'] = payload['requestId']
                entry['sent'] = True
            payload['action'] = action
            payload['reason'] = entry['reason']
            if action != 'submit':
                payload['image'] = ''
            cloud_start = time.perf_counter()
            result = state.auth.purchase_receipt_request(payload, expected_member=member)
            result['localPerformance'] = {'cloudRoundTripMs': round((time.perf_counter()-cloud_start)*1000, 2), 'operationMs': round((time.perf_counter()-started)*1000, 2)}
            if result.get('requestId'):
                entry['retryColorRequestId'] = result['requestId']
            entry['result'] = result
            if result.get('state') == 'complete':
                entry['payload']['image'] = ''
            return result
