from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCAL_HTML = ROOT / "src/purchase_tool/web/index.html"
CLOUD_HTML = ROOT / "cloud/auth-service/src/xynigo_auth/web/index.html"


class ExecutorWorkspaceWebTests(unittest.TestCase):
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
            html.index("function inferQuerySite(groupName)")
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
        self.assertIn("taskId, 300000, null", handler)
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
        self.assertIn("执行器正在预检和准备", renderer)
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
        self.assertIn("terminalWithRows && review > 0", progress)
        self.assertIn(
            "`有效 ${valid} / ${total} · 待核对 ${review}`", progress)
        self.assertIn("query-outcome-review", progress)
        self.assertIn("查询已停止：${outcomeCopy}", renderer)
        self.assertIn("查询异常结束：${outcomeCopy}", renderer)
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
            "混合登录态（允许）",
            "将按当前选择的",
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
            html.index("function inferQuerySite(groupName)")
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
            'id="queryBrowserMode"',
            'id="queryAllowOpenEnvironment"',
            'id="queryAdvancedSettings"',
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
            "本次重查 ${retryCurrent} / ${retryTotal}",
            "elapsedSec:Number.isFinite(cumulativeDuration)",
            "attemptElapsedSec:Number.isFinite(attemptDuration)",
            "queryElapsedPrefix = snap.parentRunId ? '累计' : '已用';",
            "本次异常重查已用",
            "queryBackgroundRestoreNotified",
            "window.addEventListener('beforeunload'",
            "browserMode:$('queryBrowserMode').value",
            "allowOpenEnvironment:$('queryAllowOpenEnvironment').checked",
            '不会启动或关闭该环境，也不会关闭原有标签页',
            '输入序号时会在全部 HubStudio 环境中查找',
            "共 ${shippedRows.length} 个已发货订单",
            '首轨时效中位数',
            "'Esperando para enviarse': 'st-procesando'",
            "'Reembolsado': 'st-reembolsando'",
            "'Pagado': 'st-procesando'",
            '退款已处理，无物流',
        ):
            self.assertIn(marker, html)

    def test_logistics_advanced_settings_are_outside_site_and_group_column(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        side_column = html[
            html.index('<div class="side-col">'):
            html.index('<section class="card">', html.index('<div class="side-col">'))
        ]
        self.assertNotIn('id="queryAdvancedSettings"', side_column)
        action_row = html[
            html.index('<div class="action-row">', html.index('id="queryPhaseBanner"')):
            html.index('</section>', html.index('id="queryPhaseBanner"'))
        ]
        self.assertIn('id="queryAdvancedSettings"', action_row)

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
