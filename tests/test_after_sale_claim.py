# -*- coding: utf-8 -*-
"""售后处理：纯函数、批次编排与云端投影契约测试（不触达真实浏览器）。"""
import threading
import time
import unittest

from purchase_tool.after_sale_claim import (
    AfterSaleClaimer,
    parse_order_card,
    refund_bill_id_from_url,
)
from purchase_tool.operation_executor import (
    AFTER_SALE_TERMINAL_STATES,
    BUSINESS_TASK_TYPES,
    LocalOperationExecutor,
)

# 真实卡片文本（含沉浸式翻译追加的中文）：生产必须能解析出来
CARD_DELIVERED = (
    '29 Ago 2026 06:03:40Núm. de pedido GSH1RV329000RBM 2026 年 8 月 29 日 '
    '请求编号 GSH1RV329000RBM Entregado a 已交付给 04 Sep 2026 14:36:41 '
    '2026-09-04 14:36:41 -¿No se encuentra mi paquete? Internacional '
    'Gwen Dijie 2 Artículos $MXN116.00 Paquete entregado, no recibido '
    '包裹已送达，但并未被接收。'
)
CARD_REFUNDING = (
    '29 Ago 2026 06:29:11Núm. de pedido GSH1RV46900MLN8 Entregado a '
    '05 Sep 2026 15:19:09 已交付给 Internacional Swim Vcay 1 Artículo '
    '$MXN120.47 Procesamiento de reembolsos'
)


class _FakeHub(object):
    """最小 HubStudio 适配替身：无网络、无浏览器。"""

    def __init__(self, serials):
        self._serials = [str(s) for s in serials]
        self.started = []
        self.stopped = []

    def env_list(self):
        return [{
            'containerCode': s,
            'serialNumber': '9' + s,
            'containerName': '采购环境' + s,
            'accounts': [{'accountName': 'buyer%s@example.test' % s}],
        } for s in self._serials]

    def open_container_codes(self):
        return set()

    def browser_start(self, serial, headless=True):
        self.started.append(serial)
        return {'debuggingPort': 0}

    def browser_stop(self, serial):
        self.stopped.append(serial)


class OrderCardParsingTests(unittest.TestCase):
    def test_parses_delivered_card_with_translated_label(self):
        row = parse_order_card(CARD_DELIVERED)
        self.assertEqual(row['orderNo'], 'GSH1RV329000RBM')
        # 翻译插件把「已交付给」插在标签与日期之间，日期仍须解析出来
        self.assertEqual(row['deliveredAt'], '04 Sep 2026 14:36:41')
        self.assertEqual(row['amount'], '116.00')
        self.assertFalse(row['refundInProgress'])

    def test_flags_refund_in_progress_card(self):
        row = parse_order_card(CARD_REFUNDING)
        self.assertEqual(row['orderNo'], 'GSH1RV46900MLN8')
        self.assertEqual(row['deliveredAt'], '05 Sep 2026 15:19:09')
        self.assertTrue(row['refundInProgress'])

    def test_non_order_text_returns_none(self):
        self.assertIsNone(parse_order_card('Categorías Solo para ti'))
        self.assertIsNone(parse_order_card(''))

    def test_refund_bill_id_from_success_page_url(self):
        url = ('https://www.shein.com.mx/orders/refundLabel/GSH1RV46900MLN8'
               '?refund_bill_id_list=GSH1RV46900MLN8_2390765181147136')
        self.assertEqual(refund_bill_id_from_url(url),
                         ('GSH1RV46900MLN8', '2390765181147136'))

    def test_refund_bill_id_absent_on_list_page(self):
        self.assertIsNone(refund_bill_id_from_url(
            'https://www.shein.com.mx/user/orders/list?status_type=3'))


class TrackPhaseTests(unittest.TestCase):
    """退款跟踪的阶段判定：四种真实文案 + 终态口径。"""

    def test_flat_page_text_is_not_current_node_evidence(self):
        from purchase_tool.after_sale_claim import classify_track_phase as classify
        for text in ('Reembolsos procesados', 'Procesamiento de reembolsos de SHEIN',
                     'Solicitud de reembolso aceptada ... está en revisión',
                     'Reseña de SHEIN/Vendedor Revisión fallida'):
            self.assertEqual(classify(text), '')

    def test_only_financial_institution_processed_is_confirmed_terminal(self):
        from purchase_tool import after_sale_claim as m
        self.assertEqual(m.TRACK_TERMINAL_PHASES, ('bank_processed',))
        self.assertNotIn('shein_refunded', m.TRACK_TERMINAL_PHASES)
        self.assertNotIn('evidence_required', m.TRACK_TERMINAL_PHASES)

    def test_start_track_requires_bill_and_order(self):
        from purchase_tool.after_sale_claim import AfterSaleClaimer
        claimer = AfterSaleClaimer(_FakeHub([]))
        claimer._run_track = lambda items, headless: None
        result = claimer.start_track([
            {'environmentSerial': '4586', 'orderNo': 'GSH1', 'refundBillId': '2390833880014851'},
            {'environmentSerial': '4586', 'orderNo': 'GSH1', 'refundBillId': ''},
            {'environmentSerial': '', 'orderNo': 'GSH1', 'refundBillId': '1'},
        ])
        self.assertEqual(result['mode'], 'track')
        self.assertEqual(result['total'], 1)
        self.assertIn('trackRows', claimer.snapshot())


