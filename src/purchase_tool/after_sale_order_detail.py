"""Order-detail display facts, projected from the platform's SSR document only."""
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.inside = tag == 'script'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def _order_info(html, order_no):
    parser = _Scripts()
    parser.feed(html)
    info = None
    for script in parser.parts:
        match = re.search(r'\bvar\s+gbRawData\s*=\s*', script)
        if not match:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(script[match.end():])
        except (ValueError, TypeError):
            continue
        candidate = data.get('orderInfo') if isinstance(data, dict) else None
        if isinstance(candidate, dict) and candidate.get('billno') == order_no:
            info = candidate
            break
    if info is None:
        raise ValueError('订单详情未返回匹配订单的数据')
    return info


def parse_order_detail_facts(html, order_no, platform_dates=None):
    """Do not evaluate scripts or infer quantities from images/package counts."""
    info = _order_info(html, order_no)
    goods = info.get('orderGoodsList')
    if not isinstance(goods, list) or not 1 <= len(goods) <= 100:
        raise ValueError('订单商品明细不完整')
    items, ids, mapped = [], set(), True
    for good in goods:
        if not isinstance(good, dict) or good.get('billno') != order_no:
            raise ValueError('商品明细订单标识不一致')
        raw = str(good.get('quantity') or '')
        quantity = int(raw) if re.fullmatch(r'[1-9]\d{0,5}', raw) else None
        if quantity is not None and quantity > 100000:
            quantity = None
        items.append({'name': str(good.get('goodsNameWithBlindBox') or '')[:200],
            'goodsImg': str(good.get('goodsImgWithBlindBox') or '')[:300],
            'specification': str(good.get('sku_attrs_contact_str') or '')[:200],
            'quantity': quantity})
        relations = good.get('goods_pkg_rel_list')
        if not isinstance(relations, list) or not relations:
            mapped = False
        else:
            for relation in relations:
                identity = relation.get('package_no') if isinstance(relation, dict) else None
                if identity:
                    ids.add(str(identity))
                else:
                    mapped = False
    count = sum(i['quantity'] for i in items) if all(i['quantity'] for i in items) else None
    # The platform's independent total catches partial/truncated product lists.
    if str(count) != str(info.get('orderGoodsSum')) or (count and count > 100000):
        count = None
    facts = {'goodsItems': items, 'goodsImages': list(dict.fromkeys(i['goodsImg'] for i in items if i['goodsImg'])),
        'itemCount': count, 'itemCountSource': 'order_detail' if count is not None else ''}
    packages = info.get('order_package_info_list')
    if not isinstance(packages, list) or not packages or not mapped or count is None:
        return facts
    package_ids = [str(p.get('packageNo') or '') for p in packages if isinstance(p, dict)]
    if len(package_ids) != len(packages) or len(set(package_ids)) != len(packages) or set(package_ids) != ids:
        return facts
    deliveries = []
    for package in packages:
        raw = str(package.get('signed_time') or '')
        # deliveryTime is shipment time on observed pages; only signed_time is delivery.
        date = ''
        if re.fullmatch(r'\d{10}', raw):
            instant = datetime.fromtimestamp(int(raw), timezone.utc)
            if instant <= datetime.now(timezone.utc):
                # Use this browser's own timezone database, matching the platform.
                # OS/Python tzdata can disagree after regional DST rule changes.
                candidate = (platform_dates or {}).get(raw, '')
                try:
                    datetime.strptime(candidate, '%Y-%m-%d %H:%M:%S')
                    date = candidate
                except (ValueError, TypeError):
                    pass
        deliveries.append({'packageNo': package['packageNo'], 'delivered': bool(date), 'deliveredAt': date})
    facts.update(totalPackageCount=len(packages), packageDeliveries=deliveries)
    if all(p['deliveredAt'] for p in deliveries):
        facts.update(delivered=True, deliveredAt=max(p['deliveredAt'] for p in deliveries))
    return facts


def read_order_detail_facts(page, order_no):
    if not re.fullmatch(r'[A-Z0-9]{1,32}', order_no):
        raise ValueError('订单号格式无效')
    page.goto('https://www.shein.com.mx/user/orders/detail/' + order_no,
              dom_timeout=40, settle_seconds=2)
    if not page.wait_for("document.querySelectorAll('.order-products').length > 0", timeout=20):
        raise ValueError('订单详情商品明细未就绪')
    html = page.outer_html()
    info = _order_info(html, order_no)
    times = [str(p.get('signed_time') or '') for p in info.get('order_package_info_list') or [] if isinstance(p, dict)]
    times = [t for t in times if re.fullmatch(r'\d{10}', t)]
    dates = page.js_evaluate('''(() => { const f=new Intl.DateTimeFormat('sv-SE',{
      year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
      return Object.fromEntries(%s.map(t=>[t,f.format(new Date(Number(t)*1000))])); })()''' % json.dumps(times))
    return parse_order_detail_facts(html, order_no, dates)
