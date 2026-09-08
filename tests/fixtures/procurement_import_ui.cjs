const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
const nodes = new Map();
function node(id) {
  if (nodes.has(id)) return nodes.get(id);
  let value = '', inner = '';
  const result = { id, textContent: '', className: '', disabled: false, hidden: false,
    files: [], options: [], dataset: {}, lastChild: { textContent: '' }, attributes: {},
    children: new Map(), style: { setProperty() {} }, offsetWidth: 1,
    setAttribute(key, val) { this.attributes[key] = val; },
    removeAttribute(key) { delete this.attributes[key]; },
    replaceChildren() { inner = ''; this.children.clear(); },
    querySelectorAll() { return []; },
    querySelector(selector) {
      if (!this.children.has(selector)) this.children.set(selector, node(id + '/' + selector));
      return this.children.get(selector);
    }, closest() { return this; }, click() { if (!this.disabled) this.onclick?.(); }
  };
  Object.defineProperty(result, 'value', { get: () => value, set: v => {
    value = v;
    if (id === 'procurementImportFile' && v === '') result.files = [];
  } });
  Object.defineProperty(result, 'innerHTML', { get: () => inner, set: v => {
    inner = v;
    if (id === 'procurementImportTargetSheet') {
      result.options = [...v.matchAll(/<option value="([^"]*)"/g)].map(m => ({ value: m[1] }));
      value = result.options[0]?.value || '';
    }
  } });
  result.classList = {
    contains: key => result.className.split(/\s+/).includes(key),
    add: (...keys) => { result.className = [...new Set([...result.className.split(/\s+/), ...keys])].join(' '); },
    remove: (...keys) => { result.className = result.className.split(/\s+/).filter(key => !keys.includes(key)).join(' '); },
    toggle(key, enabled) { if (enabled) this.add(key); else this.remove(key); }
  };
  nodes.set(id, result);
  return result;
}
const steps = [node('step0'), node('step1'), node('step2')];
const calls = [];
let valid = true, confirm = false, parseFailure = false, pollFailure = false, completed = false;
const summary = { planId: 'synthetic-plan', sourceRows: 60, orderCount: 30,
  detailCount: 60, quantityCount: 120, orderImageCount: 0, warningCount: 0, errorCount: 0,
  importBatch: 'synthetic-batch', issues: [], preview: Array.from({ length: 50 }, (_, index) => ({
    orderNo: 'SYNTH-' + index, packageNo: 'SYNTH-PKG-' + index, quantity: 2, imageIndex: index,
    mainSpec: 'Black', secondarySpec: 'M', currency: 'USD', guidePrice: 5, itemSalesAmount: 10
  })) };
const context = vm.createContext({
  $: node, document: { querySelectorAll: () => steps }, window: { confirm: () => confirm },
  console, setTimeout: () => 1, clearTimeout() {}, Uint8Array,
  bytesToBase64: bytes => Buffer.from(bytes).toString('base64'),
  procurementImportResourcePath: path => path,
  esc: value => String(value ?? ''), procurementMoney: value => String(value ?? ''), toast() {},
  async api(path, options = {}) {
    calls.push({ path, body: JSON.parse(options.body || '{}') });
    if (path.endsWith('/parse')) { if (parseFailure) throw new Error('synthetic invalid file'); return summary; }
    if (path.endsWith('/target/inspect')) return { sheets: [{ sheetId: 'safe', sheetName: '合成协作表', columnCount: 44 }] };
    if (path.endsWith('/target/validate')) return { valid, target: { sheetName: '合成协作表' }, headerCount: 44, detailCount: 60 };
    if (path.includes('/sheet-sync/status')) {
      if (pollFailure) throw new Error('synthetic network failure');
      return { state: completed ? 'completed' : 'writing_rows', rowsTotal: 60, rowsWritten: completed ? 60 : 5 };
    }
    if (path.endsWith('/sheet-sync')) return { jobId: 'synthetic-job', state: 'queued' };
    throw new Error('Unexpected request: ' + path);
  }
});
const section = html.slice(html.indexOf('let procurementImportSummary ='), html.indexOf('function buyerStatusBadge'));
vm.runInContext('let procurementImportPlanId = null, procurementImportTargetValidated = false, procurementImportSyncTimer = null;\n' + section, context);
const run = source => vm.runInContext(source, context);
const file = { name: 'synthetic.xlsx', size: 2048, arrayBuffer: async () => new Uint8Array([1, 2]).buffer };
const chooseFile = () => { node('procurementImportFile').files = [file]; node('procurementImportFile').onchange(); };

