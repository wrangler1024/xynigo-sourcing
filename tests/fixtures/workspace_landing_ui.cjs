const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');

async function landing(permissions, desktop = false, existingTab = '') {
  const opened = [], locked = [];
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden:false, removeAttribute() {}});
    return nodes.get(id);
  };
  const context = vm.createContext({
    location: {hostname:desktop ? '127.0.0.1' : 'xynigo.samforo.icu',
               search:desktop ? '?view=localsettings' : ''},
    URLSearchParams, CLOUD_WEB_MODE: !desktop,
    document: {querySelectorAll:() => [], body:{classList:{remove() {}}}},
    $:node, closeAuthLoginWindow() {}, renderAuthenticatedUser() {},
    setHubStatus() {}, loadLocalExecutorDevices() {},
    loadQueryDisplayConfig() {}, loadLarkRuntimeStatus() {},
    lockWorkspace(message) { locked.push(message); },
    setPrimaryModule(primary) { opened.push(['primary', primary]); },
    activateFeatureTab(module) { opened.push(['feature', module]); },
  });
  vm.runInContext('let authIdentity = null, authReady = false;\n' +
    html.slice(html.indexOf('const PRIMARY_MODULES ='), html.indexOf('const PERMISSION_MENU_GROUPS =')) +
    html.slice(html.indexOf('const LOCAL_DESKTOP_SETTINGS_VIEW ='), html.indexOf('let localSettingsPageLoaded =')) +
    html.slice(html.indexOf('function hasRole('), html.indexOf('function renderAuthenticatedUser(')) +
    html.slice(html.indexOf('async function initializeAuthenticatedWorkspace('), html.indexOf('async function refreshCloudSession(')), context);
  if (existingTab) vm.runInContext(`activeModule = ${JSON.stringify(existingTab)}; openFeatureTabs = [activeModule];`, context);
  await vm.runInContext('initializeAuthenticatedWorkspace(' + JSON.stringify({permissions, roles:[]}) + ')', context);
  return {opened, locked, tabs:JSON.parse(vm.runInContext('JSON.stringify(openFeatureTabs)', context))};
}

(async () => {
  const workbench = await landing(['workbench.access', 'fulfillment.order.read']);
  assert.deepEqual(workbench.opened, [['primary', 'workbench']]);
  assert.deepEqual(workbench.tabs, [], 'initial landing must not pre-open logistics');
  assert.deepEqual((await landing(['workbench.access'])).opened, [['primary', 'workbench']]);
  assert.deepEqual((await landing(['fulfillment.order.read'])).opened, [['feature', 'query']]);
  const plugin = await landing(['procurement.request.read', 'procurement.request.save', 'procurement.request.submit']);
  assert.equal(plugin.opened.length, 0);
  assert.match(plugin.locked[0], /仅开通提单插件/);
  assert.deepEqual((await landing(['workbench.access'], true)).opened, [['feature', 'localsettings']]);
  assert.deepEqual((await landing(['workbench.access', 'fulfillment.order.read'], false, 'query')).opened,
                   [['feature', 'query']], 'existing in-page navigation survives identity refresh');
  console.log('PASS: workbench default, permission fallback, plugin restriction, desktop settings, existing navigation.');
})().catch(error => { console.error(error); process.exitCode = 1; });
