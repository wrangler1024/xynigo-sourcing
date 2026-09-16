const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map();const node=id=>{if(!nodes.has(id))nodes.set(id,{style:{},value:'ENV',dataset:{},disabled:false,innerHTML:'',textContent:'',hidden:false});return nodes.get(id);};
const state={running:false,starting:false,type:'refund',selected:new Set(),pendingCreates:{},claimRows:[],polling:false};
let calls=[],hold=Promise.resolve(),fail=false,response={data:{taskId:'TASK',runId:'RUN'}};
const ctx=vm.createContext({AS_STATE:state,AS_TYPES:[], $:node,crypto:require('node:crypto').webcrypto,
 asRuntimeOptions:()=>({browserMode:'headless',concurrency:2}),asSyncRuntimeControls:()=>{},
 cloudFormalExecutor:async()=>{await hold;return {id:'EXEC'};},asRequireRuntimeControls:()=>{},
 cloudFetchJson:async(path,options)=>{calls.push({path,body:options?.body?JSON.parse(options.body):null});if(fail)throw Error('lost response');return response;},
 asSetPhase:(title,text)=>{node('asPhaseTitle').textContent=title;},asSaveList:()=>{},asSerials:()=>['ENV'],
 asRenderClaimRows:()=>{},asRenderTrackRows:()=>{},asRenderScanRows:()=>{},asProgress:()=>{},asPoll:()=>{},asSyncRetryButton:()=>{},
 asRunIdOf:d=>d.runId,toast:()=>{},console});
for(const name of ['asSyncScanExportButton','asCreateTask','asWriteEntryReady','asScan','asTrack','asSubmitItems','asOrderedRows','asTrackItemsFromRows','asParseManualBills','asTrackManual']){
 const m=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(m,name);vm.runInContext(m[0],ctx);
}
for(const id of ['asStop','asClearResult']){const a=html.indexOf("$('"+id+"').onclick =");vm.runInContext(html.slice(a,html.indexOf('\n};',a)+3),ctx);}
const run=code=>vm.runInContext(code,ctx);
(async()=>{
 for(const action of ['asScan()', 'asTrack([{environmentSerial:"ENV",orderNo:"ORDER",refundBillId:"B"}])','asSubmitItems([{environmentSerial:"ENV",orderNo:"ORDER"}])']){
  state.running=false;calls=[];let release;hold=new Promise(r=>release=r);
  const a=run(action), b=run(action);assert.equal(state.starting,true);release();await Promise.all([a,b]);
  assert.equal(calls.length,1,action);assert.equal(state.starting,false);assert.equal(state.running,true);
 }
 hold=Promise.resolve();state.running=false;calls=[];fail=true;
 await run('asSubmitItems([{environmentSerial:"ENV",orderNo:"ORDER"}])');assert.equal(state.starting,false);
 fail=false;await run('asSubmitItems([{environmentSerial:"ENV",orderNo:"ORDER"}])');
 assert.equal(calls[0].body.idempotencyKey,calls[1].body.idempotencyKey);
 state.mode='track';state.trackTaskId='TRACK';state.scanTaskId='OLD_SCAN';calls=[];
 await node('asStop').onclick();assert.equal(calls[0].path,'/v1/after-sale/track/TRACK/cancel');
 state.mode='claim';state.runId='ACTIVE';node('asClearResult').onclick();assert.equal(state.runId,'ACTIVE');
 const items=run('asTrackItemsFromRows([{environmentSerial:"ENV",orderNo:"ORDER",refundBillId:"B2",refunds:[{refundBillId:"B1"},{refundBillId:"B2"}]}])');
 assert.equal(items.map(r=>r.refundBillId).join(','),'B1,B2');
 state.running=false;calls=[];node('asTrackBills').value='ENV ORDER B1；BROKEN';await run('asTrackManual()');
 assert.equal(calls.length,0);assert.equal(node('asPhaseTitle').textContent,'请检查回访清单');
 node('asTrackBills').value='ENV ORDER B1；ENV ORDER B1';await run('asTrackManual()');assert.equal(calls.length,0);
 // Real poll function must distinguish failed/cancelled/uncertain terminal states.
 vm.runInContext(/async function asPoll\([^]*?\n}/.exec(html)[0],ctx);
 for(const status of ['cancelled','failed','uncertain']){
  state.mode='claim';state.runId='RUN';state.running=true;state.claimItems=[];
  response={data:{status,rows:[],failedCount:0,successCount:0}};await run('asPoll()');
  assert.match(node('asPhaseTitle').textContent, /已停止|失败|待核对/);
 }
 // Export captures the task at click time and stays locked during polling.
 vm.runInContext(/async function asExportScan\([^]*?\n}/.exec(html)[0],ctx);
 state.scanTaskId='SCAN';state.rows=[{orderNo:'ORDER'}];
 run('asSyncScanExportButton()');assert.equal(node('asScanExport').disabled,false);
 let releaseExport;let exportPath='';
 ctx.fetch=async path=>{exportPath=path;await new Promise(r=>releaseExport=r);throw Error('network');};
 node('asScanExport').textContent='导出清单 Excel';
 const downloading=run('asExportScan()');
 state.scanTaskId='NEXT';state.rows=[];
 run('asSyncScanExportButton()');assert.equal(node('asScanExport').disabled,true);
 releaseExport();await downloading;
 assert.equal(exportPath,'/v1/after-sale/scan/SCAN/export');
 assert.equal(node('asScanExport').disabled,true);
 assert.equal(node('asScanExport').textContent,'导出清单 Excel');
 state.rows=[{orderNo:'NEXT'}];run('asSyncScanExportButton()');
 assert.equal(node('asScanExport').disabled,false);
 // Upgraded devices must refresh stale capability caches before rejecting work.
 vm.runInContext(/async function asRequireRuntimeControls\([^]*?\n}/.exec(html)[0],ctx);
 ctx.localExecutorDevices=[];ctx.renderLocalExecutorDevices=()=>{};
 state.mode='scan';calls=[];
 response={items:[{id:'EXEC',connectivity:'online',capabilities:['after.sale.runtime-controls.v1']}]};
 await run('asRequireRuntimeControls({id:"EXEC",capabilities:["after.sale.scan.v1"]})');
 assert.equal(calls[0].path,'/v1/executors');
 response={items:[{id:'OTHER',connectivity:'online',capabilities:['after.sale.runtime-controls.v1']}]};
 await assert.rejects(run('asRequireRuntimeControls({id:"EXEC",capabilities:[]})'),/离线或不可用/);
 response={items:[{id:'EXEC',connectivity:'online',capabilities:[]}]};
 await assert.rejects(run('asRequireRuntimeControls({id:"EXEC",capabilities:[]})'),/升级本地执行器/);
 state.mode='track';
 response={items:[{id:'EXEC',connectivity:'online',capabilities:['after.sale.runtime-controls.v1']}]};
 await assert.rejects(run('asRequireRuntimeControls({id:"EXEC",capabilities:[]})'),/完整退款记录/);
 console.log('task lifecycle, idempotency, package fan-out and validation PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
