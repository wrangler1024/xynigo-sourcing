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
let response = {}, posts = 0, postedBodies = [], confirmation = true;
const ctx = vm.createContext({AS_STATE:state,AS_HISTORY:{},AS_TYPES:[],AFTER_SALE_TYPE:'丢件退款',
  $:node,Date,TextEncoder,confirm:()=>confirmation,crypto:require('node:crypto').webcrypto,
  esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  cloudFormalExecutor:async()=>({id:'SYNTH-EXEC'}),asRequireRuntimeControls:async()=>{},
  asRuntimeOptions:()=>({browserMode:'headless',concurrency:2}),
  cloudFetchJson:async(_path,opts)=>{if(opts?.method){posts++;postedBodies.push(JSON.parse(opts.body));return {data:{runId:'SYNTH-RUN'}};}return {data:response};},
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
  'asRenderClaimFilters','asClaimEnvironmentRows','asEnvironmentSummary','asClaimEnvironmentNote','asClaimEnvironmentRowHtml',
  'asClaimTableHtml','asEnvOutcomePills','asRenderEnvOutcomes','asSyncClaimExportButton',
  'asRetryableEnvironmentSerials','asRetryFailedEnvironments','asSyncRetryButton',
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

  // Once an order is known, active progress uses that order row; empty failed environments remain visible.
  state.mode='claim';state.running=true;state.envMode=true;state.envSerials=envs;state.claimItems=[];
  const order={environmentSerial:envs[0],orderNo:'SYNTH-ORDER',status:'running'};
  await poll({status:'running',rows:[order],environments:[{environmentSerial:envs[0],status:'running'},failures[1]]});
  assert.equal(state.claimRows.length,1);assert.match(table(),/SYNTH-ORDER/);assert.match(table(),/提交中/);
  assert.doesNotMatch(table(),/data-as-environment="900005"/);
  assert.match(table(),/as-status-active">读取\/提交中/);
  assert.match(table(),/data-as-environment="900004"/);
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
  ctx.displayEnvs[0].status='running';assert.match(display('all'),/data-as-environment-context/);
  assert.doesNotMatch(display('all'),/data-as-environment=/);
  ctx.displayEnvs[0].status='fail';ctx.displayEnvs[0].errorSummary='还有订单读取失败';
  assert.match(display('all'),/还有订单读取失败/,'environment failures must survive existing details');
  assert.doesNotMatch(display('all'),/data-as-environment=/,'environment diagnostics stay inside an order row');
  assert.equal((display('all').match(/data-as-environment-context=/g)||[]).length,1);
  assert.match(display('failed'),/data-as-environment=/,'environment-only filter still explains failure');
  ctx.displayEnvs[0]={environmentSerial:'900010',status:'skip',blockedCount:3};
  assert.match(display('all'),/已报告 3 单，已返回 2 条订单明细/);
  assert.doesNotMatch(display('all'),/data-as-environment=/);
  ctx.displayEnvs[0].blockedCount=2;ctx.displayRows[1].status='running';
  assert.match(display('skipped'),/订单仍有未结束状态/,'filter must not conceal unfinished work');
  // Same row identity persists while reading, verifying and finishing. No duplicate environment row.
  ctx.displayRows=[{environmentSerial:'900010',orderNo:'SYNTH-LIFECYCLE',status:'running'}];
  ctx.displayEnvs=[{environmentSerial:'900010',status:'running'}];
  for(const status of ['queued','running','verifying','uncertain','skip']) {
    ctx.displayRows[0].status=status;
    if(status==='uncertain')ctx.displayEnvs[0].status='uncertain';
    if(status==='skip')ctx.displayEnvs[0].status='skip';
    const before=JSON.stringify([ctx.displayRows,ctx.displayEnvs]);const rendered=display('all');
    assert.equal((rendered.match(/<tr\b/g)||[]).length,1);
    assert.match(rendered,/data-as-claim="SYNTH-LIFECYCLE"/);assert.doesNotMatch(rendered,/data-as-environment=/);
    assert.equal(JSON.stringify([ctx.displayRows,ctx.displayEnvs]),before,'rendering must not mutate order or environment data');
    if(status==='queued') {
      assert.match(rendered,/已发现，待核对/);
      assert.match(rendered,/订单已读取，详情及售后资格待核对/);
      assert.doesNotMatch(rendered,/>等待<|>提交中</);
      assert.match(display('queued'),/SYNTH-LIFECYCLE/,'display label preserves queued filter membership');
      assert.doesNotMatch(display('running'),/data-as-claim=/,'discovery does not claim the order is running');
      assert.match(run('asClaimRowHtml(displayRows[0])'),/>等待</,'specified orders keep queue semantics');
    } else assert.doesNotMatch(rendered,/已发现，待核对/);
    if(status==='skip')assert.doesNotMatch(rendered,/as-status-active/);
  }
  // Activity belongs to the live environment, never inferred from order images.
  ctx.displayRows=[{environmentSerial:'900010',orderNo:'SYNTH-PENDING',status:'queued',goodsImg:'synthetic'}];
  ctx.displayEnvs=[{environmentSerial:'900010',status:'running'}];
  const liveDisplay=()=>run('asClaimTableHtml(displayRows, displayEnvs, "all", true)');
  const beforeActivity=JSON.stringify([ctx.displayRows,ctx.displayEnvs]);
  assert.match(liveDisplay(),/as-status-active">环境处理中/);
  assert.match(liveDisplay(),/无需手动确认/);
  assert.equal((liveDisplay().match(/<tr\b/g)||[]).length,1);
  assert.doesNotMatch(liveDisplay(),/>提交中</);
  assert.equal(JSON.stringify([ctx.displayRows,ctx.displayEnvs]),beforeActivity);
  assert.doesNotMatch(display('all'),/as-status-active">环境处理中/,'history is never presented as live');
  for(const envStatus of ['queued','skip','fail','stopped','uncertain']) {
    ctx.displayEnvs[0].status=envStatus;
    assert.doesNotMatch(liveDisplay(),/as-status-active">环境处理中/,'inactive environment has no activity animation');
  }
  ctx.displayEnvs[0].status='running';
  for(const orderStatus of ['skip','ok','uncertain','stopped']) {
    ctx.displayRows[0].status=orderStatus;
    assert.doesNotMatch(liveDisplay(),/as-status-active">环境处理中/,'finished orders keep their own result');
  }
  ctx.displayEnvs[0]={environmentSerial:'900010',status:'fail',errorSummary:'<script>synthetic</script>'};
  assert.match(display('all'),/&lt;script&gt;synthetic/);assert.doesNotMatch(display('all'),/<script>/);
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
  // Failed environments without order evidence can retry, using the real create path.
  state.running=false;state.starting=false;state.mode='';state.status='partial_failure';state.runId='SYNTH-SOURCE';
  const emptyFailure={environmentSerial:'900090',status:'fail',entryCount:0,submittedCount:0,blockedCount:0,failedCount:0};
  state.environments=[emptyFailure,{...emptyFailure,environmentSerial:'900091',status:'skip'},
    {...emptyFailure,environmentSerial:'900092'}];
  state.claimRows=[{environmentSerial:'900092',orderNo:'SYNTH-EXISTING',status:'uncertain'}];
  run('asSyncRetryButton(AS_STATE.claimRows)');
  assert.equal(node('asRetryFailed').disabled,true);
  assert.equal(node('asRetryEnvironments').disabled,false);
  assert.equal(run('asRetryableEnvironmentSerials().join(",")'),'900090');
  for(const status of ['running','uncertain','queued']) {
    state.status=status;assert.equal(run('asRetryableEnvironmentSerials().length'),0);
  }
  state.status='partial_failure';
  for(const key of ['entryCount','submittedCount','blockedCount','failedCount']) {
    emptyFailure[key]=1;assert.equal(run('asRetryableEnvironmentSerials().length'),0);emptyFailure[key]=0;
  }
  emptyFailure.entryCount=null;assert.equal(run('asRetryableEnvironmentSerials().length'),0);
  delete emptyFailure.entryCount;assert.equal(run('asRetryableEnvironmentSerials().length'),0);
  emptyFailure.entryCount=0;
  state.running=true;run('asSyncRetryButton(AS_STATE.claimRows)');assert.equal(node('asRetryEnvironments').disabled,true);
  state.running=false;const beforeRetryPosts=posts;confirmation=false;
  await run('asRetryFailedEnvironments()');assert.equal(posts,beforeRetryPosts);
  confirmation=true;response={status:'queued',rows:[],environments:[]};
  await run('asRetryFailedEnvironments()');await settle();
  assert.equal(posts,beforeRetryPosts+1);
  assert.deepEqual(postedBodies.at(-1).environmentSerials,['900090']);
  assert.equal(postedBodies.at(-1).retryFromRunId,'SYNTH-SOURCE');
  assert.ok(postedBodies.at(-1).idempotencyKey);
  const lookupHtml=run('asClaimEnvironmentRowHtml({environmentSerial:"900090",status:"running",note:"正在匹配 Hub 环境，尚未读取订单或提交"})');
  assert.match(lookupHtml,/匹配环境中/);
  console.log('PASS: real environment/order table lifecycle, terminal failures, reasons, filters, history, restore, and order-only actions');
})().catch(error=>{console.error(error);process.exitCode=1;});