class OrderListReportingTests(unittest.TestCase):
    """从所有订单识别退款与运输状态，不把无入口误报为无订单。"""

    def test_refunded_marker_matches_list_card_text(self):
        from purchase_tool import after_sale_claim as m
        for text in (
            'Reembolsos procesados $MXN189.52 reembolso está siendo procesado '
            'por la institución bancaria',
            'pedido GSH1R9 Reembolsado',
        ):
            self.assertTrue(m.REFUNDED_RE.search(text), text)
        for text in ('Procesamiento de reembolsos', 'Entregado a 05 Sep 2026'):
            self.assertFalse(m.REFUNDED_RE.search(text), text)

    def test_scan_starts_from_all_orders(self):
        from purchase_tool import after_sale_claim as m
        self.assertEqual(m.ORDERS_STATUS_TYPE, 0)
        self.assertTrue(m.ORDERS_LIST_URL.endswith('/user/orders/list?status_type=0'))


class StartValidationTests(unittest.TestCase):
    def test_submit_filters_incomplete_items(self):
        claimer = AfterSaleClaimer(_FakeHub([]))
        claimer._run_claim = lambda items, headless: None  # 不真跑批次
        result = claimer.start_submit([
            {'environmentSerial': '4902', 'orderNo': 'GSH1A'},
            {'environmentSerial': '', 'orderNo': 'GSH1B'},
            {'environmentSerial': '4902'},
            'not-a-dict',
        ])
        self.assertEqual(result['total'], 1)

    def test_second_start_is_rejected_while_running(self):
        claimer = AfterSaleClaimer(_FakeHub([]))
        release = threading.Event()

        def slow(items, headless):
            release.wait(3)

        claimer._run_scan = lambda serials, headless: release.wait(3)
        first = claimer.start_scan(['11'])
        second = claimer.start_scan(['12'])
        self.assertTrue(first['running'])
        self.assertIn('error', second)
        release.set()

    def test_snapshot_exposes_both_views(self):
        claimer = AfterSaleClaimer(_FakeHub([]))
        snap = claimer.snapshot()
        self.assertEqual(snap['rows'], [])
        self.assertEqual(snap['claimRows'], [])
        self.assertFalse(snap['running'])


class ScanBatchTests(unittest.TestCase):
    def _claimer(self, serials, scan_one=None):
        hub = _FakeHub(serials)
        claimer = AfterSaleClaimer(hub)
        if scan_one is not None:
            claimer._scan_one = scan_one
        return claimer, hub

    def test_batch_marks_rows_and_clears_running(self):
        serials = ['11', '12']

        def stub(serial, env, headless):
            with claimer._lock:
                row = claimer._scan_rows[serial]
                row['status'] = 'ok'
                row['orders'] = [{'orderNo': 'GSH1X', 'claimable': True}]

        claimer, hub = self._claimer(serials, stub)
        claimer.start_scan(serials)
        deadline = time.time() + 5
        while time.time() < deadline and claimer.snapshot()['running']:
            time.sleep(0.05)
        snap = claimer.snapshot()
        self.assertFalse(snap['running'])
        self.assertEqual({r['status'] for r in snap['rows']}, {'ok'})
        self.assertEqual(len(snap['rows']), 2)

    def test_stop_marks_unfinished_rows_as_stopped(self):
        serials = ['21', '22', '23']

        def stub(serial, env, headless):
            claimer._stop_event.set()  # 第一家跑完即请求停止

        claimer, hub = self._claimer(serials, stub)
        claimer.start_scan(serials)
        deadline = time.time() + 5
        while time.time() < deadline and claimer.snapshot()['running']:
            time.sleep(0.05)
        statuses = [r['status'] for r in claimer.snapshot()['rows']]
        self.assertIn('stopped', statuses)


