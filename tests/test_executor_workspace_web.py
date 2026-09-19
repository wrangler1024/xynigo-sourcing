import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCAL_HTML = ROOT / "src/purchase_tool/web/index.html"
CLOUD_HTML = ROOT / "cloud/auth-service/src/xynigo_auth/web/index.html"


class ExecutorWorkspaceWebTests(unittest.TestCase):
    def test_logistics_feedback_layout_and_error_details_are_stable(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(html, CLOUD_HTML.read_text(encoding="utf-8"))
        for marker in (
            'class="input-row query-scope-grid"',
            '.query-scope-grid { display:grid;',
            'id="queryErrorMask"',
            'function queryErrorShortLabel(error)',
            'data-query-error=',
            'const showRecoveryFrame = recoveryPhase',
            "? '查询任务仍在继续'",
        ):
            self.assertIn(marker, html)

    def test_member_scoped_data_source_controls_are_local_and_safe(self):
        local_html = LOCAL_HTML.read_text(encoding="utf-8")
        cloud_html = CLOUD_HTML.read_text(encoding="utf-8")
        self.assertEqual(local_html, cloud_html)
        for marker in (
            "采购助手数据源",
            "localDataSourceState",
            "/api/local-config/data-sources/claim-personal",
            "/api/local-config/data-sources/inspect",
            "/api/local-config/data-sources/validate",
            "/api/local-config/data-sources/personal",
            "/api/local-config/data-sources/team",
            "/api/local-config/data-sources/buyer-default/clear",
            "/api/local-config/data-sources/environment-binding",
            "/api/local-config/data-sources/environment-binding/remove",
            "/api/local-config/data-sources/team-default/clear",
            "/api/local-config/data-sources/environment-options",
            "expectedRevision:localDataSourceState",
            "离线时可填准确 containerCode",
            "候选读取失败时保留手工输入兜底",
            "链接只用于本次读取",
            "页面仅展示安全摘要",
        ):
            self.assertIn(marker, local_html)
        data_source_section = local_html[
            local_html.index("采购助手数据源"):
            local_html.index('data-settings-view="larkconnection"')
        ]
        self.assertNotIn("spreadsheetToken", data_source_section)
        self.assertNotIn("sheetId", data_source_section)

    def test_desktop_settings_entry_uses_local_revision_guard(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "LOCAL_DESKTOP_SETTINGS_VIEW",
            "get('view') === 'localsettings'",
            "let localSettingsConfigRevision = '';",
            "localSettingsConfigRevision = String(cfg.configRevision || '');",
            "expectedRevision:localSettingsConfigRevision",
            "e.code === 'config_revision_conflict'",
            "已刷新最新值，请重新确认后保存",
        ):
            self.assertIn(marker, html)

    def test_device_settings_editing_is_open_to_members(self):
        # 兼容页（Windows「本机设置」/ macOS ?view=localsettings）与桌面端一致：
        # 写入只要求登录，不再按超级管理员角色锁定（20260917 产品决策）。
        # 渲染判定、保存守卫、按钮兜底三处都要钉死，防止单点改回。
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "const canWriteDeviceConfig = !!authIdentity;",
            "所有登录成员均可修改",
            "if (!authIdentity) {",
            "toast('请先登录后再保存本机设置');",
            "? '保存本机业务设置' : '请先登录';",
        ):
            self.assertIn(marker, html)
        for stale in (
            "canWriteDeviceConfig = hasRole('super_admin')",
            "if (!(hasRole('super_admin') && hasPermission('system.integration.manage')))",
        ):
            self.assertNotIn(stale, html)

    def test_cloud_workspace_routes_business_calls_through_new_executor(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "workspace.rpc.v1",
            "/workspace-rpc",
            "cloudWorkspaceRpcRaw",
            "'/api/query'",
            "'/api/progress'",
            "'/api/envbatch/'",
            "cloudFetchJson('/v1/environment-preferences'",
            "'/api/buyer-library/import/'",
            "'/api/resources/stores'",
            "'/api/resources/proxies'",
        ):
            self.assertIn(marker, html)
        self.assertLess(
            html.index("if (isCloudWorkspaceRpcPath(path))"),
            html.index("return cloudLocalStub(path, opts);"),
        )
        env_groups = html[
            html.index("async function loadEnvGroups(options={})"):
            html.index("function applyLarkRuntimeStatus")
        ]
        self.assertIn("/api/envbatch/preferences", env_groups)
        self.assertNotIn("api('/api/config'", env_groups)
        self.assertIn("renderEnvGroupsForSite(site, configured)", env_groups)
        self.assertIn("scheduleEnvWorkspacePreferenceSave", env_groups)

    def test_phase_two_uses_cloud_runs_and_cloud_encrypted_parse(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "environment.cloud-plan.v1",
            "environment.cloud-inventory.v1",
            "async function cloudEnvironmentPlanParse",
            "async function restoreLatestCloudEnvironmentPlan",
            "'/v1/environment-plans/parse'",
            "'/v1/operation-runs/environment-creation'",
            "'/v1/operation-runs/logistics-query'",
            "async function cloudOperationSnapshot",
            "cloudEnvironmentLegacySnapshot",
            "cloudLogisticsLegacySnapshot",
            "cancelCloudOperationRun('environment'",
            "cancelCloudOperationRun('logistics'",
            "function invalidateEnvironmentPlan(message)",
            "paintContinuousElapsed('envElapsed'",
            "}, 1000);",
            "$('envFile').disabled = busy || !selectionReady;",
            "workspace.snapshot.v1",
            "async function cloudWorkspaceSnapshot",
            "workspaceSnapshotTimeText",
            "/workspace-snapshot",
            "/retry`, {",
            "retryMode:'failed'",
            "retryMode:'single'",
            "cloudEnvironmentSnapshotWithHistory",
        ):
            self.assertIn(marker, html)
        parse_handler = html[
            html.index("$('envFile').onchange"):
            html.index("$('btnEnvPreview').onclick")
        ]
        self.assertIn("cloudEnvironmentPlanParse", parse_handler)
        self.assertIn("$('envSiteGroup').value", parse_handler)
        self.assertIn("envCloudPlanPreview", parse_handler)

    def test_cloud_environment_preview_uses_selected_local_executor(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        handler = html[
            html.index("$('btnEnvPreview').onclick"):
            html.index("$('btnEnvStart').onclick")
        ]
        self.assertIn("environment.preview-bound.v1", handler)
        self.assertIn("/v1/environment-plans/${encodeURIComponent(envCloudPlanId)}/preview", handler)
        self.assertIn("taskId, 300000, state =>", handler)
        self.assertIn("environment.preview.reading_inventory", handler)
        self.assertIn("progressCurrent", handler)
        self.assertIn("created.result || null", handler)
        self.assertIn("cloud_cache", handler)
        self.assertIn("真实干跑通过", handler)
        self.assertNotIn("未调用本地执行器", handler)

    def test_field_feedback_fixes_use_one_selected_state_source(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        preflight = html[
            html.index("async function refreshEnvPreflight"):
            html.index("/* ---------- 操作 ---------- */")
        ]
        self.assertIn("environmentGroup=' + encodeURIComponent(selectedGroup)", preflight)
        self.assertIn(
            "state = envPreflightFromSnapshot(snapshotMeta, site, selectedGroup)",
            preflight,
        )
        derived = html[
            html.index("function envPreflightFromSnapshot"):
            html.index("async function refreshEnvPreflight")
        ]
        self.assertIn("const groupFound = groups.has(selectedGroup)", derived)
        self.assertIn("正式执行前服务端会再次校验", derived)

        even_handler = html[
            html.index("$('btnEnvEven').onclick"):
            html.index("$('btnEnvClear').onclick")
        ]
        self.assertIn("splitEnvEvenly(envDefaultSplit)", even_handler)
        self.assertNotIn("envActiveBuyers()", even_handler)
        self.assertIn('均分（默认三人）', html)

        self.assertIn("payload.configSummaryStale", html)
        self.assertIn("云端仅展示执行器主动上报的严格摘要", html)
        self.assertIn("云端正在内存解析并生成加密短期计划", html)
        self.assertIn("云端只短时保存加密执行计划", html)
        self.assertNotIn("coreVersion=148", html)

    def test_cloud_device_config_is_summary_only(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "设备配置摘要",
            "config.summary.v2",
            "applyLocalExecutorConfigSummary",
            "configSummaryStale",
            "xynigo://settings",
            "摘要不可用：所选设备版本过旧",
        ):
            self.assertIn(marker, html)
        for forbidden in (
            'id="executorConfigHubPort"',
            'id="executorConfigConcurrency"',
            'id="btnLocalExecutorSaveConfig"',
            'startLocalExecutorConfigTask',
            "payload.cachedResult",
            "config-read",
        ):
            self.assertNotIn(forbidden, html)

    def test_cloud_hub_badge_and_query_gate_share_selected_executor_state(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        renderer = html[
            html.index("function renderCloudExecutorHubStatus()"):
            html.index("function setHubStatus(connected)")
        ]
        self.assertIn("hubConnected = true;", renderer)
        self.assertGreaterEqual(renderer.count("hubConnected = null;"), 2)
        self.assertIn("hubConnected = false;", renderer)
        self.assertIn("HubStudio 状态未知", renderer)
        self.assertIn("HubStudio 已启动", renderer)
        self.assertIn("HubStudio 未启动", renderer)
        self.assertIn("HubStudio 已启动 · API 未就绪", renderer)
        setter = html[
            html.index("function setHubStatus(connected)"):
            html.index("function setHiddenQueryColumns(columns)")
        ]
        cloud_branch = setter[
            setter.index("if (CLOUD_WEB_MODE)"):
            setter.index("if (connected === hubConnected")
        ]
        self.assertIn("renderCloudExecutorHubStatus();", cloud_branch)
        self.assertNotIn("hubConnected = connected;", cloud_branch)

    def test_runtime_environment_requires_an_explicit_device_selection(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'id="runtimeStatusBar"',
            'id="runtimeDrawer"',
            'id="runtimeDeviceList"',
            'id="runtimeSelectionNotice"',
            '记住此浏览器的默认设备',
            "error.code = 'executor_selection_required'",
            "$('localExecutorEntry').onclick = openRuntimeDrawer",
            "localStorage.setItem(RUNTIME_EXECUTOR_STORAGE_KEY, value)",
            "sessionStorage.setItem(RUNTIME_EXECUTOR_SESSION_STORAGE_KEY, value)",
            "const values = [sessionRuntimeExecutorId(), rememberedRuntimeExecutorId()]",
            "saveSessionRuntimeExecutorId(selected.id);",
        ):
            self.assertIn(marker, html)
        renderer = html[
            html.index("function renderCloudExecutorHubStatus()"):
            html.index("function setHubStatus(connected)")
        ]
        self.assertNotIn("onlineDevices[0]", renderer)
        selector = html[
            html.index("async function cloudWorkspaceExecutor()"):
            html.index("function cloudRunElapsed(run)")
        ]
        self.assertNotIn("find(compatible)", selector)
        self.assertIn("openRuntimeDrawer();", selector)

    def test_runtime_device_cards_always_show_executor_and_hub_status(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        renderer = html[
            html.index("function runtimeDeviceStatusPresentation(item)"):
            html.index("function openRuntimeDrawer()")
        ]
        for marker in (
            "执行器在线 · ${hubText}",
            "执行器离线 · HubStudio 状态不可用",
            "HubStudio 已启动（API 未就绪）",
            "HubStudio 未启动",
            "HubStudio 状态未上报",
        ):
            self.assertIn(marker, renderer)
        self.assertIn("const status = runtimeDeviceStatusPresentation(item)", renderer)
        self.assertNotIn("const hub = !onlineState ? ''", renderer)

    def test_cloud_logistics_start_failure_is_not_rendered_as_waiting(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        snapshot = html[
            html.index("function cloudLogisticsLegacySnapshot(run)"):
            html.index("function logisticsRunFailureText(code)")
        ]
        self.assertIn("terminal:!!run.terminal", snapshot)
        self.assertIn("errorCode:String(run.resultCode || '')", snapshot)
        renderer = html[
            html.index("function render(snap)"):
            html.index("/* ---------- 轮询 ---------- */")
        ]
        self.assertIn("terminalStartFailure", renderer)
        self.assertIn("terminalSystemFailure", renderer)
        self.assertIn("正在预检并准备执行环境", renderer)
        self.assertNotIn("$('currentEnv')", renderer)
        self.assertIn("logisticsRunFailureText(snap.errorCode)", renderer)
        self.assertIn("hubstudio_browser_core_missing", html)
        self.assertIn("hubstudio_system_resources_insufficient", html)
        self.assertIn("recovering_resources", renderer)
        self.assertIn("HubStudio 资源紧张，正在等待恢复", renderer)
        self.assertIn("当前批次采用单环境运行", renderer)

    def test_cloud_workspace_renews_sessions_and_idempotently_submits_writes(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "const CLOUD_SESSION_KEEPALIVE_MS",
            "async function refreshCloudSession()",
            "setInterval(refreshCloudSession, CLOUD_SESSION_KEEPALIVE_MS);",
            "function workspaceMutationKey(scope, payload)",
            "body:JSON.stringify({method, path, body, idempotencyKey})",
            "error.code = 'executor_rpc_timeout';",
            "let querySubmitting = false;",
            "querySubmitting || isRunning",
            "workspaceMutationKey('query-start', payload)",
            "workspaceMutationKey('env-start', idempotencyPayload)",
            "workspaceMutationKey('env-backup-start', payload)",
        ):
            self.assertIn(marker, html)

    def test_binary_results_no_longer_navigate_to_cloud_local_api_paths(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertIn("'/api/export?format=xlsx&includeScreenshots='", html)
        self.assertIn("workspaceImageUrl(path)", html)
        self.assertNotIn("location.href = '/api/export?format=xlsx'", html)
        self.assertNotIn("image.src = '/api/screenshot?serial='", html)

    def test_logistics_export_reports_progress_and_blocks_duplicate_clicks(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        handler = html[
            html.index("async function downloadLogisticsExport"):
            html.index("$('btnRetryFail').onclick")
        ]
        for marker in (
            "if (logisticsExporting) return;",
            "logisticsExporting = true;",
            "正在快速导出…",
            "已生成 ${filename}${screenshotNote}，请查看浏览器下载记录",
            "logisticsExporting = false;",
            "$('btnExport').textContent = mainLabel;",
        ):
            self.assertIn(marker, handler)

    def test_stopped_logistics_rows_are_not_rendered_as_success(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        renderer = html[
            html.index("function queryRowsHtml(rows, options={})"):
            html.index("function renderStats(rows)")
        ]
        self.assertIn("if (s === 'stopped')", renderer)
        self.assertIn("⏹ 已停止，未完成查询", renderer)
        self.assertLess(
            renderer.index("if (s === 'stopped')"),
            renderer.index("// ok"),
        )
        self.assertIn('id="cntStopped"', html)

    def test_terminal_logistics_progress_distinguishes_valid_and_review_rows(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        snapshot = html[
            html.index("function cloudLogisticsLegacySnapshot(run)"):
            html.index("function logisticsRunFailureText(code)")
        ]
        self.assertIn("cancelled:run.status === 'cancelled'", snapshot)
        renderer = html[
            html.index("function render(snap)"):
            html.index("/* ---------- 轮询 ---------- */")
        ]
        progress = html[
            html.index("function paintQueryProgress(snap, stats)"):
            html.index("function render(snap)")
        ]
        self.assertIn('id="queryProgress"', html)
        self.assertIn("Number(stats.review || 0) > 0", progress)
        self.assertIn("已处理 ${processed} / ${total}", progress)
        self.assertIn("平均 ${average}", progress)
        self.assertIn("本轮重查 ${retryDuration}", progress)
        self.assertIn("累计执行", progress)
        self.assertIn("query-outcome-review", progress)
        self.assertIn("已停止查询：${outcomeCopy}", renderer)
        self.assertNotIn("$('currentEnv')", renderer)
        self.assertIn("'查询未完成行'", renderer)

    def test_cloud_copy_is_synced_from_single_ui_source(self):
        self.assertEqual(
            LOCAL_HTML.read_bytes(),
            CLOUD_HTML.read_bytes(),
        )

    def test_cloud_workspace_keeps_fixed_four_buyer_assignment_cards(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "Object.freeze({name:'新刚', code:'XG'})",
            "Object.freeze({name:'志恒', code:'ZH'})",
            "Object.freeze({name:'康德', code:'KD'})",
            "Object.freeze({name:'宇航', code:'YH'})",
            "buyers:defaultEnvBuyers()",
            "buyerDefaultSplit:[...DEFAULT_ENV_SPLIT]",
            "envBuyers = normalizeEnvBuyers(cfg.buyers);",
            "envDefaultSplit = normalizeEnvDefaultSplit(cfg.buyerDefaultSplit);",
            "data-all=",
            "composeAssignmentSpec()",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("buyers:[], buyerDefaultSplit:[]", html)
        load_groups = html[html.index("async function loadEnvGroups(options={})"):]
        self.assertLess(
            load_groups.index("refreshAssignUi();"),
            load_groups.index("[cfg, result] = await Promise.all"),
        )

    def test_cloud_workspace_does_not_flood_idle_executor_with_progress_reads(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "let queryProgressLoaded = false;",
            "let queryPollInFlight = false;",
            "let groupLoadInFlight = null;",
            "let envProgressLoaded = false;",
            "let envPollInFlight = false;",
            "let envRetryAccountId = '';",
            "if (queryPollInFlight) return;",
            "if (CLOUD_WEB_MODE && queryProgressLoaded && !isRunning",
            "if (envPollInFlight) return;",
            "if (CLOUD_WEB_MODE && envProgressLoaded && !envRunning && !backupRunning",
            "if (groupLoadInFlight) return groupLoadInFlight;",
            "error.code === 'executor_task_busy'",
            "filename:file.name, contentBase64, site:selectedSite",
            "站点已变更，请重新选择 xlsx",
            "mixedSiteCookieCount",
            "混合登录态（需确认）",
            "须先明确确认本批账号的实际站点",
        ):
            self.assertIn(marker, html)

    def test_cloud_environment_plan_naming_reuse_and_stale_upload_guard(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            "let envCloudPlanId = null;",
            "let envLocalPlanId = null;",
            "let envUploadRevision = 0;",
            "envCloudPlanId = result.cloudPlanId;",
            "cloudPlanId:envCloudPlanId",
            "planId:envLocalPlanId",
            "检测到相同文件，已复用 ${cloudPlanExpiryTime(result.expiresAt)} 前有效的解析计划",
            "uploadRevision !== envUploadRevision",
            "$('envSite').value !== selectedSite",
            "$('envSiteGroup').value !== selectedGroup",
            "envUploadRevision += 1;",
        ):
            self.assertIn(marker, html)
        self.assertNotIn("let envPlanId = null;", html)
        self.assertNotIn("planRef:envPlanId", html)
        for removed in (
            "'/api/update/'",
            "CLOUD_WEB_MODE ? 900000 : 30000",
            "id=\"updateCheck\"",
            "id=\"updateNotice\"",
        ):
            self.assertNotIn(removed, html)
        initializer = html[
            html.index("async function initializeAuthenticatedWorkspace(identity)"):
            html.index("function closeAuthLoginWindow()")
        ]
        self.assertNotIn("if (hasFeatureAccess('query'))", initializer)
        self.assertNotIn("if (hasFeatureAccess('envbatch'))", initializer)
        query_panel = html[
            html.index("function setFeaturePanel(module)"):
            html.index("function syncPrimaryNavigation(primary)")
        ]
        self.assertIn("loadGroups();", query_panel)
        self.assertIn("loadEnvGroups().then(() => refreshEnvPreflight());", query_panel)

    def test_environment_setup_is_first_and_site_group_switches_use_cache(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        setup = html.index('id="envCardSetup"')
        parse = html.index('id="envCardParse"')
        assignment = html.index('id="envAssignTitle"')
        self.assertLess(setup, parse)
        self.assertLess(parse, assignment)
        for marker in (
            '① 选择创建参数',
            '② 载入号商名单',
            '③ 采购员分配',
            'CLOUD_WORKSPACE_SNAPSHOT_CLIENT_TTL_MS',
            'cloudWorkspaceSnapshotCachedAt',
            'renderEnvGroupsForSite(site, purchaseTags[site])',
            'scheduleEnvWorkspacePreferenceSave(site, rendered.selected)',
            'paintCachedEnvPreflightSelection()',
            '分组已从缓存即时筛选',
            '偏好正在后台保存',
        ):
            self.assertIn(marker, html)
        site_handler = html[
            html.index("$('envSite').onchange"):
            html.index("$('envSiteGroup').onchange")
        ]
        group_handler = html[
            html.index("$('envSiteGroup').onchange"):
            html.index("function applyLarkRuntimeStatus")
        ]
        self.assertNotIn('await loadEnvGroups()', site_handler)
        self.assertNotIn('refreshEnvPreflight(true)', site_handler)
        self.assertNotIn('refreshEnvPreflight(true)', group_handler)
        self.assertNotIn('e.target.disabled = true', site_handler + group_handler)

    def test_environment_file_failure_clears_previous_statistics(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        handler = html[
            html.index("$('envFile').onchange = async e => {"):
            html.index("$('btnEnvPreview').onclick")
        ]
        self.assertIn("<b>—</b>等待本次校验", handler)
        self.assertIn("<b>—</b>本次校验未通过", handler)
        self.assertLess(
            handler.index("<b>—</b>等待本次校验"),
            handler.index("api('/api/envbatch/parse'"),
        )
        self.assertLess(
            handler.index("<b>—</b>本次校验未通过"),
            handler.index("'文件校验失败：' + err.message"),
        )

    def test_environment_retry_restores_progress_polling_and_blocks_double_clicks(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        retry_handler = html[
            html.index("$('envTbody').addEventListener('click'"):
            html.index("$('btnEnvMapping').onclick")
        ]
        for marker in (
            "button.disabled || envRetryAccountId",
            "button.textContent = '⏳ 正在提交重试…';",
            "envRunning = true;",
            "envProgressLoaded = false;",
            "pollEnvBatch();",
            "setTimeout(pollEnvBatch, 400);",
            "重试已受理；正在从失败步骤继续",
        ):
            self.assertIn(marker, retry_handler)
        self.assertLess(
            retry_handler.index("button.disabled = true;"),
            retry_handler.index("await api('/api/envbatch/retry-row'"),
        )
        self.assertLess(
            retry_handler.index("envRunning = true;"),
            retry_handler.index("toast('重试已受理；正在从失败步骤继续')"),
        )

    def test_environment_stop_rolls_back_owned_rows_and_uses_mode_route(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'id="btnEnvStop"',
            "let envStopSubmitting = false;",
            "let envStopRequested = false;",
            "'/api/envbatch/stop'",
            "'/api/envbatch/backup/stop'",
            "停止并撤销会先阻止新行",
            "历史恢复环境不会删除",
            "已销毁，可重新创建",
            "cleanupStatus === 'deleted'",
            "r.state === 'stopped'",
        ):
            self.assertIn(marker, html)

    def test_new_environment_run_clears_stale_rows_and_explains_ip_errors(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        submission = html[
            html.index("function beginEnvSubmission"):
            html.index("function paintEnvSubmissionState")
        ]
        for marker in (
            "lastEnvironmentRows = [];",
            "旧批次结果已隐藏",
            "本批任务尚未进入出口 IP 检测",
        ):
            self.assertIn(marker, submission)
        render = html[
            html.index("function renderEnvBatch"):
            html.index("let envPollFailures")
        ]
        for marker in (
            "本批任务正在预检和准备，尚无逐行结果",
            "已存在，未重复创建",
            "x.errorCode",
            "检测失败",
        ):
            self.assertIn(marker, render)

    def test_environment_failed_rows_can_be_retried_in_one_batch(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'id="btnEnvRetryFailed"',
            "let envRetryFailedSubmitting = false;",
            "api('/api/envbatch/retry-failed'",
            "workspaceMutationKey('env-retry-failed'",
            "批量重试失败项",
            "只重试当前 failed 行",
        ):
            self.assertIn(marker, html)

    def test_logistics_workspace_keeps_batch_context_and_cloud_preferences(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(html, CLOUD_HTML.read_text(encoding="utf-8"))
        for marker in (
            'id="queryPhaseBanner"',
            'id="businessSummary"',
            'id="carrierDistribution"',
            'class="table-actions query-table-actions"',
            'id="bizFirstTrackLead"',
            "['firstTrackLead','首轨时效']",
            'id="btnQueryFields"',
            "QUERY_VIEW_KEY = 'fulfillment.logistics.results'",
            "/v1/workspace/view-preferences/",
            "parentRunId:cloudLogisticsRunId",
            "/screenshots/' + encodeURIComponent(serial)",
            "+ '/export?includeScreenshots='",
            "screenshotSizeKb:Number(row.screenshotSizeKb || 0)",
            "本轮重查 ${retryDuration}",
            "averageEnvironmentDurationSec",
            "elapsedSec:Number.isFinite(cumulativeDuration)",
            "attemptElapsedSec:Number.isFinite(attemptDuration)",
            "累计执行 ${elapsedClock(snap.elapsedSec || 0)}",
            "queryBackgroundRestoreNotified",
            "window.addEventListener('beforeunload'",
            '输入序号时会在全部 HubStudio 环境中查找',
            "共 ${shippedRows.length} 个已发货订单",
            '首轨时效中位数',
            "'Esperando para enviarse': 'st-procesando'",
            "'Reembolsado': 'st-reembolsando'",
            "'Pagado': 'st-procesando'",
            '退款已处理，无物流',
        ):
            self.assertIn(marker, html)

    def test_terminal_logistics_rows_never_render_spinner_and_remain_retryable(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(html, CLOUD_HTML.read_text(encoding="utf-8"))
        for marker in (
            "terminalIncomplete = terminal && ['pending','running'].includes(rawState)",
            "run.status === 'cancelled' ? 'stopped' : 'fail'",
            "function logisticsRowRetryable(row, terminal=logisticsTerminal)",
            "|| (terminal && state === 'running');",
            "logisticsRowRetryable(row, logisticsTerminal)",
        ):
            self.assertIn(marker, html)
        snapshot_adapter = html[
            html.index("function cloudLogisticsLegacySnapshot"):
            html.index("function logisticsRunFailureText")
        ]
        self.assertIn("state,", snapshot_adapter)
        self.assertNotIn("state:String(row.status || 'pending')", snapshot_adapter)

    def test_logistics_advanced_settings_are_removed_from_cloud_query_form(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        action_row = html[
            html.index('<div class="action-row">', html.index('id="queryPhaseBanner"')):
            html.index('</section>', html.index('id="queryPhaseBanner"'))
        ]
        self.assertNotIn('高级设置', action_row)
        self.assertNotIn('queryExecutorSettings', html)
        self.assertNotIn('id="queryAdvancedSettings"', html)
        self.assertNotIn('id="queryBrowserMode"', html)
        self.assertNotIn('id="queryAllowOpenEnvironment"', html)
        self.assertNotIn("browserMode:$('queryBrowserMode').value", html)
        self.assertNotIn("allowOpenEnvironment:$('queryAllowOpenEnvironment').checked", html)

    def test_logistics_history_ui_is_cloud_scoped_and_keeps_live_results_isolated(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(html, CLOUD_HTML.read_text(encoding="utf-8"))
        for marker in (
            'id="btnQueryHistory"',
            'id="queryHistoryMask"',
            'id="queryHistoryListView"',
            'id="queryHistoryDetailView"',
            'id="queryHistoryResultBody"',
            'id="queryHistoryActor"',
            '查询用户',
            "query.set('userId'",
            '只能重新查询本人创建的历史批次',
            "/v1/operation-runs/logistics-query/history?",
            "/v1/operation-runs/logistics-query/history/",
            "queryRowsHtml(rows, {history:true",
            "activeQueryHistoryDetail",
        ):
            self.assertIn(marker, html)
        detail = html[
            html.index("async function openQueryHistoryDetail"):
            html.index("function closeQueryHistory")
        ]
        self.assertNotIn("cloudLogisticsRunId =", detail)
        self.assertNotIn("lastRows =", detail)

    def test_operation_ids_environment_history_and_status_help_are_accessible(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'data-copy-id=',
            'id="btnEnvironmentHistory"',
            'id="environmentHistoryMask"',
            '/v1/operation-runs/environment-creation/history?',
            '/v1/operation-runs/environment-creation/history/',
            'id="btnQueryTitleHelp"',
            'aria-describedby="queryTitleHelpPopover"',
            'role="tooltip"',
            "if (!event.target.closest('#queryTitleHelp'))",
        ):
            self.assertIn(marker, html)
        self.assertNotIn('id="queryCurrentId"', html)
        query_title = html[
            html.index('class="card-title table-title-row query-results-title"'):
            html.index('class="table-actions query-table-actions"')
        ]
        self.assertNotIn('class="legend"', query_title)
        self.assertNotIn('id="currentEnv"', html)
        self.assertNotIn('id="elapsed"', html)

    def test_logistics_dates_stack_without_growing_the_query_row(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertIn('class="date-time-stack"', html)
        self.assertIn('.date-time-stack { min-width:82px; height:26px;', html)
        self.assertIn("cell('orderTime', dateTimeStack(r.orderTime)", html)
        self.assertIn("cell('queryTime', dateTimeStack(r.time)", html)

    def test_runtime_drawer_closes_on_outside_pointer_and_escape(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertIn('id="runtimeDrawerBackdrop"', html)
        self.assertIn("$('runtimeDrawerBackdrop').onclick = closeRuntimeDrawer", html)
        self.assertIn("e.key === 'Escape' && !$('runtimeDrawer').hidden", html)

    def test_logistics_history_rerun_requires_new_executor_confirmation(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        handler = html[
            html.index("$('btnHistoryRerun').onclick"):
            html.index("function closeQueryExportMenu")
        ]
        for marker in (
            "originalEnvironmentSerials",
            "$('serialsInput').value = serials.join('\\n');",
            "localExecutorSelectedDeviceId = '';",
            "saveSessionRuntimeExecutorId('');",
            "saveRememberedRuntimeExecutorId('');",
            "openRuntimeDrawer();",
            "不会自动开始查询",
        ):
            self.assertIn(marker, handler)
        self.assertNotIn("$('btnStart').click", handler)

    def test_logistics_export_supports_fast_full_and_missing_screenshot_feedback(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'id="queryExportSplit"',
            'id="btnExportQuick"',
            'id="btnExportFull"',
            'data-export-screenshots="false"',
            'data-export-screenshots="true"',
            "includeScreenshots=' + String(includeScreenshots)",
            "X-Xynigo-Screenshot-Included",
            "X-Xynigo-Screenshot-Missing",
            "张已过期或缺失",
            "logisticsHistoryRunId(activeQueryHistoryDetail)",
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()


class StoreFinanceRetryWiringTests(unittest.TestCase):
    """补采失败回归：勾选键(environmentId)与结果行键(environmentSerial)
    不同，重试必须显式携带序号，不得复用勾选态推序号（否则空清单秒完成）。"""

    def test_retry_sends_failed_serials_explicitly(self):
        for path in (LOCAL_HTML, CLOUD_HTML):
            html = path.read_text(encoding="utf-8")
            self.assertIn(
                "await sfStart({ queryMode: 'failed_retry', serials,",
                html)
            self.assertIn("sourceRunId: SF_STATE.runId", html)
            self.assertIn(
                "const serials = (Array.isArray(options.serials)"
                " && options.serials.length)", html)
            # 勾选展示映射按序号→环境ID，不得直接把序号塞进 selected
            self.assertNotIn("SF_STATE.selected = new Set(serials);", html)

    def test_store_finance_running_banner_keeps_spinner_and_round_copy(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(html, CLOUD_HTML.read_text(encoding="utf-8"))
        for marker in (
            # 运行横幅的转圈必须保持显示；此前写成 !stopRequested，运行中反而隐藏。
            "$('sfPhaseSpin').hidden = false;",
            "'巡检运行中'",
            "自动补采（第 ${round}/2 轮）",
            "首轮巡检 + 2 轮自动补采，共 3 轮",
        ):
            self.assertIn(marker, html)
        self.assertNotIn(
            "$('sfPhaseSpin').hidden = !SF_STATE.stopRequested;", html)


class AfterSaleClaimWiringTests(unittest.TestCase):
    """售后处理模块：两步式（扫描→勾选→提交）的界面契约与安全口径。"""

    def _read(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return local

    def test_module_is_registered_in_both_nav_layers(self):
        html = self._read()
        self.assertIn('data-module="aftersale"', html)
        self.assertIn('id="afterSalePanel"', html)
        self.assertIn("aftersale: {", html)
        self.assertIn("if (module === 'aftersale') asInit();", html)

    def test_scan_and_submit_hit_the_formal_cloud_routes(self):
        html = self._read()
        self.assertIn("cloudFormalExecutor('after.sale.scan.v1')", html)
        self.assertIn("cloudFormalExecutor('after.sale.track.v1')", html)
        # 提交入口按范围动态选能力位：按单提交与按环境直提各要求自己的能力
        submit = html[html.index('async function asSubmitItems('):]
        submit = submit[:submit.index('\n}\n')]
        self.assertIn("'after.sale.claim.v1'", submit)
        self.assertIn("'after.sale.claim-environment.v1'", submit)
        self.assertIn("'/v1/after-sale/scan'", html)
        self.assertIn("'/v1/operation-runs/after-sale-claim'", html)

    def test_refund_path_is_fixed_to_original_payment_account(self):
        # 页面默认选中 SHEIN 钱包，脚本必须固定切到原路退回，否则退款进钱包
        html = self._read()
        self.assertIn("退款路径固定「原路退回」", html)

    def test_only_claimable_rows_are_selectable(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        helper = html[html.index('function asCanSelectScanRow('):]
        helper = helper[:helper.index('\n}')]
        self.assertIn("row.claimable === true && row.status === 'ok'", helper)
        self.assertIn('!asSubmissionBlocksSelection(asSubmissionForRow(row))', helper)
        self.assertTrue('const canPick = asCanSelectScanRow(row);' in html)

    def test_after_sale_type_is_visible_in_field_and_both_tables(self):
        """本期类型固定「丢件退款」：必须在选择区与两张表里都看得见。

        只写在小字图例里不够——采购同事要能一眼看出这一单做的是什么售后，
        因此①有独立字段、②清单与③结果各有一列，且渲染同一个常量。
        """
        html = self._read()
        self.assertIn("const AFTER_SALE_TYPE = '丢件退款';", html)
        # 类型选择＝三段式卡片（与设计稿一致）：丢件退款为本期实现，另两个标注「规划」
        self.assertIn('id="asTypeBar"', html)
        self.assertIn('class="mode-bar"', html)
        self.assertIn("const AS_TYPES = [", html)
        for label in ('丢件退款', '催促发货', '取消订单'):
            self.assertIn(label, html)
        self.assertIn("state: 'plan'", html)
        # 规划类型必须走明确的未实现态，而不是摆假数据
        self.assertIn('id="asPlanBanner"', html)
        self.assertIn('为规划类型，本次未实现', html)
        self.assertIn('>售后类型<', html)          # 表头
        # 两张表的行模板都要渲染该常量；无订单的环境行不标类型
        self.assertIn("${orderNo ? esc(AFTER_SALE_TYPE) : '—'}", html)
        self.assertIn('${esc(AFTER_SALE_TYPE)}', html)
        self.assertEqual(html.count('AFTER_SALE_TYPE)'), 2)
        # 表头列数与空态 colspan 必须同步（①字段+②③各一列）
        self.assertIn('colspan="11"', html)

    def test_all_three_tables_have_header_row_cell_parity(self):
        """三张表的表头列数必须等于行模板的单元格数。

        踩过两次：改表结构时漏列/丢节点 id，导致整列错位（③ 曾把退款单号显示在
        商家名列上）。这里直接数表头 th 与行模板 td（含 asThumbCell / asClaimPill /
        timelineCell 这类自带 td 的辅助函数），比断言某个 colspan 更能防回归。
        """
        html = self._read()
        for table_id in ('asScanTable', 'asClaimTable', 'asTrackTable'):
            block = html[html.index(f'id="{table_id}"'):]
            block = block[:block.index('</table>')]
            header = block.count('<th') - block.count('<thead')
            self.assertGreater(header, 0, table_id)
        # ③ 的行模板：显式 td + 两个自带 td 的辅助函数，必须与表头 11 相等
        claim_block = html[html.index('id="asClaimTable"'):]
        claim_block = claim_block[:claim_block.index('</table>')]
        self.assertEqual(claim_block.count('<th') - claim_block.count('<thead'), 11)
        # 只数行模板本体（从 .map( 到 }).join），排除同函数里的空态字符串
        # 行模板抽成了 asClaimRowHtml：③ 与「历史详情」共用一套，列序只在一处定义
        tpl = html[html.index('function asClaimRowHtml('):]
        tpl = tpl[:tpl.index('\n}')]
        self.assertTrue('visible.map(asClaimRowHtml)' in html)
        self.assertIn('rows.map(asClaimRowHtml)', html,
                      '历史详情必须复用 ③ 的行模板，不得另写一份（列序会漂）')
        cells = tpl.count('<td') + tpl.count('${asScanGoodsHtml')
        self.assertEqual(cells, 11, '③ 行模板单元格数与表头不一致（会整列错位）')
        # 只数个数拦不住列序错（曾把状态列留在第 6 位）：这里按表头顺序逐个钉行模板
        # （从第一个 <td 起算，否则会把 tr 上的 data-as-claim 属性计入）
        expected = ['环境序号', '订单号', '商品图', '售后类型', '送达时间', '退款单号',
                    '退款路径', '退款信用卡', '状态', '操作时间', '备注']
        self.assertEqual(
            re.findall(r'<th[^>]*>([^<]+)</th>', claim_block), expected,
            '③ 表头顺序变了就必须同步改行模板与这里')
        cells = tpl[tpl.index('<td'):]
        markers = ['row.environmentSerial', 'row.orderNo', '${asScanGoodsHtml',
                   'AFTER_SALE_TYPE', 'row.deliveredAt', 'r.refundBillId',
                   'r.refundPath', 'asRefundAccountHtml', 'asClaimPill',
                   'row.operationCompletedAt', 'asClaimReasonHtml']
        order = [cells.index(k) for k in markers]
        self.assertEqual(order, sorted(order), '③ 行模板列序与表头不一致（会整列错位）')
        # ④ 的行模板同理（含 timelineCell 自带 td）
        track_block = html[html.index('id="asTrackTable"'):]
        track_block = track_block[:track_block.index('</table>')]
        self.assertEqual(track_block.count('<th') - track_block.count('<thead'), 10)
        track_tpl = html[html.index('tbody.innerHTML=selected.map('):]
        track_tpl = track_tpl[:track_tpl.index("}).join('')")]
        # ④ 的 timeline 是包在显式 <td> 里的，只额外算商品图那格
        track_cells = track_tpl.count('<td') + track_tpl.count('${asThumbCell')
        self.assertEqual(track_cells, 10, '④ 行模板单元格数与表头不一致')

    def test_track_card_wired_to_real_endpoints(self):
        html = self._read()
        self.assertIn("cloudFormalExecutor('after.sale.track.v1')", html)
        self.assertIn("'/v1/after-sale/track'", html)
        self.assertIn("'/v1/after-sale/track/'", html)
        self.assertIn("const AS_TL_ORDER = ['submitted', 'reviewing', 'processing', 'shein_refunded', 'bank_processed']", html)

    def test_track_accepts_manually_specified_bills(self):
        """④ 必须能回访「指定单」——不依赖提交记录。

        原因：回访对象若只认「本页 ③ 提交过的单」，同事换浏览器/换台机就回访不了，
        别人提交的单也回访不了。手工入口是这条链路的解耦点。
        """
        html = self._read()
        self.assertIn('id="asTrackBills"', html)
        self.assertIn('id="asTrackManual"', html)
        self.assertIn('function asParseManualBills', html)
        self.assertIn('function asTrackManual', html)
        # asTrack 必须接受显式 items（手工入口靠它），并保留从 ③ 推导的默认路径
        self.assertIn('async function asTrack(explicitItems)', html)
        self.assertIn("Array.isArray(explicitItems) && explicitItems.length", html)

    def test_stop_routes_exist_for_both_phases(self):
        html = self._read()
        self.assertIn("/cancel', { method: 'POST' })", html)
        self.assertIn("'/v1/after-sale/scan/'", html)


class ModeCardIconTests(unittest.TestCase):
    """类型卡片图标：统一用侧栏同款线性图标（.nav-icon + .nav-text），禁用 emoji。

    原先「环境创建」两张卡用 🔢/📦 表达类型，实心彩色与页面线性风格冲突；
    采购售后的类型卡也走同一套，因此这里同时钉住全局样式与两处用法。
    """

    def _htmls(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return (local,)

    def test_env_creation_cards_use_icon_tiles(self):
        for html in self._htmls():
            self.assertIn('id="envModeBar"', html)
            # 两张卡各一个图标砖，且仍保留 data-mode 契约（JS 靠它切换）
            # 注意按块计数：售后类型卡也用同一个 .mode-tab-inner（这里只数环境创建的两张）
            bar = html[html.index('id="envModeBar"'):html.index('id="envCardSetup"')]
            self.assertEqual(bar.count('class="mode-tab-inner"'), 2)
            self.assertIn('data-mode="bound"', html)
            self.assertIn('data-mode="backup"', html)
            # 图标砖数量按块断言：侧栏一级菜单本身就用了同一套 class
            bar = html[html.index('id="envModeBar"'):html.index('id="envCardSetup"')]
            self.assertEqual(bar.count('<span class="nav-icon" aria-hidden="true">'), 2)
            self.assertEqual(bar.count('<svg viewBox="0 0 24 24">'), 2)

    def test_env_creation_emoji_are_removed(self):
        for html in self._htmls():
            self.assertNotIn('🔢 绑号环境', html)
            self.assertNotIn('📦 备用·测试环境', html)

    def test_global_mode_card_icon_styles_exist(self):
        for html in self._htmls():
            self.assertIn('.mode-tab-inner { display: flex;', html)
            self.assertIn('.mode-tab .nav-text b { font-size: 14px; }', html)
            self.assertIn(
                '.mode-tab.active .nav-icon { color: #fff; '
                'background: var(--action-gradient); box-shadow: none; }', html)


class AfterSaleThumbnailWiringTests(unittest.TestCase):
    """商品图列：复用采购任务那套缩略图约定，不新造样式、不走外部代理。"""

    def _html(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return local

    def test_panel_scopes_thumbnail_size_for_dense_tables(self):
        html = self._html()
        self.assertIn('#afterSalePanel .procurement-image-thumb', html)
        self.assertIn('width: 34px; height: 44px;', html)

    def test_image_url_guard_allows_shein_cdn_and_local_preview(self):
        # 白名单是既有函数，三张表的缩略图都必须走它，不得直接拼 URL
        html = self._html()
        self.assertIn('function safeProcurementImageUrl', html)
        self.assertIn("host.endsWith('.ltwebstatic.com')", html)
        self.assertIn("parsed.pathname.startsWith('/preview-product-')", html)

    def test_thumbnails_are_lazy_and_referrer_free(self):
        html = self._html()
        self.assertIn('loading="lazy" referrerpolicy="no-referrer"', html)


class AfterSaleRunStripWiringTests(unittest.TestCase):
    """运行状态条：三段共用、面板级 sticky；扫描终态仅手动折叠。

    来自原型定版（docs/prototypes/20260915-procurement-after-sale-v2-multitype.md
    §「动态操作进度放哪里」）。踩过的两个坑都写成断言：
    ① 状态条必须在**面板级**——放进卡片里 sticky 会被卡片边界带走；
    ② 收起靠 class，进度区排版不能写内联 display（会压过收起态的 display:none）。
    """

    def _html(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return local

    def test_single_strip_and_single_nodes(self):
        html = self._html()
        for node in ('id="asRunStrip"', 'id="asPhaseBanner"', 'id="asProgress"',
                     'id="asProgressText"', 'id="asStop"', 'id="asStripToggle"'):
            self.assertEqual(html.count(node), 1, f'{node} 应只有一个')

    def test_strip_sits_above_card_one_at_panel_level(self):
        """位置＝① 之上，且是面板的直接子元素（在卡片里就吸不住）。"""
        html = self._html()
        panel = html.index('id="afterSalePanel"')
        strip = html.index('id="asRunStrip"')
        card_one = html.index('① 选择范围')
        self.assertLess(panel, strip)
        self.assertLess(strip, card_one)
        # 面板与状态条之间不能夹着卡片/段落的开始标签
        between = html[panel:strip]
        self.assertNotIn('<section', between)

    def test_strip_is_sticky(self):
        html = self._html()
        self.assertIn('#afterSalePanel #asRunStrip { position: sticky; top: 8px;',
                      html)
        self.assertIn('#afterSalePanel #asRunStrip .query-phase-banner '
                      '{ margin-bottom: 0; flex-wrap: wrap; }', html)

    def test_strip_holds_progress_and_stop_exactly_once(self):
        """进度条/进度文本/停止按钮在状态条里；① 卡里不再各留一份。"""
        html = self._html()
        strip = html[html.index('id="asRunStrip"'):]
        strip = strip[:strip.index('① 选择范围')]
        for node in ('id="asProgress"', 'id="asProgressText"', 'id="asStop"'):
            self.assertIn(node, strip, f'{node} 应在状态条里')
        card_one = html[html.index('① 选择范围'):]
        card_one = card_one[:card_one.index('② 订单处理')]
        for node in ('id="asProgress"', 'id="asProgressText"', 'id="asStop"'):
            self.assertNotIn(node, card_one, f'{node} 不该在 ① 卡里再留一份')

    def test_progress_wrap_uses_class_not_inline_display(self):
        """排版走 class：内联 display:flex 会压过收起态的 display:none。"""
        html = self._html()
        strip = html[html.index('id="asRunStrip"'):]
        strip = strip[:strip.index('① 选择范围')]
        self.assertIn('class="progress-wrap as-strip-progress"', strip)
        self.assertNotIn('style="display:flex"', strip.replace(' ', ''))
        self.assertIn('#afterSalePanel .as-strip-progress { margin-left: auto;',
                      html)

    def test_scan_terminal_bypasses_automatic_collapse(self):
        """扫描终态在定时折叠前返回，其他阶段保留原行为。"""
        html = self._html()
        self.assertNotIn('AS_PHASE_ANCHORS', html)
        self.assertNotIn('asPhaseAnchor', html)
        phase = html[html.index('function asSetPhase('):]
        phase = phase[:phase.index('\n}\n')]
        self.assertIn('asStripToggle(false)', phase)
        self.assertLess(phase.index("if (!spin && AS_STATE.mode === 'scan') {"),
                        phase.index('setTimeout(() => asStripToggle(true), 2200)'))
        self.assertIn('setTimeout(() => asStripToggle(true), 2200)', phase)
        self.assertIn('asStripTerminal', phase)
        self.assertIn('/完成|失败|已停止|不可用/', html)
        self.assertIn("$('asStripToggle').onclick", html)

    def test_each_flow_sets_its_stage_before_first_message(self):
        html = self._html()
        # 提交的阶段落在共用入口 asSubmitItems 里（asSubmit/补提/指定单/历史重提都走它）。
        # 任务模式与运行状态在创建成功后一次建立：mode 先行会在异步创建窗口里
        # 让旧任务编号被轮询冒充新任务（提交列表与进度失联的根因），失败路径也会残留。
        for func, mode in (('async function asScan(', 'scan'),
                           ('async function asSubmitItems(', 'claim'),
                           ('async function asTrack(', 'track')):
            body = html[html.index(func):]
            body = body[:body.index('\n}\n')]
            mode_at = body.index(f"AS_STATE.mode = '{mode}'")
            self.assertLess(mode_at, body.index('AS_STATE.running = true'),
                            f'{func} 任务模式必须先于运行状态建立')
            self.assertGreater(mode_at, body.index('await asCreateTask'),
                               f'{func} 任务模式必须在创建请求返回后才写入')


class AfterSaleThreeRequirementsWiringTests(unittest.TestCase):
    """三个需求的接线契约：补提失败 / 直接提交指定单 / 提交历史。

    口径来自 docs/20260916_售后需求排期与暂缓.md §1 与两份需求文档，
    其中最容易走偏的三条：① 三处提交只走一个建 Run 入口 ② 补提范围排除 blocked
    ③ 历史列表不做「本人 + 管理员」过滤。
    """

    def _html(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return local

    def _fn(self, html, signature):
        body = html[html.index(signature):]
        return body[:body.index('\n}\n')]

    def test_single_submit_entry_point(self):
        """三处提交共用同一个建 Run 入口，不得出现第二条提交路径。"""
        html = self._html()
        self.assertEqual(
            html.count("asCreateTask('/v1/operation-runs/after-sale-claim',"), 1,
            '建 Run 的请求只应出现一次（asSubmitItems）')
        self.assertIn('async function asSubmitItems(', html)
        for caller in ('async function asSubmit()',
                       'async function asRetryFailedClaims(',
                       'async function asDirectSubmit()'):
            self.assertIn('asSubmitItems(', self._fn(html, caller),
                          f'{caller} 必须走共用入口')
        self.assertIn('asRetryFailedClaims(rows, AS_HISTORY.runId)',
                      self._fn(html, 'async function asRetryHistoryBatch('))

    def test_retry_scope_excludes_blocked(self):
        """补提范围＝可恢复失败；blocked 不给入口（重提只会白跑一遍写操作）。"""
        html = self._html()
        self.assertIn(
            "const AS_RECOVERABLE_CLAIM_STATUS = ['fail', 'login', 'inuse', 'stopped'];",
            html)
        scope = html[html.index('const AS_RECOVERABLE_CLAIM_STATUS'):
                     html.index('function asClaimItemsFromRows(')]
        self.assertNotIn('blocked', scope,
                         'blocked 不能进补提范围')
        retry = self._fn(html, 'async function asRetryFailedClaims(')
        self.assertIn('confirm(', retry)          # 写操作二次确认
        self.assertIn('retryFromRunId', retry)    # 记「重提自哪一批」

    def test_direct_submit_parses_and_reports_invalid_groups(self):
        """指定单：分隔符容错 + 字段不足要报出来，不静默丢弃。"""
        html = self._html()
        parse = self._fn(html, 'function asParseDirectOrders(')
        self.assertIn('split(/[;；\\n]+/)', parse)
        self.assertIn('split(/[\\s,，、]+/)', parse)
        self.assertIn('invalid.push(', parse)
        self.assertIn('duplicates.push(', parse)
        direct = self._fn(html, 'async function asDirectSubmit(')
        self.assertIn('parsed.invalid.length', direct)
        self.assertIn('asSubmitItems(parsed.items', direct)
        self.assertIn('confirm(', direct)

    def test_history_detail_actions_reuse_existing_paths(self):
        """从历史发起的三类动作都不新造链路：回访走 ④、重提走共用入口、导出走批次路由。"""
        html = self._html()
        track = self._fn(html, 'async function asTrackHistoryBatch(')
        self.assertIn('await asTrack(items)', track)
        self.assertIn('asTrackItemsFromRows(rows)', track)
        export = self._fn(html, 'async function asExportClaimHistory(')
        self.assertIn('asDownloadClaimBatch(runId', export)
        export = self._fn(html, 'async function asDownloadClaimBatch(')
        self.assertIn("'/v1/operation-runs/after-sale-claim/history/'", export)
        self.assertIn("+ '/export'", export)
        self.assertIn('workspaceDownloadName(', export)

    def test_history_detail_table_reuses_claim_columns(self):
        """历史详情表头 = ③ 表头（同一套 11 列、同一列序）。"""
        html = self._html()
        claim = html[html.index('id="asClaimTable"'):]
        claim = claim[:claim.index('</table>')]
        detail = html[html.index('id="asHistoryDetailView"'):]
        detail = detail[:detail.index('</table>')]
        self.assertEqual(
            re.findall(r'<th[^>]*>([^<]+)</th>', claim),
            re.findall(r'<th[^>]*>([^<]+)</th>', detail))

    def test_write_entries_share_type_and_running_guard(self):
        """规划类型与「已有任务在跑」必须挡住**所有**写入口（评审必修第 2 条）。

        asSubmitItems 早退要给文案（调用方可能已弹过 confirm，静默 return 会让人
        以为提交了）；四个入口的 disabled 统一在 asSyncWriteButtons 里算，
        避免以后再加入口时漏接（漏一个就会在规划类型下真的提交退款）。
        """
        html = self._html()
        # 闸门只有一处实现，四个入口都要在**确认框之前**过它
        gate = self._fn(html, 'function asWriteEntryReady(')
        self.assertIn('if (AS_STATE.running || AS_STATE.starting) {', gate)
        self.assertIn('已有任务在跑', gate)
        self.assertIn("if (AS_STATE.type !== 'refund') {", gate)
        self.assertIn('该类型尚未实现', gate)
        for signature in ('async function asSubmit()',
                          'async function asRetryFailedClaims(',
                          'async function asDirectSubmit('):
            body = self._fn(html, signature)
            self.assertIn('asWriteEntryReady()', body,
                          f'{signature} 必须过闸门')
            if 'confirm(' in body:
                self.assertLess(body.index('asWriteEntryReady()'),
                                body.index('confirm('),
                                f'{signature} 的闸门必须在确认框之前')
        # 共用入口再留一道（任何新调用方都绕不过去）
        self.assertIn('if (!asWriteEntryReady()) return false;',
                      self._fn(html, 'async function asSubmitItems('))
        # 统一刷按钮态，且接在类型切换上
        sync = self._fn(html, 'function asSyncWriteButtons(')
        self.assertIn("AS_STATE.type !== 'refund' || AS_STATE.running", sync)
        self.assertIn("$('asDirectSubmit')", sync)
        self.assertIn("$('asHistoryRetry')", sync)
        self.assertIn('asSyncRetryButton(AS_STATE.claimRows)', sync)
        self.assertIn('asSyncWriteButtons();',
                      self._fn(html, 'function asSelectType('))
        retry = self._fn(html, 'function asSyncRetryButton(')
        self.assertIn("AS_STATE.type !== 'refund'", retry)
        # 点击前先看 disabled（disabled 只是视觉，别绕过）
        for button in ('asRetryFailed', 'asDirectSubmit'):
            self.assertIn(f"$('{button}').disabled) return", html)

    def test_history_retry_closes_modal_after_start(self):
        """历史重提成功后与「回访本批」同款关弹层：新批次进度写在弹层后面的 ③。"""
        html = self._html()
        retry = self._fn(html, 'async function asRetryHistoryBatch(')
        self.assertIn('const started = await asRetryFailedClaims(', retry)
        self.assertIn('if (started) asCloseClaimHistory();', retry)
        failed = self._fn(html, 'async function asRetryFailedClaims(')
        self.assertIn('return await asSubmitItems(', failed)

    def test_detail_meta_escapes_retry_source(self):
        """详情 meta 走 innerHTML，重提来源是请求体里的自由字符串，必须转义。"""
        html = self._html()
        detail = self._fn(html, 'function asRenderClaimHistoryDetail(')
        self.assertIn('重提自 ${esc(asShortRef(batch.retryFromRunId))}', detail)
        self.assertIn('批次 ${esc(asShortRef(batch.runId))}', detail)

    def test_history_list_environment_count_column(self):
        """历史列表「环境数」列：钉列序 + 空态 colspan + 行模板格数三处一致。

        列数从 11 变 12，漏改占位行的 colspan 只会让空态少一格（肉眼看不出），
        所以 colspan 与表头列数在这里必须相等；列序也逐列钉住，
        只数单元格个数会让整列错位照样通过。
        """
        html = self._html()
        anchor = html.index('id="asHistoryBody"')
        table = html[html.rindex('<table', 0, anchor):]
        table = table[:table.index('</table>')]
        columns = re.findall(r'<th[^>]*>([^<]+)</th>', table)
        self.assertEqual(columns, [
            '批次时间', '操作人', '执行器', '环境数', '提交', '已受理', '已跳过',
            '已停止', '失败', '状态', '重提自', '操作'])
        colspans = {int(count) for count in re.findall(r'colspan="(\d+)"', table)}
        self.assertEqual(colspans, {len(columns)},
                         '空态/错误态的 colspan 必须等于表头列数')
        render = self._fn(html, 'function asRenderClaimHistory(')
        template = render[render.index('items.map(item => `<tr>'):]
        template = template[:template.index(".join('')")]
        self.assertEqual(template.count('<td'), len(columns), '行模板格数必须等于列数')
        self.assertIn('${Number(item.environmentCount || 0)}', template)
        self.assertLess(template.index('item.executorName'),
                        template.index('item.environmentCount'))
        self.assertLess(template.index('item.environmentCount'),
                        template.index('item.totalCount'))

    def test_history_list_is_not_actor_scoped(self):
        """历史列表在租户内互相可见：云端那条路由不许出现 history_admin 过滤。"""
        main = (LOCAL_HTML.parents[3] / 'cloud' / 'auth-service' / 'src'
                / 'xynigo_auth' / 'main.py').read_text(encoding='utf-8')
        block = main[main.index('def list_after_sale_claim_history('):]
        block = block[:block.index('@app.get(')]
        self.assertNotIn('history_admin', block)
        self.assertNotIn('_user_has_role', block)
        self.assertIn('permission="assistant.access"', block)


class AfterSaleTrackExportWiringTests(unittest.TestCase):
    """④ 导出接线：按钮落在卡片里，导出对象是「这一次回访」，下载复用既有助手。"""

    def test_claim_export_handlers_keep_current_and_history_batches_separate(self):
        import shutil
        import subprocess
        if not shutil.which('node'):
            self.skipTest('Node.js is required for export handler checks')
        result = subprocess.run(
            ['node', str(ROOT / 'tests/fixtures/after_sale_export_ui.cjs')],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _html(self):
        local = LOCAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(local, CLOUD_HTML.read_text(encoding="utf-8"))
        return local

    def _body(self):
        html = self._html()
        body = html[html.index('async function asExportTrack()'):]
        return html, body[:body.index('\n}\n')]

    def test_export_button_sits_in_track_card(self):
        html = self._html()
        marker = html.index('id="asTrackExport"')
        actions = html[html.rindex('<div class="table-actions">', 0, marker):]
        actions = actions[:actions.index('</div>')]
        # 同一个 table-actions 里，导出是次要按钮、刷新仍是主按钮
        self.assertIn(
            '<button class="btn" id="asTrackExport">导出退款跟踪 Excel</button>', actions)
        self.assertIn(
            '<button class="btn primary" id="asTrack">刷新退款进度</button>',
            actions)
        self.assertLess(actions.index('asTrackExport'), actions.index('id="asTrack"'))
        self.assertIn("$('asTrackExport').onclick = asExportTrack;", html)

    def test_export_targets_current_track_task(self):
        """导出的是本页 ④ 这次回访的任务，不重跑回访、也不吃输入框里的原文。"""
        _html, body = self._body()
        self.assertIn('const taskId = AS_STATE.trackTaskId;', body)
        self.assertIn("'/v1/after-sale/track/' + encodeURIComponent(taskId) "
                      "+ '/export'", body)
        self.assertNotIn('asTrackBills', body)

    def test_export_without_track_task_asks_for_refresh_first(self):
        _html, body = self._body()
        self.assertIn("if (!taskId)", body)
        self.assertIn('刷新退款进度', body)

    def test_export_reuses_cloud_download_helpers(self):
        """下载与报错都走既有助手，不再自造一套（错误文案才不会漂移）。"""
        _html, body = self._body()
        self.assertIn("credentials:'same-origin'", body)
        self.assertIn('cloudApiError(payload, response.status)', body)
        self.assertIn('workspaceDownloadName(', body)
        self.assertIn("'X-Xynigo-Source':'cloud_web_workspace'", body)



class WebCloudContractAlignmentTests(unittest.TestCase):
    """Web 发出的报文与读取的字段，必须与云端契约对齐。

    这类错（前端读一个契约里没有的字段、或发的键云端不认）不会让任何一侧的
    单测失败——两边各自自洽，只有串跑才会暴露。实测抓到：④ 前端读
    `row.goodsImg`，而云端行契约里没有该字段 → 缩略图恒空。故常驻此检查。
    """

    CLOUD_CONTRACT = (LOCAL_HTML.parents[3] / 'cloud' / 'auth-service' / 'src'
                      / 'xynigo_auth' / 'operation_contract.py')

    def _contract_block(self, name):
        text = self.CLOUD_CONTRACT.read_text(encoding='utf-8')
        start = text.index(f'class {name}')
        rest = text[start:]
        nxt = rest.find('\n\nclass ')
        return rest[:nxt] if nxt > 0 else rest

    def test_web_payload_keys_match_cloud_track_item(self):
        """Web 的 items 键必须是云端 AfterSaleTrackItem 的子集，且必填项齐全。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        body = html[html.index('function asTrackItemsFromRows('):]
        body = body[:body.index('\n}')]
        keys = set(re.findall(r'(\w+):\s*(?:row|refund)\.\w+', body))
        fields = set(re.findall(r'^    (\w+):', self._contract_block(
            'AfterSaleTrackItem'), re.M))
        self.assertTrue(keys, '未解析到 Web 的 items 键')
        self.assertTrue(fields, '未解析到云端 AfterSaleTrackItem 字段')
        self.assertFalse(keys - fields,
                         f'Web 发了契约没定义的键：{sorted(keys - fields)}')
        for required in ('environmentSerial', 'orderNo', 'refundBillId'):
            self.assertIn(required, keys)

    def test_web_claim_items_carry_delivered_at_and_goods_img(self):
        """③ 的送达时间与商品图靠提交单从扫描清单带下来。

        这两个字段在云端契约里是可选的（default=""），漏发**不会报错**——
        只会静默变成空列（③ 上线当天送达时间恒「—」、商品图恒空就是这么来的）。
        所以这里不仅查「发的键是否是契约子集」，还点名要求这两个字段必须在。
        """
        html = LOCAL_HTML.read_text(encoding='utf-8')
        body = html[html.index('function asSelectedItems()'):]
        body = body[:body.index('\n}')]
        fields = set(re.findall(r'^    (\w+):', self._contract_block(
            'AfterSaleClaimItem'), re.M))
        keys = set(re.findall(r'^      (\w+):', body, re.M))
        self.assertTrue(keys, '未解析到 asSelectedItems 的键')
        self.assertFalse(keys - fields,
                         f'Web 发了契约没定义的键：{sorted(keys - fields)}')
        for required in ('environmentSerial', 'orderNo', 'deliveredAt', 'goodsImg', 'goodsImages', 'goodsItems', 'itemCount'):
            self.assertIn(required, keys)

    def test_web_reads_only_contracted_track_row_fields(self):
        """Web 渲染 ④ 时读到的每个行字段，云端行契约都必须提供。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        tpl = html[html.index('function asTrackPhaseLabel('):html.index('// 手工指定的回访单：')]
        reads = set(re.findall(r'row\.(\w+)', tpl))
        fields = set(re.findall(r'^    (\w+):', self._contract_block(
            'AfterSaleTrackRow'), re.M))
        self.assertTrue(reads and fields)
        missing = sorted(reads - fields)
        self.assertFalse(missing,
                         f'④ 读了云端契约没有的字段（会恒为空）：{missing}')

    def test_history_list_reads_only_fields_cloud_provides(self):
        """历史列表/详情读的批次字段，云端 _after_sale_claim_history_item 都必须给。

        这类错（Web 读一个云端没返回的字段）不会让任何一侧单测失败——两边各自
        自洽，只有打开历史弹层才看到空列。
        """
        html = LOCAL_HTML.read_text(encoding='utf-8')
        service = (self.CLOUD_CONTRACT.parent / 'operation_service.py').read_text(
            encoding='utf-8')
        block = service[service.index('def _after_sale_claim_history_item('):]
        block = block[:block.index('\n    def ')]
        keys = set(re.findall(r'"(\w+)":', block))
        self.assertTrue(keys, '未解析到云端批次字段')
        for signature, prefix in (('function asRenderClaimHistory(', 'item.'),
                                  ('function asRenderClaimHistoryDetail(', 'batch.')):
            body = html[html.index(signature):]
            body = body[:body.index('\n}\n')]
            reads = set(re.findall(prefix.replace('.', r'\.') + r'(\w+)', body))
            missing = sorted(reads - keys)
            self.assertFalse(missing,
                             f'{signature} 读了云端没给的字段（会恒为空）：{missing}')

    def test_web_environment_payload_matches_cloud_claim_body(self):
        """按环境直提的报文键必须是云端建单契约的子集，环境清单必发。

        直提与按单提交共用 asSubmitItems（只留一条建 Run 路径），只有范围
        字段与能力位分叉；键名漂移不会让任何一侧单测失败，只会表现为线上
        422（契约 extra=forbid）。
        """
        html = LOCAL_HTML.read_text(encoding='utf-8')
        body = html[html.index('async function asSubmitItems('):]
        body = body[:body.index('\n}\n')]
        self.assertIn('environmentSerials: envSerials', body)
        env_branch = body[body.index('? {executorId'):
                          body.index(': {executorId')]
        order_branch = body[body.index(': {executorId'):body.index('});')]
        keys = set(re.findall(r'(\w+):', env_branch + order_branch))
        fields = set(re.findall(r'^    (\w+):', self._contract_block(
            'AfterSaleClaimRunCreateBody'), re.M))
        self.assertTrue(keys, '未解析到建 Run 报文键')
        self.assertFalse(keys - fields,
                         f'建 Run 发了契约没定义的键：{sorted(keys - fields)}')
        self.assertIn('environmentSerials', set(re.findall(r'(\w+):', env_branch)))
        # 执行器选择必须按范围点名能力位：老执行器不具备直提能力、不能接直提任务
        self.assertIn("'after.sale.claim-environment.v1'", body)
        self.assertIn("'after.sale.claim.v1'", body)
        # 具名入口只做确认与解析，建 Run 仍回到 asSubmitItems
        wrapper = html[html.index('async function asSubmitByEnvironment('):]
        wrapper = wrapper[:wrapper.index('\n}\n')]
        self.assertIn('asSubmitItems([], {environmentSerials: serials', wrapper)

    def test_bridge_environment_rows_match_cloud_contract(self):
        """桥接的环境行投影字段，云端 AfterSaleClaimEnvironmentRow 必须都有。"""
        source = (ROOT / 'src' / 'purchase_tool'
                  / 'operation_executor.py').read_text(encoding='utf-8')
        block = source[source.index('_AFTER_SALE_ENV_ROW_FIELDS = ('):]
        block = block[:block.index(')')]
        fields = set(re.findall(r"'(\w+)'", block))
        declared = set(re.findall(r'^    (\w+):', self._contract_block(
            'AfterSaleClaimEnvironmentRow'), re.M))
        self.assertTrue(fields and declared, '未解析到环境行字段')
        missing = sorted(fields - declared)
        self.assertFalse(missing, f'桥接投影了契约没有的环境行字段：{missing}')
        self.assertIn('environmentSerial', fields)
        self.assertIn('submittedCount', fields)

    def test_web_reads_environment_results_declared_by_cloud_snapshot(self):
        """Web 读 data.environments / submitMode，云端快照都必须返回。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        service = (self.CLOUD_CONTRACT.parent / 'operation_service.py').read_text(
            encoding='utf-8')
        self.assertIn('asRenderEnvOutcomes(data.environments', html)
        self.assertIn('data?.submitMode', html)
        self.assertIn('"submitMode":', service)
        self.assertIn('"environments": (', service)

    def test_environment_results_survive_restore_and_history_detail(self):
        """环境级结果必须在「恢复批次」与「历史详情」两条路上都看得见。

        直提批次可能一行订单都没有（全 skip / 全未登录），只按 rows 判断恢复
        会让这批结果整个消失；历史详情只写 meta 计数也看不到各环境原因。
        """
        html = LOCAL_HTML.read_text(encoding='utf-8')
        restore = html[html.index('async function asLoadLatestClaim('):]
        restore = restore[:restore.index('\n}\n')]
        self.assertIn('data?.environments', restore)
        self.assertIn('!restoreEnvs.length', restore,
                      '恢复条件必须把环境级结果算进去')
        detail = html[html.index('function asRenderClaimHistoryDetail('):]
        detail = detail[:detail.index('\n}\n')]
        self.assertIn('asEnvOutcomePills(', detail)
        self.assertIn("$('asHistoryEnvOutcomes')", detail)
        self.assertIn('读取失败', detail)
        self.assertIn('已停止', detail)
        # 弹层里必须有承载节点，否则渲染无处可去
        self.assertIn('id="asHistoryEnvOutcomes"', html)
        # 共用一套 pill 渲染，避免两处文案漂移
        self.assertIn('function asEnvOutcomePills(', html)

    def test_environment_mode_flag_and_partial_failure_title(self):
        """复评未闭合项的两条护栏：横幅标题与进度单位不能只看「有没有值」。

        - 环境失败 + 订单成功时 run 是 partial_failure，横幅标题必须一致
          （不能因为 failedCount=0 就写「完成」）。
        - 进度单位/环境失败句必须看模式标志：运行中恢复时 environments 可能
          还没回传，用 envSerials.length 判断会退回「单」。
        """
        html = LOCAL_HTML.read_text(encoding='utf-8')
        poll = html[html.index('} else if (AS_STATE.mode === \'claim\''):]
        poll = poll[:poll.index('\n    }\n')]
        self.assertIn('partial_failure:head + \'完成（部分失败）\'', poll)
        self.assertIn("unit: AS_STATE.envMode ? '个环境' : '单'", poll)
        self.assertIn('AS_STATE.envMode ? (data.environments || []) : []', poll)
        submit = html[html.index('async function asSubmitItems('):]
        submit = submit[:submit.index('\n}\n')]
        self.assertIn('AS_STATE.envMode = !!envSerials;', submit)
        restore = html[html.index('async function asLoadLatestClaim('):]
        restore = restore[:restore.index('\n}\n')]
        self.assertIn('AS_STATE.envMode = envMode;', restore)

    def test_history_routes_match_web_urls(self):
        """历史列表/详情/导出的 URL 必须与云端路由对上（路径漂移只会是 404）。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        main = (self.CLOUD_CONTRACT.parent / 'main.py').read_text(
            encoding='utf-8')
        for route in ('@app.get("/v1/operation-runs/after-sale-claim/history")',
                      '@app.get("/v1/operation-runs/after-sale-claim/history/{run_id}")',
                      '@app.get("/v1/operation-runs/after-sale-claim/history/{run_id}/export")'):
            self.assertIn(route, main)
        self.assertIn("'/v1/operation-runs/after-sale-claim/history?'", html)
        self.assertIn("'/v1/operation-runs/after-sale-claim/history/'", html)
        # 注册顺序：/history 必须排在 /{run_id} 之前，否则被路径参数吃掉
        self.assertLess(
            main.index('@app.get("/v1/operation-runs/after-sale-claim/history")'),
            main.index('@app.get("/v1/operation-runs/after-sale-claim/{run_id}")'))

    def test_track_export_route_matches_web_url(self):
        """导出 URL 与云端路由必须同一条；路径漂移只会表现为线上 404。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        main = (self.CLOUD_CONTRACT.parent / 'main.py').read_text(
            encoding='utf-8')
        self.assertIn(
            '@app.get("/v1/after-sale/track/{task_id}/export")', main)
        self.assertIn(
            "'/v1/after-sale/track/' + encodeURIComponent(taskId) + '/export'",
            html)

    def test_track_export_columns_match_workbench_table(self):
        """导出列 = 工作台 ④ 表头，列序逐项相同（整列错位的防线）。"""
        html = LOCAL_HTML.read_text(encoding='utf-8')
        head = html[html.index('<table id="asTrackTable">'):]
        head = head[:head.index('</tr>')]
        columns = re.findall(r'<th[^>]*>([^<]+)</th>', head)
        source = (self.CLOUD_CONTRACT.parent / 'after_sale_export.py').read_text(
            encoding='utf-8')
        block = source[source.index('HEADERS = ('):]
        block = block[:block.index(')')]
        headers = re.findall(r'"([^"]+)"', block)
        self.assertTrue(columns and headers)
        self.assertEqual(columns, headers)
