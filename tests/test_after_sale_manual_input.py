"""Manual input examples must parse without losing orders or refund bills."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_manual_inputs_accept_chinese_english_and_mixed_separators():
    html = (Path(__file__).resolve().parents[1] / 'src/purchase_tool/web/index.html').read_text()
    functions = []
    for name in ['asParseDirectOrders', 'asParseManualBills']:
        start = html.index('function ' + name + '(')
        functions.append(html[start:html.index('\n}', start) + 2])
    checks = r'''
const assert = require('node:assert/strict');
for (const separator of [';', '；', '\n', ';；\n']) {
  const orders = ['ENV1 ORDER1', 'ENV2 ORDER2', 'ENV3 ORDER3'];
  const parsed = asParseDirectOrders(separator + orders.join(separator) + separator);
  assert.deepEqual(parsed.items.map(r => [r.environmentSerial, r.orderNo]),
    [['ENV1','ORDER1'], ['ENV2','ORDER2'], ['ENV3','ORDER3']]);
  assert.deepEqual([parsed.invalid, parsed.duplicates, parsed.trimmed], [[],[],[]]);
  const bills = asParseManualBills(orders.map((r,i) => r+' BILL'+(i+1)).join(separator));
  assert.deepEqual(bills.items.map(r => [r.environmentSerial,r.orderNo,r.refundBillId]),
    [['ENV1','ORDER1','BILL1'], ['ENV2','ORDER2','BILL2'], ['ENV3','ORDER3','BILL3']]);
}
assert.deepEqual(asParseManualBills('ENV1 ORDER1 BILL1；BAD').invalid, ['BAD']);
const mixed = asParseDirectOrders('ENV1 ORDER1；ENV2 ORDER2;ENV3 ORDER3\nENV1 ORDER1；BAD');
assert.equal(mixed.items.length, 3);
assert.deepEqual(mixed.duplicates, ['ENV1 ORDER1']);
assert.deepEqual(mixed.invalid, ['BAD']);
assert.deepEqual(asParseManualBills('ENV1 ORDER1 BILL1；BAD;ENV2 ORDER2 BILL2\nENV3 ORDER3 BILL3')
  .items.map(r => r.refundBillId), ['BILL1','BILL2','BILL3']);
assert.equal(asParseDirectOrders('；;\n').items.length, 0);
assert.deepEqual(asParseManualBills('；;\n'), {items:[], invalid:[], duplicates:[]});
'''
    result = subprocess.run(['node', '-e', '\n'.join(functions) + checks],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
