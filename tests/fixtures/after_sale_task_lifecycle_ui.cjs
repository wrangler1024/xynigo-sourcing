// 售后任务生命周期回归：真实 asSubmitItems / asPoll / asCreateTask /
// asRequireRuntimeControls / asScan 在受控 Promise 调度下验证
// 「新批次创建与旧批次轮询交错」的归属与恢复语义。
// 全部数据合成，无网络、无真实平台写入。
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');

const EXEC_CAPS = ['after.sale.runtime-controls.v1', 'after.sale.reliable-results.v1',
  'after.sale.receipt-recovery.v1', 'after.sale.claim-evidence.v1', 'after.sale.phase-evidence.v1'];

function freshState() {
  return {
    mode: '', type: 'refund', scanTaskId: null, runId: null, runLabel: '',
    orderView: 'pending', claimFilter: 'all', submissions: new Map(), detailKey: '',
    trackTaskId: null, timer: null, running: false,
    envSerials: null, envMode: false, environments: [],
    discoveryTaskId: null, discoverySerials: [], discoveryIntent: null,
    starting: false, pendingCreates: {}, polling: false, claimItems: [], trackItems: [],
    rows: [], selected: new Set(), claimRows: [], stopRequested: false,
    pollErrors: 0, etaBase: null, startedAt: null, endedAt: null, progressSnapshot: null, status: '',
    taskEpoch: 0
  };
}

function defer() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return {promise, resolve, reject};
}
const tick = () => new Promise(r => setImmediate(r));
async function settle(n = 6) { for (let i = 0; i < n; i++) await tick(); }

const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {dataset: {}, disabled: false, hidden: false,
    value: id === 'asSerials' ? '900001 900002' : '', innerHTML: '', textContent: '', style: {},
    tabIndex: 0, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    scrollIntoView: () => {}});
  return nodes.get(id);
};

const storageData = new Map();
const localStorageStub = {
  getItem: k => (storageData.has(k) ? storageData.get(k) : null),
  setItem: (k, v) => storageData.set(k, String(v)),
  removeItem: k => storageData.delete(k),
};

const getCalls = [], postCalls = [], phases = [], progressCalls = [], finishCalls = [];
let router = async () => ({data: {}});
let executorResponder = async () => ({id: 'EXEC', capabilities: EXEC_CAPS});

const ctx = vm.createContext({
  TextEncoder, console, encodeURIComponent,
  AS_STATE: freshState(), AS_TYPES: [], $: node,
  AS_DISCOVERY_KEY: 'afterSaleDiscoveryTask',
  localStorage: localStorageStub,
  crypto: require('node:crypto').webcrypto,
  localExecutorDevices: [], renderLocalExecutorDevices: () => {},
  cloudFormalExecutor: async capability => executorResponder(capability),
  cloudFetchJson: async (path, options) => {
    if (options && options.method) {
      postCalls.push({path, body: options.body ? JSON.parse(options.body) : null});
    } else {
      getCalls.push(path);
    }
    return router(path, options);
  },
  asSetPhase: (title, text) => { phases.push({title, text}); node('asPhaseTitle').textContent = title; },
  asProgress: data => { progressCalls.push(data); },
  asRuntimeOptions: () => ({browserMode: 'headless', concurrency: 2}),
  asSyncRuntimeControls: () => {},
  asSaveList: () => {}, asSyncScanExportButton: () => {},
  asSyncRetryButton: () => {},
  asRenderClaimRows: rows => { ctx.AS_STATE.claimRows = rows; },
  asRenderEnvOutcomes: rows => { ctx.AS_STATE.environments = rows || []; },
  asRenderScanRows: rows => { ctx.AS_STATE.rows = rows || []; },
  asRenderTrackRows: rows => { ctx.AS_STATE.trackRows = rows || []; },
  asSupplementClaimAccounts: () => {},
  asReviewFailed: () => false,
  asRenderDiscoveryRows: rows => { ctx.AS_STATE.discoveryRows = rows || []; },
  asDiscoveryItemsFromRows: () => [],
  asTrackItemsFromRows: () => [],
  asFinishDiscovery: async (taskId, rows, status) => { finishCalls.push({taskId, status}); },
  toast: message => { node('toast').textContent = message; }, confirm: () => true
});

function load(name) {
  const m = new RegExp('(?:async )?function ' + name + '\\([^]*?\\n}').exec(html);
  assert.ok(m, name);
  vm.runInContext(m[0], ctx);
}
for (const name of ['asBeginTaskEpoch', 'asRunIdOf', 'asWriteEntryReady', 'asCreateTask',
  'asRequireRuntimeControls', 'asPoll', 'asSubmitItems', 'asSubmitByEnvironment',
  'asScan', 'asSerials', 'asOrderedRows', 'asSetOrderView',
  'asLoadLatestClaim', 'asRestoreDiscovery', 'asDiscoveryPersist', 'asDiscoveryClearPersisted',
  'asTrack', 'asDiscoverAndTrack']) load(name);
