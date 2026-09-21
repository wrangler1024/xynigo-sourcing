# -*- coding: utf-8 -*-
"""平台查找（refund_discovery）回归：合成卡片与 Fake CDP，不连接真实平台。

覆盖：轻量退款身份发现（href/点击两途径、空态区分）、环境级进度语义、
订单/退款行投影闭集、任务运输的 purpose 分派，以及「发现链路绝不触发
平台写操作」的调用面断言。
"""
import json
from unittest.mock import Mock, patch

import pytest

from purchase_tool import after_sale_claim as module
from purchase_tool import after_sale_receipts as receipts
from purchase_tool.after_sale_receipts import discover_refund_bills
from purchase_tool.operation_executor import (
    LocalOperationExecutor, OperationExecutionError,
)


def order_card(order='SYNTH001', status='Entregado'):
    return {
        'text': ('Núm. de pedido %s\n%s\n$MXN12.34\nRecibido' % (order, status)),
        'statusText': status,
        'hasEntry': True,
        'goodsImg': 'https://img.ltwebstatic.com/synthetic.jpg',
    }


class FakeDiscoveryPage:
    """URL 感知的假页面：订单列表分页 + 每订单退款列表。"""

    def __init__(self, order_pages, refund_lists):
        self.order_pages = order_pages
        self.refund_lists = refund_lists
        self.page_index = 0
        self.url = module.ORDERS_LIST_URL
        self.visits = []
        self.clicks = []
        self.point_clicks = []
        self.waits = []

    def goto(self, url, **kwargs):
        self.url = url
        self.visits.append(url)

    def _current_refund(self):
        for order, spec in self.refund_lists.items():
            if self.url.endswith('/user/order_return/return_refund_list/' + order):
                return order, spec
        return None, None

    def wait_for(self, expression, **kwargs):
        self.waits.append(expression)
        if expression == receipts._DETAIL_LINKS + '.length > 0':
            _, spec = self._current_refund()
            return bool(spec and spec.get('bills'))
        return True

    def click_selector(self, selector):
        self.clicks.append(selector)
        self.page_index += 1

    def native_click_point(self, x, y):
        order, spec = self._current_refund()
        assert spec, '点击必须发生在退款列表页'
        spec['consumed'] = spec.get('consumed', 0)
        bill = spec['bills'][spec['consumed']]
        spec['consumed'] += 1
        self.point_clicks.append(bill)
        self.url = ('%s/orders/refundLabel/%s?refund_bill_id=%s'
                    % (receipts.ORIGIN, order, bill))

    def js_evaluate(self, expression):
        if expression == module._JS_ORDER_LIST_STATE:
            return {'ready': True,
                    'empty': not self.order_pages[self.page_index],
                    'next': self.page_index < len(self.order_pages) - 1,
                    'signature': str(self.page_index)}
        scan_js = module._JS_SCAN_ORDERS % json.dumps(module.ORDER_ENTRY_KEY)
        if expression == scan_js:
            return self.order_pages[self.page_index]
        if expression == receipts._DETAIL_LINKS + '.length':
            _, spec = self._current_refund()
            return len((spec or {}).get('bills') or [])
        if expression.startswith(receipts._DISCOVERY_HREFS + '['):
            _, spec = self._current_refund()
            index = int(expression.rstrip(']').split('[')[-1])
            if not spec or not spec.get('via_href'):
                return ''
            return ('%s/orders/refundLabel/%s?refund_bill_id=%s'
                    % (receipts.ORIGIN, self._current_refund()[0],
                       spec['bills'][index]))
        if 'scrollIntoView' in expression:
            return {'x': 3, 'y': 3}
        if expression == receipts._JS_REFUND_LIST_STATE:
            _, spec = self._current_refund()
            return {'emptyText': (spec or {}).get('emptyText', '')}
        raise AssertionError('Unexpected browser operation: %r' % expression)


