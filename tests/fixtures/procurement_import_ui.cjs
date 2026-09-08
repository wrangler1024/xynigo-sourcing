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
    if (id === 'procurementImportTargetSheet' || id === 'procurementImportHistory') {
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
const documentEvents = {};
const savedStorage = new Map();
const toasts = [];
let preferences = {targets:[]};
let availableSheets = [{sheetId:'safe',sheetName:'合成协作表',columnCount:44}];
let deferredValidate = null;
let parseDiagnostics = null;
let valid = true, confirm = false, parseFailure = false, pollFailure = false, completed = false;
const summary = { planId: 'synthetic-plan', sourceRows: 60, orderCount: 30,
  detailCount: 60, quantityCount: 120, orderImageCount: 0, warningCount: 0, errorCount: 0,
  importBatch: 'synthetic-batch', issues: [], preview: Array.from({ length: 50 }, (_, index) => ({
    orderNo: 'SYNTH-' + index, packageNo: 'SYNTH-PKG-' + index, quantity: 2, imageIndex: index,
    mainSpec: 'Black', secondarySpec: 'M', currency: 'USD', guidePrice: 5, itemSalesAmount: 10
  })) };
const context = vm.createContext({
  $: node, document: { querySelectorAll: () => steps, addEventListener(type, fn) { documentEvents[type] = fn; } },
  authIdentity:{tenant:{id:'tenant-a'},user:{id:'user-a'}}, CLOUD_WEB_MODE:true,
  localStorage:{getItem:key => savedStorage.get(key),setItem:(key,value) => savedStorage.set(key,value)}, window: { confirm: () => confirm },
  console, setTimeout: () => 1, clearTimeout() {}, Uint8Array,
  bytesToBase64: bytes => Buffer.from(bytes).toString('base64'),
  procurementImportResourcePath: path => path,
  esc: value => String(value ?? ''), procurementMoney: value => String(value ?? ''), toast(message) { toasts.push(message); },
  async api(path, options = {}) {
    calls.push({ path, body: JSON.parse(options.body || '{}') });
    if (path.endsWith('/preferences')) {
      const body = JSON.parse(options.body || '{}');
      if (body.action === 'remove') preferences.targets = preferences.targets.filter(item => item.sheetId !== body.sheetId || item.spreadsheetUrl !== body.spreadsheetUrl);
      if (body.action === 'color') preferences.targets = preferences.targets.map(item => item.sheetId === body.sheetId && item.spreadsheetUrl === body.spreadsheetUrl ? {...item,fillOrderBackground:body.fillOrderBackground} : item);
      return JSON.parse(JSON.stringify(preferences));
    }
    if (path.endsWith('/parse')) { if (parseFailure) { const error = new Error('synthetic invalid file'); error.diagnostics = parseDiagnostics; throw error; } return summary; }
    if (path.endsWith('/target/inspect')) return { sheets: availableSheets };
    if (path.endsWith('/target/validate') && deferredValidate) return deferredValidate;
    if (path.endsWith('/target/validate')) return { valid, target: {sheetName:'合成协作表',sheetId:'safe',spreadsheetUrl:node('procurementImportTargetUrl').value}, headerCount: 44, detailCount: 60 };
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
  parseFailure = false;
  Object.assign(summary, {sourceRows: 248, totalOrderCount: 124, successOrderCount: 1,
    orderCount: 1, failedOrderCount: 123, detailCount: 2, quantityCount: 2,
    errorCount: 123, warningCount: 0, canImport: false,
    issues: Array.from({length: 123}, (_, index) => ({level:'error', code:'xyp2_invalid',
      orderNo: index ? 'SYNTH-' + index : '=SUM(1)', packageNo:'PKG-' + index,
      rowNumbers:[index + 2], field:'客服备注', message:'XYP2 格式错误'}))});
  chooseFile();
  await node('btnProcurementImportParse').onclick();
  assert.equal(node('procurementImportFileBadge').textContent, '需修正');
  assert.match(node('procurementImportValidationSummary').textContent, /通过 1 单，失败 123 单/);
  assert.equal(node('btnProcurementImportDownload').disabled, true);
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.equal(node('procurementImportIssueActions').hidden, false);
  assert.match(node('procurementImportIssueList').innerHTML, /Excel 行 2/);
  node('btnProcurementImportIssueNext').onclick();
  node('btnProcurementImportIssueNext').onclick();
  assert.match(node('procurementImportIssueList').innerHTML, /SYNTH-122/);
  const csv = run('procurementImportIssuesCsv()');
  assert.equal(csv.split('\r\n').length, 124, 'download must include every issue');
  assert.ok(csv.includes("\"'=SUM(1)\""), 'CSV must escape spreadsheet formulas');
  const writesBeforeBlockedClick = calls.filter(call => call.path.endsWith('/sheet-sync')).length;
  run('procurementImportTargetValidated = true');
  await node('btnProcurementImportSyncImages').onclick();
  assert.equal(calls.filter(call => call.path.endsWith('/sheet-sync')).length, writesBeforeBlockedClick);

  parseFailure = true;
  parseDiagnostics = {...summary, orderCount:0, successOrderCount:0, detailCount:0,
    totalOrderCount:123, failedOrderCount:123, quantityCount:0};
  chooseFile();
  await node('btnProcurementImportParse').onclick();
  assert.equal(run('procurementImportPlanId'), null);
  assert.equal(node('procurementImportStats').hidden, false);
  assert.equal(node('procurementImportIssueActions').hidden, false);
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.match(node('procurementImportValidationSummary').textContent, /通过 0 单，失败 123 单/);

  parseFailure = false;
  Object.assign(summary, {orderCount:1, successOrderCount:1, failedOrderCount:0, totalOrderCount:1,
    detailCount:2, errorCount:0, warningCount:1, canImport:true,
    issues:[{level:'warning', code:'quantity_mismatch', orderNo:'SYNTH-GOOD', packageNo:'PKG-GOOD',
      rowNumbers:[2], field:'单个产品数量', message:'数量差异'}]});
  chooseFile();
  await node('btnProcurementImportParse').onclick();
  assert.equal(node('procurementImportFileBadge').textContent, '已解析');
  assert.equal(node('btnProcurementImportDownload').disabled, false);
  node('procurementImportTargetUrl').value = 'https://tenant.feishu.cn/sheets/synthetic';
  await node('btnProcurementImportInspectTarget').onclick();
  node('procurementImportTargetSheet').value = 'safe';
  valid = true;
  await node('btnProcurementImportValidateTarget').onclick();
  assert.equal(node('btnProcurementImportSyncImages').disabled, false, 'corrected input with warnings can proceed');
  assert.equal(run('procurementImportIssues.length'), 1, 'old failures must be cleared');
  // Invalid file drops and picker selections preserve the last good plan/file.
  const oldPlan = run('procurementImportPlanId');
  assert.equal(run('acceptProcurementImportFiles([{name:"bad.csv",size:10}])'), false);
  assert.equal(run('procurementImportPlanId'), oldPlan);
  assert.equal(run('procurementImportSelectedFile.name'), 'synthetic.xlsx');
  node('procurementImportFile').files = [{name:'too-large.xlsx',size:21*1024*1024}];
  node('procurementImportFile').onchange();
  assert.equal(run('procurementImportSelectedFile.name'), 'synthetic.xlsx');
  assert.equal(run('procurementImportPlanId'), oldPlan);
  assert.equal(run('acceptProcurementImportFiles([{name:"one.xlsx",size:10},{name:"two.xlsx",size:10}])'), false);
  const dropEvent = files => ({preventDefault(){this.prevented=true;},stopPropagation(){},dataTransfer:{files,items:[],types:['Files']}});
  const goodDrop = dropEvent([file]);
  node('procurementImportFileDrop').ondragenter(goodDrop);
  assert.equal(node('procurementImportFileName').textContent, '松开以选择文件');
  node('procurementImportFileDrop').ondrop(goodDrop);
  assert.equal(goodDrop.prevented, true);
  assert.equal(run('procurementImportPlanId'), null);
  assert.equal(node('procurementImportTargetSheet').value, 'safe');
  const writesBeforeAuto = calls.filter(call => call.path.endsWith('/sheet-sync')).length;
  await node('btnProcurementImportParse').onclick();
  assert.equal(node('procurementImportTargetBadge').textContent, '校验通过');
  assert.equal(calls.filter(call => call.path.endsWith('/sheet-sync')).length, writesBeforeAuto, 'auto validation never imports');

  // Missing historical sheet must not silently select the first remaining sheet.
  availableSheets = [{sheetId:'replacement',sheetName:'合成协作表',columnCount:44}];
  chooseFile();
  await node('btnProcurementImportParse').onclick();
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);
  assert.match(node('procurementImportTargetState').textContent, /已不存在/);
  availableSheets = [{sheetId:'safe',sheetName:'已改名的工作表',columnCount:44}];

  preferences = {targets:[{spreadsheetUrl:'https://tenant.feishu.cn/sheets/synthetic',sheetId:'safe',sheetName:'旧工作表名',spreadsheetName:'合成工作簿',fillOrderBackground:false}]};
  await run('loadProcurementImportPreferences()');
  assert.equal(node('procurementImportTargetSheet').value, 'safe');
  assert.equal(node('procurementImportFillBackground').attributes['aria-checked'], 'false');
  assert.equal(run('procurementImportTargetValidated'), false);
  chooseFile();
  await node('btnProcurementImportParse').onclick();
  assert.equal(run('procurementImportTargetValidated'), true);
  node('procurementImportFillBackground').onclick();
  await run('procurementImportPreferenceQueue');
  assert.equal(preferences.targets[0].fillOrderBackground, true);
  node('procurementImportFillBackground').onclick();
  await run('procurementImportPreferenceQueue');
  assert.equal(preferences.targets[0].fillOrderBackground, false);
  await node('btnProcurementImportSyncImages').onclick();
  assert.equal(calls.filter(call => call.path.endsWith('/sheet-sync')).at(-1).body.fillOrderBackground, false);
  await new Promise(resolve => setImmediate(resolve));
  node('btnProcurementImportReset').onclick();
  assert.equal(node('procurementImportTargetUrl').value, 'https://tenant.feishu.cn/sheets/synthetic');
  assert.equal(node('procurementImportTargetSheet').value, 'safe');

  // A delayed validation response cannot authorize the next target.
  chooseFile(); await node('btnProcurementImportParse').onclick();
  let release;
  deferredValidate = new Promise(resolve => { release = resolve; });
  const staleValidation = node('btnProcurementImportValidateTarget').onclick();
  node('procurementImportTargetUrl').value = 'https://tenant.feishu.cn/sheets/changed';
  node('procurementImportTargetUrl').oninput();
  release({valid:true,target:{sheetName:'stale',sheetId:'safe'}});
  await staleValidation; deferredValidate = null;
  assert.notEqual(node('procurementImportTargetBadge').textContent, '校验中');
  assert.equal(run('procurementImportTargetValidated'), false);
  assert.equal(node('btnProcurementImportSyncImages').disabled, true);

  context.authIdentity = {tenant:{id:'tenant-a'},user:{id:'user-b'}};
  preferences = {targets:[]};
  await run('loadProcurementImportPreferences()');
  assert.equal(node('procurementImportTargetUrl').value, '');
  assert.equal(run('procurementImportSelectedFile'), null);
  assert.equal(run('procurementImportHistory.length'), 0);
  assert.equal(run('procurementImportFillBackground'), true);
  console.log('PASS: history restore/account isolation/auto validation, stale responses, optional colors, file drops and invalid-input preservation.');
  console.log('PASS: real import handlers, full counts, validation and confirmation gates, pending/running locks, polling recovery, reset and parse failures.');
})().catch(error => { console.error(error); process.exitCode = 1; });
