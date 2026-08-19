# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from purchase_tool.shein_query import (
    QueryOrchestrator, parse_detail_page, parse_list_page)


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


class QueryTimerTests(unittest.TestCase):
    def test_row_query_time_includes_date_in_mexico_timezone(self):
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

    def test_capture_tracking_uses_temp_file_and_public_metadata_only(self):
        job = QueryOrchestrator(hub=None, settle_seconds=0)
        try:
            result = job._capture_tracking(
                self.FakePage(), '1001', 'GSH1TEST', ['49350000001206'])
            self.assertEqual(result['screenshotState'], 'ok')
            self.assertEqual(result['screenshotSizeKb'], 1)
            self.assertEqual(job.screenshot_bytes('1001'), b'jpeg')
            self.assertNotIn('GSH1TEST', result['screenshotFile'])
            self.assertTrue(result['screenshotFile'].endswith('1206.jpg'))
        finally:
            job.close()

    def test_list_time_overrides_detail_fallback_without_duplicate_key(self):
        detail = {'orderTime': '2026-08-18 01:06:30'}
        info = {'orderTime': '2026-08-18 01:05:40'}
        updates = dict(detail)
        updates.update({k: v for k, v in info.items() if v})
        self.assertEqual(updates['orderTime'], '2026-08-18 01:05:40')


if __name__ == '__main__':
    unittest.main()
