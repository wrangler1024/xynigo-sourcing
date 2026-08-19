# -*- coding: utf-8 -*-
"""SHEIN 墨西哥/美国站优惠券读取与环境备注摘要。"""
from dataclasses import dataclass
from datetime import datetime
import re


COUPON_URL = 'https://www.shein.com.mx/user/coupon'
COUPON_URLS = {
    'MX': COUPON_URL,
    'US': 'https://us.shein.com/user/coupon',
}
RE_DISCOUNT = re.compile(
    r'(\d+)\s*%\s*(?:DE\s*DESCUENTO|OFF)', re.I)
RE_EXPIRY_MX = re.compile(
    r'Caduca\s+en\s+(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2})?', re.I)
RE_EXPIRY_US = re.compile(
    r'(?:Expires?(?:\s+on)?|Valid\s+until)\s*:?\s*'
    r'(\d{1,2}/\d{1,2}/\d{4})(?:\s+\d{1,2}:\d{2})?', re.I)


class CouponError(Exception):
    pass


@dataclass
class CouponItem:
    discount_percent: int = 0
    no_minimum: bool = False
    expiry: str = ''

    def short_text(self):
        bits = ['%s%%OFF' % self.discount_percent if self.discount_percent
                else '未知折扣']
        if self.no_minimum:
            bits.append('无门槛')
        if self.expiry:
            bits.append('到期%s' % self.expiry)
        return ''.join(bits)


@dataclass
class CouponSummary:
    items: list

    @property
    def count(self):
        return len(self.items)

    def remark_fragment(self):
        if not self.items:
            return '优惠券0张'
        return '优惠券%d张:%s' % (
            self.count, '|'.join(item.short_text() for item in self.items))


def parse_coupon_cards(cards, site='MX'):
    site = str(site or 'MX').strip().upper()
    if site not in COUPON_URLS:
        raise CouponError('验券站点仅支持 MX 或 US')
    items = []
    for raw in cards or []:
        text = str(raw or '')
        discount = RE_DISCOUNT.search(text)
        expiry = (RE_EXPIRY_MX.search(text) if site == 'MX'
                  else RE_EXPIRY_US.search(text))
        expiry_text = ''
        if expiry:
            expiry_text = datetime.strptime(
                expiry.group(1),
                '%d/%m/%Y' if site == 'MX' else '%m/%d/%Y'
            ).strftime('%Y-%m-%d')
        items.append(CouponItem(
            discount_percent=int(discount.group(1)) if discount else 0,
            no_minimum=bool(re.search(
                r'Sin\s+m[ií]n\.\s+de\s+compra|No\s+minimum', text, re.I)),
            expiry=expiry_text))
    return CouponSummary(items)


class CouponInspector(object):
    def __init__(self, site='MX'):
        self.site = str(site or 'MX').strip().upper()
        if self.site not in COUPON_URLS:
            raise CouponError('验券站点仅支持 MX 或 US')

    def inspect(self, page):
        page.goto(COUPON_URLS[self.site], settle_seconds=8)
        page.wait_for(
            "document.querySelector('.coupon-item-v2') || "
            "document.body.innerText.includes('No hay más contenido') || "
            "document.body.innerText.includes('No more content')",
            timeout=25)
        if '/user/auth/' in page.url:
            raise CouponError('验券页登录态失效')
        cards = page._evaluate(
            "[...document.querySelectorAll('.coupon-item-v2')]"
            ".map(e=>e.innerText)")
        if cards is None:
            raise CouponError('优惠券列表读取失败')
        return parse_coupon_cards(cards, site=self.site)
