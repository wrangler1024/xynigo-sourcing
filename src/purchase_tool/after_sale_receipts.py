"""Read-only refund evidence. A found record is never proof of who submitted it."""
import re
from urllib.parse import parse_qs, urlparse

ORIGIN = 'https://www.shein.com.mx'


def refund_bill_id_from_url(url):
    """Accept both observed receipt URL forms, rejecting conflicting identities."""
    parsed = urlparse(str(url or ''))
    if parsed.scheme != 'https' or parsed.hostname not in ('www.shein.com.mx', 'mx.shein.com'):
        return None
    path = re.fullmatch(r'/orders/refundLabel/([A-Z0-9]+)/?', parsed.path)
    if not path:
        return None
    order = path.group(1)
    query = parse_qs(parsed.query)
    bills = []
    for value in query.get('refund_bill_id', []):
        if not re.fullmatch(r'\d{1,32}', value):
            return None
        bills.append(value)
    for value in query.get('refund_bill_id_list', []):
        match = re.fullmatch(r'([A-Z0-9]+)_(\d{1,32})', value)
        if not match or match.group(1) != order:
            return None
        bills.append(match.group(2))
    return (order, bills[0]) if bills and len(set(bills)) == 1 else None


_DETAIL_LINKS = "[...document.querySelectorAll('a.she-btn-black')].filter(a=>a.innerText.trim()==='Detalles')"
_DETAIL_FACTS = r'''(() => {
  const text=document.body.innerText || '';
  const pick=re=>{const m=text.match(re);return m?m[1].trim():''};
  const data=window.GB_OrderReturnLabel || {}, refund=data.refund_page_info?.cur_refund_order || {};
  const visibleBill=pick(/Código del Reembolso\s*[:：]\s*(\d+)/i);
  const same=String(refund.refund_bill_id || '')===visibleBill && data.billno===location.pathname.split('/').filter(Boolean).pop();
  const packages=same ? [...new Set((refund.refund_bill_goods_list || []).map(g=>String(g.package_no || '')).filter(Boolean))] : [];
  const paths=same ? [...new Set((refund.refund_record_list || []).filter(r=>Number(r.refund_amount_without_symbol)>0).map(r=>String(r.refund_path)))] : [];
  const pathLabels={'1':'Tarjeta de regalo','2':'Cartera SHEIN','3':'Cuenta original de pago'};
  const created=same ? Number(refund.add_time) : 0;
  return {packageNos:packages, applicationAt:created>1000000000 && created<10000000000 ? new Date(created*1000).toISOString() : '',
    reasonId:same ? String(refund.reason || '') : '',
    refundBillId:pick(/Código del Reembolso\s*[:：]\s*(\d+)/i),
    applicationTimeText:pick(/Plazo de la solicitud\s*[:：]\s*([^\n]+)/i),
    timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone || '',
    refundAccount: (()=>{const a=document.querySelector('.refundAccount-info .tip');const t=String(a?.innerText || a?.querySelector('img')?.alt || '').trim();return /^[*0-9\s-]{4,24}$/.test(t)?t:'';})(),
    refundPath: (()=>{if(paths.length)return paths.map(p=>pathLabels[p] || '其他退款渠道（名称未取得）').join(' / ');const t=document.querySelector('.refundAccount-info')?.innerText || '';const m=t.match(/Cuenta original de pago|Cartera SHEIN|Tarjeta de regalo/i);return m?m[0]:'';})(),
    phaseText: text.split('\n').filter(line=>/en revisión|En revisión vendedor|reembolso|reembolsad|rechazad/i.test(line)).join('\n').slice(0,3000)};
})()'''


def read_refund_receipts(page, order_no, classify_phase, phase_labels, on_record=None):
    """Follow only the platform's existing refund-detail links. Never submit."""
    if not re.fullmatch(r'[A-Z0-9]{1,32}', order_no):
        raise ValueError('订单号格式无效')
    url = ORIGIN + '/user/order_return/return_refund_list/' + order_no
    page.goto(url, dom_timeout=40, settle_seconds=2)
    if not page.wait_for(_DETAIL_LINKS + '.length > 0', timeout=25):
        raise RuntimeError('退款列表未提供可读取的详情入口')
    count = page.js_evaluate(_DETAIL_LINKS + '.length')
    if not isinstance(count, int) or not 1 <= count <= 100:
        raise RuntimeError('退款记录数量未确认')
    records = {}
    for index in range(count):
        if index:
            page.goto(url, dom_timeout=40, settle_seconds=2)
            if not page.wait_for(_DETAIL_LINKS + '.length === %d' % count, timeout=25):
                raise RuntimeError('退款列表在核对期间发生变化')
        point = page.js_evaluate('''(() => { const a=%s[%d];if(!a)return null;
          a.scrollIntoView({block:'center'});const r=a.getBoundingClientRect();
          return {x:r.x+r.width/2,y:r.y+r.height/2};})()''' % (_DETAIL_LINKS, index))
        if not point:
            raise RuntimeError('退款详情入口未取得')
        page.native_click_point(point['x'], point['y'])
        if not page.wait_for("location.pathname.includes('/orders/refundLabel/') && document.body.innerText.includes('Código del Reembolso')", timeout=40):
            raise RuntimeError('退款详情未加载完成')
        record = read_current_receipt(page, order_no, classify_phase, phase_labels)
        records[record['refundBillId']] = record
        if on_record:
            on_record(record)
    if len(records) != count:
        raise RuntimeError('退款详情列表重复，未确认全部记录')
    return list(records.values())


def read_current_receipt(page, order_no, classify_phase, phase_labels):
    identity = refund_bill_id_from_url(page.url)
    page.wait_for("!!document.querySelector('.refundAccount-info .tip')", timeout=8)
    facts = page.js_evaluate(_DETAIL_FACTS) or {}
    if not identity or identity[0] != order_no or identity[1] != facts.get('refundBillId'):
        raise RuntimeError('退款详情的订单或退款单号不一致')
    phase = classify_phase(facts.get('phaseText') or '')
    package_nos = [str(p)[:64] for p in (facts.get('packageNos') or []) if p][:100]
    return {
        'refundBillId': identity[1], 'phase': phase,
        'phaseLabel': phase_labels.get(phase, '平台阶段待核对'),
        'packageNo': package_nos[0] if len(package_nos) == 1 else '',
        'packageNos': package_nos,
        **{key: str(facts.get(key) or '')[:limit] for key, limit in
           [('applicationTimeText',64),('timeZone',64),('applicationAt',40),('reasonId',32),
            ('refundAccount',40),('refundPath',48)]},
        'detailsNote': '；'.join(label+'未读取' for key,label in
                              [('refundPath','退款路径'),('refundAccount','退款账户')]
                              if not facts.get(key)),
    }


def receipt_reason(records):
    parts = []
    for record in records:
        part = '退款单 %s，%s' % (record['refundBillId'], record.get('phaseLabel') or '阶段待核对')
        if record.get('applicationTimeText'):
            part += '，申请于 ' + record['applicationTimeText']
            if record.get('timeZone'):
                part += '（' + record['timeZone'] + '）'
        parts.append(part)
    return '；'.join(parts)
