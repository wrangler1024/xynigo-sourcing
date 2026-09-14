"""Read the caller's exact SHEIN tab and retain short-lived evidence in memory."""
import json
import re
import secrets
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation

from .cdp import CdpClient
from .purchase_assistant import PurchaseAssistantError

# Executed only in the tab carrying the extension's random per-document marker.
READ_ORDER = r'''(() => {
 const o=window.gbRawData?.orderInfo;
 const h=document.querySelector('h1.sui-title__text');
 const total=document.querySelector('.order-total');
 if(!o||!h||!total) return null;
 const a=h.getBoundingClientRect(),z=total.getBoundingClientRect();
 const items=(o.orderGoodsList||[]).map(x=>({sku:String(x.display_goods_sn||x.sku_code||''),quantity:String(x.quantity||'')}));
 return {orderNo:String(o.billno||''),amount:String(o.totalPrice?.amount||''),
 currency:String(o.currency_code||''),isPaid:String(o.isPaid||''),paidAt:String(o.paymentTime||''),
 status:String(o.orderStatus||''),items,
 totalText:total.innerText,
 url:location.href,marker:document.documentElement.getAttribute('data-xynigo-receipt-tab'),
 clip:{x:Math.floor(a.left+scrollX),y:Math.floor(a.top+scrollY),
 width:Math.ceil(Math.max(a.right,z.right)-a.left)+2,height:Math.ceil(z.bottom-a.top+8),scale:1}};
})()'''


def validate_order(data, url, marker):
    if not isinstance(data, dict) or data.get('url') != url or data.get('marker') != marker:
        raise PurchaseAssistantError('订单页面已变化，请重新读取')
    match = re.fullmatch(r'https://www\.shein\.com\.mx/user/orders/detail/([A-Za-z0-9-]{6,64})', url)
    if not match or match.group(1) != data.get('orderNo') or data.get('currency') != 'MXN':
        raise PurchaseAssistantError('首版仅支持墨西哥站订单详情，请核对当前页面')
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
    if (clip.get('width', 0) < 200 or clip.get('width', 0) > 3000 or
            clip.get('height', 0) < 100 or clip.get('height', 0) > 12000 or
            clip.get('x', -1) < 0 or clip.get('y', -1) < 0):
        raise PurchaseAssistantError('订单区域过长或布局异常，请人工核对截图')
    return {k: data[k] for k in ('orderNo', 'amount', 'currency', 'paidAt', 'status', 'items')}


class PurchaseDetailsService:
    def __init__(self):
        self.entries = {}
        self.lock = threading.RLock()

    def _page(self, state, identifier, url, marker):
        if not re.fullmatch(r'[A-Za-z0-9-]{20,100}', marker):
            raise PurchaseAssistantError('页面会话标识无效')
        if not re.fullmatch(r'https://www\.shein\.com\.mx/user/orders/detail/[A-Za-z0-9-]{6,64}', url):
            raise PurchaseAssistantError('首版仅支持墨西哥站订单详情')
        env = state.hub.locate_environment(identifier)
        code = str(env.get('containerCode') or '')
        if state.hub.browser_lifecycle_status(code).get('state') != 'open':
            raise PurchaseAssistantError('请先打开指定 HubStudio 环境')
        port = int(state.hub.browser_start(code, headless=False)['debuggingPort'])
        client = CdpClient(port)
        found = []
        for target in client.list_pages():
            if target.get('url') != url:
                continue
            page = client.attach_page(target_id=target['id'])
            if page._evaluate("document.documentElement.getAttribute('data-xynigo-receipt-tab')") == marker:
                found.append(page)
            else:
                page._ws.close()
        if len(found) != 1:
            for page in found:
                page._ws.close()
            raise PurchaseAssistantError('环境与当前标签页不匹配，请核对环境序号')
        return found[0]

    def handle(self, state, member, body):
        action = body.get('action')
        if action not in {'read', 'submit', 'retry-image', 'status'}:
            raise PurchaseAssistantError('采购详情操作无效')
        # Serializes this device's capture cache, never closes the user's page.
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if time.monotonic()-v['time'] < 1800}
            if action == 'read':
                if sum(1 for v in self.entries.values() if (v.get('result') or {}).get('state') != 'complete') >= 20:
                    raise PurchaseAssistantError('待处理凭证过多，请先完成当前任务')
                service, source = state.purchase_assistant_for_member(member)
                if source.get('scope') != 'team':
                    raise PurchaseAssistantError('请在桌面客户端切换到团队执行协作表；个人速填表不能回传')
                task_key = str(body.get('taskKey') or '')
                if not re.fullmatch(r'PT1-[0-9a-f]{64}', task_key):
                    raise PurchaseAssistantError('请先选择具体采购任务')
                # Cloud checks source membership, row ownership and fresh task identity.
                preview = state.auth.purchase_receipt_request({'action': 'preview',
                    'sourceId': source['dataSourceId'], 'taskKey': task_key})
                url, marker = str(body.get('pageUrl') or ''), str(body.get('marker') or '')
                page = self._page(state, str(body.get('identifier') or ''), url, marker)
                try:
                    raw = page._evaluate(READ_ORDER)
                    order = validate_order(raw, url, marker)
                    # Hide only this extension; restore the exact original visibility.
                    old = page._evaluate("(() => {const e=document.querySelector('#xynigo-purchase-assistant-host');if(!e)return null;const v=e.style.visibility;e.style.visibility='hidden';return v})()")
                    try:
                        image = page._send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 85,
                            'captureBeyondViewport': True, 'fromSurface': True, 'clip': raw['clip']})['data']
                    finally:
                        page._evaluate("(() => {const e=document.querySelector('#xynigo-purchase-assistant-host');if(e)e.style.visibility=" + json.dumps(old or '') + "})()")
                    after = validate_order(page._evaluate(READ_ORDER), url, marker)
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
                self.entries[capture] = {'member': member, 'time': time.monotonic(), 'payload': payload,
                    'sent': bool(resume), 'reason': '', 'url': url, 'marker': marker, 'identifier': str(body.get('identifier') or ''), 'order': order}
                return {'ok': True, 'captureId': capture, 'order': order, 'image': image,
                        'target': preview, 'requestId': payload['requestId']}
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
                        if validate_order(page._evaluate(READ_ORDER), entry['url'], entry['marker']) != entry['order']:
                            raise PurchaseAssistantError('订单已变化，请重新读取截图')
                    finally:
                        page._ws.close()
                    entry['reason'] = str(body.get('reason') or '').strip()[:300]
                    entry['sent'] = True
            payload['action'] = action
            payload['reason'] = entry['reason']
            if action != 'submit':
                payload['image'] = ''
            result = state.auth.purchase_receipt_request(payload)
            entry['result'] = result
            if result.get('state') == 'complete':
                entry['payload']['image'] = ''
            return result
