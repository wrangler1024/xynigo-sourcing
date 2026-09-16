"""Pure presentation facts for after-sale scan rows; never infer eligibility."""
import re
from datetime import datetime

MONTHS = {'jan':1,'ene':1,'feb':2,'mar':3,'apr':4,'abr':4,'may':5,'jun':6,
          'jul':7,'aug':8,'ago':8,'sep':9,'oct':10,'nov':11,'dec':12,'dic':12}


def delivery_date(value):
    """Parse a displayed date without browser/server timezone conversion."""
    text = str(value or '').strip()
    iso = re.match(r'^(\d{4})-(\d{2})-(\d{2})(?:\b|T)', text)
    words = re.match(r'^(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(\d{4})(?:\b|T)', text)
    try:
        if iso:
            year, month, day = map(int, iso.groups())
        elif words:
            day, name, year = words.groups()
            year, month, day = int(year), MONTHS.get(name[:3].lower(), 0), int(day)
        else:
            return ''
        return datetime(year, month, day).strftime('%Y-%m-%d')
    except ValueError:
        return ''


def complete_delivery_date(packages, expected_count):
    """All known packages must have distinct IDs and delivered timestamps."""
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 1 or len(packages) != expected_count:
        return ''
    ids, dates = set(), []
    for package in packages:
        key = str(package.get('packageNo') or '')
        value = delivery_date(package.get('deliveredAt'))
        if not key or key in ids or package.get('delivered') is not True or not value:
            return ''
        ids.add(key); dates.append(value)
    return max(dates)


def order_item_summary(card):
    """Only an explicit platform item total is accepted; images are not quantities."""
    count = card.get('itemCount')
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100000:
        count = None
    images = []
    for item in card.get('goodsImages') or []:
        url = str(item or '').strip()[:300]
        if url and url not in images:
            images.append(url)
    items = []
    for item in (card.get('goodsItems') or [])[:100]:
        if not isinstance(item, dict):
            continue
        quantity = item.get('quantity')
        items.append({'goodsImg': str(item.get('goodsImg') or '')[:300],
                      'name': str(item.get('name') or '')[:200],
                      'specification': str(item.get('specification') or '')[:200],
                      'quantity': quantity if isinstance(quantity, int) and not isinstance(quantity, bool) and 1 <= quantity <= 100000 else None})
    return {'goodsItems': items, 'itemCount': count, 'itemCountSource': (card.get('itemCountSource') or 'order_card') if count is not None else '',
            'goodsImages': images[:100]}


def delivery_summary(card, order):
    packages = order.get('packages') or []
    blocked = order.get('blockedPackages') or []
    package_ids = {str(p.get('packageNo') or '') for p in packages if isinstance(p, dict)}
    package_ids.update(str(p) for p in blocked if p)
    package_ids.discard('')
    # Several refund packages prove this is a multi-package order. A single
    # eligible package alone does not prove that there are no other packages.
    evidence = card.get('packageDeliveries') or []
    total = card.get('totalPackageCount')
    date = complete_delivery_date(evidence, total) if evidence else ''
    if date:
        return {'deliveredDate': date, 'deliveryDateStatus': 'complete', 'deliveryNote': ''}
    # Card dates are still displayed as original evidence, but are not silently
    # treated as a complete multi-package date without package-level proof.
    single_date = delivery_date(order.get('deliveredAt'))
    if not evidence and not isinstance(total, bool) and total == 1 and len(package_ids) <= 1 and card.get('delivered') is True and single_date:
        return {'deliveredDate': single_date, 'deliveryDateStatus': 'complete', 'deliveryNote': ''}
    if len(package_ids) > 1 or (isinstance(total, int) and total > 1):
        note = '多包裹送达信息不完整，待核对；不参与送达日期筛选'
    elif not order.get('deliveredAt'):
        note = '送达时间未取得，待核对；不参与送达日期筛选'
    else:
        note = '已读取列表送达时间；整单包裹完整性待核对，不参与送达日期筛选'
    return {'deliveredDate': '', 'deliveryDateStatus': 'unknown', 'deliveryNote': note}
