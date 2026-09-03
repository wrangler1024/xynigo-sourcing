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
                '采购助手数据源', '诊断与维护', '数据源注册表',
                '采购员默认映射', 'HubStudio 环境映射'):
            self.assertIn(label, self.javascript)
        self.assertIn('grid-template-columns: .9fr 1.1fr', self.css)
        self.assertIn('flex: 0 0 224px', self.css)
        self.assertIn('grid-template-columns:repeat(4', self.css)

    def test_ui_uses_real_local_apis_without_exposing_launcher_token(self):
        for path in (
                '/executor-status.json', '/api/auth/status',
                '/api/auth/start', '/api/auth/poll', '/api/auth/logout',
                '/api/config', '/api/lark/config',
                '/api/local-config/data-sources'):
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

    def test_existing_data_sources_have_real_management_actions(self):
        for marker in (
                '查看详情', '认领为我的', '重新配置',
                '更换表格/工作表', '保存修改', '设为团队默认',
                '/api/local-config/data-sources/metadata',
                '/api/local-config/data-sources/replace',
                '/api/local-config/data-sources/revalidate',
                '/api/local-config/data-sources/claim-personal'):
            self.assertIn(marker, self.javascript)
        self.assertNotIn('已提交只读验证', self.javascript)

    def test_configured_secrets_render_only_masked_recognition_hints(self):
        for marker in (
                'targetMasked', 'worksheetMasked', 'hubApiKeyMasked',
                'appSecretMasked', '当前已配置', '仅显示不可逆掩码'):
            self.assertIn(marker, self.javascript)
        self.assertIn('.masked-config', self.css)
        self.assertNotIn("field('source-url','飞书普通电子表格链接','https://",
                         self.javascript)

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