class TestDiscoverRefundBills:
    def test_href_entries_are_read_without_clicks(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {
            'bills': ['2390833880014851', '2390833880014852'], 'via_href': True}})
        bills = discover_refund_bills(page, 'SYNTH001')
        assert bills == ['2390833880014851', '2390833880014852']
        assert page.point_clicks == []

    def test_click_entries_follow_platform_links_and_validate_identity(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {
            'bills': ['111', '222'], 'via_href': False}})
        bills = discover_refund_bills(page, 'SYNTH001')
        assert bills == ['111', '222']
        assert page.point_clicks == ['111', '222']

    def test_confirmed_empty_state_returns_no_bills(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {
            'bills': [], 'emptyText': 'La lista se encuentra vacía'}})
        assert discover_refund_bills(page, 'SYNTH001') == []

    def test_missing_entries_without_empty_state_is_not_no_refunds(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {'bills': [], 'emptyText': ''}})
        with pytest.raises(RuntimeError, match='不能据此判定无退款'):
            discover_refund_bills(page, 'SYNTH001')

    def test_identity_mismatch_is_rejected(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {
            'bills': ['111'], 'via_href': True, 'spoof_order': True}})
        original = page.js_evaluate

        def evaluate(expression):
            if expression.startswith(receipts._DISCOVERY_HREFS + '['):
                return (receipts.ORIGIN
                        + '/orders/refundLabel/OTHERORDER?refund_bill_id=111')
            return original(expression)

        page.js_evaluate = evaluate
        with pytest.raises(RuntimeError, match='订单不一致'):
            discover_refund_bills(page, 'SYNTH001')

    def test_stop_check_interrupts_before_each_entry(self):
        stopped = iter([False, True])

        def stop_check():
            return next(stopped)

        page = FakeDiscoveryPage([[]], {'SYNTH001': {'bills': ['111', '222']}})
        with pytest.raises(RuntimeError, match='发现已停止'):
            discover_refund_bills(page, 'SYNTH001', stop_check=stop_check)

    def test_duplicate_entries_are_rejected(self):
        page = FakeDiscoveryPage([[]], {'SYNTH001': {
            'bills': ['111', '111'], 'via_href': True}})
        with pytest.raises(RuntimeError, match='重复'):
            discover_refund_bills(page, 'SYNTH001')


class TestAfterSaleDiscovery:
    def discover(self, order_pages, refund_lists, serial='SYNTHENV',
                 login=False, stop_after_orders=False):
        page = FakeDiscoveryPage(order_pages, refund_lists)
        claimer = module.AfterSaleClaimer(object())
        claimer._open_env = lambda *_: (page, False)
        claimer._login_required = lambda _: login
        claimer._scroll_orders_list = lambda _: None
        claimer._capture_screenshot = lambda *_: None
        claimer._scan_pre_info = Mock(side_effect=AssertionError(
            'Discovery must never call pre_info'))
        claimer._submit_package = Mock(side_effect=AssertionError(
            'Discovery must never submit'))
        if stop_after_orders:
            claimer._stop_event.set()
        claimer._discover_rows[serial] = {
            'environmentSerial': serial, 'status': 'queued',
            'environmentStatus': 'queued', 'orders': []}
        claimer._discover_one(serial, {}, False)
        claimer._scan_pre_info.assert_not_called()
        claimer._submit_package.assert_not_called()
        return claimer._discover_rows[serial], page

    def test_multi_order_environment_collects_bills_per_order(self):
        row, page = self.discover(
            [[order_card('SYNTH001'), order_card('SYNTH002', 'Enviado')]],
            {'SYNTH001': {'bills': ['111', '112'], 'via_href': True},
             'SYNTH002': {'bills': [], 'emptyText': 'se encuentra vacío'}})
        assert row['environmentStatus'] == 'ok'
        assert row['status'] == 'ok'
        assert [o['orderNo'] for o in row['orders']] == ['SYNTH001', 'SYNTH002']
        assert row['orders'][0]['refundBillIds'] == ['111', '112']
        assert row['orders'][0]['status'] == 'ok'
        assert row['orders'][1]['refundBillIds'] == []
        # 订单列表分页与退款列表都走只读入口
        assert module.ORDERS_LIST_URL in page.visits

    def test_order_without_orders_page_keeps_explicit_empty_row(self):
        row, _ = self.discover([[]], {})
        assert row['environmentStatus'] == 'ok'
        assert row['status'] == 'empty'
        assert row['orders'] == []
        assert row['errorSummary'] == '所有订单列表为空'

    def test_login_failure_is_terminal_with_clear_reason(self):
        row, _ = self.discover([[order_card()]], {}, login=True)
        assert row['status'] == 'login'
        assert row['environmentStatus'] == 'failed'
        assert '未登录' in row['errorSummary']

    def test_refund_list_failure_fails_environment_not_empty(self):
        row, _ = self.discover(
            [[order_card('SYNTH001')]],
            {'SYNTH001': {'bills': [], 'emptyText': ''}})
        assert row['environmentStatus'] == 'failed'
        assert row['status'] == 'fail'
        assert row['orders'][0]['status'] == 'fail'
        assert '不能据此判定无退款' in row['orders'][0]['note']

    def test_stop_marks_environment_stopped(self):
        row, _ = self.discover([[order_card()]], {}, stop_after_orders=True)
        assert row['environmentStatus'] == 'stopped'
        assert row['status'] == 'stopped'

    def test_pagination_of_order_list_is_complete(self):
        row, page = self.discover(
            [[order_card('SYNTH001')], [order_card('SYNTH002')]],
            {'SYNTH001': {'bills': ['111'], 'via_href': True},
             'SYNTH002': {'bills': ['221'], 'via_href': True}})
        assert [o['orderNo'] for o in row['orders']] == ['SYNTH001', 'SYNTH002']
        assert page.clicks == ['.j-order-list .sui-pagination__next']

    def test_observer_never_installed(self):
        with patch.object(module, 'install_observer',
                          side_effect=AssertionError('no writes')):
            self.discover([[order_card()]],
                          {'SYNTH001': {'bills': ['111'], 'via_href': True}})


