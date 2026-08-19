# -*- coding: utf-8 -*-
import unittest

from purchase_tool.coupon import parse_coupon_cards


class CouponTests(unittest.TestCase):
    def test_parse_current_mexico_coupon_card(self):
        result = parse_coupon_cards([
            'NUEVO\n30%DE DESCUENTO\nSin mín. de compra\n'
            'Cupón de producto. Caduca en 17/09/2026 12:17'])
        self.assertEqual(result.count, 1)
        self.assertEqual(result.items[0].discount_percent, 30)
        self.assertTrue(result.items[0].no_minimum)
        self.assertEqual(result.items[0].expiry, '2026-09-17')
        self.assertEqual(
            result.remark_fragment(),
            '优惠券1张:30%OFF无门槛到期2026-09-17')

    def test_empty_coupon_list(self):
        self.assertEqual(
            parse_coupon_cards([]).remark_fragment(), '优惠券0张')


if __name__ == '__main__':
    unittest.main()