class ClaimBatchTests(unittest.TestCase):
    def test_claim_rows_are_initialised_and_completed(self):
        hub = _FakeHub(['4902'])
        # 关掉单间随机停顿（默认 5~15 秒，用来贴近人工节奏）
        claimer = AfterSaleClaimer(hub, order_stagger=(0.0, 0.0))
        seen = []

        def stub_claim_one(page, serial, item):
            seen.append(serial)
            claimer._publish_claim(item['orderNo'], {
                'status': 'ok', 'refundBillId': '2390765181147136',
                'refundPath': 'Cuenta original de pago',
            })

        claimer._claim_one = stub_claim_one
        claimer._open_env = lambda env, serial, headless: (object(), False)
        claimer.start_submit([
            {'environmentSerial': '4902', 'orderNo': 'GSH1A',
             'storeName': '采购环境4902'},
            {'environmentSerial': '4902', 'orderNo': 'GSH1B',
             'storeName': '采购环境4902'},
        ])
        deadline = time.time() + 5
        while time.time() < deadline and claimer.snapshot()['running']:
            time.sleep(0.05)
        snap = claimer.snapshot()
        rows = {r['orderNo']: r for r in snap['claimRows']}
        self.assertEqual(set(rows), {'GSH1A', 'GSH1B'})
        self.assertEqual(rows['GSH1A']['status'], 'ok')
        self.assertEqual(rows['GSH1B']['refundBillId'],
                         '2390765181147136')
        # 同环境两单合并到一次环境打开里跑完
        self.assertEqual(seen, ['4902', '4902'])
        self.assertEqual(len(hub.started), 0)  # 已开环境不重复 start


class RouteMethodTests(unittest.TestCase):
    """执行器用 GET 调 progress/screenshot：路由必须挂在正确的分发链里。"""

    def _method_span(self, name):
        import re as _re
        source = open('src/purchase_tool/main.py', encoding='utf-8').read()
        start = source.index('def %s(self):' % name)
        nxt = _re.search(r'\n    def ', source[start + 1:])
        return source[start:start + (nxt.start() if nxt else len(source))]

    def test_progress_and_screenshot_live_in_do_get(self):
        span = self._method_span('do_GET')
        self.assertIn("path == '/api/after-sale/progress'", span)
        self.assertIn("path == '/api/after-sale/screenshot'", span)

    def test_scan_submit_stop_live_in_do_post(self):
        span = self._method_span('do_POST')
        self.assertIn("path == '/api/after-sale/scan'", span)
        self.assertIn("path == '/api/after-sale/submit'", span)
        self.assertIn("path == '/api/after-sale/claim-environments'", span)
        self.assertIn("path == '/api/after-sale/stop'", span)
        self.assertNotIn("path == '/api/after-sale/progress'", span)
        self.assertNotIn("path == '/api/after-sale/screenshot'", span)

    def test_permission_map_covers_after_sale_routes(self):
        from purchase_tool.main import AUTH_PERMISSION_BY_PATH
        for path in ('/api/after-sale/scan', '/api/after-sale/submit',
                     '/api/after-sale/claim-environments',
                     '/api/after-sale/progress', '/api/after-sale/stop',
                     '/api/after-sale/screenshot'):
            self.assertEqual(AUTH_PERMISSION_BY_PATH.get(path),
                             'assistant.access')


