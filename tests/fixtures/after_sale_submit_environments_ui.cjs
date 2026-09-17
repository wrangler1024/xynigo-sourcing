const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',disabled:false,innerHTML:'',dataset:{}});return nodes.get(id);};
let calls=[],choice=false,preview,executor='EXEC',response={},failPoll=false,hold=Promise.resolve();
const state={type:'refund',running:false,starting:false,polling:false,selected:new Set(),pendingCreates:{},submissions:new Map(),rows:[],claimRows:[],pollErrors:0};
const context={AS_STATE:state,AS_TYPES:[],AS_RECOVERABLE_CLAIM_STATUS:['fail','login','inuse','stopped'],$:node,
 TextEncoder,crypto:require('node:crypto').webcrypto,console,
 asSyncRuntimeControls:()=>{},asSyncScanExportButton:()=>{},asSetOrderView:()=>{},asSaveList:()=>{},
 asRuntimeOptions:()=>({browserMode:'headless',concurrency:3}),asRequireRuntimeControls:async()=>{},
 cloudFormalExecutor:async()=>{await hold;return {id:executor};},
 cloudFetchJson:async(path,opts)=>{
  if(!opts){if(failPoll)throw Error('offline');return response;}
  calls.push({path,body:opts.body?JSON.parse(opts.body):null});return {data:{taskId:'SCAN',runId:'RUN'}};
 },
 asSetPhase:(title,note)=>{node('phase').textContent=title;node('note').textContent=note;},asProgress:()=>{},
 asRenderScanRows:rows=>state.rows=rows,asRenderClaimRows:rows=>state.claimRows=rows,asSyncRetryButton:()=>{},
 asPoll:()=>{},toast:message=>node('toast').textContent=message,
 asConfirmEnvironmentClaims:async data=>{preview=data;return typeof choice==='function'?choice():choice;},
 asFilteredRows:()=>{throw Error('direct environment scope must not inherit list filters');},confirm:()=>true,
};
const ctx=vm.createContext(context),run=code=>vm.runInContext(code,ctx);
function load(name){const match=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(match,name);vm.runInContext(match[0],ctx);}
for(const name of ['asParseTrackEnvironments','asSerials','asParseDirectOrders','asDirectSubmit','asScan','asCreateTask',
 'asWriteEntryReady','asSubmitItems','asRunIdOf','asOrderedRows','asItemCount','asClaimItemsFromRows','asOrderIdentity',
 'asSubmissionForRow','asSubmissionProtections','asSubmissionBlocksSelection','asCanSelectScanRow','asClaimNeedsReconciliation',
 'asRecoverableClaimRows','asEnvironmentClaimPreview','asFinishEnvironmentSubmit','asSyncDirectInputMode'])load(name);
const pollSource=/async function asPoll\([^]*?\n}/.exec(html)[0];
const stopStart=html.indexOf("$('asStop').onclick =");
vm.runInContext(html.slice(stopStart,html.indexOf('\n};',stopStart)+3),ctx);
const row=(env,order,extra={})=>({environmentSerial:env,orderNo:order,status:'ok',claimable:true,
 goodsImages:['https://img.ltwebstatic.com/synthetic-a.jpg','https://img.ltwebstatic.com/synthetic-b.jpg'],
 goodsItems:[{name:'Synthetic',quantity:2}],itemCount:2,deliveredAt:'2026-09-01',...extra});
const rows=[row('900001','SYNTH-A'),row('900002','SYNTH-B'),row('900002','SYNTH-C'),
 row('900001','SYNTH-BLOCKED',{status:'blocked',claimable:false,errorSummary:'审核中'}),
 row('900001','SYNTH-PROTECTED')];
function reset(){Object.assign(state,{type:'refund',running:false,starting:false,polling:false,mode:'',rows:[],claimRows:[],
 selected:new Set(),submissions:new Map(),pendingCreates:{},directSubmitScan:null,stopRequested:false,pollErrors:0});
 calls=[];choice=false;executor='EXEC';failPoll=false;hold=Promise.resolve();preview=null;ctx.asPoll=()=>{};
 node('asDirectInputMode').value='environments';node('asDirectOrders').value='900002 900001 900002';node('asSerials').value='999999';}
