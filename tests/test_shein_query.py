# -*- coding: utf-8 -*-
import threading
import time
import unittest
from unittest.mock import patch

from purchase_tool.hub_api import HubApiError
from purchase_tool.shein_query import (
    QueryOrchestrator, friendly_carrier, normalize_site, parse_detail_page,
    parse_first_tracking_event, parse_list_page)


class SheinQueryParserTests(unittest.TestCase):
    def test_first_tracking_event_uses_chronological_minimum(self):
        result = parse_first_tracking_event(
            '2026-09-03 18:45:00 已到达配送中心\n'
            '2026-09-02 11:30:00 承运商已收到包裹',
            order_time='2026-09-01 09:30:00', site='MX',
            utc_offset_minutes=-360)
        self.assertEqual(result['firstTrackingTime'], '2026-09-02 11:30:00')
        self.assertEqual(result['firstTrackingLeadMinutes'], 1560)
        self.assertIn('承运商已收到包裹', result['firstTrackingSummary'])
        self.assertTrue(result['firstTrackingAt'].endswith('-06:00'))

    def test_first_tracking_event_accepts_mx_and_us_locales(self):
        mx = parse_first_tracking_event(
            '3 Septiembre 2026, 08:05 Paquete recibido', site='MX')
        us = parse_first_tracking_event(
            'September 3, 2026, 8:05 PM Package received', site='US')
        self.assertEqual(mx['firstTrackingTime'], '2026-09-03 08:05:00')
        self.assertEqual(us['firstTrackingTime'], '2026-09-03 20:05:00')

    def test_first_tracking_event_accepts_live_mx_timeline_without_year(self):
        result = parse_first_tracking_event(
            'Sep 03\n19:23\nCargando completo\n'
            'Sep 03\n18:49\nAlmacén Internacional, paquete recogido\n'
            'Sep 03\n00:00\nAlmacén Internacional, Pedido en espera de embalaje.',
            order_time='2026-09-02 20:18:27', site='MX',
            utc_offset_minutes=-360)
        self.assertEqual(result['firstTrackingTime'], '2026-09-03 00:00:00')
        self.assertEqual(result['firstTrackingLeadMinutes'], 221)
        self.assertIn('Pedido en espera de embalaje',
                      result['firstTrackingSummary'])
        self.assertTrue(result['firstTrackingAt'].endswith('-06:00'))

    def test_first_tracking_event_uses_previous_day_in_live_mx_timeline(self):
        result = parse_first_tracking_event(
            'Sep 03\n15:35\nCargando completo\n'
            'Sep 03\n05:46\nPaquete en espera de envío\n'
            'Sep 02\n23:15\nPedido en espera de embalaje.',
            order_time='2026-09-02 20:19:50', site='MX',
            utc_offset_minutes=-360)
        self.assertEqual(result['firstTrackingTime'], '2026-09-02 23:15:00')
        self.assertEqual(result['firstTrackingLeadMinutes'], 175)

    def test_first_tracking_event_infers_next_year_for_short_date(self):
        result = parse_first_tracking_event(
            'Jan 01\n00:20\nCarrier received package',
            order_time='2026-12-31 23:50:00', site='US',
            utc_offset_minutes=-300)
        self.assertEqual(result['firstTrackingTime'], '2027-01-01 00:20:00')
        self.assertEqual(result['firstTrackingLeadMinutes'], 30)

    def test_first_tracking_event_does_not_report_invalid_lead(self):
        result = parse_first_tracking_event(
            '2026-09-01 08:00:00 Carrier received package',
            order_time='2026-09-02 09:30:00', site='US')
        self.assertIsNone(result['firstTrackingLeadMinutes'])

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

        def element_inner_text(self, selector):
            if selector == '.track-steps-content':
                return '2026-08-19 09:30:00 Carrier received package'
            return ''

    def test_capture_tracking_uses_temp_file_and_public_metadata_only(self):
        job = QueryOrchestrator(hub=None, settle_seconds=0)
        try:
            result = job._capture_tracking(
                self.FakePage(), '1001', 'GSH1TEST', ['49350000001206'])
            self.assertEqual(result['screenshotState'], 'ok')
            self.assertEqual(result['screenshotSizeKb'], 1)
            self.assertEqual(result['carrier'], 'SpeedX')
            self.assertEqual(
                result['firstTrackingTime'], '2026-08-19 09:30:00')
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

    def test_batch_is_running_before_background_hub_read_completes(self):
        release = threading.Event()

        class BlockingHub(object):
            def env_list(self):
                release.wait(1)
                return []

            def open_container_codes(self):
                return set()

        job = QueryOrchestrator(BlockingHub(), settle_seconds=0)
        try:
            job.start_batch(['1001'], site='MX')
            snapshot = job.snapshot()
            self.assertTrue(snapshot['running'])
            self.assertEqual([row['serial'] for row in snapshot['rows']], ['1001'])
        finally:
            release.set()
            deadline = time.time() + 2
            while job.snapshot()['running'] and time.time() < deadline:
                time.sleep(0.01)
            job.close()

    def test_query_browser_runs_headless(self):
        class RecordingHub(object):
            def __init__(self):
                self.calls = []

            def browser_start(self, code, headless=False):
                self.calls.append((code, headless))
                return {'browser': 'ok'}

        hub = RecordingHub()
        job = QueryOrchestrator(hub)
        self.assertEqual(job._start_browser('container-1'), {'browser': 'ok'})
        self.assertEqual(hub.calls, [('container-1', True)])

    def test_query_browser_visible_mode_disables_headless_start(self):
        class RecordingHub(object):
            def __init__(self):
                self.calls = []

            def browser_start(self, code, headless=False):
                self.calls.append((code, headless))
                return {'browser': 'ok'}

        hub = RecordingHub()
        job = QueryOrchestrator(hub, concurrency=5)
        job.browser_mode = 'visible'
        self.assertEqual(job._start_browser('container-1'), {'browser': 'ok'})
        self.assertEqual(hub.calls, [('container-1', False)])
        self.assertEqual(job.snapshot()['browserMode'], 'visible')

    def test_preflight_rejects_missing_browser_core_before_batch_start(self):
        class MissingCoreHub(object):
            def __init__(self):
                self.marked = []

            def open_container_codes(self):
                return set()

            def browser_start(self, _code, headless=False):
                self.assert_headless = headless
                raise HubApiError(
                    'HubStudio 浏览器内核不存在',
                    'hubstudio_browser_core_missing', api_code=-10007)

            def mark_runtime_failure(self, code, message, **details):
                self.marked.append((code, message, details))

        hub = MissingCoreHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        env_index = {'1001': {
            'serialNumber': 1001,
            'containerCode': 'container-1',
            'containerName': 'XG-MX-0903-001',
        }}

        with self.assertRaises(HubApiError) as caught:
            job.preflight_batch(['1001'], env_index, site='MX')

        self.assertEqual(
            caught.exception.reason_code, 'hubstudio_browser_core_missing')
        self.assertTrue(hub.assert_headless)
        self.assertEqual(hub.marked[0][0], 'hubstudio_browser_core_missing')
        self.assertFalse(job.running)

    def test_preflight_starts_and_stops_one_eligible_environment(self):
        class ReadyHub(object):
            def __init__(self):
                self.calls = []

            def open_container_codes(self):
                return set()

            def browser_start(self, code, headless=False):
                self.calls.append(('start', code, headless))
                return {'debuggingPort': '9222'}

            def browser_stop(self, code):
                self.calls.append(('stop', code))

        hub = ReadyHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        result = job.preflight_batch(['1001'], {'1001': {
            'serialNumber': 1001,
            'containerCode': 'container-1',
            'containerName': 'XG-MX-0903-001',
        }}, site='MX')

        self.assertEqual(result, {'checked': True, 'debuggingPort': 9222})
        self.assertEqual(hub.calls, [
            ('start', 'container-1', True),
            ('stop', 'container-1'),
        ])

    def test_transient_local_api_disconnect_recovers_without_stopping_batch(self):
        class RecoveringHub(object):
            def __init__(self):
                self.start_calls = 0

            def browser_start(self, _code, headless=False):
                self.start_calls += 1
                if self.start_calls < 3:
                    raise HubApiError(
                        '无法连接 HubStudio Local API',
                        'hubstudio_local_api_unreachable')
                return {'debuggingPort': '9222', 'headless': headless}

        hub = RecoveringHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        with patch('purchase_tool.shein_query.time.sleep') as sleep:
            result = job._start_browser('container-1')

        self.assertEqual(result['debuggingPort'], '9222')
        self.assertEqual(hub.start_calls, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list], [2.0, 4.0])
        self.assertFalse(job.stop_event.is_set())
        self.assertEqual(job.fatal_error_code, '')

    def test_persistent_local_api_disconnect_stops_after_recovery_window(self):
        class OfflineHub(object):
            def __init__(self):
                self.start_calls = 0

            def browser_start(self, _code, headless=False):
                self.start_calls += 1
                raise HubApiError(
                    '无法连接 HubStudio Local API',
                    'hubstudio_local_api_unreachable')

        hub = OfflineHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        with patch('purchase_tool.shein_query.time.sleep') as sleep:
            with self.assertRaises(HubApiError) as caught:
                job._start_browser('container-1')

        self.assertEqual(
            caught.exception.reason_code, 'hubstudio_local_api_unreachable')
        self.assertEqual(hub.start_calls, 6)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [2.0, 4.0, 8.0, 8.0, 8.0])
        self.assertTrue(job.stop_event.is_set())
        self.assertEqual(
            job.fatal_error_code, 'hubstudio_local_api_unreachable')

    def test_authentication_failure_is_not_retried_as_transport_recovery(self):
        class AuthenticationFailureHub(object):
            def __init__(self):
                self.start_calls = 0

            def browser_start(self, _code, headless=False):
                self.start_calls += 1
                raise HubApiError(
                    'HubStudio Local API 认证失败',
                    'hubstudio_local_api_authentication_failed')

        hub = AuthenticationFailureHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        with patch('purchase_tool.shein_query.time.sleep') as sleep:
            with self.assertRaises(HubApiError) as caught:
                job._start_browser('container-1')

        self.assertEqual(
            caught.exception.reason_code,
            'hubstudio_local_api_authentication_failed')
        self.assertEqual(hub.start_calls, 1)
        sleep.assert_not_called()
        self.assertTrue(job.stop_event.is_set())

    def test_transport_disconnect_fails_fast_when_hubstudio_has_exited(self):
        class StoppedHub(object):
            def __init__(self):
                self.start_calls = 0
                self.client_running_getter = lambda: False

            def browser_start(self, _code, headless=False):
                self.start_calls += 1
                raise HubApiError(
                    '无法连接 HubStudio Local API',
                    'hubstudio_local_api_unreachable')

        hub = StoppedHub()
        job = QueryOrchestrator(hub, settle_seconds=0, env_interval=0)
        with patch('purchase_tool.shein_query.time.sleep') as sleep:
            with self.assertRaises(HubApiError) as caught:
                job._start_browser('container-1')

        self.assertEqual(
            caught.exception.reason_code, 'hubstudio_client_not_running')
        self.assertEqual(hub.start_calls, 1)
        sleep.assert_not_called()
        self.assertEqual(
            job.fatal_error_code, 'hubstudio_client_not_running')

    def test_systemic_browser_failure_stops_the_whole_parallel_batch(self):
        class MissingCoreHub(object):
            def __init__(self):
                self.start_calls = 0
                self.marked = []

            def open_container_codes(self):
                return set()

            def browser_start(self, _code, headless=False):
                self.start_calls += 1
                raise HubApiError(
                    'HubStudio 浏览器内核不存在',
                    'hubstudio_browser_core_missing', api_code=-10007)

            def mark_runtime_failure(self, code, message):
                self.marked.append((code, message))

        hub = MissingCoreHub()
        job = QueryOrchestrator(
            hub, settle_seconds=0, env_interval=0, concurrency=5)
        serials = [str(value) for value in range(1001, 1011)]
        env_index = {
            serial: {
                'serialNumber': int(serial),
                'containerCode': 'container-' + serial,
                'containerName': 'XG-MX-0903-' + serial,
            }
            for serial in serials
        }
        try:
            job.start_batch(serials, env_index, site='MX')
            deadline = time.time() + 2
            while job.snapshot()['running'] and time.time() < deadline:
                time.sleep(0.01)
            snapshot = job.snapshot()
        finally:
            job.close()

        self.assertFalse(snapshot['running'])
        self.assertEqual(hub.start_calls, 1)
        self.assertEqual(
            snapshot['fatalErrorCode'], 'hubstudio_browser_core_missing')
        self.assertTrue(all(row['state'] == 'fail'
                            for row in snapshot['rows']))
        self.assertTrue(all('批次已终止' in row['error']
                            or '内核不存在' in row['error']
                            for row in snapshot['rows']))


if __name__ == '__main__':
    unittest.main()
