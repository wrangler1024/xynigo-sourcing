// Real submit/poll/render/restore functions; all task data and HTTP responses are synthetic.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {textContent:'',innerHTML:'',hidden:false,disabled:false,
    style:{},dataset:{},setAttribute(){},scrollIntoView(){}});
  return nodes.get(id);
};
node('asBizSummary').children = Array.from({length:6},()=>{const b={};return {querySelector:()=>b};});
const state = {type:'refund',mode:'',running:false,starting:false,polling:false,taskEpoch:0,
  claimRows:[],claimItems:[],selected:new Set(),pendingCreates:{},environments:[],claimFilter:'all'};
let response = {}, posts = 0;
const ctx = vm.createContext({AS_STATE:state,AS_HISTORY:{},AS_TYPES:[],AFTER_SALE_TYPE:'丢件退款',
  $:node,Date,TextEncoder,crypto:require('node:crypto').webcrypto,
  esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  cloudFormalExecutor:async()=>({id:'SYNTH-EXEC'}),asRequireRuntimeControls:async()=>{},
  asRuntimeOptions:()=>({browserMode:'headless',concurrency:2}),
  cloudFetchJson:async(_path,opts)=>{if(opts?.method){posts++;return {data:{runId:'SYNTH-RUN'}};}return {data:response};},
  asSyncRuntimeControls(){},asSetOrderView(){},asReconcileScanRows(){},asSyncRetryButton(){},asSyncWriteButtons(){},
  asProgress:d=>{node('progress').data=d;},asSetPhase:title=>{node('phase').textContent=title;},
  asScanGoodsHtml:()=>'<td>—</td>',asShortRef:s=>s||'—',
});
const run = code=>vm.runInContext(code,ctx);
for(const name of ['AS_CLAIM_PILL','AS_RECOVERABLE_CLAIM_STATUS'])
  run(new RegExp('const '+name+' = (?:\\{[^]*?\\n}|[^;]+);').exec(html)[0]);
