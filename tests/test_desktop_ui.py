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
