const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('src/purchase_tool/web/index.html','utf8');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,disabled:false,checked:false,
 innerHTML:'',textContent:'',attrs:{},style:{},dataset:{},scrolls:0,
 setAttribute(k,v){this.attrs[k]=v;},scrollIntoView(){this.scrolls++;}});return nodes.get(id);}
node('asBizSummary').children=Array.from({length:6},()=>{const b={textContent:''};return {querySelector:()=>b};});
const state={rows:[],claimRows:[],submissions:new Map(),selected:new Set(),type:'refund',mode:'claim',
 runId:'RUN-A',running:false,starting:false,claimFilter:'all',orderView:'pending'};
const context=vm.createContext({AS_STATE:state,$:node,console,
 esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
 safeProcurementImageUrl:v=>String(v||'').startsWith('https://img.ltwebstatic.com/')?v:'',
 asRefreshOrderDetail:()=>{},asSyncScanExportButton:()=>{},asSyncClaimExportButton:()=>{},asSyncRetryButton:()=>{},
 asRenderEnvOutcomes:()=>{},
 asDiscoveryClearPersisted:()=>{},
 AFTER_SALE_TYPE:'丢件退款',Date});
const run=s=>vm.runInContext(s,context);
for(const name of ['AS_SCAN_PILL','AS_CLAIM_PILL','AS_RECOVERABLE_CLAIM_STATUS']){
 const expression=new RegExp('const '+name+' = (?:\\{[^]*?\\n}|[^;]+);').exec(html);assert(expression,name);run(expression[0]);
}
for(const name of ['asSetOrderView','asOrderIdentity','asSubmissionForRow','asSubmissionProtections','asSubmissionHasEvidence','asOrderRefundRows','asSubmissionBlocksSelection',
 'asCanSelectScanRow','asPendingPill','asPendingReasonHtml','asScanPill','asClaimNeedsReconciliation',
 'asClaimPill','asClaimPillText','asClaimReasonHtml','asItemCount','asFilteredRows','asSelectedItems',
 'asRecoverableClaimRows','asReconcileScanRows','asSyncSelection','asRenderScanRows','asRenderClaimRows',
 'asRenderClaimFilters','asClaimMatchesFilter','asOrderDetailHtml','asGoodsImages','asScanGoodsHtml',
 'asClaimEnvironmentRows','asEnvironmentSummary','asClaimEnvironmentRowHtml','asClaimTableHtml','asEnvOutcomePills',
 'asTrackItemsFromRows','asClaimRowHtml','asRefundPathText','asRefundAccountHtml','asSupplementClaimAccounts']){
 const match=new RegExp('function '+name+'\\([^]*?\\n}').exec(html);assert(match,name);run(match[0]);
}
const scan=(orderNo,environmentSerial='ENV-A')=>({orderNo,environmentSerial,status:'ok',claimable:true,
 reasonCode:'eligible',platformStatus:'Entregado',reasonSource:'pre_info',checkedAt:'2026-09-01T01:00:00Z',
 itemCount:2,goodsImages:['https://img.ltwebstatic.com/synthetic-a.jpg','https://img.ltwebstatic.com/synthetic-b.jpg'],
 goodsItems:[{name:'Synthetic',quantity:2}],packageCount:1,deliveredDate:'2026-09-01',deliveryDateStatus:'complete'});
