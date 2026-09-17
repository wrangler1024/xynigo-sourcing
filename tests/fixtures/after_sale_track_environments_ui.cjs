const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',placeholder:''});return nodes.get(id);};
let calls=[],tracked=[],choice=true,hold=Promise.resolve(),fail=null,preview,progress;
const state={running:false,starting:false};
const match={items:[{environmentSerial:'900002',orderNo:'ORDER',refundBillId:'BILL',storeName:''}],
 environments:[{environmentSerial:'900002',billCount:1,note:''},{environmentSerial:'900001',billCount:0,note:'系统没有记录'}]};
const ctx=vm.createContext({AS_STATE:state,$:node,asSyncRuntimeControls:()=>{},
 asProgress:data=>progress=data,
 asSetPhase:(title,note)=>{node('phase').textContent=title;node('note').textContent=note;},
 Headers,Request,Response,
 // Keep the production cloudFetchJson wrapper: Request applies the same implicit
 // text/plain content type as a browser when a JSON string lacks its header.
 fetch:async(path,opts)=>{
  const request=new Request('https://workspace.example.test'+path,opts);
  calls.push({path,body:JSON.parse(opts.body),headers:request.headers,credentials:request.credentials});
  await hold;
  return new Response(JSON.stringify(fail || {data:match}),{status:fail?422:200});
 },
 asConfirmTrackMatches:async data=>{preview=data;return choice;},
 asTrack:async items=>{assert.equal(state.starting,false);tracked.push(items);},
 esc:s=>String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),console});
for(const name of ['cloudApiError','cloudFetchJson','asParseManualBills','asTrackManual','asParseTrackEnvironments','asSyncTrackInputMode','asTrackEnvironments']){
 const m=new RegExp('(?:async )?function '+name+'\\([^]*?\\n}').exec(html);assert.ok(m,name);vm.runInContext(m[0],ctx);
}
const run=code=>vm.runInContext(code,ctx);
(async()=>{
 node('asTrackInputMode').value='environments';
 node('asTrackBills').value='900002 900001，900002；900003\n900004,900005;900006';
 assert.equal(run('asParseTrackEnvironments($("asTrackBills").value).serials.join(",")'),'900002,900001,900003,900004,900005,900006');
 const twenty=Array.from({length:20},(_,i)=>String(900000+i)).join(' ');ctx.twenty=twenty;
 assert.equal(run('asParseTrackEnvironments(twenty).serials.length'),20);
 let release;hold=new Promise(r=>release=r);
 const first=run('asTrackManual()');await run('asTrackManual()');
 assert.equal(progress.progressTotal,0);assert.equal(progress.progressCompleted,0);
 assert.equal(state.startedAt,null);assert.equal(state.endedAt,null);
 assert.equal(state.starting,true);assert.equal(calls.length,1);release();await first;
 assert.equal(calls[0].path,'/v1/after-sale/track/resolve');
 assert.equal(calls[0].headers.get('Content-Type'),'application/json');
 assert.equal(calls[0].headers.get('X-Xynigo-Web-CSRF'),'same-origin');
 assert.equal(calls[0].credentials,'same-origin');
 assert.equal(calls[0].body.environmentSerials.join(','),'900002,900001,900003,900004,900005,900006');
 assert.equal(preview.environments[1].billCount,0);assert.equal(tracked.length,1);
 assert.deepEqual(tracked[0],match.items);assert.equal(state.starting,false);
 hold=Promise.resolve();choice=false;tracked=[];await run('asTrackManual()');assert.equal(tracked.length,0);
 fail={detail:'匹配退款单超过上限，请缩小环境范围'};await run('asTrackManual()');assert.equal(state.starting,false);assert.equal(tracked.length,0);
 assert.equal(node('phase').textContent,'退款单匹配失败');
 assert.equal(node('note').textContent,fail.detail);
 fail={detail:{code:'permission_denied',message:'当前账号无权限'}};
 await run('asTrackManual()');
 assert.match(node('note').textContent,/当前角色没有执行此操作的权限/);
 assert.match(node('note').textContent,/当前账号无权限/);
 fail=null;
 for(const invalid of ['','ENV ORDER BILL',Array.from({length:301},(_,i)=>String(i)).join(' ')]){
  calls=[];node('asTrackBills').value=invalid;await run('asTrackManual()');assert.equal(calls.length,0);assert.equal(node('phase').textContent,'请检查环境序号');
 }
 node('asTrackInputMode').value='bills';node('asTrackBills').value='ENV ORDER BILL；ENV ORDER BILL2';
 run('asSyncTrackInputMode()');assert.equal(node('asTrackInputLabel').textContent,'指定退款单回访');
 await run('asTrackManual()');assert.equal(tracked[0].length,2);assert.equal(calls.length,0);
 node('asTrackBills').value='900001 900002';await run('asTrackManual()');
 assert.match(node('note').textContent,/仅有环境序号/);
 // Exercise the actual dialog: escaped identities, empty-match disabled action,
 // and closing with Escape/cancel resolves without starting work.
 let dialog,closed;
 ctx.document={createElement:()=>dialog={className:'',innerHTML:'',setAttribute:()=>{},
  querySelector:()=>({}),addEventListener:(name,fn)=>closed=fn,remove:()=>{},showModal:()=>{}},body:{appendChild:()=>{}}};
 vm.runInContext(/function asConfirmTrackMatches\([^]*?\n}/.exec(html)[0],ctx);
 ctx.unsafe={items:[],environments:[{environmentSerial:'<img>',note:'<script>'}]};
 const pending=run('asConfirmTrackMatches(unsafe)');
 assert.match(dialog.innerHTML,/data-confirm disabled/);assert.ok(!dialog.innerHTML.includes('<script>'));
 assert.ok(dialog.innerHTML.includes('&lt;img&gt;'));closed();assert.equal(await pending,false);
 console.log('environment tracking UI workflow passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
