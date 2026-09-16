const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{checked:false,attrs:{'aria-checked':'true'},getAttribute(k){return this.attrs[k];},setAttribute(k,v){this.attrs[k]=v;},disabled:false,value:'SYNTH-A SYNTH-B',dataset:{},textContent:'',innerHTML:''});return nodes.get(id);}
const chips=[2,3,5].map(n=>({dataset:{asConcurrency:String(n)},disabled:false,active:n===2}));
const state={running:false,type:'refund',claimRows:[]};
const requests=[];
let capability=true, hook=()=>{};
let ctx;
ctx=vm.createContext({AS_STATE:state,AS_TYPES:[], $:node, document:{
 querySelector:()=>chips.find(c=>c.active),querySelectorAll:()=>chips},
 cloudFormalExecutor:async()=>{hook();return {id:'executor',capabilities:capability?['after.sale.runtime-controls.v1']:[]};},
 cloudFetchJson:async(path,request)=>{requests.push({path,body:JSON.parse(request.body)});return {data:{taskId:'task',runId:'run'}};},
 asSetPhase:(title,text)=>{assert.notEqual(title,'发起失败',text);vm.runInContext('asSyncRuntimeControls()',ctx);},asSaveList:()=>{},asProgress:()=>{},
 asRenderClaimRows:()=>{},asRenderTrackRows:()=>{},asPoll:()=>{}, Set,Date,Math,Number,
});
for(const name of ['asRuntimeOptions','asSyncRuntimeControls','asRequireRuntimeControls',
 'asOrderedRows','asSerials','asRunIdOf','asWriteEntryReady','asScan','asTrack','asSubmitItems']){
 const m=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(m,name);vm.runInContext(m[0],ctx);
}
const run=code=>vm.runInContext(code,ctx);
const actions=['asScan()','asTrack([{environmentSerial:"A",orderNo:"SYNTH",refundBillId:"B"}])',
 'asSubmitItems([{environmentSerial:"A",orderNo:"SYNTH"}])'];
(async()=>{
 assert.match(html,/id="asHeadless"/);
 assert.match(html,/id="asHeadless"[^>]*aria-checked="true"/);
 for(const action of actions){
  state.running=false;await run(action);
  assert.equal(requests.at(-1).body.browserMode,'headless');assert.equal(requests.at(-1).body.concurrency,2);
  assert.equal(node('asHeadless').disabled,true);assert.ok(chips.every(c=>c.disabled));
 }
 node('asHeadless').setAttribute('aria-checked','false');chips.forEach(c=>c.active=c.dataset.asConcurrency==='5');
 for(const action of actions){state.running=false;await run(action);assert.equal(requests.at(-1).body.browserMode,'visible');assert.equal(requests.at(-1).body.concurrency,5);}
 state.running=false;run('asSyncRuntimeControls()');assert.equal(node('asHeadless').disabled,false);
 const before=requests.length;capability=false;
 for(const action of actions){state.running=false;await run(action);}
 assert.equal(requests.length,before,'old executor must not receive runtime-control requests');
 capability=true;state.running=false;
 hook=()=>{node('asHeadless').setAttribute('aria-checked','true');chips.forEach(c=>c.active=c.dataset.asConcurrency==='3');};
 await run('asScan()');
 assert.equal(requests.at(-1).body.browserMode,'visible');assert.equal(requests.at(-1).body.concurrency,5,'capture settings before executor lookup');
 console.log('PASS: all three task payloads, headless/concurrency defaults, explicit choices, busy controls, capability gate and request snapshot');
})().catch(e=>{console.error(e);process.exitCode=1});
