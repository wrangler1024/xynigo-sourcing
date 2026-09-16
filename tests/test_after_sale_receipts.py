import json
from unittest.mock import Mock

import pytest

from purchase_tool import after_sale_claim as module
from purchase_tool.after_sale_receipts import refund_bill_id_from_url
from purchase_tool.after_sale_order_detail import parse_order_detail_facts
from purchase_tool.after_sale_display import delivery_summary
from purchase_tool.operation_executor import LocalOperationExecutor


@pytest.mark.parametrize('query', ['refund_bill_id=12345', 'refund_bill_id_list=ORDER_12345',
    'refund_bill_id=12345&refund_bill_id_list=ORDER_12345'])
def test_receipt_identity_supports_both_verified_url_forms(query):
    assert refund_bill_id_from_url('https://www.shein.com.mx/orders/refundLabel/ORDER?' + query) == ('ORDER','12345')


@pytest.mark.parametrize('url', [
    'https://example.test/orders/refundLabel/ORDER?refund_bill_id=12345',
    'https://www.shein.com.mx/other/ORDER?refund_bill_id=12345',
    'https://www.shein.com.mx/orders/refundLabel/ORDER?refund_bill_id_list=OTHER_12345',
    'https://www.shein.com.mx/orders/refundLabel/ORDER?refund_bill_id=12&refund_bill_id_list=ORDER_34',
    'https://www.shein.com.mx/orders/refundLabel/ORDER?refund_bill_id=12&refund_bill_id=34',
])
def test_receipts_reject_cross_order_host_or_conflicting_identifiers(url):
    assert refund_bill_id_from_url(url) is None


def claimer():
    c = module.AfterSaleClaimer(None)
    c._claim_rows = {'ORDER': {'orderNo':'ORDER','status':'running','environmentSerial':'ENV'}}
    c._capture_screenshot = lambda *args: None
    c._login_required = lambda _: False
    return c


def test_empty_eligibility_reports_verified_receipt_without_inventing_submission_time(monkeypatch):
    c = claimer()
    c._pre_info = lambda *args: {'eligible': []}
    def read(*args, **kwargs):
        record = {'refundBillId':'12345','phase':'reviewing','phaseLabel':'审核中',
                  'applicationTimeText':'7 Sep 2026 12:00:00','timeZone':'America/Mexico_City'}
        kwargs['on_record'](record)
        return [record]
    monkeypatch.setattr(module, 'read_refund_receipts', read)
    c._submit_package = Mock(side_effect=AssertionError('must not write'))
    page = Mock(); page.wait_for.return_value = True; page.js_evaluate.return_value = ''
    c._claim_one(page, 'ENV', {'orderNo':'ORDER'})
    row = c._claim_rows['ORDER']
    assert row['status'] == 'blocked'
    assert all(s in row['errorSummary'] for s in ('12345','审核中','7 Sep 2026 12:00:00'))
    assert '可能' not in row['errorSummary']
    assert row['refunds'][0]['source'] == 'existing'
    assert row['submittedAt'] == '' and row['refundPath'] == ''
    c._submit_package.assert_not_called()


def test_recovery_failure_keeps_partial_evidence_and_unknown_write(monkeypatch):
    c = claimer()
    def read(*args, **kwargs):
        kwargs['on_record']({'refundBillId':'12345','phaseLabel':'审核中'})
        raise RuntimeError('second detail unavailable')
    monkeypatch.setattr(module, 'read_refund_receipts', read)
    c._uncertain_claim(Mock(), 'ORDER', 'no redirect')
    row = c._claim_rows['ORDER']
    assert row['status'] == 'uncertain'
    assert row['refunds'][0]['refundBillId'] == '12345'
    assert row['refunds'][0]['source'] == 'recovered'
    assert row['submittedAt'] == ''
    summary = LocalOperationExecutor._after_sale_summary(1, [row])
    assert summary['runStatus'] == 'uncertain' and summary['uncertainCount'] == 1
    assert summary['progressCompleted'] == 1 and summary['failedCount'] == 0
    projected = LocalOperationExecutor._after_sale_rows([row])[0]
    assert projected['status'] == 'uncertain'
    assert projected['refunds'][0]['source'] == 'recovered'


