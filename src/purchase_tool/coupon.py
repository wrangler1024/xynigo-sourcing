# -*- coding: utf-8 -*-
"""SHEIN 墨西哥站优惠券读取与环境备注摘要。"""
from dataclasses import dataclass
from datetime import datetime
import re


COUPON_URL = 'https://www.shein.com.mx/user/coupon'
RE_DISCOUNT = re.compile(r'(\d+)\s*%\s*DE\s*DESCUENTO', re.I)
RE_EXPIRY = re.compile(r'Caduca\s+en\s+(\d{2}/\d{2}/\d{4})(?:\s+\d{2}:\d{2})?', re.I)


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


def parse_coupon_cards(cards):
    items = []
    for raw in cards or []:
        text = str(raw or '')
        discount = RE_DISCOUNT.search(text)
        expiry = RE_EXPIRY.search(text)
        expiry_text = ''
        if expiry:
            expiry_text = datetime.strptime(
                expiry.group(1), '%d/%m/%Y').strftime('%Y-%m-%d')
        items.append(CouponItem(
            discount_percent=int(discount.group(1)) if discount else 0,
            no_minimum=bool(re.search(r'Sin\s+m[ií]n\.\s+de\s+compra', text, re.I)),
            expiry=expiry_text))
    return CouponSummary(items)


class CouponInspector(object):
    def inspect(self, page):
        page.goto(COUPON_URL, settle_seconds=8)
        page.wait_for(
            "document.querySelector('.coupon-item-v2') || "
            "document.body.innerText.includes('No hay más contenido')",
            timeout=25)
        if '/user/auth/' in page.url:
            raise CouponError('验券页登录态失效')
        cards = page._evaluate(
            "[...document.querySelectorAll('.coupon-item-v2')]"
            ".map(e=>e.innerText)")
        if cards is None:
            raise CouponError('优惠券列表读取失败')
        return parse_coupon_cards(cards)
