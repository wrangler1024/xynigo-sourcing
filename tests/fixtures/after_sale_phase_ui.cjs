const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map();
const node=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',value:'all'});return nodes.get(id);};
const ctx=vm.createContext({AS_STATE:{},$:node,asThumbCell:()=>'<td></td>',
 esc:s=>String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')});
vm.runInContext(html.slice(html.indexOf('const AS_TL_ORDER'),html.indexOf('// 手工指定的回访单：')),ctx);
const run=code=>vm.runInContext(code,ctx);
ctx.rows=[{environmentSerial:'900001',refundBillId:'SYNTH',status:'ok',phase:'evidence_required',
 phaseLabel:'审核未通过 · 待补充凭证',checkedAt:'2026-01-01T10:00:00+00:00',note:'原因：凭证不足',
 phaseEvidence:{step:2,title:'审核',detail:'<script>untrusted</script>',reason:'Comprobantes insuficientes'}},
 {environmentSerial:'900002',refundBillId:'OLD',status:'fail',phase:'reviewing',checkedAt:'2026-01-01T09:00:00+00:00',errorSummary:'本次页面未完整加载'},
 {environmentSerial:'900003',refundBillId:'LEGACY',status:'ok',phase:'refunded',phaseLabel:'已退款'}];
run('asRenderTrackRows(rows)');
assert.match(node('asTrackRows').innerHTML,/待补充凭证/);
assert.match(node('asTrackRows').innerHTML,/凭证不足/);
assert.match(node('asTrackRows').innerHTML,/上次成功读取/);
assert.match(node('asTrackRows').innerHTML,/本次状态未确认/);
assert.match(node('asTrackRows').innerHTML,/历史退款状态 · 待回访/);
assert.ok(!node('asTrackRows').innerHTML.includes('<script>'));
const timeline=run("asTimeline('evidence_required')");
assert.equal((timeline.match(/<i /g)||[]).length,5);
assert.equal((timeline.match(/class="done"/g)||[]).length,2); // first dot and connector
assert.equal((timeline.match(/class="bad"/g)||[]).length,1);
assert.ok(!run("asTimeline('refunded')").includes('class="cur"'));
node('asTrackFilter').value='evidence_required';node('asTrackFilter').onchange();
assert.match(node('asTrackRows').innerHTML,/900001/);
assert.ok(!node('asTrackRows').innerHTML.includes('900002'));
assert.match(node('asTrackFilterHint').textContent,/显示 1 \/ 3/);
node('asTrackFilter').value='unconfirmed';node('asTrackFilter').onchange();
assert.match(node('asTrackRows').innerHTML,/900002/);
assert.ok(!node('asTrackRows').innerHTML.includes('900001'));
console.log('five-node review exceptions and stale-state UI passed');
