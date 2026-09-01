from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOCAL_HTML = ROOT / "src/purchase_tool/web/index.html"
CLOUD_HTML = ROOT / "cloud/auth-service/src/xynigo_auth/web/index.html"


class ExecutorWorkspaceWebTests(unittest.TestCase):
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

        self.assertIn("payload.cachedResult", html)
        self.assertIn("只读配置快照", html)
        self.assertIn("云端正在内存解析并生成加密短期计划", html)
        self.assertIn("云端只短时保存加密执行计划", html)
        self.assertNotIn("coreVersion=148", html)

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
        self.assertIn("downloadWorkspaceResource('/api/export?format=xlsx'", html)
        self.assertIn("workspaceImageUrl(path)", html)
        self.assertNotIn("location.href = '/api/export?format=xlsx'", html)
        self.assertNotIn("image.src = '/api/screenshot?serial='", html)

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

    def test_environment_safe_stop_is_visible_and_uses_mode_specific_route(self):
        html = LOCAL_HTML.read_text(encoding="utf-8")
        for marker in (
            'id="btnEnvStop"',
            "let envStopSubmitting = false;",
            "let envStopRequested = false;",
            "'/api/envbatch/stop'",
            "'/api/envbatch/backup/stop'",
            "安全停止不会强行中断",
            "未开始行已保留",
            "r.state === 'stopped'",
        ):
            self.assertIn(marker, html)

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


if __name__ == "__main__":
    unittest.main()
