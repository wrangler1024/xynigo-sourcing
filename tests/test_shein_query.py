# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from purchase_tool.shein_query import (
    QueryOrchestrator, friendly_carrier, normalize_site, parse_detail_page,
    parse_list_page)


class SheinQueryParserTests(unittest.TestCase):
    def test_parse_standard_list_card(self):
        text = (
            'Todos los pedidos No pagado Procesando Enviado\n'
            '18 Ago 2026 00:05:40Núm. de pedido GSH1TEST\n'
            'Enviado\n$MXN123.45\nAlmacén Internacional.\n'
            'Detalles de Pedido')
        result = parse_list_page(text)
        self.assertEqual(result['orderTime'], '2026-08-18 00:05:40')
        self.assertEqual(result['status'], 'Enviado')
        self.assertEqual(result['statusCn'], '已发货')

    def test_status_does_not_have_to_end_order_card(self):
        # 1001 现场结构：状态后还有时间、仓库和包裹文案。
        text = (
            'Todos los pedidos No pagado Procesando Enviado\n'
            '18 Ago 2026 00:05:40Núm. de pedido GSH1TEST\n'
            'Enviado\n18 Ago 2026 19:03:50\n'
            'Almacén Internacional, Paquete en espera.\n'
            'Enviado\nDetalles de Pedido')
        result = parse_list_page(text)
        self.assertEqual(result['status'], 'Enviado')

    def test_header_status_is_not_used_for_order(self):
        text = (
            'Todos los pedidos No pagado Procesando Enviado\n'
            'Núm. de pedido GSH1TEST\n'
            'Almacén Internacional, Paquete en espera.\n'
            'Detalles de Pedido')
        result = parse_list_page(text)
        self.assertIsNone(result['status'])

    def test_detail_add_time_fills_missing_list_time(self):
        # 1001 对照校准：1787036740 → 墨西哥时间 2026-08-18 01:05:40。
        html = (
            '<script>{"paymentTime":"1787036790000",'
            '"addTime":1787036740,"shipping_no":"JMX123",'
            '"package_no":"PKG1"}</script>')
        result = parse_detail_page(html, 'Enviado')
        self.assertEqual(result['orderTime'], '2026-08-18 01:05:40')

    def test_detail_payment_time_is_last_resort(self):
        html = '<script>{"paymentTime":"1787036790000"}</script>'
        result = parse_detail_page(html, '')
        self.assertEqual(result['orderTime'], '2026-08-18 01:06:30')

    def test_parse_us_list_card_without_matching_navigation_tabs(self):
        text = (
            'All Orders Unpaid Orders Processing Orders Shipped Orders '
            'Return Orders Refund\n'
            'Aug 17 2026 06:29:46Order NO. GSH1USTEST\n'
            'Shipped\nAug 18 2026 16:48:57\nLoading complete\n'
            'Delivery: Aug 21-31(4-10 business days.)\n$42.99\n'
            'Track\nOrder details')
        result = parse_list_page(text, 'US')
        self.assertEqual(result['orderNo'], 'GSH1USTEST')
        self.assertEqual(result['orderTime'], '2026-08-17 06:29:46')
        self.assertEqual(result['amount'], '$42.99')
        self.assertEqual(result['status'], 'Shipped')
        self.assertEqual(result['statusCn'], '已发货')
        self.assertEqual(result['stage'], 'Loading complete')

    def test_us_detail_refund_navigation_is_not_kandan(self):
        html = '<script>{"addTime":1787036740}</script>'
        result = parse_detail_page(
            html, 'Return Orders\nRefund\nORDER DETAILS\nShipped',
            'US', utc_offset_minutes=-300)
        self.assertFalse(result['kanDan'])
        self.assertEqual(result['orderTime'], '2026-08-18 02:05:40')

    def test_us_detail_explicit_refunding_is_kandan(self):
        result = parse_detail_page('', 'Your refund is being processed', 'US')
        self.assertTrue(result['kanDan'])

    def test_us_detail_risk_verification_overrides_paid_business_state(self):
        html = ('<script>{"is_verify":"1",'
                '"sensitive_status":"no_submit"}</script>')
        text = ('Paid\nYour order is detected to be at risk and needs to be '
                'verified, please provide the supporting documents.')
        result = parse_detail_page(html, text, 'US')
        self.assertTrue(result['riskOrder'])
        self.assertIn('提交证明材料', result['riskMessage'])

    def test_us_internal_route_codes_map_to_commercial_carriers(self):
        self.assertEqual(
            friendly_carrier('AWrSPX-MIA3-HSS-PB-BBX-PJ-Na', []),
            'SpeedX')
        self.assertEqual(
            friendly_carrier('ASnGOFOCL-JFK3-HSS-PB-BBX-PJ-Na', []),
            'GOFO')

    def test_parse_us_paid_order_without_tracking(self):
        text = (
            'Aug 18 2026 14:48:06Order NO. GSH1USPAID\n'
            'Delivery: Aug 24-Sep 01\n$19.99\nView Invoice\n'
            'Repurchase\nPaid\nOrder details')
        result = parse_list_page(text, 'US')
        self.assertEqual(result['status'], 'Paid')
        self.assertEqual(result['statusCn'], '已支付/待备货')

    def test_only_mx_and_us_are_supported(self):
        self.assertEqual(normalize_site('us'), 'US')
        with self.assertRaisesRegex(ValueError, 'MX.*US'):
            normalize_site('CA')


