const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
const nodes = new Map(), writes = [], calls = [], messages = [];
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', hidden:false, disabled:false, innerHTML:'', textContent:'', className:'',
    focus() { this.focused = true; }, select() { this.selected = true; }});
  return nodes.get(id);
}
const result = {site:'MX', siteName:'墨西哥站', currency:'MXN', detailCount:2, quantityCount:3,
  guideTotal:37, roundingAmount:null, warnings:[], items:[
    {index:1, sellerSku:'SYNTH-A', mainSpec:'Black', secondarySpec:'M', quantity:2, guidePrice:10.25,
      goodsId:'123456789', skuCode:'SYNTH-A', mainAttr:'', originalPrice:null, couponRate:null,
      purchaseLink:'https://www.shein.com.mx/x-p-123456789.html?skucode=SYNTH-A'},
    {index:2, sellerSku:'SYNTH-B', mainSpec:'Gray', secondarySpec:'L', quantity:1, guidePrice:16.5,
      goodsId:'123456790', skuCode:'SYNTH-B', mainAttr:'', originalPrice:33, couponRate:.5,
      purchaseLink:'https://www.shein.com.mx/x-p-123456790.html?skucode=SYNTH-B'}
  ]};
let reply = async () => result, denyClipboard = false;
const context = vm.createContext({$:node, URL,
  esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c])),
  toast: text => messages.push(text),
  navigator:{clipboard:{async writeText(text) { if (denyClipboard) throw Error('denied'); writes.push(text); }}},
  async api(path, options) { calls.push({path, body:JSON.parse(options.body)}); return reply(); }
});
vm.runInContext(html.slice(html.indexOf('let xyp2ParserResult ='), html.indexOf('let procurementImportSummary =')), context);
const run = code => vm.runInContext(code, context);
const type = value => { node('xyp2ParserInput').value = value; node('xyp2ParserInput').oninput(); };
(async () => {
  assert.equal(calls.length, 0);
  assert.equal(node('btnXyp2Parse').disabled, true);
  assert.equal(node('btnXyp2CopySheet').disabled, true);
  type('synthetic remark');
  await node('btnXyp2Parse').onclick();
  assert.deepEqual(calls[0], {path:'/api/assistant/xyp2/parse', body:{remark:'synthetic remark'}});
  assert.equal(node('xyp2ParserResult').hidden, false);
  assert.match(node('xyp2ParserSummary').innerHTML, /37.00/);
  assert.match(node('xyp2ParserDetails').innerHTML, /凑单金额：未提供/);
  await node('btnXyp2CopySheet').onclick();
  const rows = writes.at(-1).split('\n').map(row => row.split('\t'));
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], [result.items[0].purchaseLink, 'Black', 'M', '2', '10.25']);
  assert.deepEqual(rows[1].slice(-2), ['1', '16.5']);
  await node('btnXyp2CopyList').onclick();
  assert.match(writes.at(-1), /指导价合计：MXN 37.00/);
  await node('btnXyp2CopyLinks').onclick();
  assert.equal(writes.at(-1), result.items.map(item => item.purchaseLink).join('\n'));
  denyClipboard = true;
  await node('btnXyp2CopySheet').onclick();
  assert.equal(node('xyp2CopyFallback').hidden, false);
  assert.equal(node('xyp2CopyText').selected, true);
  assert.equal(node('xyp2CopyText').value.split('\n').length, 2);

  type('bad remark');
  assert.equal(node('xyp2ParserResult').hidden, true);
  assert.equal(node('btnXyp2CopySheet').disabled, true);
  assert.equal(node('xyp2CopyText').value, '');
  reply = async () => { throw new Error('XYP2 conflict'); };
  await node('btnXyp2Parse').onclick();
  assert.match(node('xyp2ParserState').textContent, /XYP2 conflict/);
  assert.equal(node('btnXyp2CopyList').disabled, true);
  const copied = writes.length;
  await node('btnXyp2CopyList').onclick();
  assert.equal(writes.length, copied);

  let complete;
  reply = () => new Promise(resolve => { complete = resolve; });
  type('old request');
  const pending = node('btnXyp2Parse').onclick();
  type('new text');
  complete(result); await pending;
  assert.equal(run('xyp2ParserResult'), null, 'stale response cannot restore old data');
  assert.equal(node('btnXyp2Parse').disabled, false);
  const pending2 = node('btnXyp2Parse').onclick();
  node('btnXyp2Clear').onclick();
  complete(result); await pending2;
  assert.equal(node('xyp2ParserInput').value, '');
  assert.equal(run('xyp2ParserResult'), null);

  const hostile = structuredClone(result);
  hostile.items[0].mainSpec = '=1+1\tred\nblue';
  hostile.items[0].sellerSku = '<img src=x onerror=alert(1)>';
  reply = async () => hostile;
  type('synthetic escaped values');
  await node('btnXyp2Parse').onclick();
  assert.ok(!node('xyp2ParserRows').innerHTML.includes('<img'));
  const sheet = run('xyp2ClipboardText(xyp2ParserResult, "sheet")');
  assert.equal(sheet.split('\n').length, 2);
  assert.equal(sheet.split('\n')[0].split('\t').length, 5);
  assert.match(sheet, /'=1\+1 red blue/);
  hostile.items[0].purchaseLink = 'javascript:alert(1)';
  type('unsafe response');
  await node('btnXyp2Parse').onclick();
  assert.equal(run('xyp2ParserResult'), null);
  console.log('PASS: decoding, three copy formats, stale-state guards, clipboard fallback, and escaped cells.');
})().catch(error => { console.error(error); process.exitCode = 1; });
