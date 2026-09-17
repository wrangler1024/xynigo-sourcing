const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, {dataset:{}, disabled:false,
    textContent:id === 'asClaimExport' ? '导出提交结果 Excel' : '导出 Excel'});
  return nodes.get(id);
};
const requests = [], downloads = [], messages = [], envCalls = [];
let response = async () => ({ok:true,headers:{get:()=>null},blob:async()=>({})});
let latest = async () => ({data:{runId:'restored',rows:[{orderNo:'SYNTH-1'}]}});
const state = {runId:null,claimRows:[],running:false};
const history = {runId:'history-run'};
const context = vm.createContext({
  AS_STATE:state, AS_HISTORY:history, $:node, encodeURIComponent,
  toast:message=>messages.push(message),
  fetch:async(path,options)=>{requests.push({path,options});return response();},
  cloudFetchJson:()=>latest(),
  cloudApiError:(_,status)=>Error('export failed '+status),
  workspaceDownloadName:(_,fallback)=>fallback,
  URL:{createObjectURL:()=> 'blob:synthetic',revokeObjectURL:()=>{}},
  setTimeout:fn=>fn(),
  document:{body:{appendChild:()=>{}},createElement:()=>({
    click(){downloads.push(this.download);},remove(){}})},
  asRenderClaimRows:rows=>{state.claimRows=rows;vm.runInContext('asSyncClaimExportButton()',context);},
  asSetPhase:()=>{},asProgress:()=>{},asSyncRuntimeControls:()=>{},asPoll:()=>{state.pollCalls=(state.pollCalls||0)+1;},
 asRenderEnvOutcomes:rows=>{envCalls.push(rows||[]);}
});
for (const name of ['asRunIdOf','asSyncClaimExportButton','asDownloadClaimBatch',
  'asExportClaimResults','asExportClaimHistory','asLoadLatestClaim','asOrderedRows']) {
  const match = new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);
  assert.ok(match, name);
  vm.runInContext(match[0],context);
}
const run = code=>vm.runInContext(code,context);
(async()=>{
  run('asSyncClaimExportButton()');assert.equal(node('asClaimExport').disabled,true);
  await run('asExportClaimResults()');assert.equal(requests.length,0);
  await run('asLoadLatestClaim()');assert.equal(state.runId,'restored');
  assert.equal(node('asClaimExport').disabled,false);
  await run('asExportClaimResults()');
  assert.equal(requests.at(-1).path,'/v1/operation-runs/after-sale-claim/history/restored/export');
  assert.equal(requests.at(-1).options.credentials,'same-origin');
  assert.equal(history.runId,'history-run');assert.equal(downloads.at(-1),'售后提交结果.xlsx');
  await run('asExportClaimHistory()');
  assert.equal(requests.at(-1).path,'/v1/operation-runs/after-sale-claim/history/history-run/export');
  assert.equal(state.runId,'restored');
  response = async()=>({ok:false,status:403,json:async()=>({})});
  await run('asExportClaimResults()');
  assert.equal(messages.at(-1),'export failed 403');
  assert.equal(node('asClaimExport').textContent,'导出提交结果 Excel');
  assert.equal(node('asClaimExport').disabled,false);
  let resolve;
  response=()=>new Promise(r=>resolve=r);
  const before=requests.length;
  const pending=run('asExportClaimResults()');
  run('asSyncClaimExportButton()');assert.equal(node('asClaimExport').disabled,true);
  await run('asExportClaimResults()');assert.equal(requests.length,before+1);
  state.runId=null;state.claimRows=[];
  resolve({ok:true,headers:{get:()=>null},blob:async()=>({})});await pending;
  assert.equal(node('asClaimExport').disabled,true,'clearing results during download must keep export disabled');
  let restore;
  latest=()=>new Promise(r=>restore=r);
  const restoring=run('asLoadLatestClaim()');
  state.runId='new-run';state.claimRows=[{orderNo:'SYNTH-NEW'}];
  restore({data:{runId:'old-run',rows:[{orderNo:'SYNTH-OLD'}]}});await restoring;
  assert.equal(state.runId,'new-run');assert.equal(state.claimRows[0].orderNo,'SYNTH-NEW');
  // Refresh restores polling for a running batch without changing the chosen tab.
  state.runId=null;state.claimRows=[];state.orderView='pending';
  latest=async()=>({data:{runId:'active',status:'running',createdAt:'2026-09-01T00:00:00Z',rows:[{orderNo:'SYNTH-1',status:'queued'}]}});
  await run('asLoadLatestClaim()');
  assert.equal(state.mode,'claim');assert.equal(state.running,true);assert.equal(state.pollCalls,1);
  assert.equal(state.orderView,'pending');assert.equal(state.claimItems.length,0);
  assert.equal(run('asOrderedRows(AS_STATE.claimItems,[{orderNo:"SYNTH-1"},{orderNo:"SYNTH-2"}],"orderNo").length'),2,'later rows are not dropped');
  assert.equal(node('asStop').disabled,false);
  state.runId=null;state.claimRows=[];state.running=false;
  latest=async()=>({data:{runId:'empty-active',status:'queued',progressTotal:3,rows:[]}});
  await run('asLoadLatestClaim()');
  assert.equal(state.runId,'empty-active');assert.equal(state.running,true);assert.equal(state.pollCalls,2);
  // 按环境直提的批次可能一行订单都没有（全跳过/全未登录）：
  // 环境级结果就是它的全部结果，刷新后必须仍能恢复并展示
  state.runId=null;state.claimRows=[];state.running=false;state.starting=false;
  latest=async()=>({data:{runId:'env-run',status:'completed',submitMode:'environments',
    totalCount:2,progressTotal:2,rows:[],
    environments:[{environmentSerial:'9001',status:'skip',note:'未发现丢件退款入口'},
      {environmentSerial:'9002',status:'login',errorSummary:'买家端未登录'}]}});
  envCalls.length=0;
  await run('asLoadLatestClaim()');
  assert.equal(state.runId,'env-run','env-only batch must restore');
  assert.equal(state.running,false);
  assert.equal(envCalls.length,1);assert.equal(envCalls[0].length,2);
  assert.equal(envCalls[0][0].environmentSerial,'9001');
  assert.equal(envCalls[0][1].status,'login');
  state.runId=null;state.claimRows=[];state.envSerials=null;state.environments=[];
  // An in-flight create owns the UI even before a run ID exists.
  state.runId=null;state.claimRows=[];state.running=false;state.starting=true;
  await run('asLoadLatestClaim()');assert.equal(state.runId,null);
  console.log('PASS: current/history scope, restored run identity, empty state, failed download, duplicate clicks and late responses');
})().catch(error=>{console.error(error);process.exitCode=1;});
