from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import urllib.request

import purchase_tool.main as main_module


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'src/purchase_tool/web'


class DesktopUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (WEB / 'desktop.html').read_text(encoding='utf-8')
        cls.css = (WEB / 'desktop.css').read_text(encoding='utf-8')
        cls.javascript = (WEB / 'desktop.js').read_text(encoding='utf-8')

    def test_ui_matches_reviewed_prototype_information_architecture(self):
        for label in (
            '使用飞书授权登录', '状态总览', '本机设置',
                '采购助手数据源', '诊断与维护', '当前设备数据源',
                '采购员默认映射', '本机环境映射'):
            self.assertIn(label, self.javascript)
        self.assertIn('grid-template-columns: .9fr 1.1fr', self.css)
        self.assertIn('flex: 0 0 224px', self.css)
        self.assertIn('grid-template-columns:repeat(4', self.css)

    def test_ui_uses_real_local_apis_without_exposing_launcher_token(self):
        for path in (
                '/executor-status.json', '/api/auth/status',
                '/api/auth/start', '/api/auth/poll', '/api/auth/logout',
                '/api/config',
                '/api/local-config/data-sources',
                '/api/local-config/data-sources/organization-sync',
                '/api/hub-cache/preflight'):
            self.assertIn(path, self.javascript)
        rendered = self.html + self.css + self.javascript
        self.assertNotIn('XYNIGO_LAUNCHER_TOKEN', rendered)
        self.assertNotIn('X-Xynigo-Launcher', rendered)
        self.assertIn("sourceURL.host == \"127.0.0.1\"", (
            ROOT / 'packaging/macos/desktop_client.swift'
        ).read_text(encoding='utf-8'))

    def test_preview_samples_are_never_used_as_production_fallback(self):
        self.assertIn(
            'state.status || (previewRole ? sampleStatus : emptyStatus)',
            self.javascript,
        )
        self.assertIn(
            'state.sources || (previewRole ? sampleSources : emptySources)',
            self.javascript,
        )

    def test_cloud_channel_failures_are_not_shown_as_endless_connecting(self):
        self.assertIn('function cloudPresentation(channel, paired)',
                      self.javascript)
        self.assertIn("validation_failed: '客户端与云端协议不兼容'",
                      self.javascript)
        self.assertIn("auth_failed: '云端认证请求失败'",
                      self.javascript)

    def test_hub_status_separates_local_api_from_automation_capability(self):
        for marker in (
                'function hubPresentation(hub, port)',
                'localApiConnected', 'automationAvailable',
                "value:'Local API 已连接'",
                'HubStudio 自动化能力受限',
                '云端通道和 Local API 已连接；请处理下方 HubStudio 自动化能力异常'):
            self.assertIn(marker, self.javascript)
        self.assertNotIn(
            "hubReady ? 'Local API 正常' : 'Local API 未连接'",
            self.javascript,
        )
        mac_client = (
            ROOT / 'packaging/macos/desktop_client.swift'
        ).read_text(encoding='utf-8')
        for marker in (
                'localApiConnected', 'automationAvailable',
                '已连接 · 能力受限',
                'HubStudio Local API 已连接；自动化能力需要处理'):
            self.assertIn(marker, mac_client)

    def test_existing_data_sources_have_real_management_actions(self):
        for marker in (
                '查看详情', '认领为我的', '重新配置',
                '更换表格/工作表', '保存修改', '设为团队默认',
                '调整默认数据源', 'save-buyer-default:',
                '我的默认数据源', '已改用团队默认数据源',
                '团队默认是兜底策略', '改用团队默认',
                '个人速填表不可跨采购员共享',
                '已有个人默认的采购员不会自动切换',
                '为指定采购员新建个人速填表',
                'id="source-owner"', '归属采购员',
                '同一张个人表不能分配给多人',
                'memberId:ownerMemberId',
                '/api/local-config/data-sources/metadata',
                '/api/local-config/data-sources/replace',
                '/api/local-config/data-sources/revalidate',
                '/api/local-config/data-sources/claim-personal',
                '/api/local-config/data-sources/buyer-default',
                '/api/local-config/data-sources/buyer-default/clear'):
            self.assertIn(marker, self.javascript)
        self.assertNotIn('已提交只读验证', self.javascript)
        self.assertIn('.buyer-default-control', self.css)

    def test_data_source_hybrid_sync_keeps_environment_bindings_local(self):
        rendered = self.javascript + self.css
        for marker in (
                'sourceSyncPanel', '当前设备已与组织配置同步',
                '同步组织配置', '发布本机更改', '建立组织配置',
                'organization-sync/pull', 'organization-sync/publish',
                'expectedOrganizationRevision',
                '数据源定义和采购员默认由组织加密同步',
                'containerCode 环境映射仅保存在',
                'renderSourceSyncConfirmation', '同步范围已隔离'):
            self.assertIn(marker, rendered)
        self.assertIn('.source-sync-panel', self.css)

    def test_configured_secrets_render_only_masked_recognition_hints(self):
        for marker in (
                'targetMasked', 'worksheetMasked', 'hubApiKeyMasked',
                '当前已配置', '仅显示不可逆掩码'):
            self.assertIn(marker, self.javascript)
        self.assertIn('.masked-config', self.css)
        self.assertNotIn("field('source-url','飞书普通电子表格链接','https://",
                         self.javascript)

    def test_enterprise_feishu_secret_is_never_rendered_or_configured_locally(self):
        self.assertNotIn('cfg-lark-id', self.javascript)
        self.assertNotIn('cfg-lark-secret', self.javascript)
        self.assertNotIn("post('/api/lark/config'", self.javascript)
        self.assertIn('本机不保存 App ID 或 App Secret', self.javascript)
        self.assertIn('cloudIntegrationVisible', self.javascript)

    def test_update_panel_renders_live_cross_platform_progress(self):
        for marker in (
                'updatePresentation', 'updatePanel', 'update-progress-track',
                'downloadReceivedBytes', 'downloadSpeedBytesPerSecond',
                'downloadEtaSeconds', '等待系统安装器',
                'Windows 静默安装器', 'setUpdateStatus',
                "state.view === 'diagnostics'", 'scheduleStatusRefresh(250)'):
            self.assertIn(marker, self.javascript + self.css)
        self.assertIn(
            "updateBusy(currentStatus().update) ? 800 : 5000",
            self.javascript,
        )

    def test_hub_cache_cleanup_has_live_modal_and_background_progress(self):
        rendered = self.javascript + self.css
        for marker in (
                'renderHubCacheProgressModal', 'hubCacheProgressBar',
                '缓存清理正在后台进行', '查看实时进度', '转到后台',
                'cache-progress-background', 'cache-progress-rescan',
                'role="progressbar"', 'aria-valuenow',
                'hub-cache-progress-track', 'hub-cache-spinner',
                'renderHubCacheScanModal', '正在检测 HubStudio 缓存',
                '已检测 ', 'cache-scan-background',
                'open-hub-cache-scan-progress',
                'hubCacheEtaLabel', '预计还需约 ',
                '正在估算剩余时间', '剩余时间暂无法准确估算',
                'hubCacheGlobalTaskNotice', 'global-task-notice',
                '请先完全退出 HubStudio', '我已退出，重新检查',
                'cache-preflight-retry', '请勿启动 HubStudio 或退出 Xynigo'):
            self.assertIn(marker, rendered)
        self.assertIn('if (state.cacheProgressOpen) renderHubCacheProgressModal()',
                      self.javascript)

    def test_local_task_card_opens_redacted_live_details(self):
        rendered = self.javascript + self.css
        for marker in (
                'task-details', 'renderTaskDetailsModal',
                'task-detail-grid', 'startedAt', 'resourceCount',
                '任务明细仅包含类型、时间、状态和资源数量',
                '不包含订单号、账号或凭证'):
            self.assertIn(marker, rendered)
        self.assertIn('aria-label="', self.javascript)
        self.assertIn("state.taskDetailsOpen = false", self.javascript)

    def test_logistics_advanced_settings_are_configured_on_the_executor(self):
        self.assertIn('物流查询浏览器模式', self.javascript)
        self.assertIn('id="cfg-query-browser-mode"', self.javascript)
        self.assertIn('允许连接已打开环境（只读）', self.javascript)
        self.assertIn('id="cfg-query-allow-open"', self.javascript)
        self.assertIn("queryBrowserMode:document.getElementById('cfg-query-browser-mode').value", self.javascript)
        self.assertIn("queryAllowOpenEnvironment:document.getElementById('cfg-query-allow-open').checked", self.javascript)
        self.assertIn('网页不再临时覆盖', self.javascript)

    def test_hub_core_repair_requires_confirmation_and_shows_audit_state(self):
        rendered = self.javascript + self.css
        for marker in (
                'coreRepairPanel', 'repair-hub-core',
                'confirm-hub-core-repair', '/api/hub-core-repair/start',
                '下载并修复 HubStudio 内核', '下载后自动验证',
                '全程写入脱敏审计日志', 'auditState',
                '.core-repair-panel'):
            self.assertIn(marker, rendered)
        self.assertIn('不会从第三方地址下载安装程序', self.javascript)

    def test_windows_launcher_receives_desktop_ready_handshake(self):
        self.assertIn(
            "nativeAction('desktop-ready',{platform:platform,"
            "path:location.pathname,origin:location.origin})",
            self.javascript,
        )


class DesktopUIRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(
            ('127.0.0.1', 0), main_module.Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def fetch(self, path):
        url = 'http://127.0.0.1:%d%s' % (
            self.server.server_address[1], path)
        with urllib.request.urlopen(url, timeout=3) as response:
            return (
                response.status,
                response.headers.get_content_type(),
                response.read().decode('utf-8'),
            )

    def test_desktop_entry_and_assets_are_served_locally(self):
        status, mime, html = self.fetch('/desktop/?platform=mac')
        self.assertEqual((status, mime), (200, 'text/html'))
        self.assertIn('/desktop.css', html)
        self.assertIn('/desktop.js', html)
        self.assertEqual(self.fetch('/desktop.css')[:2], (200, 'text/css'))
        self.assertEqual(
            self.fetch('/desktop.js')[:2],
            (200, 'text/javascript'),
        )


if __name__ == '__main__':
    unittest.main()