for(const name of ['asBeginTaskEpoch','asRunIdOf','asCreateTask','asWriteEntryReady','asSubmitItems','asPoll',
  'asOrderedRows','asClaimNeedsReconciliation','asClaimMatchesFilter','asClaimPill','asClaimPillText',
  'asClaimReasonHtml','asRefundPathText','asRefundAccountHtml','asClaimRowHtml','asRenderClaimRows',
  'asRenderClaimFilters','asClaimEnvironmentRows','asEnvironmentSummary','asClaimEnvironmentRowHtml',
  'asClaimTableHtml','asEnvOutcomePills','asRenderEnvOutcomes','asSyncClaimExportButton',
  'asRecoverableClaimRows','asTrackItemsFromRows','asRenderClaimHistoryDetail','asOpenClaimHistoryDetail','asLoadLatestClaim']) {
  const m = new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(m,name);run(m[0]);
}
const settle = async()=>{for(let i=0;i<4;i++)await new Promise(resolve=>setImmediate(resolve));};
const envs=['900005','900004','900003','900002','900001'];
const table=()=>node('asClaimRows').innerHTML;
const poll=async data=>{response=data;await run('asPoll()');assert.equal(state.pollErrors,0);};
(async()=>{
  response={status:'queued',rows:[],environments:[],progressTotal:5,progressCompleted:0};
  await run(`asSubmitItems([], {environmentSerials:${JSON.stringify(envs)}})`);await settle();
  assert.equal(posts,1);assert.equal(state.claimRows.length,0);
  assert.equal((table().match(/data-as-environment=/g)||[]).length,5);
  assert.ok(table().indexOf('900005')<table().indexOf('900001'),'preserve submitted order');
  assert.match(table(),/等待执行器/);assert.match(node('asClaimFilters').innerHTML,/等待 0 单 · 5 环境/);
  assert.equal(node('asBizSummary').children[0].querySelector('b').textContent,0);
  assert.equal(node('asClaimExport').disabled,true);

  await poll({status:'running',rows:[],environments:[{environmentSerial:envs[0],status:'running'}]});
  assert.match(table(),/读取订单/);assert.equal((table().match(/data-as-environment=/g)||[]).length,5);
  const failures=envs.map(environmentSerial=>({environmentSerial,status:'fail',errorSummary:'合成环境连接失败 <script>unsafe</script>'}));
  await poll({status:'failed',rows:[],environments:failures,progressCompleted:5,progressTotal:5});
  assert.equal(state.running,false);assert.equal(state.mode,'');assert.equal(node('asStop').disabled,true);
  assert.equal(state.claimRows.length,0);assert.equal((table().match(/data-as-environment=/g)||[]).length,5);
  assert.match(table(),/合成环境连接失败/);assert.match(table(),/&lt;script&gt;/);assert.doesNotMatch(table(),/<script>|等待订单明细回传|运行中/);
  assert.match(node('asEnvOutcomes').textContent,/失败 5 个/);
  assert.match(node('asClaimFilters').innerHTML,/失败 0 单 · 5 环境/);
  assert.equal(node('asBizSummary').children[3].querySelector('b').textContent,0,'environment failures are not order failures');
  assert.equal(run('asRecoverableClaimRows(AS_STATE.claimRows).length'),0);
  assert.equal(run('asTrackItemsFromRows(AS_STATE.claimRows).length'),0);
  state.claimFilter='failed';run('asRenderClaimRows(AS_STATE.claimRows)');assert.match(table(),/合成环境连接失败/);

  // Terminal history uses the same table, not an empty "no eligible orders" claim.
  ctx.detail={batch:{runId:'SYNTH-RUN',submitMode:'environments',status:'failed',totalCount:5},rows:[],environments:failures};
  response=ctx.detail;await run('asOpenClaimHistoryDetail("SYNTH-RUN")');
  assert.match(node('asHistoryDetailMeta').innerHTML,/5 个环境读取失败/);
  assert.equal(node('asHistoryTrack').disabled,true);assert.match(node('asHistoryDetailBody').innerHTML,/合成环境连接失败/);
  assert.doesNotMatch(node('asHistoryDetailBody').innerHTML,/无可申请订单|等待订单明细回传/);
  state.runId=null;state.claimRows=[];state.claimFilter='all';
  response={runId:'RESTORED',submitMode:'environments',status:'failed',rows:[],environments:failures};
  await run('asLoadLatestClaim()');assert.match(table(),/合成环境连接失败/);assert.doesNotMatch(table(),/运行中/);

  // Mixed batch: environment group followed by its orders, and a failing env without orders.
  state.mode='claim';state.running=true;state.envMode=true;state.envSerials=envs;state.claimItems=[];
  const order={environmentSerial:envs[0],orderNo:'SYNTH-ORDER',status:'running'};
  await poll({status:'running',rows:[order],environments:[{environmentSerial:envs[0],status:'running'},failures[1]]});
  assert.equal(state.claimRows.length,1);assert.match(table(),/SYNTH-ORDER/);assert.match(table(),/提交中/);
  assert.ok(table().indexOf('data-as-environment="900005"')<table().indexOf('data-as-claim="SYNTH-ORDER"'));
  await poll({status:'partial_failure',rows:[{...order,status:'ok',refundBillId:'SYNTH-BILL'}],
    environments:[{environmentSerial:envs[0],status:'ok'},failures[1]],progressCompleted:5,progressTotal:5,successCount:1});
  assert.match(table(),/未收到该环境的最终结果/);assert.doesNotMatch(table(),/等待执行器|读取\/提交中/);
  assert.equal(state.claimRows.length,1);assert.equal(node('asBizSummary').children[0].querySelector('b').textContent,1);
  assert.equal(node('asClaimExport').disabled,false);
  assert.equal(run('asTrackItemsFromRows(AS_STATE.claimRows).length'),1);
  assert.doesNotMatch(table(),/data-as-environment="900005"/,'completed environment no longer duplicates its order');
  state.claimFilter='failed';run('asRenderClaimRows(AS_STATE.claimRows)');assert.match(table(),/900004/);assert.doesNotMatch(table(),/SYNTH-ORDER/);

  // Finished order details replace normal environment placeholders in both current and history tables.
  ctx.displayRows=[{environmentSerial:'900010',orderNo:'SYNTH-SKIP',status:'skip',note:'已有退款，跳过'}];
  ctx.displayEnvs=[{environmentSerial:'900010',status:'skip',blockedCount:1}];
  const display=filter=>run(`asClaimTableHtml(displayRows, displayEnvs, ${JSON.stringify(filter)})`);
  assert.doesNotMatch(display('all'),/data-as-environment/);
  assert.equal((display('all').match(/data-as-claim=/g)||[]).length,1);
  assert.match(display('skipped'),/已有退款，跳过/);
  ctx.displayRows.push({environmentSerial:'900010',orderNo:'SYNTH-OTHER',status:'ok'});
  ctx.displayEnvs[0]={environmentSerial:'900010',status:'ok',submittedCount:1,blockedCount:1};
  assert.equal((display('all').match(/data-as-claim=/g)||[]).length,2);
  assert.doesNotMatch(display('all'),/data-as-environment/,'multi-order environment keeps one row per order');
  ctx.displayEnvs[0].status='running';assert.match(display('all'),/data-as-environment/);
  ctx.displayEnvs[0].status='fail';ctx.displayEnvs[0].errorSummary='还有订单读取失败';
  assert.match(display('all'),/还有订单读取失败/,'environment failures must survive existing details');
  ctx.displayEnvs[0]={environmentSerial:'900010',status:'skip',blockedCount:3};
  assert.match(display('all'),/data-as-environment/,'incomplete details keep the environment summary');
  ctx.displayEnvs[0].blockedCount=2;ctx.displayRows[1].status='running';
  assert.match(display('skipped'),/data-as-environment/,'filter must not conceal unfinished work');
  ctx.displayRows=[];assert.match(display('all'),/data-as-environment/,'legacy empty detail still explains outcome');

  // Skip, login, inuse and cancelled contexts all retain inline reasons without fake orders.
  for(const status of ['skip','blocked','login','inuse','stopped']){
    state.claimFilter='all';state.running=true;state.mode='claim';state.envMode=true;state.envSerials=['900099'];
    await poll({status:status==='skip'||status==='blocked'?'completed':'failed',rows:[],
      environments:[{environmentSerial:'900099',status,note:'合成原因 '+status}]});
    assert.match(table(),new RegExp('合成原因 '+status));assert.equal(state.claimRows.length,0);assert.doesNotMatch(table(),/运行中/);
  }
  state.running=true;state.mode='claim';state.envMode=true;state.envSerials=['900098'];
  await poll({status:'cancelled',rows:[],environments:[]});assert.match(table(),/待核对/);assert.doesNotMatch(table(),/等待执行器/);

  // Empty terminal batch without any environment result must not keep a running empty state.
  state.running=true;state.mode='claim';state.envSerials=null;state.envMode=false;
  await poll({status:'failed',rows:[],environments:[]});assert.match(table(),/本批次已结束/);assert.doesNotMatch(table(),/运行中/);
  // Specified orders still render before any response and remain the only actionable records.
  state.status='queued';state.mode='claim';state.running=true;state.claimItems=[{environmentSerial:'900097',orderNo:'SYNTH-EXACT'}];
  run('asRenderClaimRows(asOrderedRows(AS_STATE.claimItems, [], "orderNo"))');assert.match(table(),/SYNTH-EXACT/);assert.match(table(),/等待/);
  assert.doesNotMatch(table(),/data-as-environment/);
  console.log('PASS: real environment/order table lifecycle, terminal failures, reasons, filters, history, restore, and order-only actions');
})().catch(error=>{console.error(error);process.exitCode=1;});