state.rows=[Object.freeze(scan('ORDER-A')),Object.freeze(scan('ORDER-B'))];
const original=JSON.stringify(state.rows);state.selected=new Set(['ORDER-A','ORDER-B']);
context.update={orderNo:'ORDER-A',environmentSerial:'ENV-A',status:'uncertain',errorSummary:'synthetic lost response'};
run('asReconcileScanRows([update])');
assert.equal(JSON.stringify(state.rows),original,'submission must not overwrite scan facts');
assert.equal(state.selected.has('ORDER-A'),false,'uncertain selection is removed');
assert.equal(state.selected.has('ORDER-B'),true);
assert.match(node('asScanRows').innerHTML,/待核对/);
assert.equal(run('asSelectedItems().map(r=>r.orderNo).join()'),'ORDER-B');
assert.equal(run('asSelectedItems()[0].goodsImages.length'),2,'P0 product arrays remain in request projection');
assert.match(run('asOrderDetailHtml(AS_STATE.rows[0], update)'),/Entregado/);
assert.match(run('asOrderDetailHtml(AS_STATE.rows[0], update)'),/2026-09-01T01:00:00Z/);
// A later definitive result updates the locked row instead of being skipped.
context.update={...context.update,status:'ok',errorSummary:'',refundBillId:'12345',operationCompletedAt:'2026-09-01T01:02:03Z',refunds:[{refundBillId:'12345',phaseLabel:'审核中',source:'submit_response'}]};
run('asReconcileScanRows([update])');assert.match(node('asScanRows').innerHTML,/已受理/);
assert.doesNotMatch(node('asScanRows').innerHTML,/结果未确认/);
assert.equal(JSON.stringify(state.rows),original);
context.update.status='fail';assert.equal(run('asSubmissionForRow(AS_STATE.rows[0]).status'),'ok','caller mutation cannot alter cached evidence');
// Only explicitly recoverable, evidence-free failures may be selected again.
for(const status of ['queued','running','verifying','ok','blocked','uncertain','skip','empty','unexpected']){
 context.sample={orderNo:'ORDER-B',environmentSerial:'ENV-A',status};
 assert.equal(run('asSubmissionBlocksSelection(sample)'),true,status);
}
for(const status of ['fail','login','inuse','stopped']){
 context.sample={orderNo:'ORDER-B',environmentSerial:'ENV-A',status};
 assert.equal(run('asSubmissionBlocksSelection(sample)'),false,status);
 context.sample.refundBillId='12345';assert.equal(run('asSubmissionBlocksSelection(sample)'),true,status+' with bill');
 context.sample.refundBillId='';context.sample.refunds=[{refundBillId:'12345'}];
 assert.equal(run('asSubmissionBlocksSelection(sample)'),true,status+' with receipt');
}
context.sample={orderNo:'ORDER-B',environmentSerial:'ENV-A',status:'fail',errorSummary:'提交后未跳转成功页'};
assert.equal(run('asSubmissionBlocksSelection(sample)'),true,'legacy uncertainty remains locked');
// Environment identity and run identity both matter.
context.other=scan('ORDER-A','ENV-B');assert.equal(run('asCanSelectScanRow(other)'),true);
state.runId='RUN-B';context.update={orderNo:'ORDER-A',environmentSerial:'ENV-A',status:'queued'};run('asReconcileScanRows([update])');
context.update.status='fail';run('asReconcileScanRows([update],"RUN-A")');
assert.equal(run('asSubmissionForRow(AS_STATE.rows[0]).status'),'queued','late older run ignored');
// A new batch's ordinary failure must never erase a prior accepted receipt.
run('asReconcileScanRows([update])');
assert.equal(run('asSubmissionForRow(AS_STATE.rows[0]).status'),'fail');
assert.equal(run('asCanSelectScanRow(AS_STATE.rows[0])'),false,'old accepted receipt still protects');
assert.match(run('asPendingPill(AS_STATE.rows[0])'),/已有提交记录/);
assert.match(run('asOrderDetailHtml(AS_STATE.rows[0], asSubmissionForRow(AS_STATE.rows[0]))'),/12345/);
assert.equal(run('asTrackItemsFromRows(asOrderRefundRows(AS_STATE.rows[0])).map(r=>r.refundBillId).join()'),'12345');
assert.equal(run('asSubmissionProtections(AS_STATE.rows[0])[0].row.errorSummary'),'','resolved uncertainty removes obsolete diagnosis');
// Unconfirmed submission without a receipt also survives an unrelated later failure.
context.uncertain={orderNo:'ORDER-B',environmentSerial:'ENV-A',status:'uncertain',errorSummary:'synthetic unknown outcome'};
run('asReconcileScanRows([uncertain])');state.runId='RUN-C';
context.later={...context.uncertain,status:'login',errorSummary:'synthetic login failure'};
run('asReconcileScanRows([later])');
assert.equal(run('asCanSelectScanRow(AS_STATE.rows[1])'),false);
assert.match(run('asOrderDetailHtml(AS_STATE.rows[1], asSubmissionForRow(AS_STATE.rows[1]))'),/synthetic unknown outcome/);
assert.equal(JSON.stringify(state.rows),original);
// Switching views leaves execution identity and running state alone.
state.running=true;state.mode='track';const id=state.runId;
run('asSetOrderView("result",true)');assert.equal(node('asResultView').hidden,false);assert.equal(node('asPendingView').hidden,true);
assert.equal(state.mode,'track');assert.equal(state.runId,id);assert.equal(state.running,true);
run('asSetOrderView("pending")');assert.equal(node('asPendingTab').attrs['aria-selected'],'true');
assert.equal(node('asResultTab').tabIndex,-1);
state.running=false;
// Result filters never change full-batch counts or row order.
context.batch=[{...scan('ORDER-A'),status:'ok',operationCompletedAt:'2026-09-01T01:02:03Z'},
 {...scan('ORDER-B'),status:'fail',errorSummary:'synthetic pre-write failure'},
 {...scan('ORDER-C'),status:'fail',refundBillId:'98765'},
 {...scan('ORDER-D'),status:'verifying'}, {...scan('ORDER-E'),status:'blocked'}];