class ProjectionTests(unittest.TestCase):
    """执行器 → 云端的行投影：闭集字段、可空语义、状态兜底。"""

    def test_flatten_scan_rows_emits_one_row_per_order(self):
        rows = LocalOperationExecutor._flatten_scan_rows([{
            'environmentSerial': '4902',
            'storeName': '采购环境4902',
            'accountName': 'buyer@example.test',
            'status': 'ok',
            'orders': [
                {'orderNo': 'GSH1A', 'deliveredAt': '05 Sep 2026 15:19:09',
                 'amount': '120.47', 'claimable': True,
                 'packages': [{'shippingNo': '49415946103334'}]},
                {'orderNo': 'GSH1B', 'deliveredAt': '', 'amount': '10.00',
                 'claimable': False, 'packages': []},
            ],
        }])
        self.assertEqual([r['orderNo'] for r in rows], ['GSH1A', 'GSH1B'])
        self.assertEqual(rows[0]['packageCount'], 1)
        self.assertEqual(rows[0]['trackingNo'], '49415946103334')
        self.assertFalse(rows[1]['claimable'])

    def test_flatten_scan_rows_keeps_env_row_without_orders(self):
        rows = LocalOperationExecutor._flatten_scan_rows([{
            'environmentSerial': '4901', 'status': 'skip',
            'storeName': '采购环境4901', 'errorSummary': '无订单',
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['orderNo'], '')
        self.assertEqual(rows[0]['packageCount'], 0)

    def test_claim_rows_keep_none_error_summary(self):
        rows = LocalOperationExecutor._after_sale_rows([{
            'orderNo': 'GSH1A', 'environmentSerial': '4902', 'status': 'ok',
            'errorSummary': None, 'screenshotSha256': None,
            'refundBillId': '2390765181147136',
        }])
        # 可空字段收成 "" 会被云端 422 整份拒绝，必须保持 None
        self.assertIsNone(rows[0]['errorSummary'])
        self.assertIsNone(rows[0]['screenshotSha256'])
        self.assertEqual(rows[0]['refundBillId'], '2390765181147136')

    def test_unknown_status_falls_back_to_running(self):
        rows = LocalOperationExecutor._after_sale_rows([
            {'orderNo': 'GSH1A', 'status': 'weird'}])
        self.assertEqual(rows[0]['status'], 'running')

    def test_summary_counts_skipped_separately_from_failed(self):
        summary = LocalOperationExecutor._after_sale_summary(4, [
            {'status': 'ok'}, {'status': 'blocked'},
            {'status': 'skip'}, {'status': 'fail'},
        ])
        self.assertEqual(summary['successCount'], 1)
        self.assertEqual(summary['skippedCount'], 2)
        self.assertEqual(summary['failedCount'], 1)
        self.assertEqual(summary['runStatus'], 'partial_failure')

    def test_summary_all_blocked_is_completed_not_failed(self):
        summary = LocalOperationExecutor._after_sale_summary(2, [
            {'status': 'blocked'}, {'status': 'skip'}])
        self.assertEqual(summary['failedCount'], 0)
        self.assertEqual(summary['runStatus'], 'completed')

    def test_terminal_states_cover_skip_and_blocked(self):
        self.assertTrue({'blocked', 'skip', 'empty'} <= AFTER_SALE_TERMINAL_STATES)

    def test_claim_rows_carry_design_fields(self):
        rows = LocalOperationExecutor._after_sale_rows([{
            'orderNo': 'GSH1RV90A001B2', 'environmentSerial': '5121',
            'status': 'ok', 'refundAccount': '****2281',
            'deliveredAt': '05 Sep 2026 15:19:09',
            'goodsImg': '//img.ltwebstatic.com/x.jpg',
        }])
        self.assertEqual(rows[0]['refundAccount'], '****2281')
        self.assertEqual(rows[0]['deliveredAt'], '05 Sep 2026 15:19:09')
        self.assertEqual(rows[0]['goodsImg'], '//img.ltwebstatic.com/x.jpg')

    def test_scan_row_carries_goods_img(self):
        rows = LocalOperationExecutor._flatten_scan_rows([{
            'environmentSerial': '5121', 'status': 'ok', 'orders': [{
                'orderNo': 'GSH1RV90A001B2', 'claimable': True,
                'packages': [{'shippingNo': 'JMX1', 'goodsImg': '//img.ltwebstatic.com/a.jpg'}],
            }],
        }])
        self.assertEqual(rows[0]['goodsImg'], '//img.ltwebstatic.com/a.jpg')

    def test_task_types_registered(self):
        self.assertTrue({'after.sale.scan.v1', 'after.sale.claim.v1'}
                        <= BUSINESS_TASK_TYPES)


class TrackBridgeTests(unittest.TestCase):
    """退款跟踪的桥接投影与汇总（timeline 不上行是本轮刻意的取舍）。"""

    def test_track_row_projection_drops_timeline(self):
        rows = LocalOperationExecutor._after_sale_track_rows([{
            'refundBillId': '2390833880014851', 'orderNo': 'GSH1RV21M00NV67',
            'environmentSerial': '4586', 'status': 'ok', 'phase': 'reviewing',
            'phaseLabel': '审核中', 'countdown': '23:48:25',
            'refundAccount': '****7935', 'amount': '270.22',
            'checkedAt': '2026-09-16T01:08:17+00:00',
            'timeline': '很长的平台时间轴原文' * 20,   # 必须被丢掉
            'note': '未终态，下次回访继续跟',
            'errorSummary': None, 'durationSeconds': 6,
        }])
        self.assertNotIn('timeline', rows[0])
        self.assertEqual(rows[0]['phase'], 'reviewing')
        self.assertEqual(rows[0]['refundAccount'], '****7935')
        self.assertIsNone(rows[0]['errorSummary'])

    def test_track_summary_counts_phases(self):
        summary = LocalOperationExecutor._after_sale_track_summary(3, [
            {'status': 'ok', 'phase': 'reviewing'},
            {'status': 'ok', 'phase': 'refunded'},
            {'status': 'fail', 'phase': ''},
        ])
        self.assertEqual(summary['successCount'], 2)
        self.assertEqual(summary['failedCount'], 1)
        self.assertEqual(summary['phaseCounts'], {'reviewing': 1, 'refunded': 1})
        self.assertEqual(summary['runStatus'], 'partial_failure')

    def test_track_task_type_registered(self):
        self.assertIn('after.sale.track.v1', BUSINESS_TASK_TYPES)


class _FakePage(object):
    def __init__(self, serial):
        self.serial = serial
        self.gotos = []

    def goto(self, url, dom_timeout=None, settle_seconds=None):
        self.gotos.append(url)


class ClaimEnvironmentBatchTests(unittest.TestCase):
    """按环境单遍直提：环境只开一遍，订单动态发现、无入口环境跳过。"""

    def _claimer(self, serials):
        hub = _FakeHub(serials)
        # 关掉单间随机停顿（默认 5~15 秒，用来贴近人工节奏）
        return AfterSaleClaimer(hub, order_stagger=(0.0, 0.0)), hub

    @staticmethod
    def _wait(claimer):
        deadline = time.time() + 5
        while time.time() < deadline and claimer.snapshot()['running']:
            time.sleep(0.05)

    def test_start_claim_environments_dedupes_and_cleans(self):
        claimer, _hub = self._claimer([])
        claimer._run_claim_environments = lambda serials, headless: None
        result = claimer.start_claim_environments(
            ['9002', '9001', '9002', '', None])
        self.assertEqual(result['mode'], 'claim_env')
        self.assertEqual(result['total'], 2)

    def test_snapshot_exposes_claim_env_rows(self):
        claimer, _hub = self._claimer([])
        self.assertEqual(claimer.snapshot()['claimEnvRows'], [])

    def test_env_rows_cover_outcomes(self):
        serials = ['11', '12', '13']

        def stub(serial, env, headless):
            if serial == '11':
                claimer._publish_claim('GSH1A', {'status': 'ok'})
                claimer._count_env_result('11', 'ok')
                claimer._finish_env_row('11', 'ok', time.time())
            elif serial == '12':
                claimer._finish_env_row('12', 'skip', time.time(),
                                        note='未发现丢件退款入口，环境跳过')
            else:
                claimer._finish_env_row('13', 'login', time.time(),
                                        errorSummary='买家端未登录')

        claimer, _hub = self._claimer(serials)
        claimer._claim_env_one = stub
        claimer.start_claim_environments(serials)
        self._wait(claimer)
        snap = claimer.snapshot()
        self.assertFalse(snap['running'])
        self.assertEqual(snap['mode'], 'claim_env')
        rows = {r['environmentSerial']: r for r in snap['claimEnvRows']}
        self.assertEqual(rows['11']['status'], 'ok')
        self.assertEqual(rows['11']['submittedCount'], 1)
        self.assertEqual(rows['12']['status'], 'skip')
        self.assertIn('入口', rows['12']['note'])
        self.assertEqual(rows['13']['status'], 'login')
        self.assertEqual([r['orderNo'] for r in snap['claimRows']], ['GSH1A'])

    def test_real_env_pass_skips_env_without_entry(self):
        """只有带入口的订单会进申请页；无入口环境整体跳过、不留订单行。"""
        cards = [{'text': CARD_DELIVERED, 'hasEntry': True,
                  'goodsImg': '', 'deliveredAt': '04 Sep 2026 14:36:41'},
                 {'text': CARD_REFUNDING, 'hasEntry': False}]
        claimer, _hub = self._claimer(['21', '22'])
        claimer._open_env = lambda env, serial, headless: (
            _FakePage(serial), False)
        claimer._login_required = lambda page: False
        claimer._read_all_order_cards = lambda page: (
            cards if page.serial == '21' else [])
        claimer._env_claim_item = lambda serial, env, page, card, parsed: {
            'environmentSerial': serial, 'orderNo': parsed['orderNo'],
            'storeName': '', 'packageNo': ''}
        submitted = []

        def fake_claim_one(page, serial, item):
            submitted.append(dict(item))
            claimer._publish_claim(item['orderNo'], {'status': 'ok'})

        claimer._claim_one = fake_claim_one
        claimer.start_claim_environments(['21', '22'])
        self._wait(claimer)
        snap = claimer.snapshot()
        rows = {r['environmentSerial']: r for r in snap['claimEnvRows']}
        self.assertEqual(rows['21']['status'], 'ok')
        self.assertEqual(rows['21']['entryCount'], 1)
        self.assertEqual(rows['21']['submittedCount'], 1)
        self.assertEqual(rows['22']['status'], 'skip')
        self.assertEqual(rows['22']['entryCount'], 0)
        self.assertEqual([r['orderNo'] for r in snap['claimRows']],
                         ['GSH1RV329000RBM'])
        self.assertEqual([item['environmentSerial'] for item in submitted],
                         ['21'])

    def test_env_with_only_blocked_orders_is_skipped_not_failed(self):
        claimer, _hub = self._claimer(['31'])
        claimer._open_env = lambda env, serial, headless: (
            _FakePage(serial), False)
        claimer._login_required = lambda page: False
        claimer._read_all_order_cards = lambda page: [
            {'text': CARD_DELIVERED, 'hasEntry': True, 'goodsImg': '',
             'deliveredAt': ''}]
        claimer._env_claim_item = lambda serial, env, page, card, parsed: {
            'environmentSerial': serial, 'orderNo': parsed['orderNo'],
            'storeName': '', 'packageNo': ''}

        def fake_claim_one(page, serial, item):
            claimer._publish_claim(item['orderNo'], {
                'status': 'blocked', 'errorSummary': '已无可申请包裹'})

        claimer._claim_one = fake_claim_one
        claimer.start_claim_environments(['31'])
        self._wait(claimer)
        row = claimer.snapshot()['claimEnvRows'][0]
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['blockedCount'], 1)
        self.assertEqual(row['failedCount'], 0)

    def test_stop_marks_unfinished_envs_stopped(self):
        claimer, _hub = self._claimer(['41', '42'])
        claimer._claim_env_one = lambda serial, env, headless: None
        claimer._stop_event.set()
        claimer.start_claim_environments(['41', '42'])
        self._wait(claimer)
        statuses = [r['status'] for r in claimer.snapshot()['claimEnvRows']]
        self.assertEqual(statuses, ['stopped', 'stopped'])


class BridgeClaimEnvironmentTests(unittest.TestCase):
    """云端任务 → 按环境直提：环境级快照与「跳过不算失败」的汇总口径。"""

    def _run(self, script, payload=None, cancel=None):
        rpc = _FakeRpc(script)
        reports = []
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        outcome, code, summary = executor._execute_after_sale_claim_environments(
            payload or {'runKey': 'as-env-1',
                        'environmentSerials': ['4904', '4905', '4904'],
                        'browserMode': 'visible'},
            lambda **event: reports.append(event), cancel or threading.Event())
        return rpc, reports, outcome, code, summary

    def test_posts_serial_and_projects_environment_rows(self):
        rpc, reports, outcome, code, summary = self._run([
            {'running': True, 'claimRows': [], 'claimEnvRows': [
                {'environmentSerial': '4904', 'storeName': '采购环境4904',
                 'status': 'running', 'entryCount': 2, 'submittedCount': 0,
                 'blockedCount': 0, 'failedCount': 0}]},
            {'running': False,
             'claimRows': [
                 {'orderNo': 'GSH1A', 'environmentSerial': '4904',
                  'status': 'ok', 'refundBillId': '2390765181147136'},
                 {'orderNo': 'GSH1B', 'environmentSerial': '4904',
                  'status': 'blocked', 'errorSummary': '已无可申请包裹'}],
             'claimEnvRows': [
                 {'environmentSerial': '4904', 'storeName': '采购环境4904',
                  'status': 'ok', 'entryCount': 2, 'submittedCount': 1,
                  'blockedCount': 1, 'failedCount': 0},
                 {'environmentSerial': '4905', 'storeName': '采购环境4905',
                  'status': 'skip', 'entryCount': 0, 'submittedCount': 0,
                  'blockedCount': 0, 'failedCount': 0,
                  'note': '未发现丢件退款入口，环境跳过'}]},
        ])
        posted = next(body for method, path, body in rpc.bodies
                      if method == 'POST'
                      and path == '/api/after-sale/claim-environments')
        self.assertEqual(posted['serials'], ['4904', '4905'])
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual(code, 'after_sale_completed')
        # 进度按环境计、成败计数按订单计
        self.assertEqual(summary['progressTotal'], 2)
        self.assertEqual(summary['progressCompleted'], 2)
        self.assertEqual(summary['successCount'], 1)
        self.assertEqual(summary['skippedCount'], 1)
        self.assertEqual(summary['failedCount'], 0)
        self.assertEqual(summary['runStatus'], 'completed')
        envs = {row['environmentSerial']: row
                for row in summary['environments']}
        self.assertEqual(envs['4905']['status'], 'skip')
        self.assertIn('入口', envs['4905']['note'])
        self.assertTrue(any('environments' in (event.get('snapshot') or {})
                            for event in reports))

    def test_env_payload_rejects_missing_or_oversized_serials(self):
        executor = LocalOperationExecutor(
            _FakeRpc([{'running': False, 'claimRows': []}]))
        with self.assertRaises(Exception) as ctx:
            executor._execute_after_sale_claim_environments(
                {'environmentSerials': []}, lambda **e: None, None)
        self.assertIn('缺少环境序号', str(ctx.exception))
        with self.assertRaises(Exception) as ctx:
            executor._execute_after_sale_claim_environments(
                {'environmentSerials': [str(i) for i in range(301)]},
                lambda **e: None, None)
        self.assertIn('上限', str(ctx.exception))

    def test_dispatch_routes_env_payload_to_environment_handler(self):
        """同一 claim 任务类型按报文体分流：带 environmentSerials 走直提。"""
        rpc = _FakeRpc([
            {'running': True, 'claimRows': [], 'claimEnvRows': [
                {'environmentSerial': '4904', 'status': 'running',
                 'entryCount': 0, 'submittedCount': 0, 'blockedCount': 0,
                 'failedCount': 0}]},
            {'running': False, 'claimRows': [], 'claimEnvRows': [
                {'environmentSerial': '4904', 'status': 'skip',
                 'entryCount': 0, 'submittedCount': 0, 'blockedCount': 0,
                 'failedCount': 0, 'note': '未发现丢件退款入口，环境跳过'}]},
        ])
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        outcome, code, _summary = executor.execute(
            'after.sale.claim.v1',
            {'environmentSerials': ['4904'], 'browserMode': 'visible'},
            lambda **event: None, threading.Event())
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual(code, 'after_sale_completed')
        self.assertEqual(rpc.calls[0],
                         ('POST', '/api/after-sale/claim-environments'))

    def test_env_summary_marks_partial_failure_and_uncertain(self):
        summary = LocalOperationExecutor._after_sale_environment_summary(
            2, [{'environmentSerial': '4904', 'status': 'ok'},
                {'environmentSerial': '4905', 'status': 'fail'}],
            [{'orderNo': 'A', 'status': 'ok'},
             {'orderNo': 'B', 'status': 'fail'}])
        self.assertEqual(summary['runStatus'], 'partial_failure')
        self.assertEqual(summary['failedCount'], 1)
        self.assertEqual(summary['successCount'], 1)
        # 环境尚未终态 → 整批待核对，不提前报完成
        summary = LocalOperationExecutor._after_sale_environment_summary(
            2, [{'environmentSerial': '4904', 'status': 'running'}], [])
        self.assertEqual(summary['runStatus'], 'uncertain')
        # 全部环境无入口（无订单行）也是 completed：跳过不是故障
        summary = LocalOperationExecutor._after_sale_environment_summary(
            1, [{'environmentSerial': '4904', 'status': 'skip'}], [])
        self.assertEqual(summary['runStatus'], 'completed')
        self.assertEqual(summary['successCount'], 0)

    def test_env_row_projection_clamps_and_drops_unknown_fields(self):
        rows = LocalOperationExecutor._after_sale_env_rows([{
            'environmentSerial': '4904', 'environmentId': 'C1',
            'storeName': '采购环境4904', 'accountName': 'buyer@example.test',
            'status': 'ok', 'entryCount': 2, 'submittedCount': 1,
            'blockedCount': 1, 'failedCount': 0, 'note': 'x' * 500,
            'errorSummary': None, 'durationSeconds': 12,
            'orders': [{'orderNo': 'GSH1A'}],   # 本地字段必须被丢掉
            'internal': 'secret',
        }])
        self.assertNotIn('orders', rows[0])
        self.assertNotIn('internal', rows[0])
        self.assertEqual(len(rows[0]['note']), 200)
        self.assertIsNone(rows[0]['errorSummary'])
        self.assertEqual(rows[0]['durationSeconds'], 12)
        # 未知状态回落到 running，不能把非法状态带上行
        fallback = LocalOperationExecutor._after_sale_env_rows(
            [{'environmentSerial': '1', 'status': 'made-up'}])
        self.assertEqual(fallback[0]['status'], 'running')


if __name__ == '__main__':
    unittest.main()


class _FakeRpc(object):
    """替身本地业务接口：按路径返回脚本化响应。"""

    def __init__(self, progress_script):
        self.progress_script = list(progress_script)
        self.calls = []
        self.bodies = []   # [(method, path, body)]：断言上行报文体用

    def __call__(self, request):
        path = request.get('path') or ''
        self.calls.append((request.get('method'), path))
        self.bodies.append((request.get('method'), path,
                            request.get('body')))
        if path.startswith('/api/after-sale/scan'):
            return {'httpStatus': 200, 'responseType': 'json', 'body': {}}
        if path.startswith('/api/after-sale/submit'):
            return {'httpStatus': 200, 'responseType': 'json', 'body': {}}
        if path.startswith('/api/after-sale/claim-environments'):
            return {'httpStatus': 200, 'responseType': 'json', 'body': {}}
        if path.startswith('/api/after-sale/stop'):
            return {'httpStatus': 200, 'responseType': 'json', 'body': {}}
        if path.startswith('/api/after-sale/progress'):
            snapshot = (self.progress_script.pop(0)
                        if len(self.progress_script) > 1
                        else self.progress_script[0])
            return {'httpStatus': 200, 'responseType': 'json',
                    'body': snapshot}
        return {'httpStatus': 404, 'responseType': 'json',
                'body': {'error': 'not found'}}


class BridgeScanTests(unittest.TestCase):
    """云端任务 → 本地扫描：进度上报与终态结果形状。"""

    def test_scan_returns_flat_order_rows(self):
        order = {
            'orderNo': 'GSH1RV13Y00NQUV',
            'deliveredAt': '04 Sep 2026 10:37:20',
            'amount': '108.22',
            'claimable': True,
            'packages': [{'packageNo': 'C26082901904542',
                          'shippingNo': 'JMX300959285918'}],
        }
        env_row = {
            'environmentSerial': '4904', 'storeName': 'ZH-MX-0829-079',
            'accountName': 'buyer@example.test',
        }
        rpc = _FakeRpc([
            {'running': True, 'rows': [dict(env_row, status='running',
                                            orders=[])]},
            {'running': False, 'rows': [dict(env_row, status='ok',
                                             orders=[order])]},
        ])
        reports = []
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        outcome, code, summary = executor._execute_after_sale_scan(
            {'environmentSerials': ['4904'], 'browserMode': 'visible'},
            lambda **event: reports.append(event), threading.Event())
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual(code, 'after_sale_scan_completed')
        self.assertEqual(summary['totalCount'], 1)
        self.assertEqual(summary['claimableCount'], 1)
        self.assertEqual(summary['rows'][0]['orderNo'], 'GSH1RV13Y00NQUV')
        self.assertTrue(summary['rows'][0]['claimable'])
        self.assertEqual(summary['rows'][0]['trackingNo'], 'JMX300959285918')
        # 进度阶段名与本地 /api/after-sale/scan 的调用顺序
        self.assertTrue(reports)
        self.assertEqual(rpc.calls[0], ('POST', '/api/after-sale/scan'))

    def test_scan_rejects_empty_serials(self):
        executor = LocalOperationExecutor(_FakeRpc([{'running': False,
                                                     'rows': []}]))
        with self.assertRaises(Exception) as ctx:
            executor._execute_after_sale_scan({}, lambda **e: None, None)
        self.assertIn('缺少环境序号', str(ctx.exception))


class BridgeClaimTests(unittest.TestCase):
    """云端任务 → 本地提交：终态汇总与取消下发。"""

    def test_claim_summary_counts_blocked_as_skipped(self):
        rpc = _FakeRpc([
            {'running': True, 'claimRows': []},
            {'running': False, 'claimRows': [
                {'orderNo': 'GSH1A', 'environmentSerial': '4904',
                 'status': 'ok', 'refundBillId': '2390765181147136',
                 'refundPath': 'Cuenta original de pago'},
                {'orderNo': 'GSH1B', 'environmentSerial': '4905',
                 'status': 'blocked', 'errorSummary': '已无可申请包裹'}]},
        ])
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        outcome, code, summary = executor._execute_after_sale_claim({
            'runKey': 'as-run-1',
            'items': [{'environmentSerial': '4904', 'orderNo': 'GSH1A'},
                      {'environmentSerial': '4905', 'orderNo': 'GSH1B'}],
            'browserMode': 'visible',
        }, lambda **event: None, threading.Event())
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual(code, 'after_sale_completed')
        self.assertEqual(summary['successCount'], 1)
        self.assertEqual(summary['skippedCount'], 1)
        self.assertEqual(summary['failedCount'], 0)
        self.assertEqual(summary['runStatus'], 'completed')

    def test_claim_cancel_requests_local_stop(self):
        rpc = _FakeRpc([
            {'running': True, 'claimRows': []},
            {'running': True, 'claimRows': []},
            {'running': False, 'claimRows': [
                {'orderNo': 'GSH1A', 'status': 'stopped'}]},
        ])
        cancel = threading.Event()
        cancel.set()
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        executor._execute_after_sale_claim({
            'runKey': 'as-run-2',
            'items': [{'environmentSerial': '4904', 'orderNo': 'GSH1A'}],
            'browserMode': 'visible',
        }, lambda **event: None, cancel)
        self.assertIn(('POST', '/api/after-sale/stop'), rpc.calls)

    def test_claim_items_pass_through_display_fields(self):
        """送达时间/商品图必须在桥接层重建条目时透传。

        只测 Web 的 `asSelectedItems()` 会全绿——值恰恰是在这一跳被
        「只保留四个字段」的重建丢掉的（真机 4589/4598 两单 ③ 那两列为空即此因）。
        这条盯的是「Web 发了 ≠ 值到得了」。
        """
        rpc = _FakeRpc([{'running': False, 'claimRows': []}])
        executor = LocalOperationExecutor(rpc, poll_interval=0.001,
                                          sleep_fn=lambda _s: None)
        executor._execute_after_sale_claim({
            'runKey': 'as-run-passthrough',
            'items': [{
                'environmentSerial': '4589', 'orderNo': 'GSH1RV19M00NEMB',
                'storeName': 'XG-MX-0828-134', 'packageNo': '',
                'deliveredAt': '04 Sep 2026 16:56:59',
                'goodsImg': '//img.ltwebstatic.com/v4/j/pi/x.jpg',
            }],
            'browserMode': 'visible',
        }, lambda **event: None, threading.Event())
        posted = next(body for method, path, body in rpc.bodies
                      if method == 'POST' and path == '/api/after-sale/submit')
        self.assertEqual(posted['items'][0]['deliveredAt'],
                         '04 Sep 2026 16:56:59')
        self.assertEqual(posted['items'][0]['goodsImg'],
                         '//img.ltwebstatic.com/v4/j/pi/x.jpg')

    def test_claim_rejects_item_without_order_no(self):
        executor = LocalOperationExecutor(_FakeRpc([{'running': False,
                                                     'claimRows': []}]))
        with self.assertRaises(Exception) as ctx:
            executor._execute_after_sale_claim({
                'runKey': 'as-run-3',
                'items': [{'environmentSerial': '4904', 'orderNo': ''}],
            }, lambda **e: None, None)
        self.assertIn('缺少环境序号或订单号', str(ctx.exception))
