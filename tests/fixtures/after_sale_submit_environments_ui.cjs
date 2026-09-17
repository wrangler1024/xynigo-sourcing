const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',disabled:false,innerHTML:'',hidden:true,dataset:{}});return nodes.get(id);};
let calls=[],allow=true,capability='',executor='EXEC',phaseNote='';
const state={type:'refund',running:false,starting:false,polling:false,selected:new Set(),pendingCreates:{},
  submissions:new Map(),rows:[],claimRows:[],claimItems:[],pollErrors:0,envSerials:null,environments:[]};
const context={AS_STATE:state,AS_TYPES:[],$ : node,
 TextEncoder,crypto:require('node:crypto').webcrypto,console,
 esc:value=>String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'),
 asSyncRuntimeControls:()=>{},asSyncRetryButton:()=>{},asRequireRuntimeControls:async()=>{},
 asRuntimeOptions:()=>({browserMode:'headless',concurrency:3}),
 cloudFormalExecutor:async cap=>{capability=cap;return {id:executor};},
 cloudFetchJson:async(path,opts)=>{
  if(!opts)return {data:{}};
  calls.push({path,body:opts.body?JSON.parse(opts.body):null});
  return {data:{taskId:'SCAN',runId:'RUN-1'}};
 },
 asSetPhase:(title,note)=>{phaseNote=note;node('phase').textContent=note;},asProgress:()=>{},
 asSetOrderView:()=>{},asRenderClaimRows:rows=>{state.claimRows=rows;},
 asPoll:()=>{},toast:message=>node('toast').textContent=message,
 confirm:()=>allow,asSaveList:()=>{},asSyncScanExportButton:()=>{},
};
const ctx=vm.createContext(context),run=code=>vm.runInContext(code,ctx);
function load(name){const match=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(match,name);vm.runInContext(match[0],ctx);}
for(const name of ['asParseTrackEnvironments','asSerials','asParseDirectOrders','asDirectSubmit','asScan','asCreateTask',
 'asWriteEntryReady','asSubmitItems','asSubmitByEnvironment','asRenderEnvOutcomes','asRunIdOf','asOrderedRows',
 'asSyncDirectInputMode'])load(name);
const stopStart=html.indexOf("$('asStop').onclick =");
vm.runInContext(html.slice(stopStart,html.indexOf('\n};',stopStart)+3),ctx);
function reset(){Object.assign(state,{type:'refund',running:false,starting:false,polling:false,mode:'',rows:[],claimRows:[],
 claimItems:[],selected:new Set(),submissions:new Map(),pendingCreates:{},runId:null,runLabel:'',envSerials:null,
 environments:[],stopRequested:false,pollErrors:0});
 calls=[];allow=true;capability='';executor='EXEC';phaseNote='';
 node('asDirectInputMode').value='environments';node('asDirectOrders').value='900002 900001 900002';}
const claims=()=>calls.filter(c=>c.path==='/v1/operation-runs/after-sale-claim');
(async()=>{
 // 单遍直提：一次确认后直接建提交 Run，不再先扫描、不再出核对清单
 reset();await run('asDirectSubmit()');
 assert.equal(capability,'after.sale.claim-environment.v1');
 assert.equal(calls.length,1);
 assert.equal(claims().length,1);
 assert.equal(claims()[0].body.environmentSerials.join(','),'900002,900001');
 assert.equal(claims()[0].body.environmentSerials.length,2);
 assert.equal(claims()[0].body.browserMode,'headless');assert.equal(claims()[0].body.concurrency,3);
 assert.ok(!calls.some(c=>c.path==='/v1/after-sale/scan'),'direct submit must not scan first');
 assert.equal(state.runLabel,'按环境提交');assert.equal(state.starting,false);
 assert.equal(JSON.stringify(state.envSerials),JSON.stringify(['900002','900001']));
 assert.equal(node('asEnvOutcomes').hidden,true);

 // 环境结果条：环境级反馈按状态着色，空列表隐藏
 run('asRenderEnvOutcomes([{environmentSerial:"900001",status:"ok",submittedCount:2},'
   +'{environmentSerial:"900002",status:"skip",note:"未发现丢件退款入口"}])');
 assert.equal(node('asEnvOutcomes').hidden,false);
 assert.match(node('asEnvOutcomes').innerHTML,/900001/);
 assert.match(node('asEnvOutcomes').innerHTML,/无售后入口/);
 assert.match(node('asEnvOutcomes').innerHTML,/提交2/);
 run('asRenderEnvOutcomes([])');
 assert.equal(node('asEnvOutcomes').hidden,true);

 // 拒绝确认 / 非法输入 / 超上限 / 规划类型：都不许发写请求
 reset();allow=false;await run('asDirectSubmit()');assert.equal(calls.length,0);
 for(const [input,marker] of [['','请先输入'],['ENV ORDER','只填数字'],
   [Array.from({length:301},(_,i)=>String(i)).join(' '),'300']]){
  reset();node('asDirectOrders').value=input;await run('asDirectSubmit()');
  assert.equal(calls.length,0,input);assert.match(node('phase').textContent,new RegExp(marker));
 }
 reset();state.type='planned';await run('asDirectSubmit()');assert.equal(calls.length,0);

 // 起跑互斥：starting 期间的第二次点击不产生第二份报文
 reset();let release;const hold=new Promise(r=>release=r);
 context.cloudFormalExecutor=async()=>{await hold;capability='after.sale.claim-environment.v1';return {id:executor};};
 const first=run('asDirectSubmit()'),second=run('asDirectSubmit()');
 assert.equal(state.starting,true);release();await Promise.all([first,second]);
 assert.equal(claims().length,1);
 context.cloudFormalExecutor=async cap=>{capability=cap;return {id:executor};};

 // 运行中点停止：走提交 Run 的 cancel，与按单提交同一条路
 reset();await run('asDirectSubmit()');assert.equal(claims().length,1);
 await node('asStop').onclick();
 assert.ok(calls.some(c=>c.path==='/v1/operation-runs/after-sale-claim/RUN-1/cancel'));

 // 帮助文案必须带环境上限提示
 reset();run('asSyncDirectInputMode()');
 assert.match(node('asDirectHelp').textContent,/最多 300 个环境/);
 assert.equal(node('asDirectSubmit').textContent,'按环境提交');

 // 按订单号（完整信息）入口保持不变
 reset();node('asDirectInputMode').value='orders';node('asDirectOrders').value='900001 SYNTH-A；900002 SYNTH-B';
 run('asSyncDirectInputMode()');assert.equal(node('asDirectSubmit').textContent,'直接提交');
 await run('asDirectSubmit()');
 assert.equal(claims().length,1);assert.equal(claims()[0].body.items.length,2);
 assert.equal(claims()[0].body.environmentSerials,undefined);
 assert.equal(calls.length,1);
 console.log('PASS: environment submit is single-pass direct write with env outcomes and 300-env cap');
})().catch(error=>{console.error(error);process.exitCode=1;});
