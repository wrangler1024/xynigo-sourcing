"""Display-only normalization; stored platform evidence is left intact."""
import re
import unicodedata


def display_refund_path(value):
    labels = []
    for part in re.split(r'\s*[/／]\s*', str(value or '')):
        label = part.strip()
        normalized = re.sub(r'\s+', '', unicodedata.normalize('NFKC', label))
        if label and normalized != '其他退款渠道(名称未取得)' and label not in labels:
            labels.append(label)
    return ' / '.join(labels)


def masked_refund_account(value):
    text = re.sub(r'[\s-]+', '', str(value or ''))
    return '****' + text[-4:] if re.fullmatch(r'[*•●xX]{2,}\d{4}', text) else ''


def review_note(refund):
    evidence = refund.get('phaseEvidence') or {}
    if not evidence or refund.get('phase') not in ('review_failed', 'evidence_required'):
        return ''
    reason = str(evidence.get('reason') or '')
    if reason.casefold() == 'comprobantes insuficientes':
        reason = '凭证不足（%s）' % reason
    return ('平台审核未通过；' + ('原因：'+reason if reason else '具体原因尚未取得，请查看平台协商记录')
            + ('；可补充凭证重新审核' if evidence.get('canSupplement')
               else '；本次未确认可补充凭证入口'))[:200]


def present_refund(refund):
    result = dict(refund)
    result['refundPath'] = display_refund_path(result.get('refundPath'))
    if masked_refund_account(result.get('refundAccount')):
        parts = re.split(r'[；;]', str(result.get('detailsNote') or ''))
        result['detailsNote'] = '；'.join(part for part in parts if part and not part.startswith('退款账户'))
    return result
