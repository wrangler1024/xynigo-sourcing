"""Read the current refund timeline node; future labels are never evidence."""
import re
import time
import unicodedata
from datetime import datetime

PHASE_ORDER = ('submitted', 'reviewing', 'processing', 'shein_refunded', 'bank_processed')
PHASE_LABELS = {
    'submitted': '已受理', 'reviewing': '审核中', 'processing': 'SHEIN处理中',
    'shein_refunded': 'SHEIN退款成功', 'bank_processed': '金融机构已处理',
    'review_failed': '审核未通过', 'evidence_required': '审核未通过 · 待补充凭证',
    # Old records did not distinguish platform success from bank processing.
    'refunded': '历史退款状态 · 待回访', 'rejected': '历史拒绝状态 · 待回访',
    'overdue': '超期未出结果', 'fail': '本次状态未确认',
}
STEP_TITLES = (
    'solicitud de reembolso aceptada', 'resena de shein/vendedor',
    'procesamiento de reembolsos de shein', 'reembolso de shein exitoso',
    'reembolso procesado por su institucion financiera',
)

TRACK_STATE_JS = r'''(() => {
  const clean = s => String(s || '').replace(/[\u4e00-\u9fa5]+/g,' ').replace(/\s+/g,' ').trim();
  const cleanLines = s => String(s || '').split(/\r?\n/).map(clean).filter(Boolean).join('\n');
  const visible = e => !!e && !!e.getClientRects().length;
  const roots = [...document.querySelectorAll('.return-steps')].filter(visible);
  const body = document.body?.innerText || '';
  const bill = (clean(body).match(/Código del Reembolso\s*[:：]\s*(\d+)/i) || [])[1] || '';
  const steps = roots.length === 1 ? [...roots[0].querySelectorAll('.return-step-item')].map(e => {
    const title = e.querySelector('.return-step-item__title');
    const flags = ['active','finish','wait'].filter(k => title?.classList.contains('is-'+k));
    const detail = e.querySelector('.return-step-item__collapse');
    return {title:clean(title?.textContent).slice(0,240), state:flags.length===1?flags[0]:'',
      visible:visible(title), detail:cleanLines(visible(detail)?detail.innerText:'').slice(0,800),
      canSupplement:[...e.querySelectorAll('button,a[href],[role=button]')].some(n =>
        visible(n) && !n.disabled && !n.matches(':disabled') &&
        !n.closest('[disabled],[aria-disabled="true"],[inert]') &&
        /^subir comprobante(?:s)?$/i.test(clean(n.innerText)))};
  }) : [];
  return {orderNo:location.pathname.split('/').filter(Boolean).pop(), refundBillId:bill, steps,
    amounts:(body.match(/\$MXN\s*([\d,]+\.\d{2})/g)||[]).slice(0,3)};
})()'''


