"""The account node is not proof that its asynchronous card details are ready."""
import json
import subprocess
from unittest.mock import Mock

import pytest
from purchase_tool import after_sale_receipts as receipts

URL = 'https://www.shein.com.mx/orders/refundLabel/ORDER?refund_bill_id=12345'


@pytest.fixture(autouse=True)
def confirmed_phase(monkeypatch):
    # Card-loading timing is independent of the separately tested node reader.
    monkeypatch.setattr(receipts, 'read_refund_phase', lambda *args, **kwargs: {
        'phase':'reviewing', 'phaseLabel':'审核中', 'phaseEvidence':None,
        'note':'未终态，下次回访继续跟'})


def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(receipts.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(receipts.time, 'sleep', lambda seconds: now.__setitem__(0, now[0]+seconds))
    return now


def test_waits_for_late_card_in_existing_placeholder(monkeypatch):
    now = clock(monkeypatch)
    page = Mock(url=URL)
    page.js_evaluate.side_effect = lambda code: (
        ('****1234' if now[0] >= 5 else '') if code == receipts.REFUND_ACCOUNT_JS
        else {'refundBillId':'12345','refundPath':'Cuenta original de pago','refundAccount':'','phaseText':'ready'})
    result = receipts.read_current_receipt(page, 'ORDER', lambda _: 'reviewing', {'reviewing':'审核中'})
    assert result['refundAccount'] == '****1234' and not result['detailsNote']
    assert 5 <= now[0] < 6


def test_timeout_keeps_identity_and_explains_missing_account(monkeypatch):
    now = clock(monkeypatch)
    page = Mock(url=URL)
    page.js_evaluate.side_effect = lambda code: '' if code == receipts.REFUND_ACCOUNT_JS else {
        'refundBillId':'12345','refundPath':'Cuenta original de pago'}
    result = receipts.read_current_receipt(page, 'ORDER', lambda _: '', {})
    assert now[0] == 15 and result['refundBillId'] == '12345'
    assert result['refundAccount'] == '' and '待回访补全' in result['detailsNote']


@pytest.mark.parametrize('value', ['', '****9876'])
def test_navigation_during_wait_is_rejected(monkeypatch, value):
    clock(monkeypatch)
    page = Mock(url=URL)
    def read(_):
        page.url = URL.replace('ORDER', 'OTHER')
        return value
    page.js_evaluate.side_effect = read
    with pytest.raises(RuntimeError, match='发生切换'):
        receipts.read_refund_account(page, expected_identity=('ORDER','12345'))


def test_mask_reader_checks_alt_independently_and_rejects_unmasked_values():
    program = r'''
const assert=require('node:assert/strict');
let tips=[]; const document={querySelectorAll:()=>tips};
const read=()=>READER;
tips=[{innerText:'Cuenta de Pago Original',querySelectorAll:()=>[{alt:'****1234'}]}];
assert.equal(read(),'****1234');
tips=[{innerText:'Error',querySelectorAll:()=>[]},{innerText:'•••• 5678',querySelectorAll:()=>[]}];
assert.equal(read(),'****5678');
for(const text of ['Cuenta de Pago Original','Error','1234','1234567812345678','****12']) {
 tips=[{innerText:text,querySelectorAll:()=>[]}];assert.equal(read(),'');
}
'''.replace('READER',receipts.REFUND_ACCOUNT_JS)
    result=subprocess.run(['node','-e',program],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
