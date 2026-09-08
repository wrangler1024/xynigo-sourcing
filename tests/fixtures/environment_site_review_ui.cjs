const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
for (const cloud of [false, true]) {
  const html = fs.readFileSync(cloud ? 'cloud/auth-service/src/xynigo_auth/web/index.html' : 'src/purchase_tool/web/index.html', 'utf8');
  // Parse the complete shipped script as well as executing the production review handlers.
  new vm.Script(html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>')));
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) { const classes = new Set(); nodes.set(id, {value:'',disabled:false,textContent:'',
      classList:{add:v=>classes.add(v), toggle(v,on){if(on)classes.add(v);else classes.delete(v);}, contains:v=>classes.has(v)}}); }
    return nodes.get(id);
  }
  let allowConflict = false, prompts = 0;
  const ctx = vm.createContext({$:node, CLOUD_WEB_MODE:cloud, confirm(){prompts++;return allowConflict;},
    envMode:'bound', envUploadRevision:1, envCloudPlanId:'cloud-plan-1',envLocalPlanId:'local-plan-1',
    envPreviewRunning:false,envSubmitting:false,envRunning:false,backupRunning:false,
    envAssignedTotal:()=>1,envAccountCount:1,envPreflightReady:true,envBackup:{count:1},envBackupMax:25,
    envStopSubmitting:false,envStopRequested:false,envRetryAccountId:'',envRetryFailedSubmitting:false,
    envFailedCount:0,larkReady:true});
  node('envSite').value='US';node('envSiteGroup').value='美国采购分组';
  vm.runInContext(html.slice(html.indexOf('function hasEnvironmentPlan()'),html.indexOf('function cloudPlanExpiryTime(')) +
    html.slice(html.indexOf('function updateEnvButtons()'),html.indexOf('function splitEnvEvenly(')),ctx);
  const result={filename:'mx-20.xlsx',mixedSiteCookieCount:20,filenameSiteHints:['MX'],filenameSiteConflict:true,siteConfirmationRequired:true};
  const install = data => vm.runInContext(`setEnvironmentSiteReview(${JSON.stringify(data)}); updateEnvButtons();`,ctx);
  const ready=()=>vm.runInContext('environmentSiteReviewReady()',ctx);
  install(result);
  assert.equal(node('envConfirmedSite').value,'');
  assert.equal(node('btnEnvStart').disabled,true);
  assert.equal(node('btnEnvPreview').disabled,true);
  node('envConfirmedSite').value='MX';node('envConfirmedSite').onchange();
  assert.equal(node('btnEnvConfirmSite').disabled,true);
  assert.match(node('envSiteReviewStatus').textContent,/修改上方/);
  node('btnEnvConfirmSite').onclick();assert.equal(ready(),false);
  node('envConfirmedSite').value='US';node('envConfirmedSite').onchange();
  node('btnEnvConfirmSite').onclick();assert.equal(ready(),false);assert.equal(prompts,1);
  assert.throws(()=>vm.runInContext('environmentSiteConfirmationPayload()',ctx),/确认/);
  allowConflict=true;node('btnEnvConfirmSite').onclick();
  assert.equal(ready(),true);assert.equal(node('btnEnvStart').disabled,false);
  assert.equal(node('envMixedCookieLabel').textContent,'混合登录态（已确认）');
  assert.equal(vm.runInContext('environmentSiteConfirmationPayload().confirmFilenameSiteMismatch',ctx),true);
  ctx.envUploadRevision++;vm.runInContext('updateEnvButtons()',ctx);
  assert.equal(ready(),false);assert.equal(node('btnEnvStart').disabled,true);
  install(result);assert.equal(ready(),false);assert.equal(node('envConfirmedSite').value,'');
  node('envConfirmedSite').value='US';node('envConfirmedSite').onchange();node('btnEnvConfirmSite').onclick();
  node('envSiteGroup').value='美国采购二组';assert.equal(ready(),false);
  node('envSiteGroup').value='美国采购分组';node('envSite').value='MX';assert.equal(ready(),false);
  node('envSite').value='US';ctx.envCloudPlanId='new-plan';ctx.envLocalPlanId='new-plan';assert.equal(ready(),false);
  install({...result,filename:'US-20.xlsx',filenameSiteConflict:false,filenameSiteHints:['US']});
  assert.equal(ready(),false); // Matching filename does not waive mixed-cookie confirmation.
  node('envConfirmedSite').value='US';node('envConfirmedSite').onchange();node('btnEnvConfirmSite').onclick();
  ctx.envRunning=true;vm.runInContext('updateEnvButtons()',ctx);
  assert.equal(node('envConfirmedSite').disabled,true);assert.equal(node('btnEnvStart').disabled,true);
  ctx.envRunning=false;
  install({filename:'US-20.xlsx',mixedSiteCookieCount:0,filenameSiteConflict:false,siteConfirmationRequired:false});
  assert.equal(ready(),true);assert.equal(node('btnEnvStart').disabled,false);
  assert.equal(node('envSiteReview').classList.contains('hidden'),true);
  vm.runInContext('resetEnvironmentSiteReview(); updateEnvButtons()',ctx);assert.equal(ready(),false);
  ctx.envMode='backup';vm.runInContext('updateEnvButtons()',ctx);assert.equal(node('btnEnvStart').disabled,false);
}
console.log('PASS: local/cloud confirmation, filename acknowledgment, cancellation, context invalidation, busy state, normal and backup flows');
