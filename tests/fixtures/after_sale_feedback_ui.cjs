// Synthetic responses, real task handoff / render / polling functions. No platform access.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const names=['asEnvOutcomePills','asDiscoveryEnvPill','asDiscoveryEnvConclusion','asRenderDiscoveryRows',
 'asDiscoveryItemsFromRows','asValidateDiscoveredItems','asContinueDiscovery','asFinishDiscovery',
 'asTrack','asOrderedRows','asBeginTaskEpoch','asRequireRuntimeControls','asCreateTask','asPoll',
 'asReviewFailed','asTrackPhaseLabel','asTimeline','asTrackEvidence','asTrackStageCell','asRenderTrackRows'];
const item={environmentSerial:'900001',orderNo:'SYNTHORDER',refundBillId:'900000000000001',storeName:'Synthetic'};
const good={...item,refundBillIds:[item.refundBillId],status:'ok',environmentStatus:'ok',goodsImg:'https://img.ltwebstatic.com/synthetic.jpg'};
const bad={environmentSerial:'900002',status:'fail',environmentStatus:'failed',errorSummary:'<failed>'};
const caps=['after.sale.runtime-controls.v1','after.sale.reliable-results.v1','after.sale.phase-evidence.v1'];
function setup(rows=[good,bad]) {
 const nodes=new Map(),requests=[],phases=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'all',disabled:false,scrollIntoView(){},
   button:{},querySelector(){return this.innerHTML.includes('data-as-discovery-continue')?this.button:null;}});return nodes.get(id);};
 const state={running:false,starting:false,polling:false,taskEpoch:1,mode:'',discoveryTaskId:'DISCOVERY',discoverySerials:['900001','900002'],trackTaskId:null};
 let trackResult={status:'succeeded',summary:{progressCompleted:1,progressTotal:1,rows:[{...item,status:'ok',phase:'reviewing',checkedAt:'2026-09-01T00:00:00Z'}]}};
 const executor={id:'EXEC',capabilities:caps};
 const ctx=vm.createContext({AS_STATE:state,$:node,TextEncoder,crypto:require('node:crypto').webcrypto,console,
  AS_DISCOVERY_ENV_LABEL:{queued:['run','等待'],running:['run','查找中'],ok:['ok','已完成'],failed:['bad','读取失败'],stopped:['warn','已停止']},
  AS_TL_ORDER:['submitted','reviewing','processing','shein_refunded','bank_processed'],AS_TL_LABEL:{reviewing:'审核中'},
  esc:v=>String(v||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  asThumbCell:v=>'<td>'+String(v||'—')+'</td>',asSupplementClaimAccounts(){},asSyncRetryButton(){},
  asSyncRuntimeControls(){},asSetPhase:(title,note)=>phases.push({title,note}),asProgress(){},
  asDiscoveryExecutor:async()=>({executor}),cloudFormalExecutor:async()=>executor,
  asRuntimeOptions:()=>({browserMode:'headless',concurrency:2}),
  cloudFetchJson:async(path,options)=>{requests.push({path,method:options?.method||'GET',body:options?.body && JSON.parse(options.body)});
   if(path==='/v1/after-sale/scan/DISCOVERY')return {data:{status:'succeeded',summary:{rows}}};
   if(path==='/v1/after-sale/track/validate-discovered')return {data:{items:[item],conflicts:[]}};
   if(path==='/v1/after-sale/track' && options?.method==='POST')return {data:{taskId:'TRACK'}};
   if(path==='/v1/after-sale/track/TRACK')return {data:trackResult};
   throw Error('Unexpected request: '+path);
  }});
 for(const name of names){const m=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(m,name);vm.runInContext(m[0],ctx);}
 return {ctx,state,node,requests,phases,rows,run:s=>vm.runInContext(s,ctx)};
}
async function settle(){for(let i=0;i<10;i++)await new Promise(r=>setImmediate(r));}
(async()=>{
 const c=setup();c.ctx.rows=c.rows;
 const active=c.run('asEnvOutcomePills([{environmentSerial:"900001",status:"running"}])');
 assert.match(active,/as-status-active/);assert.ok(!active.includes('900001'));
 const terminal=c.run('asEnvOutcomePills([{environmentSerial:"900001",status:"skip"}])');
 assert.ok(!terminal.includes('as-status-active'));assert.ok(!terminal.includes('900001'));
 for(const status of ['queued','running','failed','stopped']){
  c.ctx.pending=[{environmentSerial:'900001',environmentStatus:status,status}];c.run('asRenderDiscoveryRows(pending)');
  assert.ok(!c.node('asTrackRows').innerHTML.includes('平台确认无订单'));
  assert.ok(!c.node('asTrackRows').innerHTML.includes('该环境无订单'));
  assert.equal((c.node('asTrackRows').innerHTML.match(/<td(?:\s|>)/g)||[]).length,10);
 }
 c.ctx.empty=[{environmentSerial:'900001',environmentStatus:'ok',status:'empty'}];c.run('asRenderDiscoveryRows(empty)');
 assert.match(c.node('asTrackRows').innerHTML,/平台确认无订单/);
 await c.run('asFinishDiscovery("DISCOVERY",rows,"succeeded")');
 assert.ok(c.node('asTrackRows').button.onclick,'real render wires the manual button');
 assert.match(c.node('asTrackRows').innerHTML,/&lt;failed&gt;/);
 assert.match(c.node('asTrackRows').innerHTML,/SYNTHORDER/);
 assert.match(c.node('asTrackRows').innerHTML,/synthetic.jpg/);
 assert.ok(!c.node('asTrackRows').innerHTML.includes('colspan="10" style="padding:8px'));
 assert.equal(c.requests.filter(r=>r.path==='/v1/after-sale/track').length,0);
 await c.node('asTrackRows').button.onclick();await settle();
 assert.equal(c.requests.filter(r=>r.path==='/v1/after-sale/track').length,1,'manual continuation must create exactly one task');
 assert.equal(c.state.starting,false);assert.equal(c.state.running,false);assert.equal(c.state.trackTaskId,'TRACK');
 assert.ok(c.phases.some(p=>p.title==='步骤 2/2：回访运行中'));
 assert.ok(c.phases.some(p=>p.title==='步骤 2/2：回访完成'));
 assert.match(c.node('asTrackRows').innerHTML,/synthetic.jpg/,'discovered image survives empty history image');
 assert.ok(!('goodsImg' in c.requests.find(r=>r.path==='/v1/after-sale/track').body.items[0]),'display facts do not change request contract');
 // Automatic complete discovery uses the same real track and poll chain.
 const auto=setup([good]);auto.ctx.rows=auto.rows;auto.state.discoveryIntent={taskId:'DISCOVERY',executorId:'EXEC'};
 auto.state.discoverySerials=['900001'];
 await auto.run('asFinishDiscovery("DISCOVERY",rows,"succeeded")');await settle();
 assert.equal(auto.requests.filter(r=>r.path==='/v1/after-sale/track').length,1);
 assert.equal(auto.state.running,false);
 // A nominally successful response missing an entire environment is still partial.
 const missing=setup([good]);missing.ctx.rows=missing.rows;missing.state.discoveryIntent={taskId:'DISCOVERY',executorId:'EXEC'};
 await missing.run('asFinishDiscovery("DISCOVERY",rows,"succeeded")');
 assert.equal(missing.requests.filter(r=>r.path==='/v1/after-sale/track').length,0);
 assert.ok(missing.node('asTrackRows').button.onclick);
 assert.ok(missing.phases.some(p=>p.note.includes('1 个环境未查完')));
 // Stop/partial discovery never auto-starts, and stale continuation cannot write to a newer task.
 const stopped=setup([good]);stopped.ctx.rows=stopped.rows;stopped.state.stopRequested=true;
 stopped.state.discoveryIntent={taskId:'DISCOVERY',executorId:'EXEC'};
 await stopped.run('asFinishDiscovery("DISCOVERY",rows,"cancelled")');
 assert.equal(stopped.requests.filter(r=>r.path==='/v1/after-sale/track').length,0);
 for(const entry of ['asContinueDiscovery()','asFinishDiscovery("DISCOVERY",rows,"succeeded")']){
  const stale=setup([good]);stale.ctx.rows=stale.rows;
  const original=stale.ctx.cloudFetchJson;let release;
  stale.ctx.cloudFetchJson=async(path,opts)=>{if(path.includes('validate-discovered')){await new Promise(r=>release=r);}return original(path,opts);};
  const pending=stale.run(entry);await settle();assert.equal(typeof release,'function');
  stale.state.taskEpoch++;stale.state.starting=true;stale.state.mode='claim';stale.state.runId='NEW';release();await pending;
  assert.equal(stale.requests.filter(r=>r.path==='/v1/after-sale/track').length,0);
  assert.equal(stale.state.starting,true);assert.equal(stale.state.mode,'claim');
 }
 // Double-click while validation is pending does not duplicate task creation.
 const twice=setup();const original=twice.ctx.cloudFetchJson;let release;
 twice.ctx.cloudFetchJson=async(path,opts)=>{if(path.includes('validate-discovered'))await new Promise(r=>release=r);return original(path,opts);};
 const pending=twice.run('asContinueDiscovery()');await settle();await twice.run('asContinueDiscovery()');release();await pending;await settle();
 assert.equal(twice.requests.filter(r=>r.path==='/v1/after-sale/track').length,1);
 console.log('after-sale feedback UI: status, fields, partial/manual, automatic, stop, stale and double-click cases passed');
})().catch(e=>{console.error(e);process.exitCode=1});