def normalize(text):
    text = re.sub(r'[\u4e00-\u9fa5]+', ' ', str(text or ''))
    text = ''.join(c for c in unicodedata.normalize('NFKD', text) if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', text).strip().lower()


def classify_phase_state(state):
    """Fail closed for partial, conflicting or unrecognized page structures."""
    if not isinstance(state, dict):
        return None
    steps = state.get('steps')
    if not isinstance(steps, list) or len(steps) != 5 or any(not isinstance(s, dict) for s in steps):
        return None
    for step, expected in zip(steps, STEP_TITLES):
        if not normalize(step.get('title')).replace(' ', '').startswith(expected.replace(' ', '')):
            return None
    active = [i for i, s in enumerate(steps) if s.get('state') == 'active']
    if not active and all(s.get('state') == 'finish' for s in steps):
        index = 4
    elif len(active) == 1:
        index = active[0]
        if not steps[index].get('visible'):
            return None
        if any(s.get('state') != ('finish' if i < index else 'wait')
               for i, s in enumerate(steps) if i != index):
            return None
    else:
        return None
    current = steps[index]
    detail = str(current.get('detail') or '')
    text = normalize(str(current.get('title') or '') + '\n' + detail)
    failed = bool(re.search(r'revision fallida|reembolso rechazado|solicitud rechazada|reembolso denegado', text))
    can_supplement = current.get('canSupplement') is True
    if (failed or can_supplement) and index != 1:
        return None
    phase = PHASE_ORDER[index]
    if index == 1:
        if failed:
            phase = 'evidence_required' if can_supplement else 'review_failed'
        elif can_supplement or not re.search(r'en revision|termina en', normalize(detail)):
            return None
    reason = ''
    reason_match = re.search(r'(?:Reembolso rechazado|Motivo del rechazo)\s*[:：]\s*([^\n]+)', detail, re.I)
    if failed and reason_match:
        reason = reason_match.group(1).strip()[:300]
    evidence = {'source':'timeline_nodes_v1', 'step':index+1,
                'title':str(current.get('title') or '')[:240], 'detail':detail[:800],
                'canSupplement':can_supplement, 'reason':reason,
                'reasonSource':'current_node' if reason else ''}
    countdown = re.search(r'Termina en\s+(\d{1,3}\s*:\s*\d{2}\s*:\s*\d{2})(?!\d)', detail, re.I)
    return {'phase':phase, 'phaseLabel':PHASE_LABELS[phase], 'phaseEvidence':evidence,
            'countdown':re.sub(r'\s+', '', countdown.group(1)) if countdown else ''}


def latest_rejection_reason(text, bill):
    """Only the newest dated event may explain the current failed review."""
    text = re.sub(r'[\u4e00-\u9fa5]+', ' ', str(text or ''))
    bills = re.findall(r'Código del Reembolso\s*[:：]\s*(\d+)\b', text, re.I)
    if not bills or set(bills) != {bill}:
        return ''
    # The observed dialog lists actor / event / reason / platform-local date.
    # Dates are compared within one page, without guessing a timezone.
    lines = [re.sub(r'[\u4e00-\u9fa5]+', '', x).strip() for x in text.splitlines()]
    months = {'jan':1,'ene':1,'feb':2,'mar':3,'apr':4,'abr':4,'may':5,'jun':6,
              'jul':7,'aug':8,'ago':8,'sep':9,'oct':10,'nov':11,'dec':12,'dic':12}
    events, block, actor = [], [], ''
    for line in lines:
        if not line:
            continue
        if normalize(line) in ('vendedor', 'shein', 'vendedor/shein', 'usuario'):
            if actor:
                # A newer event without a recognized date cannot be discarded.
                return ''
            actor, block = normalize(line), []
        elif actor:
            m = re.fullmatch(r'(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})', line)
            if m:
                try:
                    moment = datetime(int(m[3]), months[m[2].lower()], int(m[1]), int(m[4]), int(m[5]), int(m[6]))
                except (ValueError, KeyError):
                    return ''
                events.append((moment, actor, '\n'.join(block)))
                actor, block = '', []
            else:
                block.append(line)
        elif events or not (normalize(line) in ('historial de negociacion', 'ver detalles >', 'ver detalles')
                or re.fullmatch(r'Código del Reembolso\s*[:：]\s*'+re.escape(bill), line, re.I)
                or re.fullmatch(r'Plazo de la solicitud\s*[:：]\s*\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2}', line, re.I)):
            # Unknown actors, dates or trailing content may be newer evidence.
            return ''
    if actor or not events:
        return ''
    newest = max(e[0] for e in events)
    candidates = [e for e in events if e[0] == newest]
    if len(candidates) != 1:
        return ''
    _, actor, body = candidates[0]
    if actor == 'usuario' or not re.search(r'revision fallida|reembolso rechazado', normalize(body)):
        return ''
    match = re.search(r'Reembolso rechazado\s*[:：]\s*([^\n]+)', body, re.I)
    return match.group(1).strip()[:300] if match else ''


def phase_note(result):
    evidence = result['phaseEvidence']
    if result['phase'] in ('review_failed', 'evidence_required'):
        reason = evidence.get('reason') or ''
        translated = '凭证不足（%s）' % reason if normalize(reason) == 'comprobantes insuficientes' else reason
        return ('平台审核未通过；' + ('原因：'+translated if translated else '具体原因尚未取得，请查看平台协商记录')
                + ('；可补充凭证重新审核' if evidence['canSupplement'] else '；本次未确认可补充凭证入口'))[:200]
    if result['phase'] == 'shein_refunded':
        return 'SHEIN退款成功，金融机构处理尚未完成'
    if result['phase'] == 'bank_processed':
        return '平台显示金融机构已处理退款'
    return '未终态，下次回访继续跟'


def read_refund_phase(page, expected_identity, timeout=15, include_reason=True):
    from .after_sale_receipts import refund_bill_id_from_url
    def read():
        if refund_bill_id_from_url(page.url) != expected_identity:
            raise RuntimeError('退款详情身份在阶段读取期间发生变化')
        state = page.js_evaluate(TRACK_STATE_JS) or {}
        if ((state.get('orderNo'), state.get('refundBillId')) != expected_identity
                or refund_bill_id_from_url(page.url) != expected_identity):
            return None, state
        return classify_phase_state(state), state
    deadline, previous = time.monotonic()+max(0, timeout), None
    while True:
        result, state = read()
        if result and (result == previous or timeout == 0):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('本次状态未确认：退款时间轴未完整加载或当前节点信号不一致')
        previous = result
        time.sleep(min(0.5, max(0, deadline-time.monotonic())))
    if include_reason and result['phase'] in ('review_failed', 'evidence_required') and not result['phaseEvidence']['reason']:
        opened = False
        try:
            point = page.js_evaluate(r'''(() => {
              const active=document.querySelector('.return-step-item__title.is-active')?.closest('.return-step-item');
              const buttons=[...(active?.querySelectorAll('.business-or-shein__btns .btn-info button')||[])];
              const e=buttons.find(n=>/Historial de negociación/i.test(n.innerText));
              if(!e || !e.getClientRects().length)return null;
              e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
              return {x:r.x+r.width/2,y:r.y+r.height/2};})()''')
            if point:
                page.native_click_point(point['x'], point['y']); opened = True
                js = r'''(() => {
                  const dialogs=[...document.querySelectorAll('.sui-dialog__wrapper')].filter(e=>
                    e.getClientRects().length && /HISTORIAL DE NEGOCIACI[ÓO]N/i.test(e.innerText));
                  return dialogs.length===1 ? dialogs[0].innerText : '';})()'''
                page.wait_for('Boolean('+js+')', timeout=10)
                history = page.js_evaluate(js) or ''
                latest, _ = read()
                if latest != result:
                    raise RuntimeError('退款阶段在协商记录读取期间变化，请重新回访')
                reason = latest_rejection_reason(str(history), expected_identity[1])
                if reason:
                    result['phaseEvidence'].update(reason=reason, reasonSource='negotiation_history')
        except Exception:
            # History is supplementary. A failed read must never invent a reason,
            # or turn a confirmed review failure into processing/success.
            current, _ = read()
            if not current or current['phase'] != result['phase']:
                raise RuntimeError('本次状态未确认：读取协商记录期间平台状态发生变化')
        finally:
            if opened:
                page.press_key('Escape', 'Escape', 27)
    result['note'] = phase_note(result)
    result['amounts'] = state.get('amounts') or []
    return result
