const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
let locked = '';
const context = vm.createContext({
  CLOUD_WEB_MODE: true, closeAuthLoginWindow() {},
  lockWorkspace(message) { locked = message; },
});
vm.runInContext('let authIdentity = null;\n' +
  html.slice(html.indexOf('const PRIMARY_MODULES ='), html.indexOf('const PERMISSION_MENU_GROUPS =')) +
  html.slice(html.indexOf('function hasRole('), html.indexOf('function applyPermissionVisibility(')) +
  html.slice(html.indexOf('async function initializeAuthenticatedWorkspace('), html.indexOf('async function refreshCloudSession(')), context);
const run = code => vm.runInContext(code, context);
const permissions = ['procurement.request.read', 'procurement.request.save', 'procurement.request.submit'];

(async () => {
  run('authIdentity = ' + JSON.stringify({permissions, roles: ['synthetic_plugin']}));
  assert.equal(run('Object.keys(FEATURE_MODULES).some(hasFeatureAccess)'), false);
  assert.equal(run('Object.keys(PRIMARY_MODULES).some(hasPrimaryAccess)'), false);
  await run('initializeAuthenticatedWorkspace(authIdentity)');
  assert.match(locked, /仅开通提单插件/);
  run('authIdentity.permissions.push("procurement.access")');
  assert.equal(run('hasFeatureAccess("procurementorders")'), true);
  assert.equal(run('hasFeatureAccess("procurementimport")'), false);
  run('authIdentity = ' + JSON.stringify({permissions: [...permissions, 'assistant.access']}));
  assert.equal(run('hasFeatureAccess("procurementimport")'), true);
  assert.equal(run('hasFeatureAccess("procurementorders")'), false);
  run('authIdentity.workspaceAccess = false');
  assert.equal(run('Object.keys(FEATURE_MODULES).some(hasFeatureAccess)'), false);
  console.log('PASS: plugin-only identity cannot open workspace modules; supervisor access remains explicit.');
})().catch(error => { console.error(error); process.exitCode = 1; });
