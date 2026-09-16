"""所有订单扫描回归：只用合成卡片与 Fake CDP，不连接真实平台。"""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from purchase_tool import after_sale_claim as module
from purchase_tool.operation_executor import LocalOperationExecutor


def card(order='SYNTH001', status='Enviado', entry=False):
    return {
        'text': ('Núm. de pedido %s\n%s\n$MXN12.34\nRecibido' % (order, status)),
        'statusText': status,
        'hasEntry': entry,
        'goodsImg': 'https://img.ltwebstatic.com/synthetic.jpg',
    }


class FakePage:
    def __init__(self, pages, ready=True):
        self.pages = pages
        self.index = 0
        self.ready = ready
        self.visits = []
        self.clicks = []
        self.waits = []

    def goto(self, url, **kwargs):
        self.visits.append(url)

    def wait_for(self, expression, **kwargs):
        self.waits.append(expression)
        return self.ready

    def js_evaluate(self, expression):
        if expression == module._JS_ORDER_LIST_STATE:
            return {'ready': self.ready, 'empty': not self.pages[self.index],
                    'next': self.index < len(self.pages) - 1,
                    'signature': str(self.index)}
        if expression == module._JS_SCAN_ORDERS % json.dumps(module.ORDER_ENTRY_KEY):
            return self.pages[self.index]
        raise AssertionError('Unexpected browser operation')

    def click_selector(self, selector):
        self.clicks.append(selector)
        self.index += 1


