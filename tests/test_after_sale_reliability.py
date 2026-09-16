"""Failure-path tests use synthetic pages/RPC only; never contact a buyer account."""
from unittest.mock import Mock
import pytest
from purchase_tool.after_sale_claim import AfterSaleClaimer, classify_track_phase
from purchase_tool.operation_executor import LocalOperationExecutor
from test_operation_executor import FakeRpc


def claimer():
    c = AfterSaleClaimer(None)
    c._claim_rows = {'ORDER': {'orderNo':'ORDER', 'environmentSerial':'ENV', 'status':'queued'}}
    c._login_required = lambda p: False
    c._capture_screenshot = lambda *a: None
    return c


@pytest.mark.parametrize('second_fails', [False, True])
def test_each_accepted_package_survives_later_failure(second_fails):
    c = claimer()
    c._pre_info = lambda *a: {'eligible':[{'packageNo':'P1'},{'packageNo':'P2'}]}
    results = iter([
        {'ok':True,'packageNo':'P1','refundBillId':'B1','remaining':[{'packageNo':'P2'}]},
        {'ok':False,'reason':'synthetic failure'} if second_fails else
        {'ok':True,'packageNo':'P2','refundBillId':'B2','remaining':[]},
    ])
    c._submit_package = lambda *a: next(results)
    page = Mock(); page.wait_for.return_value = True; page.js_evaluate.return_value = ''
    c._claim_one(page, 'ENV', {'orderNo':'ORDER'})
    row = c._claim_rows['ORDER']
    assert [r['refundBillId'] for r in row['refunds']] == (['B1'] if second_fails else ['B1','B2'])
    assert row['status'] == ('fail' if second_fails else 'ok')
    assert row['refundBillId'] == ('B1' if second_fails else 'B2')


def test_post_acceptance_read_failure_keeps_refund_receipt(monkeypatch):
    from purchase_tool import after_sale_claim as m
    c = claimer(); c._click = lambda *a: True
    c._select_refund_path = lambda p: (True, '')
    def error(*args): raise RuntimeError('synthetic connection failure')
    c._pre_info = error
    monkeypatch.setattr(m.time, 'sleep', lambda _: None)
    page = Mock();page.wait_for.return_value = True
    page.url = 'https://mx.shein.com/orders/refundLabel/ORDER?refund_bill_id_list=ORDER_123456789'
    def evaluate(code):
        if code == m._JS_PICK_POINTS: return [{'x':1,'y':2}]
        if code == m._JS_CHECKED_PACKAGES: return ['P1']
        return ''
    page.js_evaluate.side_effect = evaluate
    result = c._submit_package(page, 'ORDER')
    assert result['ok'] and result['verificationError']
    assert c._claim_rows['ORDER']['refunds'][0]['refundBillId'] == '123456789'


def test_unknown_tracking_page_is_failed_not_submitted():
    c = claimer();c._track_rows = {'B1':{'refundBillId':'B1'}}
    page = Mock();page.js_evaluate.return_value = {'timeline':'Service unavailable','fullText':'Service unavailable'}
    c._track_one(page, {'orderNo':'ORDER','refundBillId':'B1'})
    assert c._track_rows['B1']['status'] == 'fail'
    assert not c._track_rows['B1'].get('phase')
    assert classify_track_phase('Service unavailable') == ''


def test_submit_qualification_uses_strict_checker():
    c = claimer();c._scan_pre_info = Mock(side_effect=RuntimeError('business error'))
    with pytest.raises(RuntimeError, match='business error'):
        c._pre_info(Mock(), 'ORDER')


@pytest.mark.parametrize('mode,key', [('claim','claimRows'),('track','trackRows')])
def test_final_receipt_keeps_rows_when_all_progress_uploads_fail(mode,key):
    row = {'orderNo':'ORDER','environmentSerial':'ENV','refundBillId':'B1','status':'ok',
           'refunds':[{'packageNo':'P1','refundBillId':'B1'}]}
    rpc = FakeRpc('/api/after-sale/progress', [{'running':False, key:[row]}])
    executor = LocalOperationExecutor(rpc, sleep_fn=lambda _: None)
    def offline(**kw): raise RuntimeError('offline')
    outcome, _, summary = executor.execute('after.sale.'+mode+'.v1',
        {'items':[{'environmentSerial':'ENV','orderNo':'ORDER','refundBillId':'B1'}]}, offline)
    assert outcome == 'succeeded'
    assert summary['rows'][0]['refundBillId'] == 'B1'
    if mode == 'claim': assert summary['rows'][0]['refunds'][0]['refundBillId'] == 'B1'


def test_real_web_task_lifecycle_and_input_validation():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node',str(root/'tests/fixtures/after_sale_reliability_ui.cjs')],
                            cwd=root,text=True,capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_tracking_full_text_can_advance_truncated_submitted_timeline():
    c = claimer(); c._track_rows={'B1':{'refundBillId':'B1'}}
    c._read_refund_account=lambda *a,**k: ''
    page=Mock();page.js_evaluate.return_value={
        'timeline':'Solicitud de reembolso aceptada',
        'fullText':'Solicitud de reembolso aceptada ... está en revisión'}
    c._track_one(page,{'orderNo':'ORDER','refundBillId':'B1'})
    assert c._track_rows['B1']['phase']=='reviewing'


@pytest.mark.parametrize('method', ['_after_sale_summary', '_after_sale_track_summary'])
def test_incomplete_final_rows_are_uncertain(method):
    summary = getattr(LocalOperationExecutor, method)(2, [{'status':'ok'}])
    assert summary['runStatus'] == 'uncertain'