async function complete(status='succeeded',dataRows=rows){
 response={data:{status,summary:{totalCount:2,rows:dataRows}}};vm.runInContext(pollSource,ctx);await run('asPoll()');
}
const claims=()=>calls.filter(c=>c.path==='/v1/operation-runs/after-sale-claim');
(async()=>{
 reset();let release;hold=new Promise(r=>release=r);
 const first=run('asDirectSubmit()'),second=run('asDirectSubmit()');assert.equal(state.starting,true);release();await Promise.all([first,second]);
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/v1/after-sale/scan');
 assert.equal(calls[0].body.environmentSerials.join(','),'900002,900001');
 assert.equal(calls[0].body.browserMode,'headless');assert.equal(calls[0].body.concurrency,3);assert.equal(claims().length,0);
 assert.equal(node('asSerials').value,'999999');await complete();assert.equal(claims().length,0,'cancel never writes');
 assert.equal(state.directSubmitScan,null);assert.equal(state.starting,false);

 reset();state.submissions.set(run('asOrderIdentity({environmentSerial:"900001",orderNo:"SYNTH-PROTECTED"})'),
  {row:{status:'ok',refundBillId:'OLD-BILL'},protections:[{runId:'OLD',row:{status:'ok'}}]});
 await run('asDirectSubmit()');choice=true;await complete();
 assert.equal(claims().length,1);const items=claims()[0].body.items;
 assert.equal(items.map(i=>i.orderNo).join(','),'SYNTH-B,SYNTH-C,SYNTH-A');
 assert.equal(items[0].goodsImages.length,2);assert.equal(items[0].goodsItems[0].quantity,2);assert.equal(items[0].itemCount,2);
 assert.equal(preview.environments[1].details.filter(r=>!r.canSubmit).length,2);
 await run('asFinishEnvironmentSubmit("SCAN",AS_STATE.rows,"succeeded")');assert.equal(claims().length,1,'one scan can only confirm once');

 for(const status of ['failed','cancelled','uncertain']){
  reset();await run('asDirectSubmit()');choice=true;await complete(status);assert.equal(claims().length,0);assert.equal(preview,null);
 }
 reset();await run('asDirectSubmit()');choice=true;await node('asStop').onclick();await complete();
 assert.equal(claims().length,0);assert.equal(preview,null,'stop disarms even if scan finishes successfully');
 assert.ok(calls.some(c=>c.path==='/v1/after-sale/scan/SCAN/cancel'));
 reset();await run('asDirectSubmit()');choice=true;failPoll=true;vm.runInContext(pollSource,ctx);
 for(let i=0;i<5;i++)await run('asPoll()');assert.equal(state.directSubmitScan,null);
 failPoll=false;await complete();assert.equal(claims().length,0);assert.equal(preview,null);

 reset();await run('asDirectSubmit()');choice=()=>{executor='OTHER';return true;};await complete();
 assert.equal(claims().length,0);assert.equal(node('phase').textContent,'执行器已变化');
  reset();await run('asDirectSubmit()');choice=()=>{state.rows=[];return true;};await complete();
  assert.equal(claims().length,0);assert.match(node('toast').textContent,/已变化/);
  reset();await run('asDirectSubmit()');choice=()=>{state.rows[0].amount='changed';return true;};
  await complete('succeeded',rows.map(r=>({...r,amount:'100'})));assert.equal(claims().length,0,'in-place row mutation invalidates confirmation');
 reset();await run('asDirectSubmit()');state.directSubmitScan=null;choice=true;await complete();
 assert.equal(claims().length,0,'ordinary/reloaded scan never auto-submits');
 reset();await run('asScan()');choice=true;await complete();assert.equal(preview,null);assert.equal(claims().length,0);

 for(const invalid of ['','ENV ORDER',Array.from({length:301},(_,i)=>String(i)).join(' ')]){
  reset();node('asDirectOrders').value=invalid;await run('asDirectSubmit()');assert.equal(calls.length,0);
 }
 reset();state.type='planned';await run('asDirectSubmit()');assert.equal(calls.length,0);
 reset();node('asDirectInputMode').value='orders';node('asDirectOrders').value='900001 SYNTH-A；900002 SYNTH-B';
 run('asSyncDirectInputMode()');assert.equal(node('asDirectSubmit').textContent,'直接提交');await run('asDirectSubmit()');
 assert.equal(claims().length,1);assert.equal(claims()[0].body.items.length,2);assert.equal(calls.length,1);

 // Conflicting environment ownership and contradictory duplicate rows are excluded.
 reset();ctx.conflicting=[row('900001','SAME'),row('900002','SAME'),row('900001','DIFFERENT'),
   row('900001','DIFFERENT',{status:'blocked',claimable:false})];
 const conflict=run('asEnvironmentClaimPreview(["900001","900002"],conflicting)');assert.equal(conflict.items.length,0);
 reset();await run('asDirectSubmit()');choice=true;await complete('succeeded',[]);assert.equal(claims().length,0);
 reset();await run('asDirectSubmit()');choice=true;
 await complete('succeeded',Array.from({length:501},(_,i)=>row('900001','SYNTH-'+i)));assert.equal(claims().length,0);
 console.log('PASS: environment scan -> explicit confirmation -> protected claim, cancellation, failure, stale scope and executor guards');
})().catch(error=>{console.error(error);process.exitCode=1;});
