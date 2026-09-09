const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
(async () => {
for (const path of ['src/purchase_tool/web/index.html', 'cloud/auth-service/src/xynigo_auth/web/index.html']) {
  const html = fs.readFileSync(path, 'utf8');
  new vm.Script(html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>')));
  let banner;
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', options:[], classList:{toggle(){}}, style:{}});
    return nodes.get(id);
  };
  const ctx = vm.createContext({$:node, envSubmitting:false, ENV_RUN_PHASE_LABEL:{},
    cloudRunElapsed:()=>0, cloudRunHubConnected:()=>true, cloudEnvironmentAncestorCache:new Map(),
    setEnvExecutionState:(...args)=>{banner=args;}});
  vm.runInContext(html.slice(html.indexOf('function cloudEnvironmentLegacySnapshot('),
    html.indexOf('async function cloudOperationSnapshot(')) +
    html.slice(html.indexOf('function renderEnvExecutionState('),html.indexOf('function renderEnvBatch(')),ctx);
  const rows = Array.from({length:234},(_,i)=>({accountRef:String(i), status:i<79?'success':i<81?'running':'queued',
    currentStep:i<79?'done':i<81?'env_created':'pending', createdInRun:i<81, completedSteps:[]}));
  const interrupted = {runId:'original',executorId:'original-pc',mode:'bound',site:'MX',environmentGroup:'MX采购',
    terminal:true,status:'uncertain',phase:'uncertain',totalCount:234,successCount:79,failedCount:0,createdCount:81,rows};
  ctx.run = interrupted;
  vm.runInContext('renderEnvExecutionState(cloudEnvironmentLegacySnapshot(run), "bound")',ctx);
  assert.equal(banner[0], 'warning');
  assert.match(banner[1], /结果待核对/); assert.match(banner[2], /79\/234/); assert.match(banner[2], /155 行未确认/);
  ctx.run = {...interrupted,status:'completed',phase:'completed',totalCount:79,rows:rows.slice(0,79)};
  vm.runInContext('renderEnvExecutionState(cloudEnvironmentLegacySnapshot(run), "bound")',ctx);
  assert.equal(banner[0],'success');assert.match(banner[1], /新建 79/);assert.doesNotMatch(banner[1], /新建 81/);
  // Old completion flags with unfinished rows also fail closed.
  ctx.run = {...interrupted,status:'completed',phase:'completed'};
  vm.runInContext('renderEnvExecutionState(cloudEnvironmentLegacySnapshot(run), "bound")',ctx);
  assert.equal(banner[0], 'warning');
  // A child run retains successful rows from the original batch.
  ctx.cloudOperationSnapshot = async () => interrupted;
  ctx.run = {...interrupted,runId:'child',parentRunId:'original',rows:rows.slice(79).map(r=>({...r,status:'success'})),
    totalCount:155,successCount:155,status:'completed',phase:'completed'};
  const combined = await vm.runInContext('cloudEnvironmentSnapshotWithHistory(run)',ctx);
  assert.equal(combined.summary.total,234);assert.equal(combined.summary.done,234);
  // Real resume handler: missing original file, another PC and cancel send no mutation.
  let mutations=0, confirmation=true, selected='original-pc', notices=[];
  Object.assign(ctx,{cloudEnvironmentRecovery:{...interrupted,accountRefs:['79','80']},envCloudPlanId:null,
    envRunning:false,envRetryFailedSubmitting:false, envMode:'bound',envLocalPlanId:null,
    cloudFormalExecutor:async()=>({id:selected}),environmentSiteConfirmationPayload:()=>({confirmedSite:'MX'}),
    confirm:()=>confirmation,toast:s=>notices.push(s),workspaceMutationKey:()=> 'synthetic-resume-key',
    clearWorkspaceMutationKey:()=>{},beginEnvSubmission:()=>{},finishEnvSubmission:()=>{},
    renderEnvBatch:()=>{},setTimeout:()=>{},pollEnvBatch:()=>{},
    cloudFetchJson:async(_url,opts)=>{mutations++;const body=JSON.parse(opts.body);
      assert.equal(body.retryMode,'interrupted');assert.equal(body.confirmOriginalStopped,true);
      assert.equal(body.executorId,'original-pc'); return {data:{...interrupted,runId:'child'}};}});
  node('envSite').value='MX';node('envSiteGroup').value='MX采购';
  vm.runInContext(html.slice(html.indexOf("$('btnEnvResume').onclick ="),html.indexOf("$('btnEnvRetryFailed').onclick =")),ctx);
  await node('btnEnvResume').onclick(); assert.equal(mutations,0); assert.match(notices.pop(),/原始 xlsx/);
  ctx.envCloudPlanId='new-plan'; selected='another-pc';
  await node('btnEnvResume').onclick(); assert.equal(mutations,0); assert.match(notices.pop(),/原采购电脑/);
  selected='original-pc';confirmation=false;
  await node('btnEnvResume').onclick(); assert.equal(mutations,0);
  confirmation=true; await node('btnEnvResume').onclick(); assert.equal(mutations,1);
}
console.log('PASS: interruption status, accurate counts, merged recovery and guarded resume submission');
})().catch(error=>{console.error(error);process.exit(1);});