class TestDiscoveryProjection:
    def env_row(self, orders=None, environment_status='ok', status='ok',
                error=None):
        return {
            'environmentSerial': '101', 'storeName': '合成环境',
            'accountName': 'buyer', 'status': status,
            'environmentStatus': environment_status,
            'errorSummary': error, 'screenshotSha256': None,
            'durationSeconds': 12,
            'orders': orders if orders is not None else [{
                'orderNo': 'SYNTH001', 'status': 'ok',
                'refundBillIds': ['111'], 'note': '',
                'deliveredAt': '04 Sep 2026', 'amount': '12.34',
                'goodsImg': 'https://img.ltwebstatic.com/s.jpg',
                'checkedAt': '2026-09-17T00:00:00+00:00'}],
        }

    def test_flatten_keeps_order_rows_with_bills_and_env_status(self):
        rows = LocalOperationExecutor._flatten_discover_rows([self.env_row()])
        assert len(rows) == 1
        assert rows[0]['refundBillIds'] == ['111']
        assert rows[0]['environmentStatus'] == 'ok'
        assert rows[0]['claimable'] is False
        assert rows[0]['packageCount'] == 0
        wire = LocalOperationExecutor._after_sale_rows(rows)[0]
        assert wire['refundBillIds'] == ['111']
        assert wire['environmentStatus'] == 'ok'
        assert wire['claimable'] is False

    def test_flatten_empty_environment_keeps_explicit_row(self):
        rows = LocalOperationExecutor._flatten_discover_rows(
            [self.env_row(orders=[], status='empty')])
        assert len(rows) == 1
        assert rows[0]['orderNo'] == ''
        assert rows[0]['environmentStatus'] == 'ok'

    def test_projection_drops_invalid_bills_and_statuses(self):
        row = self.env_row(orders=[{
            'orderNo': 'SYNTH001', 'status': 'ok',
            'refundBillIds': ['111', 'not-a-bill', '2' * 33],
            'note': '', 'deliveredAt': '', 'amount': '', 'goodsImg': '',
            'checkedAt': ''}])
        rows = LocalOperationExecutor._flatten_discover_rows([row])
        wire = LocalOperationExecutor._after_sale_rows(rows)[0]
        assert wire['refundBillIds'] == ['111']

    def test_scan_projection_does_not_carry_discovery_fields(self):
        scan_env = {
            'environmentSerial': '101', 'storeName': 's', 'accountName': 'a',
            'status': 'skip', 'errorSummary': None, 'screenshotSha256': None,
            'durationSeconds': 5,
            'orders': [{'orderNo': 'SYNTH001', 'status': 'skip',
                        'packages': [], 'claimable': False,
                        'deliveredAt': '', 'amount': '', 'goodsImg': ''}],
        }
        rows = LocalOperationExecutor._flatten_scan_rows([scan_env])
        wire = LocalOperationExecutor._after_sale_rows(rows)[0]
        assert 'refundBillIds' not in wire
        assert 'environmentStatus' not in wire


def response(body, status=200):
    return {'httpStatus': status, 'responseType': 'json',
            'contentType': 'application/json', 'body': body}


