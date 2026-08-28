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
            "'/api/buyer-library/import/'",
            "'/api/resources/stores'",
            "'/api/resources/proxies'",
        ):
            self.assertIn(marker, html)
        self.assertLess(
            html.index("if (isCloudWorkspaceRpcPath(path))"),
            html.index("return cloudLocalStub(path, opts);"),
        )

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
        load_groups = html[html.index("async function loadEnvGroups()"):]
        self.assertLess(
            load_groups.index("refreshAssignUi();"),
            load_groups.index("const [cfg, result] = await Promise.all"),
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
            "if (CLOUD_WEB_MODE && queryProgressLoaded && !isRunning) return;",
            "if (envPollInFlight) return;",
            "if (CLOUD_WEB_MODE && envProgressLoaded && !envRunning && !backupRunning) return;",
            "if (groupLoadInFlight) return groupLoadInFlight;",
            "error.code === 'executor_task_busy'",
            "filename:file.name, contentBase64, site:$('envSite').value",
            "站点已变更，请重新选择 xlsx",
            "mixedSiteCookieCount",
            "混合登录态（允许）",
            "将按当前选择的",
            "'/api/update/'",
            "CLOUD_WEB_MODE ? 900000 : 30000",
            "已确认在线升级；执行器正在后台下载、校验并自动重启",
            "需要最后一次覆盖安装在线升级引导版",
        ):
            self.assertIn(marker, html)
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


if __name__ == "__main__":
    unittest.main()