(async () => {
  run('initializeProcurementImportLayout()');
  assert.equal(calls.length, 0, 'initialization must not parse or write sample data');
  assert.equal(node('procurementImportFileBadge').textContent, '待解析');
  assert.equal(steps[0].attributes['aria-current'], 'step');
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.equal(run('procurementImportQuantityCount({detailCount:60,preview:Array(50).fill({quantity:2})})'), null);
  assert.equal(run('procurementImportQuantityCount({detailCount:2,preview:[{quantity:2},{quantity:3}]})'), 5);
  chooseFile();
  const parsing = node('btnProcurementImportParse').onclick();
  assert.equal(node('btnProcurementImportChooseFile').disabled, true);
  node('btnProcurementImportReset').onclick();
  assert.equal(node('procurementImportFile').files.length, 1, 'reset is blocked during parsing');
  await parsing;
  assert.equal(node('procurementImportQuantity').textContent, '120');
  assert.equal(node('procurementImportPreviewCount').textContent, '60 条明细 · 预览前 50 条');
  assert.equal(node('procurementImportFileBadge').textContent, '已解析');
  assert.equal(steps[1].attributes['aria-current'], 'step');
  node('procurementImportTargetUrl').value = 'https://tenant.feishu.cn/sheets/synthetic';
  node('procurementImportTargetUrl').oninput();
  await node('btnProcurementImportInspectTarget').onclick();
  node('procurementImportTargetSheet').value = 'safe';
  node('procurementImportTargetSheet').onchange();
  valid = false;
  await node('btnProcurementImportValidateTarget').onclick();
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.equal(node('procurementImportTargetBadge').textContent, '需处理');
  valid = true;
  await node('btnProcurementImportValidateTarget').onclick();
  assert.equal(steps[2].attributes['aria-current'], 'step');
  assert.equal(node('btnProcurementImportSyncImages').disabled, false);
  await node('btnProcurementImportSyncImages').onclick();
  assert.equal(calls.filter(call => call.path.endsWith('/sheet-sync')).length, 0, 'cancel must not write');
  confirm = true;
  await node('btnProcurementImportSyncImages').onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.find(call => call.path.endsWith('/sheet-sync')).body.confirmWrite, true);
  assert.equal(node('procurementImportTargetUrl').disabled, true);
  assert.equal(node('btnProcurementImportReset').disabled, true);
  pollFailure = true;
  await run('pollProcurementImportSync("synthetic-job")');
  assert.equal(node('btnProcurementImportSyncImages').disabled, false, 'poll failure must allow safe retry');
  assert.equal(node('procurementImportTargetUrl').disabled, true, 'unknown job state must keep its target');
  pollFailure = false; completed = true;
  await run('pollProcurementImportSync("synthetic-job")');
  assert.equal(node('procurementImportFooterSummary').textContent, '本批导入已完成');
  assert.equal(node('btnProcurementImportReset').disabled, false);
  valid = false;
  await node('btnProcurementImportValidateTarget').onclick();
  assert.equal(steps[1].attributes['aria-current'], 'step', 'revalidation must clear stale completion');
  assert.notEqual(node('procurementImportFooterSummary').textContent, '本批导入已完成');
  valid = true;
  await node('btnProcurementImportValidateTarget').onclick();
  run('renderProcurementImportSyncStatus({state:"completed", rowsWritten:60})');
  node('procurementImportTargetSheet').onchange();
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.equal(steps[1].attributes['aria-current'], 'step', 'changed target invalidates completion');
  node('btnProcurementImportReset').onclick();
  assert.equal(node('procurementImportFile').files.length, 0);
  assert.equal(node('procurementImportQuantity').textContent, '0');
  chooseFile(); parseFailure = true;
  await node('btnProcurementImportParse').onclick();
  assert.equal(node('procurementImportFileBadge').textContent, '解析失败');
  assert.equal(node('procurementImportState').hidden, false);
  assert.equal(node('procurementImportMatchNotice').hidden, true);
  assert.equal(node('btnProcurementImportParse').disabled, false);
  console.log('PASS: real import handlers, full counts, validation and confirmation gates, pending/running locks, polling recovery, reset and parse failures.');
})().catch(error => { console.error(error); process.exitCode = 1; });
