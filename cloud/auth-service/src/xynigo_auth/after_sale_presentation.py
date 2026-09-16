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


def present_refund(refund):
    result = dict(refund)
    result['refundPath'] = display_refund_path(result.get('refundPath'))
    if masked_refund_account(result.get('refundAccount')):
        parts = re.split(r'[；;]', str(result.get('detailsNote') or ''))
        result['detailsNote'] = '；'.join(part for part in parts if part and not part.startswith('退款账户'))
    return result