state.claimFilter='failed';run('asRenderClaimRows(batch)');
assert.match(node('asClaimRows').innerHTML,/ORDER-B/);assert.doesNotMatch(node('asClaimRows').innerHTML,/ORDER-C/);
assert.equal(state.claimRows.length,5);assert.equal(node('asBizSummary').children[0].querySelector('b').textContent,5);
assert.equal(node('asBizSummary').children[5].querySelector('b').textContent,1);
assert.match(node('asClaimFilters').innerHTML,/显示 1 \/ 5 单/);
assert.match(node('asClaimFilters').innerHTML,/导出与补提不受此筛选影响/);
assert.equal(run('asRecoverableClaimRows(AS_STATE.claimRows).length'),1);
assert.equal(run('asClaimMatchesFilter(batch[2],"uncertain")'),true);
assert.equal(run('asClaimMatchesFilter(batch[3],"running")'),true);
// Clear the current result display using the real handler. Protection survives.
const clearStart=html.indexOf("$('asClearResult').onclick =");run(html.slice(clearStart,html.indexOf('\n};',clearStart)+3));
node('asClearResult').onclick();assert.equal(state.claimRows.length,0);
assert.equal(run('asCanSelectScanRow(AS_STATE.rows[0])'),false);
assert.equal(JSON.stringify(state.rows),original);
// Read-only refund progress must not recategorize a submission.
const submissionSnapshot=JSON.stringify([...state.submissions]);
context.unsafe={...scan('ORDER-X'),platformStatus:'<img src=x onerror=alert(1)>',storeName:'<script>unsafe</script>'};
const details=run('asOrderDetailHtml(unsafe,{...unsafe,status:"uncertain",submissionError:"<script>error</script>"})');
assert.doesNotMatch(details,/<script>|<img src=x/);assert.match(details,/&lt;script&gt;/);
assert.match(details,/不会自动改写提交结果/);assert.doesNotMatch(details,/id="asOrderDetailTrack"/);
assert.equal(JSON.stringify([...state.submissions]),submissionSnapshot);
// A read-only account supplement is bound to order, environment and refund identity.
const receipt={orderNo:'ORDER-A',environmentSerial:'ENV-A',status:'uncertain',refundBillId:'12345',
 operationCompletedAt:'2026-09-01T01:02:03Z',refunds:[{refundBillId:'12345',source:'submit_response',detailsNote:'退款账户未读取'}]};
const receiptBefore=JSON.stringify(receipt);
state.claimRows=[receipt];state.claimFilter='all';context.AS_HISTORY={detail:{rows:[receipt]}};
context.tracking={refundBillId:'12345',orderNo:'OTHER',environmentSerial:'ENV-A',status:'ok',refundAccount:'****1234',checkedAt:'2026-09-02T01:00:00Z'};
run('asSupplementClaimAccounts([tracking])');assert.equal(state.claimRows[0].refunds[0].refundAccount,undefined);
context.tracking.orderNo='ORDER-A';context.tracking.environmentSerial='OTHER';
run('asSupplementClaimAccounts([tracking])');assert.equal(state.claimRows[0].refunds[0].refundAccount,undefined);
context.tracking.environmentSerial='ENV-A';run('asSupplementClaimAccounts([tracking])');
assert.equal(state.claimRows[0].refunds[0].refundAccount,'****1234');
assert.equal(state.claimRows[0].status,'uncertain');assert.equal(state.claimRows[0].operationCompletedAt,receipt.operationCompletedAt);
assert.match(node('asClaimRows').innerHTML,/回访补全/);assert.match(node('asHistoryDetailBody').innerHTML,/回访补全/);
assert.equal(JSON.stringify(receipt),receiptBefore,'raw submission snapshot is not mutated');
context.tracking.refundAccount='****9999';run('asSupplementClaimAccounts([tracking])');
assert.equal(state.claimRows[0].refunds[0].refundAccount,'****1234','known card cannot be silently replaced');
for(const path of ['Cuenta original de pago / 其他退款渠道（名称未取得）','Cuenta original de pago ／ 其他退款渠道 ( 名称未取得 )']) {
 context.path=path;assert.equal(run('asRefundPathText(path)'),'Cuenta original de pago');
}
assert.equal(run("asRefundPathText('其他退款渠道（名称未取得）')"),'');
assert.equal(run("asRefundPathText('Cuenta original de pago / Cartera SHEIN')"),'Cuenta original de pago / Cartera SHEIN');
console.log('PASS: immutable scans, continuous reconciliation, protected retry matrix, environment/run isolation, tabs, full-batch filters, clear protection, P0 fields and escaped detail evidence');