class TestDiscoveryTaskTransport:
    def rpc(self, snapshots):
        calls = []

        def executor(payload):
            calls.append(payload)
            if payload['method'] == 'GET' and payload['path'] == '/api/after-sale/progress':
                return response(snapshots.pop(0))
            return response({'running': True, 'mode': 'discover', 'total': 2})

        return calls, executor

    def run_discovery(self, snapshots, payload_overrides=None):
        calls, executor = self.rpc(snapshots)
        runner = LocalOperationExecutor(executor, poll_interval=0)
        payload = {'environmentSerials': ['101', '102'],
                   'browserMode': 'headless', 'concurrency': 2,
                   'purpose': 'refund_discovery'}
        payload.update(payload_overrides or {})
        reports = []
        outcome, code, result = runner.execute(
            'after.sale.scan.v1', payload, lambda **event: reports.append(event))
        return outcome, code, result, calls, reports

    @staticmethod
    def env_snapshot(serial, environment_status, orders):
        return {
            'environmentSerial': serial, 'storeName': 'env-' + serial,
            'accountName': '', 'status': 'ok' if environment_status == 'ok'
            else 'fail' if environment_status == 'failed' else 'running',
            'environmentStatus': environment_status,
            'errorSummary': None, 'screenshotSha256': None,
            'durationSeconds': 9, 'orders': orders,
        }

    def test_purpose_routes_to_discovery_endpoint(self):
        done = self.env_snapshot('101', 'ok', [{
            'orderNo': 'SYNTH001', 'status': 'ok', 'refundBillIds': ['111'],
            'note': '', 'deliveredAt': '', 'amount': '', 'goodsImg': '',
            'checkedAt': ''}])
        other = self.env_snapshot('102', 'ok', [])
        outcome, code, result, calls, reports = self.run_discovery([
            {'running': False, 'discoverRows': [done, other]},
        ])
        assert outcome == 'succeeded'
        assert code == 'after_sale_discovery_completed'
        posts = [c for c in calls if c['method'] == 'POST'
                 and c['path'] == '/api/after-sale/refund-discovery']
        assert posts and posts[0]['body']['serials'] == ['101', '102']
        assert result['claimableCount'] == 0
        assert result['refundCount'] == 1
        assert result['totalCount'] == 2
        assert result['progressTotal'] == 2
        assert result['progressCompleted'] == 2
        assert [r['orderNo'] for r in result['rows']] == ['SYNTH001', '']

    def test_first_order_done_does_not_complete_environment_progress(self):
        partial = self.env_snapshot('101', 'running', [{
            'orderNo': 'SYNTH001', 'status': 'ok', 'refundBillIds': ['111'],
            'note': '', 'deliveredAt': '', 'amount': '', 'goodsImg': '',
            'checkedAt': ''}])
        done = self.env_snapshot('102', 'ok', [])
        outcome, _, result, _, reports = self.run_discovery([
            {'running': True, 'discoverRows': [partial, done]},
            {'running': False, 'discoverRows': [
                self.env_snapshot('101', 'ok', partial['orders']), done]},
        ])
        assert outcome == 'succeeded'
        running_events = [r for r in reports
                          if r['phase'] == 'after_sale.discovery.running']
        assert running_events and running_events[0]['current'] == 1
        assert running_events[0]['total'] == 2
        assert result['progressCompleted'] == 2

    def test_invalid_purpose_is_rejected(self):
        _, executor = self.rpc([])
        runner = LocalOperationExecutor(executor, poll_interval=0)
        with pytest.raises(OperationExecutionError):
            runner.execute('after.sale.scan.v1', {
                'environmentSerials': ['101'], 'purpose': 'unexpected',
                'browserMode': 'headless', 'concurrency': 2}, lambda **_: None)

    def test_default_purpose_still_posts_scan_endpoint_and_rejects_empty_result(self):
        calls, executor = self.rpc([
            {'running': False, 'rows': []},
        ])
        runner = LocalOperationExecutor(executor, poll_interval=0)
        outcome, code, result = runner.execute('after.sale.scan.v1', {
            'environmentSerials': ['101'], 'browserMode': 'headless',
            'concurrency': 2}, lambda **_: None)
        assert outcome == 'failed' and code == 'after_sale_scan_failed'
        assert result['runStatus'] == 'failed'
        assert any(c['method'] == 'POST' and c['path'] == '/api/after-sale/scan'
                   for c in calls)
        assert not any(c['path'] == '/api/after-sale/refund-discovery'
                       for c in calls)

    def test_discovery_rows_outside_request_are_ignored(self):
        outsider = self.env_snapshot('999', 'ok', [{
            'orderNo': 'SYNTHOUT', 'status': 'ok', 'refundBillIds': ['999'],
            'note': '', 'deliveredAt': '', 'amount': '', 'goodsImg': '',
            'checkedAt': ''}])
        outcome, _, result, _, _ = self.run_discovery([
            {'running': False, 'discoverRows': [
                self.env_snapshot('101', 'ok', []), outsider]},
        ])
        assert result['refundCount'] == 0
        assert all(r['environmentSerial'] != '999' for r in result['rows'])
