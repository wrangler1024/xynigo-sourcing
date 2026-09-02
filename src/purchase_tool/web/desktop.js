(function () {
  'use strict';

  var params = new URLSearchParams(location.search);
  var platform = params.get('platform') === 'mac' ? 'mac' : 'windows';
  var previewRole = params.get('preview') || '';
  var app = document.getElementById('app');
  var modalRoot = document.getElementById('modal-root');
  var toast = document.getElementById('toast');
  var toastTimer = null;
  var authPollTimer = null;
  var refreshTimer = null;
  var state = {
    authMode: 'checking',
    identity: null,
    view: ['overview','settings','sources','diagnostics'].indexOf(params.get('view')) >= 0 ? params.get('view') : 'overview',
    sourceTab: 'registry',
    accountOpen: false,
    status: null,
    config: null,
    lark: null,
    sources: null,
    members: [],
    environments: [],
    sourceDraft: null,
    notice: ''
  };

  var sampleStatus = {
    schemaVersion: 1,
    version: '0.13.8',
    localPort: 8765,
    executor: {running:true, paired:true, displayName:'采购电脑 · 上海办公室 03', platform:platform, architecture:platform === 'mac' ? 'arm64' : 'amd64'},
    cloudChannel: {status:'online', lastPollAt:new Date(Date.now() - 18000).toISOString(), phase:'polling'},
    hubStudio: {connected:true, status:'ready'},
    tasks: {activeCount:0, safeParallel:true, items:[]},
    update: {enabled:true, state:'current', installMode:'standard', currentVersion:'0.13.8', latestVersion:'0.13.8', message:'已是推荐版本'}
  };
  var sampleConfig = {hubPort:6873, concurrency:2, envCreateWorkers:5, verifySampleCount:3, safeParallelTasks:true, configRevision:'e5a931'};
  var sampleSources = {
    registryRevision:'sample-rev', teamDefaultDataSourceId:'ds-team',
    counts:{dataSourceCount:4,buyerProfileCount:4,environmentBindingCount:31,mappingConflictCount:0},
    dataSources:[
      {id:'ds-xg',scope:'personal',ownerMemberId:'member-xg',label:'新刚 · 个人速填表',sheetName:'个人速填区',cellRange:'A1:Q',enabled:true,migrationState:'ready',environmentCount:8},
      {id:'ds-zh',scope:'personal',ownerMemberId:'member-zh',label:'志恒 · 个人速填表',sheetName:'采购速填',cellRange:'A1:Q',enabled:true,migrationState:'ready',environmentCount:7},
      {id:'ds-kd',scope:'personal',ownerMemberId:'member-kd',label:'康德 · 个人速填表',sheetName:'Sheet1',cellRange:'A1:Q',enabled:true,migrationState:'ready',environmentCount:6},
      {id:'ds-team',scope:'team',ownerMemberId:'',label:'采购执行协作表',sheetName:'执行协作区',cellRange:'A1:AQ',enabled:true,migrationState:'ready',environmentCount:11}
    ],
    buyerProfiles:[
      {memberId:'member-xg',defaultDataSourceId:'ds-xg'}, {memberId:'member-zh',defaultDataSourceId:'ds-zh'},
      {memberId:'member-kd',defaultDataSourceId:'ds-kd'}, {memberId:'member-yh',defaultDataSourceId:'ds-team'}
    ],
    environmentBindings:[
      {memberId:'member-xg',containerCode:'CN-4F21-A802',dataSourceId:'ds-xg'},
      {memberId:'member-xg',containerCode:'CN-9A17-14BC',dataSourceId:'ds-xg'},
      {memberId:'member-zh',containerCode:'CN-73D4-22F1',dataSourceId:'ds-zh'},
      {memberId:'member-kd',containerCode:'CN-204C-9D90',dataSourceId:'ds-kd'},
      {memberId:'member-yh',containerCode:'CN-80E5-71A3',dataSourceId:'ds-team'}
    ]
  };
  var sampleMembers = [
    {id:'member-xg',name:'新刚',code:'XG'}, {id:'member-zh',name:'志恒',code:'ZH'},
    {id:'member-kd',name:'康德',code:'KD'}, {id:'member-yh',name:'宇航',code:'YH'}
  ];
  var sampleEnvironments = [
    {serialNumber:'4254',containerCode:'CN-4F21-A802',containerName:'XG-MX-0902-01',groupName:'希音墨西哥采购'},
    {serialNumber:'4255',containerCode:'CN-9A17-14BC',containerName:'XG-MX-0902-02',groupName:'希音墨西哥采购'},
    {serialNumber:'4261',containerCode:'CN-73D4-22F1',containerName:'ZH-MX-0902-01',groupName:'希音墨西哥采购'},
    {serialNumber:'4270',containerCode:'CN-204C-9D90',containerName:'KD-US-0902-01',groupName:'美国采购分组'},
    {serialNumber:'4276',containerCode:'CN-80E5-71A3',containerName:'YH-MX-测试-01',groupName:'希音墨西哥采购'},
    {serialNumber:'4280',containerCode:'CN-114F-00D2',containerName:'待分配环境',groupName:'希音墨西哥采购'}
  ];
  var emptyStatus = {version:'—',localPort:'—',executor:{running:false,paired:false,displayName:'这台采购电脑',architecture:'—'},cloudChannel:{status:'offline'},hubStudio:{connected:false},tasks:{activeCount:0,safeParallel:false,items:[]},update:{enabled:false,state:'disabled',message:'本机执行器尚未就绪'}};
  var emptySources = {registryRevision:'',teamDefaultDataSourceId:'',counts:{dataSourceCount:0,buyerProfileCount:0,environmentBindingCount:0,mappingConflictCount:0},dataSources:[],buyerProfiles:[],environmentBindings:[]};
  function currentStatus() { return state.status || (previewRole ? sampleStatus : emptyStatus); }
  function currentConfig() { return state.config || (previewRole ? sampleConfig : {hubPort:6873,concurrency:2,envCreateWorkers:5,verifySampleCount:1,safeParallelTasks:true,configRevision:''}); }
  function currentSources() { return state.sources || (previewRole ? sampleSources : emptySources); }

  function icon(name, extra) {
    return '<svg class="icon' + (extra ? ' ' + extra : '') + '" aria-hidden="true"><use href="#i-' + name + '" xlink:href="#i-' + name + '"></use></svg>';
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
  }
  function safeId(value) {
    var text = String(value || '');
    if (text.length <= 14) return text || '—';
    return text.slice(0, 6) + '…' + text.slice(-5);
  }
  function nowTime() {
    return new Date().toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit', hour12:false});
  }
  function roleInfo() {
    var identity = state.identity || {};
    var roles = identity.roles || [];
    var user = identity.user || {};
    var admin = roles.indexOf('super_admin') >= 0 || roles.indexOf('admin') >= 0;
    var superAdmin = roles.indexOf('super_admin') >= 0;
    var role = superAdmin ? '超级管理员' : (admin ? '管理员' : '采购员');
    var name = user.name || '当前成员';
    var initials = user.initials || name.replace(/\s/g, '').slice(0, 2).toUpperCase() || 'XY';
    return {admin:admin, superAdmin:superAdmin, role:role, name:name, initials:initials, id:user.id || ''};
  }
  function hasPermission(permission) {
    var permissions = state.identity && state.identity.permissions || [];
    return permissions.indexOf(permission) >= 0;
  }
  function canConfigure() {
    return roleInfo().superAdmin && hasPermission('system.integration.manage');
  }
  function api(path, options) {
    return fetch(path, options || {}).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.error || ('请求失败 HTTP ' + response.status));
          error.code = body.code || '';
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }
  function nativeAction(action, payload) {
    var message = {action:action, payload:payload || {}};
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.xynigo) {
      window.webkit.messageHandlers.xynigo.postMessage(message);
      return;
    }
    if (window.chrome && window.chrome.webview) {
      window.chrome.webview.postMessage(JSON.stringify(message));
      return;
    }
    if (window.external && typeof window.external.invoke === 'function') {
      window.external.invoke(JSON.stringify(message));
      return;
    }
    if (action === 'open-external' && payload && payload.url) window.open(payload.url, '_blank', 'noopener');
  }
  function showToast(message, title) {
    clearTimeout(toastTimer);
    toast.innerHTML = '<span class="toast-dot">' + icon('check') + '</span><span><b>' + esc(title || '操作已完成') + '</b><span>' + esc(message) + '</span></span>';
    toast.className = 'toast show';
    toastTimer = setTimeout(function () { toast.className = 'toast'; }, 4200);
  }
  function showError(error) {
    showToast(error && error.message ? error.message : String(error || '操作失败'), '操作未完成');
  }
  function post(path, body) {
    return api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});
  }

  function authStory(active) {
    var paired = currentStatus().executor && currentStatus().executor.paired;
    return '<section class="auth-story">' +
      '<div class="auth-brand"><span class="brand-mark">X</span><div><p class="brand-name">Xynigo</p><p class="brand-sub">Local Executor</p></div></div>' +
      '<div class="auth-copy"><span class="pill pill-soft">' + (paired ? '设备已配对 · 后台执行器在线' : '设备待配对 · 后台执行器已启动') + '</span><h1>先确认当前采购员，<br>再进入这台电脑。</h1><p>飞书授权只用于识别当前成员、角色和操作审计。后台执行器使用独立设备凭证，不会因用户退出登录而停止。</p></div>' +
      '<ol class="auth-steps"><li><span class="step-dot done">' + icon('check') + '</span><div><b>设备身份已就绪</b><small>' + esc((state.status && state.status.executor && state.status.executor.displayName) || '这台采购电脑') + '</small></div></li>' +
      '<li><span class="step-dot ' + (active ? 'live' : '') + '">2</span><div><b>飞书成员授权</b><small>确认身份与组织角色</small></div></li>' +
      '<li class="pending"><span class="step-dot">3</span><div><b>按角色进入客户端</b><small>管理员与采购员看到不同权限</small></div></li></ol></section>';
  }
  function renderAuth() {
    var waiting = state.authMode === 'authorizing' || state.authMode === 'checking';
    var card;
    if (waiting) {
      card = '<div class="auth-card"><span class="pulse"><span class="feishu-mark">飞</span></span><span class="pill pill-blue" style="margin-top:22px">' + (state.authMode === 'checking' ? '正在检查登录状态' : '等待浏览器授权返回') + '</span>' +
        '<h2>' + (state.authMode === 'checking' ? '正在连接 Xynigo' : '正在连接飞书账号') + '</h2><p>' + (state.authMode === 'checking' ? '正在读取本机安全会话，请稍候。' : '已在系统浏览器中打开飞书 OAuth。完成授权后，此窗口会自动进入客户端。') + '</p>' +
        '<div class="auth-app"><span class="feishu-mark">飞</span><div><b>小犀代采</b><span>请求基本身份 · 当前组织角色</span></div></div>' +
        (previewRole ? '<button class="auth-button teal" data-action="preview-auth-complete">' + icon('check') + '模拟授权成功并返回</button>' : '<button class="auth-button teal" data-action="auth-reopen">' + icon('refresh') + '重新打开授权页</button>') + '</div>';
    } else {
      card = '<div class="auth-card"><span class="feishu-mark">飞</span><h2>使用飞书授权登录</h2><p>' + esc(state.notice || '登录后将从云端读取你的成员 UUID、角色和可用功能，不会读取飞书聊天、通讯录明细或个人密码。') + '</p>' +
        '<button class="auth-button" data-action="auth-start"><span class="feishu-mark" style="width:24px;height:24px;border-radius:6px;font-size:10px;box-shadow:none">飞</span>使用飞书授权登录 ' + icon('arrow') + '</button>' +
        '<div class="auth-safety"><div>' + icon('shield') + '<span>授权在系统浏览器中完成，客户端不接触飞书密码。</span></div><div>' + icon('lock') + '<span>Xynigo 会话保存在 ' + (platform === 'mac' ? 'macOS Keychain' : 'Windows CurrentUser DPAPI') + '。</span></div><div>' + icon('user') + '<span>同一电脑切换采购员时必须切换登录账号。</span></div></div>' +
        '<span class="auth-foot">未登录仍可在系统托盘/菜单栏查看执行器基础状态</span></div>';
    }
    app.className = '';
    app.innerHTML = '<div class="auth-shell">' + authStory(waiting) + '<section class="auth-panel">' + card + '</section></div>';
  }

  function navButton(view, label, iconName) {
    return '<button class="nav-button ' + (state.view === view ? 'active' : '') + '" data-view="' + view + '">' + icon(iconName) + '<span>' + label + '</span>' + (state.view === view ? '<i class="nav-dot"></i>' : '') + '</button>';
  }
  function sidebar() {
    var role = roleInfo();
    return '<aside class="sidebar"><div class="sidebar-brand"><span class="brand-mark">X</span><div><p class="brand-name">Xynigo</p><p class="brand-sub">Local Executor</p></div></div>' +
      '<nav class="side-nav" aria-label="桌面客户端导航">' + navButton('overview','状态总览','dashboard') + navButton('settings','本机设置','sliders') + navButton('sources','采购助手数据源','list') + navButton('diagnostics','诊断与维护','wrench') + '</nav>' +
      '<div class="sidebar-bottom"><div class="account-wrap"><button class="account-button ' + (state.accountOpen ? 'open' : '') + '" data-action="account-toggle"><span class="avatar">' + esc(role.initials) + '</span><span class="account-copy"><b>' + esc(role.name) + '</b><span>' + esc(role.role) + '</span></span><span>⌄</span></button>' +
      (state.accountOpen ? '<div class="account-menu"><div class="account-menu-head"><b>' + esc(role.name) + '</b><span>飞书成员已验证 · 安全会话有效</span></div><hr><button data-action="auth-switch">' + icon('refresh') + '切换飞书账号</button><button class="danger" data-action="auth-logout">' + icon('power') + '退出登录</button></div>' : '') + '</div>' +
      '<div class="compliance"><div class="compliance-head"><span>配置合规</span><b>100%</b></div><div class="progress-track"><i></i></div><p>' + (platform === 'mac' ? '菜单栏' : '系统托盘') + '持续驻留<br>本机配置策略已应用</p></div></div></aside>';
  }
  var viewCopy = {
    overview:['状态总览','这台电脑的连接、任务与配置健康状态'],
    settings:['本机设置','所有配置只保存在当前电脑，云端仅接收脱敏摘要'],
    sources:['采购助手数据源','管理飞书表、采购员默认值和 HubStudio 环境映射'],
    diagnostics:['诊断与维护','连接检测、运行日志、更新与迁移状态']
  };
  function header(view, actions) {
    var copy = viewCopy[view];
    return '<header class="content-header"><div class="header-copy"><h1>' + copy[0] + '</h1>' + (view === 'overview' ? '<span class="pill pill-ok">设备在线</span>' : '') + '<p>' + copy[1] + '</p></div><div class="header-actions">' + (actions || '') + '</div></header>';
  }
  function button(label, action, iconName, classes, disabled) {
    return '<button class="button ' + (classes || '') + '" data-action="' + action + '"' + (disabled ? ' disabled' : '') + '>' + (iconName ? icon(iconName) : '') + label + '</button>';
  }
  function relativeTime(value) {
    var timestamp = Date.parse(value || '');
    if (!timestamp) return '尚未心跳';
    var seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return seconds + ' 秒前心跳';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前心跳';
    return Math.floor(seconds / 3600) + ' 小时前心跳';
  }
  function statusCard(title, value, detail, iconName, tone, ok) {
    return '<article class="card status-card"><div class="status-card-head"><span class="status-icon ' + (tone || '') + '">' + icon(iconName) + '</span><span class="normal" style="' + (ok ? '' : 'color:#b45309') + '"><i style="' + (ok ? '' : 'background:#f59e0b') + '"></i>' + (ok ? '正常' : '注意') + '</span></div><p class="label">' + esc(title) + '</p><p class="value">' + esc(value) + '</p><p class="detail">' + esc(detail) + '</p></article>';
  }
  function cloudPresentation(channel, paired) {
    channel = channel || {};
    if (!paired) return {value:'等待配对', detail:'需要一次性配对码'};
    if (channel.status === 'online') {
      return {value:'已连接', detail:relativeTime(channel.lastPollAt)};
    }
    var failures = {
      validation_failed: '客户端与云端协议不兼容',
      auth_failed: '云端认证请求失败',
      cloud_unreachable: '暂时无法访问云端',
      executor_credential_invalid: '设备凭证已失效',
      executor_revoked: '设备已被撤销',
      executor_credential_unavailable: '无法读取设备凭证'
    };
    var failure = failures[channel.lastErrorCode];
    if (failure) {
      return {value:failure, detail:relativeTime(channel.lastPollAt)};
    }
    return {
      value:channel.status === 'reconnecting' ? '正在重连' : '正在连接',
      detail:relativeTime(channel.lastPollAt)
    };
  }
  function renderOverview() {
    var s = currentStatus();
    var paired = !!(s.executor && s.executor.paired);
    var cloudOnline = !!(s.cloudChannel && s.cloudChannel.status === 'online');
    var hubReady = !!(s.hubStudio && s.hubStudio.connected);
    var activeCount = Number(s.tasks && s.tasks.activeCount || 0);
    var update = s.update || {};
    var healthy = paired && cloudOnline && hubReady;
    var cloud = cloudPresentation(s.cloudChannel, paired);
    var cloudValue = cloud.value;
    var cloudDetail = cloud.detail;
    var actions = button('刷新状态','refresh-status','refresh') + button('打开云端工作台','open-cloud','arrow','primary');
    var pair = paired ? '' : '<section class="pair-strip">' + icon('alert') + '<div><b>这台电脑尚未配对</b><div style="font-size:10px;color:#92400e;margin-top:3px">在云端生成 8 位一次性配对码后完成绑定。</div></div><input id="pair-code" class="input" placeholder="ABCD-EFGH" maxlength="9"><button class="button primary" data-action="pair-device">配对这台电脑</button></section>';
    return header('overview',actions) + '<div class="content stack">' + pair +
      '<section class="health-banner"><span class="health-check">' + icon(healthy ? 'check' : 'alert') + '</span><div class="health-copy"><h2>' + (healthy ? '本地执行器运行正常' : '本地执行器需要处理') + '</h2><p>' + (healthy ? '云端任务通道和 HubStudio Local API 均已就绪，当前没有需要处理的异常。' : '请根据下方状态卡完成配对或检查本机连接。') + '</p></div><span class="pill">最近检查 ' + nowTime() + '</span></section>' +
      '<section class="status-grid">' + statusCard('云端通道',cloudValue,cloudDetail,'cloud','',cloudOnline) + statusCard('HubStudio',hubReady ? 'Local API 正常' : 'Local API 未连接','v1 · 端口 ' + esc((state.config && state.config.hubPort) || '6873') + (hubReady ? ' · 已认证' : ' · 请检查'),'gauge','blue',hubReady) + statusCard('本机任务',activeCount ? activeCount + ' 个运行中' : '当前空闲',s.tasks && s.tasks.safeParallel ? '安全并行已开启' : '安全并行未开启','activity','green',true) + statusCard('执行器版本','v' + esc(s.version || update.currentVersion || '—'),esc(update.message || '当前运行时已加载'),'shield','amber',update.state !== 'failed') + '</section>' +
      '<section class="overview-bottom"><article class="card device-card"><div class="device-head"><div><p class="eyebrow">Paired Device</p><h2>' + esc((s.executor && s.executor.displayName) || '这台采购电脑') + '</h2><p class="device-copy">' + (platform === 'mac' ? 'macOS · ' + esc((s.executor && s.executor.architecture) || 'Apple Silicon') + ' · 登录钥匙串' : 'Windows · ' + esc((s.executor && s.executor.architecture) || 'x86_64') + ' · 当前用户 DPAPI') + '</p></div><span class="device-icon">' + icon('monitor') + '</span></div><div class="device-meta"><div><span>设备状态</span><b>' + (paired ? '已配对' : '待配对') + '</b></div><div><span>本地端口</span><b>' + esc(s.localPort || '—') + '</b></div><div><span>配置版本</span><b>rev · ' + safeId((state.config && state.config.configRevision) || '读取中') + '</b></div></div></article>' +
      '<article class="card quick-card"><div class="quick-title">✦ 快捷操作</div><div class="quick-grid">' + button('本机设置','go-settings','sliders','dark') + button('日志目录','open-logs','folder','dark') + button('重启执行器','restart-executor','power','dark') + button('检查更新','go-diagnostics','refresh','dark') + '</div></article></section></div>';
  }

  function sectionTitle(iconName, title, description, badge) {
    return '<div class="section-title"><span class="section-title-icon">' + icon(iconName) + '</span><div><h2>' + title + '</h2><p>' + description + '</p></div>' + (badge ? '<span class="pill">' + badge + '</span>' : '') + '</div>';
  }
  function field(id, label, value, hint, type, disabled, placeholder) {
    return '<div class="field"><label for="' + id + '">' + label + '</label><input class="input" id="' + id + '" type="' + (type || 'text') + '" value="' + esc(value == null ? '' : value) + '" placeholder="' + esc(placeholder || '') + '"' + (disabled ? ' disabled' : '') + '><small>' + esc(hint || '') + '</small></div>';
  }
  function renderSettings() {
    var cfg = currentConfig();
    var lark = state.lark || {};
    var locked = !canConfigure();
    var secureStore = platform === 'mac' ? 'Keychain' : 'CurrentUser DPAPI';
    var actions = '<span class="pill" style="border:1px solid #e2e8f0;background:#fff;color:#475569">' + (locked ? '采购员 · 只读' : '配置 rev · ' + safeId(cfg.configRevision)) + '</span>' + button('保存更改','save-settings','check','primary',locked);
    var readOnly = locked ? '<section class="notice-strip span-2">' + icon('lock') + '<div><b>普通采购员仅可查看设备级设置</b><p>运行参数、HubStudio Key 和企业应用凭证需要超级管理员修改。你仍可在“采购助手数据源”配置自己的个人速填表。</p></div></section>' : '';
    return header('settings',actions) + '<div class="content two-column">' + readOnly +
      '<section class="card section-card">' + sectionTitle('gauge','运行参数','控制本机执行器并发、抽检与安全模式','仅保存在本机') + '<div class="section-body field-grid">' +
      field('cfg-hub-port','HubStudio Local API 端口',cfg.hubPort || 6873,'范围 1–65535','number',locked) + field('cfg-concurrency','订单查询并发',cfg.concurrency || 2,'组织策略上限：5','number',locked) + field('cfg-env-workers','建环境并发',cfg.envCreateWorkers || 5,'当前有效值受安全并行策略封顶','number',locked) + field('cfg-verify','新建完成抽检数',cfg.verifySampleCount == null ? 1 : cfg.verifySampleCount,'0 表示不执行抽检','number',locked) +
      '<label class="toggle-row"><div><b>安全并行</b><p>允许物流查询与一种环境创建任务并行，同一环境仍禁止双开。</p></div><input id="cfg-safe" class="switch" type="checkbox"' + (cfg.safeParallelTasks !== false ? ' checked' : '') + (locked ? ' disabled' : '') + '></label></div></section>' +
      '<section class="card section-card">' + sectionTitle('monitor','HubStudio 连接','连接本机 Local API，密钥永不回显',secureStore) + '<div class="section-body stack"><div class="connection-box"><span class="connection-check">' + icon('check') + '</span><div><b>API Key 已安全保存</b><span>' + ((state.status && state.status.hubStudio && state.status.hubStudio.connected) ? '认证成功 · HubStudio Local API v1' : '等待本机 Local API 连接') + '</span></div>' + button('测试连接','test-hub','play','small',locked) + '</div>' + field('cfg-hub-key','覆盖 HubStudio Local API Key','','留空保持现状；保存到 ' + secureStore + '。','password',locked,'输入新 Key 后覆盖，现有值不会回显') + '</div></section>' +
      '<section class="card section-card span-2">' + sectionTitle('key','飞书企业应用连接','采购助手数据源共用同一份企业应用凭证',secureStore) + '<div class="section-body credential-grid">' +
      field('cfg-lark-id','企业自建应用 App ID','','','text',locked,lark.credentialConfigured ? ('已配置 ' + (lark.appIdMasked || '企业应用') + '；留空保持') : '输入企业自建应用 App ID') + field('cfg-lark-secret','App Secret','','','password',locked,lark.credentialConfigured ? '已安全保存；留空保持' : '输入 App Secret') + '<div style="display:flex;align-items:flex-end">' + button('只读验证连接','test-lark','shield','',locked) + '</div>' +
      '<div class="compat-row"><span>' + icon('lock') + '</span><div><b>旧买家号台账迁移源</b><p>' + (lark.ledgerTargetConfigured ? '兼容目标已配置，仅用于一次性迁移和排障；日常采购助手不读取或写入旧台账。' : '尚未配置旧迁移源；不影响新的采购助手数据源注册表。') + '</p></div>' + button('查看兼容设置','open-legacy-settings','','ghost small',locked) + '</div></div></section></div>';
  }

  function sourceCounts() {
    var ds = currentSources();
    var sources = ds.dataSources || [];
    var role = roleInfo();
    var currentId = role.id;
    var personals = sources.filter(function (s) { return s.scope === 'personal' && (role.admin || !s.ownerMemberId || s.ownerMemberId === currentId); });
    var teams = sources.filter(function (s) { return s.scope === 'team'; });
    var bindings = (ds.environmentBindings || []).filter(function (b) { return role.admin || b.memberId === currentId; });
    var mapped = role.admin ? Number(ds.counts && ds.counts.environmentBindingCount || bindings.length) : bindings.length;
    var pending = role.admin && state.environments.length ? Math.max(0, state.environments.length - mapped) : 0;
    return {personal:personals.length,team:teams.length,mapped:mapped,pending:pending};
  }
  function metric(title, value, detail, iconName, warn) {
    return '<article class="card metric ' + (warn ? 'warn' : '') + '"><span class="metric-icon">' + icon(iconName) + '</span><div><p>' + title + '</p><b>' + value + '</b><small>' + detail + '</small></div></article>';
  }
  function sourceName(id) {
    var source = (currentSources().dataSources || []).find(function (item) { return item.id === id; });
    return source ? source.label : '数据源不可用';
  }
  function sourceById(id) {
    return (currentSources().dataSources || []).find(function (item) { return item.id === id; }) || null;
  }
  function sourceCanEdit(source) {
    var role = roleInfo();
    if (!source) return false;
    if (source.scope === 'team') return role.admin;
    if (!source.ownerMemberId) return false;
    return role.admin || source.ownerMemberId === role.id;
  }
  function memberName(id) {
    var role = roleInfo();
    if (id === role.id) return role.name;
    var member = (state.members || []).find(function (item) { return item.id === id; });
    return member ? (member.name || safeId(id)) : safeId(id);
  }
  function sourceRegistry() {
    var role = roleInfo();
    var ds = currentSources();
    var bindings = ds.environmentBindings || [];
    var sources = (ds.dataSources || []).filter(function (source) {
      return role.admin || source.scope === 'team' || !source.ownerMemberId || source.ownerMemberId === role.id;
    });
    if (!sources.length) return '<div class="card empty">本机尚未登记采购助手数据源。</div>';
    return '<div class="source-grid">' + sources.map(function (source) {
      var count = Number(source.environmentCount || bindings.filter(function (b) { return b.dataSourceId === source.id; }).length);
      var personal = source.scope === 'personal';
      var pending = personal && !source.ownerMemberId;
      var actions = button('查看详情','source-details:'+source.id,'','ghost small') + (pending ? button('认领为我的','claim-source:'+source.id,'','small') : (sourceCanEdit(source) ? button('重新配置','source-reconfigure:'+source.id,'','ghost small') : '')) + button('重新验证','revalidate-source:'+source.id,'','ghost small');
      return '<article class="card source-card"><div class="source-head"><span class="source-icon ' + (personal ? '' : 'team') + '">' + icon(personal ? 'user' : 'users') + '</span><div><h3>' + esc(source.label) + '</h3><span class="pill ' + (personal ? '' : 'pill-blue') + '">' + (personal ? '个人' : '团队') + '</span><p>归属：' + (personal ? esc(source.ownerMemberId ? memberName(source.ownerMemberId) : '待认领') : '采购团队') + '</p></div></div><div class="source-details"><div><span>工作表</span><b>' + esc(source.sheetName || '名称未记录') + '</b></div><div><span>读取范围</span><b>' + esc(source.cellRange || '—') + '</b></div><div><span>关联环境</span><b>' + count + ' 个</b></div></div><div class="source-foot"><span class="source-state">' + icon(source.migrationState === 'ready' ? 'check' : 'alert') + '<span style="margin-left:5px">' + (source.migrationState === 'ready' ? (source.enabled === false ? '当前已停用' : '表头校验通过') : '等待确认归属') + '</span></span><span class="source-actions">' + actions + '</span></div></article>';
    }).join('') + '</div>';
  }
  function buyerDefaults() {
    var ds = currentSources();
    var profiles = ds.buyerProfiles || [];
    var members = (state.members || []).length ? state.members : profiles.map(function (profile) { return {id:profile.memberId,name:memberName(profile.memberId)}; });
    if (!members.length) return '<div class="card empty">当前没有采购员默认映射。</div>';
    return '<div class="card table-card"><table class="data-table"><thead><tr><th>采购员</th><th>默认数据源</th><th>环境数</th><th>状态</th></tr></thead><tbody>' + members.map(function (member) {
      var profile = profiles.find(function (item) { return item.memberId === member.id; });
      var sourceId = profile ? profile.defaultDataSourceId : ds.teamDefaultDataSourceId;
      var envs = (ds.environmentBindings || []).filter(function (binding) { return binding.memberId === member.id; }).length;
      var team = !profile && !!ds.teamDefaultDataSourceId;
      return '<tr><td><b>' + esc(member.name || memberName(member.id)) + '</b><br><small>云端成员已关联</small></td><td><b>' + esc(sourceId ? sourceName(sourceId) : '尚未配置') + '</b></td><td>' + envs + '</td><td><span class="pill ' + (sourceId ? (team ? 'pill-blue' : 'pill-ok') : 'pill-warn') + '">' + (sourceId ? (team ? '团队默认' : '已就绪') : '待配置') + '</span></td></tr>';
    }).join('') + '</tbody></table></div>';
  }
  function environmentMappings() {
    var ds = currentSources();
    var environments = state.environments || [];
    var sources = (ds.dataSources || []).filter(function (item) { return item.enabled && item.migrationState === 'ready'; });
    if (!environments.length) {
      environments = (ds.environmentBindings || []).map(function (item, index) { return {serialNumber:String(index + 1),containerCode:item.containerCode,containerName:item.containerCode,groupName:'HubStudio'}; });
    }
    return '<div class="card table-card"><div class="table-toolbar"><input id="environment-search" class="input" placeholder="搜索环境名、序号或 containerCode"><button class="button" data-action="discover-environments">' + icon('refresh') + '重新发现环境</button></div><table class="data-table"><thead><tr><th style="width:10%">序号</th><th style="width:22%">环境</th><th style="width:17%">Hub 分组</th><th style="width:16%">采购员</th><th style="width:22%">数据源</th><th>状态</th></tr></thead><tbody>' + environments.map(function (env) {
      var binding = (ds.environmentBindings || []).find(function (item) { return item.containerCode === env.containerCode; });
      var memberOptions = '<option value="">选择</option>' + (state.members || []).map(function (member) { return '<option value="' + esc(member.id) + '"' + (binding && binding.memberId === member.id ? ' selected' : '') + '>' + esc(member.name || safeId(member.id)) + '</option>'; }).join('');
      var sourceOptions = '<option value="">选择数据源</option>' + sources.map(function (source) { return '<option value="' + esc(source.id) + '"' + (binding && binding.dataSourceId === source.id ? ' selected' : '') + '>' + esc(source.label) + '</option>'; }).join('');
      return '<tr class="' + (binding ? '' : 'warn') + '" data-container="' + esc(env.containerCode) + '"><td><b>#' + esc(env.serialNumber || '—') + '</b><br><small>' + esc(safeId(env.containerCode)) + '</small></td><td><b>' + esc(env.containerName || env.name || env.containerCode) + '</b></td><td>' + esc(env.groupName || env.group || '—') + '</td><td><select class="select binding-member">' + memberOptions + '</select></td><td><select class="select binding-source">' + sourceOptions + '</select></td><td><span class="pill ' + (binding ? 'pill-ok' : 'pill-warn') + '">' + (binding ? '已绑定' : '待处理') + '</span></td></tr>';
    }).join('') + '</tbody></table></div>';
  }
  function renderSources() {
    var role = roleInfo();
    var counts = sourceCounts();
    var admin = role.admin;
    if (!admin) state.sourceTab = 'registry';
    var actions = '<span class="pill" style="border:1px solid #e2e8f0;background:#fff;color:#475569">' + (admin ? '管理员范围' : '仅管理本人数据源') + '</span>' + button(admin ? '添加数据源' : '配置我的个人表','add-source','plus','primary');
    var roleNotice = admin ? '' : '<section class="notice-strip" style="border-color:#dbeafe;background:#eff6ff;color:#172554">' + icon('user') + '<div><b>当前登录：' + esc(role.name) + ' · 采购员</b><p style="color:#1e40af">只显示你的个人速填表和可用的团队协作表；其他采购员与环境映射由管理员维护。</p></div></section>';
    var tabs = '<div class="tabs"><button class="tab ' + (state.sourceTab === 'registry' ? 'active' : '') + '" data-source-tab="registry">数据源注册表<span class="count">' + ((state.sources && state.sources.dataSources || []).length || counts.personal + counts.team) + '</span></button>' + (admin ? '<button class="tab ' + (state.sourceTab === 'buyers' ? 'active' : '') + '" data-source-tab="buyers">采购员默认映射<span class="count">' + ((state.sources && state.sources.buyerProfiles || []).length || 0) + '</span></button><button class="tab ' + (state.sourceTab === 'environments' ? 'active' : '') + '" data-source-tab="environments">HubStudio 环境映射<span class="count">' + counts.mapped + '</span></button>' : '') + '</div>';
    var panel = state.sourceTab === 'buyers' ? buyerDefaults() : (state.sourceTab === 'environments' ? environmentMappings() : sourceRegistry());
    return header('sources',actions) + '<div class="content stack">' + roleNotice + '<section class="metric-grid">' + metric('个人速填表',counts.personal,admin ? '已配置的成员个人表' : '当前账号已配置','user') + metric('团队协作表',counts.team,'可作团队默认','table') + metric('已映射环境',counts.mapped,admin ? 'containerCode 精确绑定' : '当前账号可用','route') + metric('待处理',counts.pending,counts.pending ? '新发现环境尚未映射' : '没有待处理项','alert',counts.pending > 0) + '</section>' + tabs + panel + '</div>';
  }

  function diagnosticRow(iconName, title, detail, ok) {
    return '<div class="diagnostic-row"><span class="row-icon">' + icon(iconName) + '</span><div><b>' + title + '</b><p>' + esc(detail) + '</p></div>' + icon(ok ? 'check' : 'alert') + '</div>';
  }
  function renderDiagnostics() {
    var s = currentStatus();
    var ds = currentSources();
    var update = s.update || {};
    var sourceCount = ds.counts && ds.counts.dataSourceCount || (ds.dataSources || []).length;
    var bindingCount = ds.counts && ds.counts.environmentBindingCount || (ds.environmentBindings || []).length;
    var actions = button('运行完整诊断','run-diagnostics','play','primary');
    return header('diagnostics',actions) + '<div class="content diagnostic-layout"><section class="card section-card">' + sectionTitle('shield','连接与配置检查','只读检测，不会修改配置或启动 HubStudio 环境',nowTime() + ' 检查') +
      diagnosticRow('monitor','本机配置文件','schema v2 · revision ' + safeId(state.config && state.config.configRevision) + ' · 文件权限正常',!!state.config) +
      diagnosticRow('lock','安全凭证存储','HubStudio Key、飞书凭证、设备凭证均由系统安全存储托管',true) +
      diagnosticRow('cloud','云端出站通道',(s.cloudChannel && s.cloudChannel.status === 'online' ? 'TLS 正常 · ' : '正在重连 · ') + relativeTime(s.cloudChannel && s.cloudChannel.lastPollAt),s.cloudChannel && s.cloudChannel.status === 'online') +
      diagnosticRow('gauge','HubStudio Local API',(s.hubStudio && s.hubStudio.connected ? '客户端运行中 · v1 已认证' : '当前未连接') + ' · 端口 ' + ((state.config && state.config.hubPort) || '6873'),s.hubStudio && s.hubStudio.connected) +
      diagnosticRow('database','飞书只读访问',sourceCount + ' 个数据源已登记 · ' + (state.lark && state.lark.ready ? '企业应用连接正常' : '等待连接验证'),!!(state.lark && state.lark.ready)) +
      diagnosticRow('route','环境映射完整性',bindingCount + ' 个已映射 · 0 个冲突',true) + '</section>' +
      '<div class="side-stack"><section class="card section-card">' + sectionTitle('download','软件更新','签名安装包由云端发布清单验证','') + '<div class="section-body"><div class="version-row"><div><p>当前版本</p><b>v' + esc(s.version || update.currentVersion || '—') + '</b></div><span class="pill ' + (update.state === 'available' ? 'pill-warn' : 'pill-ok') + '">' + (update.state === 'available' ? '有新版本' : '已是最新') + '</span></div><div class="platform-note">' + (platform === 'mac' ? 'Universal App · Apple 签名与公证校验 · 自动检查更新' : 'x86_64 · 数字签名校验 · WebView2 Evergreen · 自动检查更新') + '</div>' + button('重新检查更新','check-update','refresh','',false) + '</div></section>' +
      '<section class="card section-card">' + sectionTitle('terminal','日志与维护','敏感字段在写入日志前完成脱敏','') + '<div class="section-body maintenance-buttons">' + button('打开日志目录','open-logs','folder') + button('导出脱敏诊断包','export-diagnostics','download') + button('备份当前配置','backup-config','refresh') + '<p class="path-note">' + (platform === 'mac' ? '~/Library/Application Support/XynigoSourcing/' : '%LOCALAPPDATA%\\Programs\\Xynigo\\') + '</p></div></section></div></div>';
  }
  function renderWorkspace() {
    var page = state.view === 'settings' ? renderSettings() : (state.view === 'sources' ? renderSources() : (state.view === 'diagnostics' ? renderDiagnostics() : renderOverview()));
    app.className = '';
    app.innerHTML = '<div class="desktop-shell">' + sidebar() + '<main class="workspace">' + page + '</main></div>';
  }

  function loadStatus(render) {
    if (previewRole) {
      state.status = sampleStatus;
      if (render !== false && state.identity && state.view === 'overview') renderWorkspace();
      return Promise.resolve(sampleStatus);
    }
    return api('/executor-status.json').then(function (value) {
      state.status = value;
      if (render !== false && state.identity && state.view === 'overview') renderWorkspace();
      return value;
    }).catch(function (error) {
      if (render !== false) showError(error);
      return null;
    });
  }
  function loadWorkspaceData() {
    if (previewRole) {
      state.config = sampleConfig;
      state.lark = {ready:true,credentialConfigured:true,appIdMasked:'cli_aaf…9be2',ledgerTargetConfigured:true};
      state.sources = sampleSources;
      state.members = sampleMembers;
      state.environments = sampleEnvironments;
      renderWorkspace();
      return Promise.resolve();
    }
    var role = roleInfo();
    var jobs = [
      api('/api/config').then(function (x) { state.config = x; }),
      api('/api/lark/status').then(function (x) { state.lark = x; }),
      api('/api/local-config/data-sources').then(function (x) { state.sources = x; })
    ];
    if (role.admin) {
      jobs.push(api('/api/admin/members?status=active').then(function (x) { state.members = x.members || []; }).catch(function () {}));
      jobs.push(api('/api/local-config/data-sources/environment-options?limit=200').then(function (x) { state.environments = x.environments || []; }).catch(function () {}));
    }
    if (canConfigure()) jobs.push(api('/api/lark/config').then(function (x) { state.lark = x; }).catch(function () {}));
    return Promise.allSettled(jobs).then(function () { renderWorkspace(); });
  }
  function authenticated(identity) {
    state.identity = identity;
    state.authMode = 'signedIn';
    clearTimeout(authPollTimer);
    loadWorkspaceData();
    clearInterval(refreshTimer);
    refreshTimer = setInterval(function () { loadStatus(true); }, 5000);
  }
  function initializeAuth() {
    loadStatus(false);
    if (previewRole) {
      state.authMode = 'signedOut';
      state.notice = '';
      renderAuth();
      return;
    }
    state.authMode = 'checking';
    renderAuth();
    api('/api/auth/status').then(function (result) {
      if (result.authenticated && result.identity) authenticated(result.identity);
      else if (result.loginPending) { state.authMode = 'authorizing'; renderAuth(); pollAuth(); }
      else { state.authMode = 'signedOut'; state.notice = result.message || ''; renderAuth(); }
    }).catch(function (error) { state.authMode = 'signedOut'; state.notice = error.message; renderAuth(); });
  }
  function previewIdentity(role) {
    if (role === 'purchaser') return {user:{id:'member-xg',name:'新刚',initials:'XG'},roles:['purchaser'],permissions:['operations.access']};
    return {user:{id:'member-admin',name:'胡康凯',initials:'HK'},roles:['super_admin'],permissions:['system.integration.manage','system.lark_connection.manage','system.member.manage','system.role.manage']};
  }
  function beginAuth() {
    if (previewRole) {
      state.authMode = 'authorizing'; renderAuth(); return;
    }
    state.authMode = 'checking'; renderAuth();
    post('/api/auth/start',{}).then(function (started) {
      state.authMode = 'authorizing'; state.loginUrl = started.loginUrl || ''; renderAuth();
      if (state.loginUrl) nativeAction('open-external',{url:state.loginUrl});
      pollAuth();
    }).catch(function (error) { state.authMode = 'signedOut'; state.notice = error.message; renderAuth(); });
  }
  function pollAuth() {
    clearTimeout(authPollTimer);
    post('/api/auth/poll',{}).then(function (result) {
      if (result.status === 'authenticated' && result.identity) { authenticated(result.identity); showToast('飞书授权成功：' + roleInfo().name + ' · ' + roleInfo().role); return; }
      authPollTimer = setTimeout(pollAuth, 1500);
    }).catch(function (error) { state.authMode = 'signedOut'; state.notice = error.message; renderAuth(); });
  }

  function openSourceModal(sourceId) {
    var role = roleInfo();
    var existing = sourceId ? sourceById(sourceId) : null;
    var editing = !!existing;
    var scope = existing ? existing.scope : 'personal';
    state.sourceDraft = {scope:scope,sourceId:sourceId || '',inspection:null,validation:null};
    var personalDisabled = editing ? ' disabled' : '';
    var teamDisabled = (editing || !role.admin) ? ' disabled' : '';
    var title = editing ? '重新配置飞书数据源' : '添加飞书数据源';
    var intro = editing ? '重新读取并校验表格后，将原子替换当前数据源并保留采购员默认值与环境映射。' : '仅在本机读取和校验表格。完整链接、token、sheet ID 与数据内容不会上传云端。';
    modalRoot.innerHTML = '<div class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true"><h2>' + title + '</h2><p>' + intro + '</p><div class="modal-body"><div class="field"><span>数据源类型</span><div class="source-types"><button class="source-type ' + (scope === 'personal' ? 'active' : '') + '" data-source-scope="personal"' + personalDisabled + '>' + icon('user') + '<b>个人速填表</b><span>绑定当前采购员</span></button><button class="source-type ' + (scope === 'team' ? 'active' : '') + '" data-source-scope="team"' + teamDisabled + '>' + icon('users') + '<b>团队协作表</b><span>多人共享或兜底</span></button></div></div>' + field('source-url','飞书普通电子表格链接','','仅接受企业飞书 /sheets/ 链接；现有链接不会回显','text',false,'https://tenant.feishu.cn/sheets/...') + '<div class="field-grid"><div class="field"><label for="source-sheet">选择工作表</label><select id="source-sheet" class="select" disabled><option>请先读取表格</option></select></div><div class="field" style="display:flex;align-items:flex-end"><button class="button" data-action="inspect-source">读取工作表</button></div></div><div id="source-validation" class="validation-box">' + icon('shield') + '<span>链接只在本机解析；请选择工作表并完成字段校验。</span></div></div><div class="modal-footer"><button class="button" data-action="modal-close">取消</button><button class="button" data-action="validate-source-draft" disabled id="source-validate">校验字段</button><button class="button primary" data-action="save-source-draft" disabled id="source-save">' + (editing ? '替换并保留映射' : '校验并保存') + '</button></div></section></div>';
  }
  function openSourceDetails(sourceId) {
    var source = sourceById(sourceId);
    if (!source) return showError(new Error('数据源不存在或已更新'));
    var editable = sourceCanEdit(source);
    var pending = source.scope === 'personal' && !source.ownerMemberId;
    var owner = source.scope === 'team' ? '采购团队' : (source.ownerMemberId ? memberName(source.ownerMemberId) : '待认领');
    var teamDefault = source.scope === 'team' && currentSources().teamDefaultDataSourceId === source.id;
    var policyButton = source.scope === 'team' && editable ? '<button class="button" data-action="toggle-team-default:' + esc(source.id) + '">' + (teamDefault ? '取消团队默认' : '设为团队默认') + '</button>' : '';
    state.sourceDraft = {sourceId:sourceId,scope:source.scope};
    modalRoot.innerHTML = '<div class="modal-backdrop"><section class="modal" role="dialog" aria-modal="true"><h2>数据源详情</h2><p>敏感表格标识只保存在本机注册表，详情页不会回显 token、Sheet ID 或表格内容。</p><div class="modal-body"><div class="source-details"><div><span>类型</span><b>' + (source.scope === 'team' ? '团队协作表' : '个人速填表') + '</b></div><div><span>归属</span><b>' + esc(owner) + '</b></div><div><span>数据源编号</span><b>' + esc(safeId(source.id)) + '</b></div><div><span>工作表</span><b>' + esc(source.sheetName || '名称未记录') + '</b></div><div><span>读取范围</span><b>' + esc(source.cellRange || '—') + '</b></div><div><span>关联环境</span><b>' + Number(source.environmentCount || 0) + ' 个</b></div></div>' + field('source-label','显示名称',source.label,'1–120 个字符；仅修改本机显示名称','text',!editable) + '<label class="toggle-row"><div><b>启用此数据源</b><p>停用后不会用于采购员默认值或环境解析，现有映射仍保留。</p></div><input id="source-enabled" class="switch" type="checkbox"' + (source.enabled !== false ? ' checked' : '') + (editable ? '' : ' disabled') + '></label><div class="validation-box">' + icon('lock') + '<span>Spreadsheet Token 与 Sheet ID：已安全保存且禁止回显。</span></div></div><div class="modal-footer"><button class="button" data-action="modal-close">关闭</button>' + (pending ? '<button class="button primary" data-action="claim-source:' + esc(source.id) + '">认领为我的个人表</button>' : '') + policyButton + (editable ? '<button class="button" data-action="source-reconfigure:' + esc(source.id) + '">更换表格/工作表</button><button class="button primary" data-action="save-source-metadata">保存修改</button>' : '') + '</div></section></div>';
  }
  function closeModal() { modalRoot.innerHTML = ''; state.sourceDraft = null; }
  function inspectSource() {
    var url = document.getElementById('source-url').value.trim();
    if (!url) return showError(new Error('请粘贴飞书普通电子表格链接'));
    var buttonNode = document.querySelector('[data-action="inspect-source"]');
    buttonNode.disabled = true;
    post('/api/local-config/data-sources/inspect',{spreadsheetUrl:url}).then(function (result) {
      state.sourceDraft.inspection = result;
      document.getElementById('source-url').value = '';
      var sheet = document.getElementById('source-sheet');
      sheet.innerHTML = (result.sheets || []).map(function (item) { return '<option value="' + esc(item.selectionId) + '">' + esc(item.sheetName || '未命名工作表') + ' · ' + Number(item.rowCount || 0) + ' 行</option>'; }).join('');
      sheet.disabled = !(result.sheets || []).length;
      document.getElementById('source-validate').disabled = sheet.disabled;
      document.getElementById('source-validation').innerHTML = icon('check') + '<span>已安全读取 ' + (result.sheets || []).length + ' 个工作表，请选择并校验字段。</span>';
    }).catch(showError).finally(function () { buttonNode.disabled = false; });
  }
  function validateSourceDraft() {
    var draft = state.sourceDraft;
    var sheet = document.getElementById('source-sheet');
    if (!draft || !draft.inspection || !sheet.value) return;
    post('/api/local-config/data-sources/validate',{inspectionId:draft.inspection.inspectionId,selectionId:sheet.value}).then(function (result) {
      draft.validation = result;
      document.getElementById('source-save').disabled = false;
      document.getElementById('source-validation').innerHTML = icon('check') + '<span><b>表头校验通过</b><br>' + esc(result.sheetName || '工作表') + ' · ' + Number(result.headerCount || 0) + ' 列 · 读取范围 ' + esc(result.cellRange || '已确认') + '</span>';
    }).catch(showError);
  }
  function saveSourceDraft() {
    var draft = state.sourceDraft;
    if (!draft || !draft.validation) return;
    var replacing = !!draft.sourceId;
    var path = replacing ? '/api/local-config/data-sources/replace' : (draft.scope === 'team' ? '/api/local-config/data-sources/team' : '/api/local-config/data-sources/personal');
    post(path,{sourceId:draft.sourceId || '',validationId:draft.validation.validationId,setDefault:draft.scope === 'team',expectedRevision:state.sources && state.sources.registryRevision || ''}).then(function (result) {
      state.sources = result; closeModal(); renderWorkspace(); showToast(replacing ? '数据源已替换，原有默认值与环境映射已保留' : '数据源已安全保存到本机注册表');
    }).catch(showError);
  }
  function saveSourceMetadata() {
    var draft = state.sourceDraft;
    var label = document.getElementById('source-label');
    var enabled = document.getElementById('source-enabled');
    if (!draft || !draft.sourceId || !label || !enabled) return;
    var value = label.value.trim();
    if (!value || value.length > 120) return showError(new Error('显示名称必须为 1–120 个字符'));
    post('/api/local-config/data-sources/metadata',{sourceId:draft.sourceId,label:value,enabled:enabled.checked,expectedRevision:state.sources.registryRevision}).then(function (result) {
      state.sources=result; closeModal(); renderWorkspace(); showToast('数据源名称与启用状态已保存');
    }).catch(function (error) { if (error.code === 'config_revision_conflict') loadWorkspaceData(); showError(error); });
  }
  function claimSource(sourceId) {
    post('/api/local-config/data-sources/claim-personal',{sourceId:sourceId,expectedRevision:state.sources.registryRevision}).then(function (result) {
      state.sources=result; closeModal(); renderWorkspace(); showToast('旧个人速填表已认领，并设为你的默认数据源');
    }).catch(function (error) { if (error.code === 'config_revision_conflict') loadWorkspaceData(); showError(error); });
  }
  function revalidateSource(sourceId) {
    post('/api/local-config/data-sources/revalidate',{sourceId:sourceId}).then(function (result) {
      showToast((result.sheetName || sourceName(sourceId)) + ' 验证通过：' + Number(result.headerCount || 0) + ' 列，范围 ' + (result.cellRange || '已确认'));
    }).catch(showError);
  }
  function toggleTeamDefault(sourceId) {
    var current = currentSources().teamDefaultDataSourceId;
    var path = current === sourceId ? '/api/local-config/data-sources/team-default/clear' : '/api/local-config/data-sources/team-default';
    post(path,{sourceId:sourceId,expectedRevision:state.sources.registryRevision}).then(function (result) {
      state.sources=result; closeModal(); renderWorkspace(); showToast(current === sourceId ? '已取消团队默认数据源' : '已设为团队默认数据源');
    }).catch(function (error) { if (error.code === 'config_revision_conflict') loadWorkspaceData(); showError(error); });
  }

  function saveSettings() {
    if (!canConfigure()) return;
    var payload = {
      expectedRevision:state.config && state.config.configRevision || '',
      hubPort:Number(document.getElementById('cfg-hub-port').value),
      concurrency:Number(document.getElementById('cfg-concurrency').value),
      envCreateWorkers:Number(document.getElementById('cfg-env-workers').value),
      verifySampleCount:Number(document.getElementById('cfg-verify').value),
      safeParallelTasks:document.getElementById('cfg-safe').checked
    };
    post('/api/config',payload).then(function (result) {
      state.config = Object.assign({}, state.config || {}, payload, result); renderWorkspace(); showToast('本机配置已校验并原子保存；云端仅收到脱敏摘要');
    }).catch(function (error) { if (error.code === 'config_revision_conflict') loadWorkspaceData(); showError(error); });
  }
  function saveHubKey() {
    var input = document.getElementById('cfg-hub-key');
    var value = input && input.value.trim();
    if (!value) return showToast('现有 HubStudio API Key 保持不变');
    post('/api/hub-api-key',{apiKey:value,clear:false}).then(function () { input.value=''; showToast('HubStudio API Key 已保存到系统安全存储'); loadStatus(); }).catch(showError);
  }
  function saveLarkCredentials() {
    var appId = document.getElementById('cfg-lark-id').value.trim();
    var appSecret = document.getElementById('cfg-lark-secret').value.trim();
    if (!appId && !appSecret) return api('/api/lark/config').then(function (x) { state.lark=x; renderWorkspace(); showToast('飞书企业应用连接状态已刷新'); }).catch(showError);
    if (!appId || !appSecret) return showError(new Error('App ID 与 App Secret 必须同时填写'));
    post('/api/lark/config',{appId:appId,appSecret:appSecret,ledgerUrl:'',clearCredential:false,clearLedgerTarget:false,expectedRevision:state.lark && state.lark.configRevision || ''}).then(function (x) { state.lark=x; renderWorkspace(); showToast('飞书企业应用凭证已保存并完成只读验证'); }).catch(showError);
  }
  function saveEnvironmentRow(row) {
    var memberId = row.querySelector('.binding-member').value;
    var sourceId = row.querySelector('.binding-source').value;
    var containerCode = row.getAttribute('data-container');
    if (!memberId || !sourceId) return;
    post('/api/local-config/data-sources/environment-binding',{memberId:memberId,containerCode:containerCode,sourceId:sourceId,expectedRevision:state.sources.registryRevision}).then(function (x) { state.sources=x; renderWorkspace(); showToast('HubStudio 环境数据源映射已保存'); }).catch(showError);
  }

  document.addEventListener('click', function (event) {
    var viewButton = event.target.closest('[data-view]');
    if (viewButton) { state.view=viewButton.getAttribute('data-view'); state.accountOpen=false; renderWorkspace(); return; }
    var tabButton = event.target.closest('[data-source-tab]');
    if (tabButton) { state.sourceTab=tabButton.getAttribute('data-source-tab'); renderWorkspace(); return; }
    var scopeButton = event.target.closest('[data-source-scope]');
    if (scopeButton && state.sourceDraft && !scopeButton.disabled) {
      state.sourceDraft.scope=scopeButton.getAttribute('data-source-scope');
      document.querySelectorAll('[data-source-scope]').forEach(function (node) { node.classList.toggle('active',node===scopeButton); });
      return;
    }
    var actionNode = event.target.closest('[data-action]');
    if (!actionNode) {
      if (state.accountOpen && !event.target.closest('.account-wrap')) { state.accountOpen=false; renderWorkspace(); }
      return;
    }
    var action = actionNode.getAttribute('data-action');
    if (action === 'auth-start' || action === 'auth-reopen') beginAuth();
    else if (action === 'preview-auth-complete') { authenticated(previewIdentity(previewRole)); showToast('飞书授权成功：' + roleInfo().name + ' · ' + roleInfo().role); }
    else if (action === 'account-toggle') { state.accountOpen=!state.accountOpen; renderWorkspace(); }
    else if (action === 'auth-logout' || action === 'auth-switch') {
      var done = function () { state.identity=null; state.accountOpen=false; state.authMode='signedOut'; state.notice=action === 'auth-switch' ? '请使用新的飞书账号继续登录。' : '已安全退出当前账号。'; renderAuth(); };
      if (previewRole) done(); else post('/api/auth/logout',{}).catch(showError).finally(done);
    }
    else if (action === 'refresh-status') loadStatus().then(function () { showToast('状态已刷新'); });
    else if (action === 'open-cloud') nativeAction('open-external',{url:'https://xynigo.samforo.icu'});
    else if (action === 'go-settings') { state.view='settings'; renderWorkspace(); }
    else if (action === 'go-diagnostics') { state.view='diagnostics'; renderWorkspace(); }
    else if (action === 'open-logs') nativeAction('open-logs');
    else if (action === 'restart-executor') nativeAction('restart-executor');
    else if (action === 'check-update') nativeAction('check-update');
    else if (action === 'run-diagnostics') { nativeAction('run-diagnostics'); showToast('完整诊断已启动，结果会写入脱敏日志'); }
    else if (action === 'export-diagnostics') nativeAction('export-diagnostics');
    else if (action === 'backup-config') nativeAction('backup-config');
    else if (action === 'open-legacy-settings') nativeAction('open-legacy-settings');
    else if (action === 'pair-device') {
      var pairCode = document.getElementById('pair-code').value.trim();
      if (!/^[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}$/i.test(pairCode)) return showError(new Error('请输入有效的 8 位一次性配对码'));
      nativeAction('pair-device',{code:pairCode});
    }
    else if (action === 'save-settings') saveSettings();
    else if (action === 'test-hub') saveHubKey();
    else if (action === 'test-lark') saveLarkCredentials();
    else if (action === 'add-source') openSourceModal();
    else if (action.indexOf('source-details:') === 0) openSourceDetails(action.split(':')[1]);
    else if (action.indexOf('source-reconfigure:') === 0) openSourceModal(action.split(':')[1]);
    else if (action.indexOf('claim-source:') === 0) claimSource(action.split(':')[1]);
    else if (action.indexOf('revalidate-source:') === 0) revalidateSource(action.split(':')[1]);
    else if (action.indexOf('toggle-team-default:') === 0) toggleTeamDefault(action.split(':')[1]);
    else if (action === 'modal-close') closeModal();
    else if (action === 'inspect-source') inspectSource();
    else if (action === 'validate-source-draft') validateSourceDraft();
    else if (action === 'save-source-draft') saveSourceDraft();
    else if (action === 'save-source-metadata') saveSourceMetadata();
    else if (action === 'discover-environments') {
      api('/api/local-config/data-sources/environment-options?limit=200').then(function (x) { state.environments=x.environments || []; renderWorkspace(); showToast('已从 HubStudio 重新发现 ' + state.environments.length + ' 个环境'); }).catch(showError);
    }
  });
  document.addEventListener('change', function (event) {
    var row = event.target.closest('tr[data-container]');
    if (row && (event.target.classList.contains('binding-member') || event.target.classList.contains('binding-source'))) saveEnvironmentRow(row);
  });

  window.xynigoDesktop = {
    refresh:function () { loadStatus(); },
    notify:function (message) { showToast(message); },
    navigate:function (view) {
      if (['overview','settings','sources','diagnostics'].indexOf(view) < 0 || !state.identity) return;
      state.view = view;
      state.accountOpen = false;
      renderWorkspace();
    },
    focusPairing:function () {
      state.view = 'overview';
      if (state.identity) renderWorkspace();
      setTimeout(function () {
        var field = document.getElementById('pair-code');
        if (field) field.focus();
      }, 0);
    }
  };
  initializeAuth();
}());