class AllOrdersScanTests(unittest.TestCase):
    def scan(self, pages, info=None, ready=True):
        page = FakePage(pages, ready)
        claimer = module.AfterSaleClaimer(object())
        claimer._open_env = lambda *_: (page, False)
        claimer._login_required = lambda _: False
        claimer._scroll_orders_list = lambda _: None
        claimer._capture_screenshot = lambda *_: None
        claimer._pre_info = Mock(side_effect=info) if callable(info) else Mock(
            return_value=info or {'eligible': [{'packageNo': 'SYNTHPKG'}]})
        claimer._submit_package = Mock(side_effect=AssertionError('Scan must never submit'))
        claimer._scan_rows['SYNTHENV'] = {'environmentSerial': 'SYNTHENV', 'status': 'queued'}
        claimer._scan_one('SYNTHENV', {}, False)
        self.assertEqual(page.visits, [module.ORDERS_LIST_URL])
        claimer._submit_package.assert_not_called()
        rows = LocalOperationExecutor._after_sale_rows(
            LocalOperationExecutor._flatten_scan_rows(claimer.snapshot()['rows']))
        return rows, claimer, page

    def test_shipped_order_is_retained_with_status_and_image(self):
        rows, claimer, _ = self.scan([[card()]])
        self.assertEqual(rows[0]['orderNo'], 'SYNTH001')
        self.assertEqual(rows[0]['status'], 'skip')
        self.assertIn('运输中', rows[0]['errorSummary'])
        self.assertNotIn('已送达', rows[0]['errorSummary'])  # Recibido 按钮不是签收状态
        self.assertEqual(rows[0]['goodsImg'], card()['goodsImg'])
        self.assertFalse(rows[0]['claimable'])
        claimer._pre_info.assert_not_called()

    def test_mixed_orders_on_all_pages_keep_independent_eligibility(self):
        rows, claimer, page = self.scan([
            [card('SYNTH001')],
            [card('SYNTH002', 'Entregado a 已交付给 04 Sep 2026 14:36:41', True),
             card('SYNTH003', 'Reembolsado')],
        ])
        self.assertEqual([r['orderNo'] for r in rows], ['SYNTH001', 'SYNTH002', 'SYNTH003'])
        self.assertEqual([r['status'] for r in rows], ['skip', 'ok', 'skip'])
        self.assertEqual([r['claimable'] for r in rows], [False, True, False])
        self.assertIsNone(rows[1]['errorSummary'])
        self.assertIn('已退款', rows[2]['errorSummary'])
        claimer._pre_info.assert_called_once_with(page, 'SYNTH002')
        self.assertEqual(page.clicks, ['.j-order-list .sui-pagination__next'])
        self.assertTrue(any('s.signature !==' in x for x in page.waits))

    def test_delivered_without_entry_does_not_imply_eligibility_or_expiry(self):
        rows, claimer, _ = self.scan([[card(status='Entregado')]])
        self.assertFalse(rows[0]['claimable'])
        self.assertIn('已送达', rows[0]['errorSummary'])
        self.assertNotIn('窗口已过', rows[0]['errorSummary'])
        claimer._pre_info.assert_not_called()

    def test_empty_requires_confirmed_empty_list(self):
        rows, _, _ = self.scan([[]])
        self.assertEqual(rows[0]['status'], 'empty')
        self.assertEqual(rows[0]['orderNo'], '')
        self.assertEqual(rows[0]['errorSummary'], '所有订单列表为空')

    def test_loading_timeout_is_failure_not_empty(self):
        rows, _, _ = self.scan([[]], ready=False)
        self.assertEqual(rows[0]['status'], 'fail')
        self.assertIn('未就绪', rows[0]['errorSummary'])

    def test_pre_info_failure_does_not_erase_other_orders(self):
        def info(_page, order):
            if order == 'SYNTH001':
                raise RuntimeError('synthetic read timeout')
            return {'eligible': [{'packageNo': 'SYNTHPKG'}]}
        rows, _, _ = self.scan([[card('SYNTH001', entry=True),
                                card('SYNTH002', entry=True)]], info)
        self.assertEqual([r['status'] for r in rows], ['fail', 'ok'])
        self.assertFalse(rows[0]['claimable'])
        self.assertTrue(rows[1]['claimable'])

    def test_empty_eligible_list_is_blocked_not_empty_order(self):
        rows, _, _ = self.scan([[card(entry=True)]], {'eligible': [], 'blocked': ['SYNTHPKG']})
        self.assertEqual(rows[0]['status'], 'blocked')
        self.assertEqual(rows[0]['orderNo'], 'SYNTH001')
        self.assertFalse(rows[0]['claimable'])

    def test_duplicate_cards_across_pages_do_not_duplicate_order(self):
        rows, _, _ = self.scan([[card()], [card(), card('SYNTH002')]])
        self.assertEqual(len(rows), 2)

    def test_unrecognised_card_fails_instead_of_silently_disappearing(self):
        rows, _, _ = self.scan([[{'text': 'unexpected card markup'}]])
        self.assertEqual(rows[0]['status'], 'fail')

    def test_next_page_timeout_does_not_return_a_complete_partial_list(self):
        page = FakePage([[card()], [card('SYNTH002')]])
        page.wait_for = Mock(side_effect=[True, True, False])
        claimer = module.AfterSaleClaimer(object())
        claimer._scroll_orders_list = lambda _: None
        with self.assertRaisesRegex(RuntimeError, '翻页未完成'):
            claimer._read_all_order_cards(page)

    def test_stopped_scan_does_not_read_more_pages_or_claim_empty(self):
        claimer = module.AfterSaleClaimer(object())
        claimer._stop_event.set()
        page = FakePage([[card()]])
        self.assertEqual(claimer._read_all_order_cards(page), [])
        self.assertFalse(page.waits)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for DOM predicate tests')
    def test_page_readiness_uses_order_empty_state_and_waits_for_loading(self):
        js = '''
const assert = require('node:assert/strict');
let selected='0', loading=false, cards=[], empty=false;
const shown={offsetWidth:1,offsetHeight:1};
const root={
  querySelector(s) {
    if (s.includes('[role=tab]')) return {getAttribute:()=>selected};
    if (s==='.sui-pagination__next') return {...shown,disabled:true,getAttribute:()=>null,
      classList:{contains:()=>true}};
    return null;
  },
  querySelectorAll(s) {
    if (s==='.order-list-loading') return loading ? [shown] : [];
    if (s==='li.list-item') return cards;
    if (s==='.c-order-search') return empty ? [{...shown,innerText:'Se encuentra vacío :-('}] : [];
    return [];
  }
};
global.document={querySelector:s=>s==='.j-order-list' ? root : null};
'''
        js += 'const read=()=>(' + module._JS_ORDER_LIST_STATE + ');\n'
        js += '''
assert.equal(read().ready,false); // 没卡片且无明确空态，不得报无订单
empty=true;
assert.equal(read().ready,true);
assert.equal(read().empty,true);
loading=true;
assert.equal(read().ready,false);
loading=false;selected='3';
assert.equal(read().ready,false); // 旧标签的卡片不能当作所有订单结果
selected='0';empty=false;
cards=[{...shown,innerText:'Núm. de pedido SYNTH001'}];
assert.equal(read().ready,true);
assert.equal(read().empty,false);
assert.equal(read().signature,'SYNTH001');
assert.equal(read().next,false);
'''
        subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True)


class ScanStatusWebTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for Web behavior tests')
    def test_nonclaimable_order_is_not_labelled_empty_including_old_executor(self):
        html = (Path(__file__).resolve().parents[1] / 'src/purchase_tool/web/index.html').read_text()
        js = html[html.index('const AS_SCAN_PILL = {'):html.index('const AS_CLAIM_PILL = {')]
        js += '''
const assert = require('node:assert/strict');
for (const status of ['skip', 'empty']) {
  assert.match(asScanPill({orderNo:'SYNTH001',status}), /暂不可申请/);
  assert.doesNotMatch(asScanPill({orderNo:'SYNTH001',status}), /无订单/);
}
assert.match(asScanPill({orderNo:'',status:'empty'}), /无订单/);
assert.match(asScanPill({orderNo:'SYNTH002',status:'ok',claimable:true}), />可申请</);
assert.match(asScanPill({orderNo:'SYNTH003',status:'fail'}), /失败/);
'''
        subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True)