def test_missing_receipt_cannot_become_ordinary_failure_after_click(monkeypatch):
    c = claimer(); c._click = lambda *args: True
    c._select_refund_path = lambda _: (True, '')
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setattr(module, 'install_observer', lambda *a: True)
    page = Mock(); page.url = 'https://www.shein.com.mx/orders/refundApplication?billno=ORDER'
    page.wait_for.side_effect = lambda js, **kw: js != module._JS_SUBMIT_SETTLED
    def evaluate(js):
        if js == module._JS_PICK_POINTS: return [{'x':1,'y':1}]
        if js == module._JS_CHECKED_PACKAGES: return ['PKG']
        return ''
    page.js_evaluate.side_effect = evaluate
    result = c._submit_package(page, 'ORDER')
    assert result['uncertain'] and not result['ok']
    assert c._claim_rows['ORDER']['status'] == 'verifying'
    assert c._claim_rows['ORDER']['_writeAttempted']
    page.click_selector.assert_called_once_with('.order-refund-apply__footer button')


def detail_html(quantity='2', total=2, second_package=False):
    packages = [{'packageNo':'P1','signed_time':'1757289600'}]
    relations = [{'package_no':'P1'}]
    if second_package:
        packages.append({'packageNo':'P2','signed_time':'0'})
        relations.append({'package_no':'P2'})
    data = {'orderInfo':{'billno':'ORDER','orderGoodsSum':total,
        'orderGoodsList':[{'billno':'ORDER','quantity':quantity,'goodsNameWithBlindBox':'Synthetic item',
            'goodsImgWithBlindBox':'//img.ltwebstatic.com/sample.jpg','sku_attrs_contact_str':'Blue / M',
            'goods_pkg_rel_list':relations}], 'order_package_info_list':packages}}
    return '<script>var gbRawData = '+json.dumps(data)+';throw Error("must never execute");</script>'


def test_same_sku_quantity_two_is_multi_and_single_package_date_is_complete():
    facts = parse_order_detail_facts(detail_html(), 'ORDER', {'1757289600':'2025-09-08 00:00:00'})
    assert facts['itemCount'] == 2 and len(facts['goodsItems']) == 1
    assert facts['goodsItems'][0]['quantity'] == 2
    assert facts['goodsItems'][0]['specification'] == 'Blue / M'
    assert delivery_summary(facts, facts)['deliveredDate'] == '2025-09-08'


def test_partial_goods_and_packages_never_enable_misleading_filters():
    facts = parse_order_detail_facts(detail_html(total=3, second_package=True), 'ORDER', {'1757289600':'2025-09-08 00:00:00'})
    assert facts['itemCount'] is None
    assert delivery_summary(facts, facts)['deliveryDateStatus'] == 'unknown'
    assert 'deliveredAt' not in facts
    with pytest.raises(ValueError, match='匹配订单'):
        parse_order_detail_facts(detail_html(), 'OTHER')
    incomplete = parse_order_detail_facts(detail_html(total=3), 'ORDER', {'1757289600':'2025-09-08 00:00:00'})
    assert delivery_summary(incomplete, incomplete)['deliveryDateStatus'] == 'unknown'


def test_missing_browser_date_mapping_does_not_use_host_timezone():
    facts = parse_order_detail_facts(detail_html(), 'ORDER')
    assert facts['itemCount'] == 2
    assert delivery_summary(facts, facts)['deliveryDateStatus'] == 'unknown'


def test_write_verification_does_not_complete_progress_early():
    from test_operation_executor import FakeRpc
    rpc = FakeRpc('/api/after-sale/progress', [
        {'running':True, 'claimRows':[{'orderNo':'ORDER','environmentSerial':'ENV','status':'verifying'}]},
        {'running':False, 'claimRows':[{'orderNo':'ORDER','environmentSerial':'ENV','status':'uncertain'}]},
    ])
    progress = []
    executor = LocalOperationExecutor(rpc, sleep_fn=lambda _: None)
    outcome, _, summary = executor.execute('after.sale.claim.v1',
        {'items':[{'environmentSerial':'ENV','orderNo':'ORDER'}]}, lambda **event: progress.append(event))
    assert outcome == 'failed' and summary['runStatus'] == 'uncertain'
    assert progress[0]['current'] == 0 and progress[-1]['current'] == 1
    assert summary['rows'][0]['status'] == 'uncertain'


def test_stop_does_not_visit_order_details(monkeypatch):
    c = claimer(); c._stop_event.set()
    page = Mock(); c._open_env = lambda *args: (page,False)
    c._scroll_orders_list = lambda _: None
    c._read_all_order_cards = lambda _: [{'text':'Núm. de pedido SYNTH001\nEnviado'}]
    detail = Mock(side_effect=AssertionError('must not navigate after stop'))
    monkeypatch.setattr(module, 'read_order_detail_facts', detail)
    c._scan_one('ENV',{},False)
    detail.assert_not_called()