class QueryTimerTests(unittest.TestCase):
    def test_row_query_time_includes_date_in_environment_timezone(self):
        job = QueryOrchestrator(hub=None)
        row = job._blank_row('1001')
        with patch('purchase_tool.shein_query._query_timestamp',
                   return_value='2026-08-18 21:01:02'):
            job._update(row, state='ok')
        self.assertEqual(row['time'], '2026-08-18 21:01:02')

    def test_elapsed_time_freezes_after_batch_finishes(self):
        job = QueryOrchestrator(hub=None)
        job.started_at = 100.0
        job.finished_at = 125.9
        job.running = False
        with patch('purchase_tool.shein_query.time.time', return_value=999.0):
            first = job.snapshot()['elapsedSec']
        with patch('purchase_tool.shein_query.time.time', return_value=1999.0):
            second = job.snapshot()['elapsedSec']
        self.assertEqual(first, 25)
        self.assertEqual(second, 25)

    def test_elapsed_time_advances_while_running(self):
        job = QueryOrchestrator(hub=None)
        job.started_at = 100.0
        job.running = True
        with patch('purchase_tool.shein_query.time.time', return_value=112.8):
            self.assertEqual(job.snapshot()['elapsedSec'], 12)


class TrackingScreenshotTests(unittest.TestCase):
    class FakePage(object):
        def __init__(self):
            self.url = ''

        def goto(self, url, settle_seconds=0):
            self.url = url

        def wait_selector(self, selector, timeout=0):
            return selector == '.track-steps-content'

        def capture_element_union(self, *args, **kwargs):
            return b'jpeg', 1008, 535

        def outer_html(self):
            return '<script>{"carrier_name":"SpeedX"}</script>'

    def test_capture_tracking_uses_temp_file_and_public_metadata_only(self):
        job = QueryOrchestrator(hub=None, settle_seconds=0)
        try:
            result = job._capture_tracking(
                self.FakePage(), '1001', 'GSH1TEST', ['49350000001206'])
            self.assertEqual(result['screenshotState'], 'ok')
            self.assertEqual(result['screenshotSizeKb'], 1)
            self.assertEqual(result['carrier'], 'SpeedX')
            self.assertEqual(job.screenshot_bytes('1001'), b'jpeg')
            self.assertNotIn('GSH1TEST', result['screenshotFile'])
            self.assertTrue(result['screenshotFile'].endswith('1206.jpg'))
        finally:
            job.close()

    def test_capture_tracking_uses_us_route(self):
        job = QueryOrchestrator(hub=None, settle_seconds=0)
        page = self.FakePage()
        try:
            job._capture_tracking(
                page, '1002', 'GSH1USTEST', ['TRACK123'], site='US')
            self.assertEqual(
                page.url,
                'https://us.shein.com/orders/track?billno=GSH1USTEST')
        finally:
            job.close()

    def test_list_time_overrides_detail_fallback_without_duplicate_key(self):
        detail = {'orderTime': '2026-08-18 01:06:30'}
        info = {'orderTime': '2026-08-18 01:05:40'}
        updates = dict(detail)
        updates.update({k: v for k, v in info.items() if v})
        self.assertEqual(updates['orderTime'], '2026-08-18 01:05:40')

    def test_site_mismatch_is_rejected_before_browser_start(self):
        job = QueryOrchestrator(hub=None)
        row = job._blank_row('1001', 'US')
        job._query_one(
            row, '1001', {
                '1001': {
                    'containerCode': 'fake',
                    'containerName': '采购-甲-MX-0819-001',
                }
            }, set(), 'US')
        self.assertEqual(row['state'], 'fail')
        self.assertIn('与所选 US 站不一致', row['error'])


if __name__ == '__main__':
    unittest.main()