const stopStart = html.indexOf("$('asStop').onclick =");
vm.runInContext(html.slice(stopStart, html.indexOf('\n};', stopStart) + 3), ctx);
const clearStart = html.indexOf("$('asClearResult').onclick =");
vm.runInContext(html.slice(clearStart, html.indexOf('\n};', clearStart) + 3), ctx);
const run = code => vm.runInContext(code, ctx);
const state = () => ctx.AS_STATE;

function resetState(extra) {
  ctx.AS_STATE = Object.assign(freshState(), extra || {});
  getCalls.length = 0; postCalls.length = 0; phases.length = 0; progressCalls.length = 0;
  finishCalls.length = 0; storageData.clear();
  ['asStop', 'asScan', 'asSubmit'].forEach(id => { node(id).disabled = false; });
  router = async () => ({data: {}});
  executorResponder = async () => ({id: 'EXEC', capabilities: EXEC_CAPS});
}
const claimGet = runId => '/v1/operation-runs/after-sale-claim/' + encodeURIComponent(runId);
const claimCreatePath = '/v1/operation-runs/after-sale-claim';
const envs5 = ['900001', '900002', '900003', '900004', '900005'];

(async () => {
  // ── 场景 1：旧批次已终态残留，直提创建期间定时轮询触发 ──
  {
    resetState({runId: 'OLD', claimRows: [{orderNo: 'O-OLD', status: 'ok'}]});
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const submitted = run(`asSubmitItems([], {environmentSerials: ${JSON.stringify(envs5)}, label: "按环境提交"})`);
    await settle();
    assert.equal(state().starting, true);
    assert.equal(state().mode, '', '异步准备阶段不得先行写入任务模式');
    run('asPoll()'); await settle();                       // 模拟 2.5s 定时器在创建期间触发
    assert.ok(!getCalls.some(p => p.includes('OLD')), 'starting 期间不得查询旧批次');
    createGate.resolve({data: {runId: 'NEW'}});
    assert.equal(await submitted, true);
    assert.ok(state().starting === false && state().mode === 'claim' && state().runId === 'NEW'
      && state().running === true && state().envMode === true, '创建成功后任务身份/模式/运行状态一次建立');
    await settle();
    assert.ok(getCalls.some(p => p === claimGet('NEW')), 'starting 解除后首轮轮询必须已发出');
    // 部分回传 → 完整终态
    const gate1 = defer();
    router = path => path === claimGet('NEW') ? gate1.promise : {data: {}};
    run('asPoll()'); await settle();
    gate1.resolve({data: {status: 'running', rows: [{orderNo: 'SYNTH-1', status: 'ok', environmentSerial: '900001'}],
      environments: [{environmentSerial: '900001', status: 'ok'}], progressCompleted: 2, progressTotal: 5}});
    await settle();
    assert.equal(state().claimRows.length, 1);
    assert.equal(state().pollErrors, 0);
    assert.equal(state().running, true);
    const gate2 = defer();
    router = path => path === claimGet('NEW') ? gate2.promise : {data: {}};
    run('asPoll()'); await settle();
    gate2.resolve({data: {status: 'completed', successCount: 5, progressCompleted: 5, progressTotal: 5,
      rows: [{orderNo: 'SYNTH-1', status: 'ok'}], environments: envs5.map(s => ({environmentSerial: s, status: 'ok'}))}});
    await settle();
    assert.equal(state().running, false, '终态必须解除忙碌');
    assert.equal(state().mode, '');
    assert.equal(state().envSerials, null);
    assert.equal(state().envMode, false);
    assert.equal(node('asStop').disabled, true);
    assert.ok(phases.at(-1).title.includes('完成'));
    console.log('scenario 1 (旧批次 + 创建期间轮询 + 全生命周期) OK');
  }

  // ── 场景 2：旧轮询响应晚到（成功与失败）都被任务代次拦截 ──
  {
    resetState({mode: 'claim', runId: 'OLD', running: true, envSerials: ['900001'], envMode: true});
    const oldGet = defer();
    router = () => oldGet.promise;
    const pollDone = run('asPoll()'); await settle();
    assert.ok(getCalls.includes(claimGet('OLD')));
    run('asBeginTaskEpoch()'); state().runId = 'NEW';      // 模拟新任务切换任务上下文
    oldGet.resolve({data: {status: 'completed', rows: [], environments: [], successCount: 0}});
    await pollDone; await settle();
    assert.equal(state().mode, 'claim', '旧响应不得清掉新任务的模式');
    assert.equal(state().running, true, '旧响应不得解除新任务的忙碌状态');
    assert.equal(node('asStop').disabled, false);
    assert.equal(state().pollErrors, 0);
    // 失败版本：归属已换的请求 reject 不污染新任务错误计数
    state().taskEpoch = 9;
    const oldGet2 = defer();
    router = () => oldGet2.promise;
    const pollDone2 = run('asPoll()'); await settle();
    run('asBeginTaskEpoch()');
    oldGet2.reject(new Error('session expired'));
    await pollDone2; await settle();
    assert.equal(state().pollErrors, 0, '被取代请求的失败不计入新任务');
    console.log('scenario 2 (旧响应晚到成功/失败均被代次拦截) OK');
  }

  // ── 场景 3：首次提交，页面从未有过旧 Run ──
  {
    resetState();
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const submitted = run(`asSubmitItems([{environmentSerial: "900001", orderNo: "SO-1"}], {})`);
    await settle();
    run('asPoll()'); await settle();
    createGate.resolve({data: {runId: 'FIRST'}});
    assert.equal(await submitted, true);
    await settle();
    assert.ok(state().mode === 'claim' && state().runId === 'FIRST' && getCalls.includes(claimGet('FIRST')));
    console.log('scenario 3 (首次提交) OK');
  }

  // ── 场景 4：已恢复终态旧历史（含退款单号行）后再次提交 ──
  {
    resetState({runId: 'OLD', claimRows: [{orderNo: 'R1', refundBillId: 'RB-1', status: 'ok'}]});
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const submitted = run(`asSubmitItems([], {environmentSerials: ["900010"], label: "按环境提交"})`);
    await settle();
    run('asPoll()'); await settle();
    createGate.resolve({data: {runId: 'NEW-AFTER-RESTORE'}});
    assert.equal(await submitted, true);
    await settle();
    assert.ok(state().mode === 'claim' && state().runId === 'NEW-AFTER-RESTORE'
      && state().running === true && getCalls.includes(claimGet('NEW-AFTER-RESTORE')));
    assert.equal(state().claimRows.length, 0, '新批次起始列表为空是合法状态');
    console.log('scenario 4 (恢复终态历史后再提交) OK');
  }

  // ── 场景 5：失败路径——创建失败可重试且幂等键复用、执行器不可用、能力缺失 ──
  {
    resetState({runId: 'OLD'});
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const submitted = run(`asSubmitItems([], {environmentSerials: ${JSON.stringify(envs5)}, label: "按环境提交"})`);
    await settle();
    createGate.reject(new Error('create boom'));
    assert.equal(await submitted, false);
    assert.ok(state().starting === false && state().mode === '' && state().running === false,
      '创建失败不得显示虚假运行中，也不得残留任务模式');
    assert.notEqual(state().runId, 'NEW');
    const createGate2 = defer();
    router = (path, options) => (options && options.method) ? createGate2.promise : {data: {}};
    const retry = run(`asSubmitItems([], {environmentSerials: ${JSON.stringify(envs5)}, label: "按环境提交"})`);
    await settle();
    createGate2.resolve({data: {runId: 'NEW-RETRY'}});
    assert.equal(await retry, true);
    const first = postCalls.find(p => p.path === claimCreatePath).body;
    const second = postCalls.filter(p => p.path === claimCreatePath).at(-1).body;
    assert.equal(first.idempotencyKey, second.idempotencyKey, '同指纹重试必须复用幂等键');
    await settle();
    assert.ok(state().running === true && getCalls.includes(claimGet('NEW-RETRY')));

    resetState();
    executorResponder = async () => { throw new Error('executor offline'); };
    const r1 = run(`asSubmitItems([], {environmentSerials: ["900011"], label: "按环境提交"})`);
    assert.equal(await r1, false);
    assert.ok(phases.at(-1).title === '执行器不可用' && state().mode === '' && state().running === false);

    resetState();
    const weakCaps = ['after.sale.runtime-controls.v1', 'after.sale.reliable-results.v1'];
    executorResponder = async () => ({id: 'EXEC', capabilities: weakCaps});
    router = async path => path === '/v1/executors'
      ? {items: [{id: 'EXEC', status: 'active', connectivity: 'online', capabilities: weakCaps}]}
      : {data: {}};
    const r2 = run(`asSubmitItems([], {environmentSerials: ["900012"], label: "按环境提交"})`);
    assert.equal(await r2, false);
    assert.equal(phases.at(-1).title, '执行器不可用');
    assert.ok(phases.at(-1).text.includes('退款凭证补查'), '推迟 mode 后 claim 专属能力位仍必须被校验');
    assert.ok(state().mode === '' && state().running === false && !postCalls.some(p => p.path === claimCreatePath));

    resetState();
    executorResponder = async () => ({id: 'EXEC', capabilities: ['after.sale.runtime-controls.v1']});
    const createGate3 = defer();
    router = async (path, options) => {
      if (path === '/v1/executors') return {items: [{id: 'EXEC', status: 'active',
        connectivity: 'online', capabilities: EXEC_CAPS}]};
      if (options && options.method) return createGate3.promise;
      return {data: {}};
    };
    const r3 = run(`asSubmitItems([], {environmentSerials: ["900013"], label: "按环境提交"})`);
    await settle();
    createGate3.resolve({data: {runId: 'REFRESHED'}});
    assert.equal(await r3, true, '刷新后能力齐备应放行');
    console.log('scenario 5 (创建失败/重试幂等/执行器不可用/能力缺失与刷新) OK');
  }

  // ── 场景 6：共用入口——按订单 items 模式与 asSubmitByEnvironment 包装 ──
  {
    resetState();
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const submitted = run(`asSubmitItems([{environmentSerial: "900001", orderNo: "SO-1"},
      {environmentSerial: "900002", orderNo: "SO-2"}], {label: "提交售后"})`);
    await settle();
    createGate.resolve({data: {runId: 'ORDERS'}});
    assert.equal(await submitted, true);
    const body = postCalls.filter(p => p.path === claimCreatePath).at(-1).body;
    assert.equal(body.items.length, 2);
    assert.ok(!('environmentSerials' in body), '按单提交不得携带环境范围字段');
    assert.ok(!postCalls.some(p => p.path === '/v1/after-sale/scan'), '提交不得额外发起扫描');
    await settle();

    resetState();
    const createGate4 = defer();
    router = (path, options) => (options && options.method) ? createGate4.promise : {data: {}};
    const viaWrapper = run('asSubmitByEnvironment(["900020"])');
    await settle();
    createGate4.resolve({data: {runId: 'WRAP'}});
    assert.equal(await viaWrapper, true);
    const wrapBody = postCalls.filter(p => p.path === claimCreatePath).at(-1).body;
    assert.deepEqual(wrapBody.environmentSerials, ['900020']);
    assert.equal(postCalls.filter(p => p.path === claimCreatePath).length, 1, '只建一个 Run');
    console.log('scenario 6 (共用入口 items/环境包装) OK');
  }

  // ── 场景 7：终态矩阵与全环境跳过 ──
  {
    const terminal = async (status, data, expectTitle) => {
      resetState({mode: 'claim', runId: 'T1', running: true, envMode: true,
        envSerials: ['900001', '900002'], claimItems: [], claimRows: []});
      router = async () => ({data: Object.assign({status}, data)});
      await run('asPoll()'); await settle();
      assert.equal(state().running, false, status + ' 必须解除忙碌');
      assert.equal(state().mode, '');
      assert.equal(state().envSerials, null);
      assert.ok(phases.at(-1).title.includes(expectTitle), status + ' → ' + phases.at(-1).title);
    };
    await terminal('completed', {successCount: 5}, '完成');
    await terminal('partial_failure', {successCount: 4, failedCount: 1}, '部分失败');
    await terminal('failed', {successCount: 0}, '失败');
    await terminal('cancelled', {successCount: 2}, '已停止');
    await terminal('uncertain', {successCount: 1}, '待核对');
    // 全环境跳过：rows 为空是合法完整结果，环境级反馈必须呈现
    resetState({mode: 'claim', runId: 'T2', running: true, envMode: true,
      envSerials: ['900001', '900002'], claimItems: [], claimRows: []});
    router = async () => ({data: {status: 'completed', successCount: 0, rows: [],
      environments: [{environmentSerial: '900001', status: 'skip'}, {environmentSerial: '900002', status: 'skip'}],
      progressCompleted: 2, progressTotal: 2}});
    await run('asPoll()'); await settle();
    assert.equal(state().running, false);
    assert.equal(state().claimRows.length, 0);
    assert.ok(state().environments.length === 2 && phases.at(-1).text.includes('已跳过'),
      '全跳过批次的环境结果必须可见');
    console.log('scenario 7 (终态矩阵 + 全环境跳过) OK');
  }

  // ── 场景 8：停止指向当前任务编号；视图切换不改变任务模式 ──
  {
    resetState({mode: 'claim', runId: 'STOP-1', running: true, envMode: true, envSerials: ['900001']});
    let cancelPath = null;
    router = async (path, options) => {
      if (options && options.method === 'POST' && path.endsWith('/cancel')) cancelPath = path;
      return {data: {}};
    };
    await node('asStop').onclick();
    assert.equal(cancelPath, claimGet('STOP-1') + '/cancel', '取消必须发给当前任务编号');
    // 真实页签切换：视图状态与 DOM 生效，但任务模式、停止目标不受影响
    run("asSetOrderView('pending', true)");
    assert.equal(state().orderView, 'pending');
    assert.equal(node('asPendingTab').attrs['aria-selected'], 'true');
    assert.equal(node('asResultView').hidden, true);
    assert.equal(state().mode, 'claim', '切页签不是任务切换');
    run("asSetOrderView('result', true)");
    assert.equal(state().orderView, 'result');
    assert.equal(state().mode, 'claim', '切回结果页同样不影响任务');
    assert.equal(state().runId, 'STOP-1');
    console.log('scenario 8 (停止指向当前任务 + 真实页签切换) OK');
  }

  // ── 场景 9：姊妹路径 asScan 同窗口——模式后置、旧编号不被查询 ──
  {
    resetState({scanTaskId: 'OLD-SCAN', rows: [{orderNo: 'OLD-ROW'}]});
    const createGate = defer();
    router = (path, options) => (options && options.method) ? createGate.promise : {data: {}};
    const scanned = run('asScan()');
    await settle();
    assert.equal(state().starting, true);
    assert.equal(state().mode, '', '扫描同样不得先行写入任务模式');
    run('asPoll()'); await settle();
    assert.ok(!getCalls.some(p => p.includes('OLD-SCAN')), 'starting 期间不得查询旧扫描任务');
    createGate.resolve({data: {taskId: 'NEW-SCAN'}});
    await scanned; await settle();
    assert.ok(state().mode === 'scan' && state().scanTaskId === 'NEW-SCAN' && state().running === true);
    assert.ok(getCalls.some(p => p.endsWith('/after-sale/scan/NEW-SCAN')), '扫描创建后首轮轮询必须发出');
    console.log('scenario 9 (asScan 姊妹路径) OK');
  }

  // ── 场景 10：asLoadLatestClaim 真实恢复终态批次，随后真实再提交 ──
  {
    resetState();
    let createGate = null;
    router = async (path, options) => {
      if (path === '/v1/operation-runs/after-sale-claim/latest') {
        return {data: {runId: 'LATEST-1', submitMode: 'environments', status: 'completed',
          rows: [{orderNo: 'L1', status: 'ok'}],
          environments: [{environmentSerial: '900001', status: 'ok'}], progressTotal: 1, progressCompleted: 1}};
      }
      if (options && options.method && path === claimCreatePath) {
        createGate = createGate || defer();
        return createGate.promise;
      }
      return {data: {}};
    };
    await run('asLoadLatestClaim()');
    assert.ok(state().runId === 'LATEST-1' && state().claimRows.length === 1,
      '真实恢复必须回填最近批次与订单行');
    assert.ok(state().running === false && state().mode === '', '终态批次恢复不接管轮询');
    assert.equal(state().runLabel, '按环境提交');
    const submitted = run(`asSubmitItems([], {environmentSerials: ["900030"], label: "按环境提交"})`);
    await settle();
    createGate.resolve({data: {runId: 'AFTER-LATEST'}});
    assert.equal(await submitted, true);
    await settle();
    assert.ok(state().mode === 'claim' && state().runId === 'AFTER-LATEST'
      && state().running === true && getCalls.includes(claimGet('AFTER-LATEST')),
      '恢复终态历史后真实再提交必须建立并轮询新任务');
    console.log('scenario 10 (asLoadLatestClaim 真实恢复 + 再提交) OK');
  }

  // ── 场景 11：asLoadLatestClaim 恢复运行中批次并跟进到终态 ──
  {
    resetState();
    const liveGet = defer();
    router = async path => path === '/v1/operation-runs/after-sale-claim/latest'
      ? {data: {runId: 'LIVE-1', status: 'running', submitMode: 'environments',
        rows: [{orderNo: 'V1', status: 'running'}], environments: [], progressTotal: 3, progressCompleted: 1}}
      : (path === claimGet('LIVE-1') ? liveGet.promise : {data: {}});
    await run('asLoadLatestClaim()');
    assert.ok(state().mode === 'claim' && state().running === true && state().runId === 'LIVE-1',
      '恢复运行中批次必须接管轮询');
    assert.ok(state().taskEpoch >= 1, '恢复接手必须取任务代次');
    assert.ok(getCalls.includes(claimGet('LIVE-1')), '恢复后首轮轮询必须发出');
    liveGet.resolve({data: {status: 'completed', successCount: 3, progressCompleted: 3, progressTotal: 3,
      rows: [{orderNo: 'V1', status: 'ok'}], environments: []}});
    await settle();
    assert.ok(state().running === false && state().mode === '', '恢复的任务也必须正常终态');
    console.log('scenario 11 (asLoadLatestClaim 恢复运行中批次) OK');
  }

  // ── 场景 12：asRestoreDiscovery 旧响应晚到不得覆盖新发现任务（成功与 reject）──
  {
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'OLD-DISC', serials: ['900001']}));
    const restoreGate = defer(), createGate = defer();
    router = (path, options) => {
      if (path.endsWith('/after-sale/scan/OLD-DISC')) return restoreGate.promise;
      if (options && options.method && path === '/v1/after-sale/scan') return createGate.promise;
      return {data: {}};
    };
    const restoreDone = run('asRestoreDiscovery()');
    await settle();
    assert.ok(getCalls.some(p => p.endsWith('/after-sale/scan/OLD-DISC')), '旧恢复请求已发出');
    const discovery = run(`asDiscoverAndTrack(["900001", "900002"],
      {id: "EXEC-2", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await settle();
    createGate.resolve({data: {taskId: 'NEW-DISC'}});
    await discovery; await settle();
    assert.ok(state().mode === 'discover' && state().discoveryTaskId === 'NEW-DISC'
      && state().running === true, '新发现任务已建立');
    // 旧恢复响应此刻返回运行中状态，试图写回旧编号——必须被归属校验拦下
    restoreGate.resolve({data: {status: 'running',
      summary: {purpose: 'refund_discovery', rows: [], progressTotal: 1, progressCompleted: 0}}});
    await restoreDone; await settle();
    assert.equal(state().discoveryTaskId, 'NEW-DISC', '旧恢复响应不得覆盖新任务编号');
    assert.equal(state().mode, 'discover');
    assert.equal(state().running, true);
    // 停止仍指向新任务编号
    let cancelPath = null;
    router = async (path, options) => {
      if (options && options.method === 'POST' && path.endsWith('/cancel')) cancelPath = path;
      return {data: {}};
    };
    await node('asStop').onclick();
    assert.ok(cancelPath && cancelPath.endsWith('/after-sale/scan/NEW-DISC/cancel'),
      '停止必须仍指向新任务编号');
    // reject 版本：旧恢复请求失败时不得清掉新任务的持久化
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'OLD-DISC-2', serials: ['900001']}));
    const restoreGate2 = defer(), createGate2 = defer();
    router = (path, options) => {
      if (path.endsWith('/after-sale/scan/OLD-DISC-2')) return restoreGate2.promise;
      if (options && options.method && path === '/v1/after-sale/scan') return createGate2.promise;
      return {data: {}};
    };
    const restoreDone2 = run('asRestoreDiscovery()');
    await settle();
    const discovery2 = run(`asDiscoverAndTrack(["900009"],
      {id: "EXEC-2", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await settle();
    createGate2.resolve({data: {taskId: 'NEW-DISC-2'}});
    await discovery2; await settle();
    restoreGate2.reject(new Error('session expired'));
    await restoreDone2; await settle();
    const persisted = storageData.get('afterSaleDiscoveryTask') || '';
    assert.ok(persisted.includes('NEW-DISC-2'), '旧恢复失败不得清除新任务的持久化');
    console.log('scenario 12 (asRestoreDiscovery 旧响应晚到成功/reject 均不覆盖) OK');
  }

  // ── 场景 13：asRestoreDiscovery 无交错正常恢复（active 与终态）──
  {
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'SOLO-DISC', serials: ['900001']}));
    const soloGet = defer();
    router = path => path.endsWith('/after-sale/scan/SOLO-DISC') ? soloGet.promise : {data: {}};
    const restoring = run('asRestoreDiscovery()');
    await settle();
    soloGet.resolve({data: {status: 'running',
      summary: {purpose: 'refund_discovery', rows: [], progressTotal: 1, progressCompleted: 0}}});
    await restoring; await settle();
    assert.ok(state().mode === 'discover' && state().discoveryTaskId === 'SOLO-DISC'
      && state().running === true, '无交错时恢复必须正常接手');
    assert.ok(getCalls.some(p => p.endsWith('/after-sale/scan/SOLO-DISC')), '恢复后必须开始轮询');
    // 终态批次恢复：展示结果但不接管
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'DONE-DISC', serials: ['900001']}));
    router = async path => path.endsWith('/after-sale/scan/DONE-DISC')
      ? {data: {status: 'succeeded', summary: {purpose: 'refund_discovery', rows: [], progressTotal: 1, progressCompleted: 1}}}
      : {data: {}};
    await run('asRestoreDiscovery()');
    assert.ok(state().running === false && state().mode === '', '终态恢复不接管');
    assert.ok(state().discoveryIntent === null, '刷新恢复不得带出自动接续意图');
    console.log('scenario 13 (asRestoreDiscovery 正常恢复 active/终态) OK');
  }

  // ── 场景 14：asTrack 真实回访创建、窗口期模式后置与终态 ──
  {
    resetState();
    const trackCreate = defer();
    router = (path, options) => (options && options.method
      && path === '/v1/after-sale/track') ? trackCreate.promise : {data: {}};
    const tracked = run(`asTrack([{environmentSerial: "900001", orderNo: "SO-1", refundBillId: "RB-1"}])`);
    await settle();
    assert.equal(state().mode, '', '回访创建期间同样不得先行写入任务模式');
    run('asPoll()'); await settle();
    assert.ok(!getCalls.some(p => p.includes('/after-sale/track/')), 'starting 期间不得发旧回访查询');
    trackCreate.resolve({data: {taskId: 'TR-1'}});
    await tracked; await settle();
    assert.ok(state().mode === 'track' && state().trackTaskId === 'TR-1' && state().running === true);
    assert.ok(getCalls.includes('/v1/after-sale/track/TR-1'), '回访创建后首轮轮询必须发出');
    const trackGet = defer();
    router = path => path === '/v1/after-sale/track/TR-1' ? trackGet.promise : {data: {}};
    run('asPoll()'); await settle();
    trackGet.resolve({data: {status: 'succeeded', summary: {progressTotal: 1, progressCompleted: 1,
      rows: [{refundBillId: 'RB-1', status: 'ok', phase: 'reviewing'}]}}});
    await settle();
    assert.ok(state().running === false && state().mode === '', '回访终态必须解除忙碌');
    assert.ok((state().trackRows || []).some(r => r.refundBillId === 'RB-1'), '回访行必须渲染');
    console.log('scenario 14 (asTrack 真实创建与终态) OK');
  }

  // ── 场景 15：asDiscoverAndTrack 真实创建、持久化与 discover 终态 ──
  {
    resetState();
    const discCreate = defer();
    router = (path, options) => (options && options.method
      && path === '/v1/after-sale/scan') ? discCreate.promise : {data: {}};
    const discovery = run(`asDiscoverAndTrack(["900041"],
      {id: "EXEC-3", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await settle();
    assert.equal(state().mode, '', '发现创建期间同样不得先行写入任务模式');
    discCreate.resolve({data: {taskId: 'DISC-1'}});
    await discovery; await settle();
    assert.ok(state().mode === 'discover' && state().discoveryTaskId === 'DISC-1' && state().running === true);
    assert.ok((storageData.get('afterSaleDiscoveryTask') || '').includes('DISC-1'),
      '发现任务创建后必须写入持久化（刷新可恢复）');
    const discGet = defer();
    router = path => path.endsWith('/after-sale/scan/DISC-1') ? discGet.promise : {data: {}};
    run('asPoll()'); await settle();
    discGet.resolve({data: {status: 'succeeded',
      summary: {purpose: 'refund_discovery', rows: [], progressTotal: 1, progressCompleted: 1}}});
    await settle();
    assert.ok(state().running === false && state().mode === '', '发现终态必须解除忙碌');
    assert.ok(finishCalls.some(c => c.taskId === 'DISC-1' && c.status === 'succeeded'),
      '发现终态必须进入收尾处理（自动接续入口）');
    console.log('scenario 15 (asDiscoverAndTrack 真实创建与终态) OK');
  }

  // ── 场景 16：新发现任务已完成、页面重回空闲后，旧恢复响应不得倒退接管 ──
  {
    // 16a active：旧恢复响应此刻返回 running，不得重新接管旧编号
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'STALE-DISC', serials: ['900001']}));
    const restoreGate = defer();
    let newGet = null;
    router = (path, options) => {
      if (path.endsWith('/after-sale/scan/STALE-DISC')) return restoreGate.promise;
      if (path.endsWith('/after-sale/scan/NEW-FIN')) { newGet = newGet || defer(); return newGet.promise; }
      if (options && options.method && path === '/v1/after-sale/scan') {
        return Promise.resolve({data: {taskId: 'NEW-FIN'}});
      }
      return {data: {}};
    };
    const restoreDone = run('asRestoreDiscovery()');
    await settle();
    const discovery = run(`asDiscoverAndTrack(["900001"],
      {id: "EXEC-4", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await discovery; await settle();
    assert.ok(state().running === true && state().discoveryTaskId === 'NEW-FIN');
    // 新任务跑完到终态，页面回到空闲（busy 检查全部失效，只剩代次能拦）
    newGet.resolve({data: {status: 'succeeded',
      summary: {purpose: 'refund_discovery', rows: [{environmentSerial: '900001', environmentStatus: 'ok'}],
        progressTotal: 1, progressCompleted: 1}}});
    await settle();
    assert.ok(state().running === false && state().mode === '' && state().taskEpoch >= 1,
      '新任务已完成，页面重回空闲');
    const staleRestoreCalls = getCalls.filter(p => p.endsWith('/STALE-DISC')).length;
    restoreGate.resolve({data: {status: 'running',
      summary: {purpose: 'refund_discovery', rows: [], progressTotal: 1, progressCompleted: 0}}});
    await restoreDone; await settle();
    assert.equal(state().discoveryTaskId, 'NEW-FIN', '旧恢复响应不得把任务编号倒退');
    assert.ok(state().mode === '' && state().running === false, '旧恢复响应不得重新接管轮询');
    assert.equal(getCalls.filter(p => p.endsWith('/STALE-DISC')).length, staleRestoreCalls,
      '不得开始查询旧任务');
    assert.ok(!phases.some(p => p.title.includes('已恢复运行中的平台查找')), '不得倒退提示');

    // 16b terminal：旧恢复响应返回 succeeded，不得用旧结果覆盖新任务结果
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'STALE-2', serials: ['900001']}));
    const restoreGate2 = defer();
    router = async path => {
      if (path.endsWith('/after-sale/scan/STALE-2')) return restoreGate2.promise;
      if (path.endsWith('/after-sale/scan/NEW-FIN2')) return {data: {status: 'succeeded',
        summary: {purpose: 'refund_discovery', rows: [{environmentSerial: '900001', environmentStatus: 'ok'}],
          progressTotal: 1, progressCompleted: 1}}};
      if (path === '/v1/after-sale/scan') return {data: {}};
      return {data: {}};
    };
    // 创建请求也走 router：POST /v1/after-sale/scan 返回 {data:{taskId}} —— 手动补
    let created2 = false;
    const realRouter = router;
    router = async (path, options) => {
      if (options && options.method && path === '/v1/after-sale/scan' && !created2) {
        created2 = true; return {data: {taskId: 'NEW-FIN2'}};
      }
      return realRouter(path, options);
    };
    const restoreDone2 = run('asRestoreDiscovery()');
    await settle();
    const discovery2 = run(`asDiscoverAndTrack(["900002"],
      {id: "EXEC-4", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await discovery2; await settle();
    run('asPoll()'); await settle();
    assert.ok(state().running === false, '新任务已完成');
    const rowsBefore = (state().discoveryRows || []).length;
    const phaseCount = phases.length;
    restoreGate2.resolve({data: {status: 'succeeded',
      summary: {purpose: 'refund_discovery', rows: [{environmentSerial: '900001', environmentStatus: 'failed'}],
        progressTotal: 1, progressCompleted: 1}}});
    await restoreDone2; await settle();
    assert.equal((state().discoveryRows || []).length, rowsBefore, '旧恢复结果不得覆盖新任务结果');
    assert.equal(phases.length, phaseCount, '旧恢复提示不得覆盖新任务终态提示');

    // 16c reject：旧恢复请求失败，不得删掉新任务的持久化
    resetState();
    storageData.set('afterSaleDiscoveryTask', JSON.stringify({taskId: 'STALE-3', serials: ['900001']}));
    const restoreGate3 = defer();
    router = (path, options) => {
      if (path.endsWith('/after-sale/scan/STALE-3')) return restoreGate3.promise;
      if (options && options.method && path === '/v1/after-sale/scan') {
        return Promise.resolve({data: {taskId: 'NEW-FIN3'}});
      }
      return {data: {status: 'succeeded', summary: {purpose: 'refund_discovery',
        rows: [], progressTotal: 1, progressCompleted: 1}}};
    };
    const restoreDone3 = run('asRestoreDiscovery()');
    await settle();
    const discovery3 = run(`asDiscoverAndTrack(["900003"],
      {id: "EXEC-4", capabilities: ${JSON.stringify(EXEC_CAPS)}})`);
    await discovery3; await settle();
    run('asPoll()'); await settle();
    assert.ok(state().running === false, '新任务已完成、页面空闲');
    restoreGate3.reject(new Error('session expired'));
    await restoreDone3; await settle();
    assert.ok((storageData.get('afterSaleDiscoveryTask') || '').includes('NEW-FIN3'),
      '旧恢复失败不得删除新任务的持久化');
    console.log('scenario 16 (新任务完成后旧恢复响应 active/terminal/reject 均不倒退) OK');
  }

  // ── 场景 17：新提交批次完成并清空结果后，旧 latest 响应不得接管旧批次 ──
  {
    resetState();
    const latestGate = defer(), createGate = defer(), newGet = defer();
    router = (path, options) => {
      if (path === '/v1/operation-runs/after-sale-claim/latest') return latestGate.promise;
      if (options && options.method && path === claimCreatePath) return createGate.promise;
      if (path === claimGet('NEW-C')) return newGet.promise;
      return {data: {}};
    };
    const restoring = run('asLoadLatestClaim()');
    await settle();
    const submitted = run(`asSubmitItems([{environmentSerial: "900001", orderNo: "SO-9"}], {label: "提交售后"})`);
    await settle();
    createGate.resolve({data: {runId: 'NEW-C'}});
    assert.equal(await submitted, true);
    await settle();
    newGet.resolve({data: {status: 'completed', successCount: 1, progressCompleted: 1, progressTotal: 1,
      rows: [{orderNo: 'SO-9', status: 'ok'}], environments: []}});
    await settle();
    assert.ok(state().running === false && state().runId === 'NEW-C' && state().claimRows.length === 1);
    // 清空结果：页面完全空闲（busy 检查全部失效，只剩代次能拦）
    node('asClearResult').onclick();
    assert.ok(state().runId === null && (state().claimRows || []).length === 0, '结果已清空');
    const phaseCount = phases.length;
    latestGate.resolve({data: {runId: 'OLD-C', status: 'running', submitMode: 'environments',
      rows: [{orderNo: 'OLD-9'}], environments: [], progressTotal: 2, progressCompleted: 0}});
    await restoring; await settle();
    assert.equal(state().runId, null, '旧 latest 响应不得接管旧批次');
    assert.ok(state().mode === '' && state().running === false);
    assert.ok(!getCalls.some(p => p === claimGet('OLD-C')), '不得开始查询旧批次');
    assert.equal(phases.length, phaseCount, '不得出现旧批次恢复提示');
    console.log('scenario 17 (清空后旧 latest 响应不接管) OK');
  }

  console.log('PASS: task lifecycle race regression (submit/poll/epoch/terminal/stop/scan/restore/discover/track) — synthetic data only');
})().catch(e => { console.error(e); process.exitCode = 1; });
