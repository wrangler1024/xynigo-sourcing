const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',style:{}});return nodes.get(id)};
const state={rows:[],selected:new Set(),startedAt:Date.now()-120000,endedAt:null};
const ctx=vm.createContext({AS_STATE:state,$:node,Date,asEtaText:()=>''});
for(const name of ['asOrderIdentity','asSubmissionForRow','asSubmissionProtections','asSubmissionHasEvidence','asOrderRefundRows','asSubmissionBlocksSelection','asCanSelectScanRow','asItemCount','asFilteredRows','asSelectedItems','asProgress']){
 const m=new RegExp('function '+name+'\\([^]*?\\n}').exec(html);assert(m,name);vm.runInContext(m[0],ctx);
}
const run=s=>vm.runInContext(s,ctx);
state.rows=[{orderNo:'A',environmentSerial:'E',claimable:true,status:'ok',itemCount:1,deliveredDate:'2026-09-07',deliveryDateStatus:'complete'},
{orderNo:'B',environmentSerial:'F',claimable:true,status:'ok',itemCount:2,deliveredDate:'2026-09-09',deliveryDateStatus:'complete'},
{orderNo:'C',environmentSerial:'G',claimable:true,status:'ok',itemCount:null,deliveredAt:'2026-09-08',deliveryDateStatus:'unknown'},
{orderNo:'D',environmentSerial:'H',claimable:false,status:'skip',itemCount:1,deliveredDate:'2026-09-07',deliveryDateStatus:'complete'}];
node('asDateFrom').value='2026-09-07';node('asDateTo').value='2026-09-09';node('asItemKind').value='single';node('asEligibility').value='yes';
assert.equal(run('asFilteredRows().map(r=>r.orderNo).join()'),'A');
state.selected=new Set(['A','B','C']);assert.equal(run('asSelectedItems().map(r=>r.orderNo).join()'),'A');
node('asItemKind').value='multi';assert.equal(run('asFilteredRows().map(r=>r.orderNo).join()'),'B');
node('asDateFrom').value='2026-09-10';assert.equal(run('asFilteredRows().length'),0);
node('asDateFrom').value='';node('asDateTo').value='';node('asItemKind').value='unknown';assert.equal(run('asFilteredRows().map(r=>r.orderNo).join()'),'C');
run('asProgress({unit:"个环境",progressTotal:3,progressCompleted:2,rows:[{environmentSerial:"E",status:"ok",durationSeconds:30},{environmentSerial:"E",status:"ok",durationSeconds:30},{environmentSerial:"F",status:"empty",durationSeconds:10}]})');
assert.match(node('asProgressText').textContent,/平均 20秒\/个环境/);assert.match(node('asProgressText').textContent,/累计用时 02:00/);
state.endedAt=state.startedAt+90000;run('asProgress({progressTotal:1,progressCompleted:0,rows:[]})');assert.match(node('asProgressText').textContent,/累计用时 01:30/);
console.log('after-sale filters, hidden selection, delivery completeness and duration dedup PASS');
