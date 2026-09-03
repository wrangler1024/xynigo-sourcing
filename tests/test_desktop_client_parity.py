from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CLIENT = ROOT / 'packaging/windows/launcher/main.go'
MACOS_CLIENT = ROOT / 'packaging/macos/desktop_client.swift'
DESKTOP_HTML = ROOT / 'src/purchase_tool/web/desktop.html'
DESKTOP_CSS = ROOT / 'src/purchase_tool/web/desktop.css'
DESKTOP_JS = ROOT / 'src/purchase_tool/web/desktop.js'


class DesktopClientParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.windows = WINDOWS_CLIENT.read_text(encoding='utf-8')
        cls.macos = MACOS_CLIENT.read_text(encoding='utf-8')
        cls.desktop = '\n'.join(path.read_text(encoding='utf-8') for path in (
            DESKTOP_HTML, DESKTOP_CSS, DESKTOP_JS))

    def test_both_platforms_have_a_visible_primary_window(self):
        for marker in ('Xynigo 本地执行器', '打开桌面客户端'):
            self.assertIn(marker, self.windows)
            self.assertIn(marker, self.macos)
        self.assertIn('MainWindow{', self.windows)
        self.assertIn('NSWindow(', self.macos)
        self.assertIn('edge.NewChromium()', self.windows)
        self.assertIn('WKWebView(', self.macos)
        self.assertIn('Xynigo', self.desktop)
        self.assertIn('Local Executor', self.desktop)
        self.assertIn('showAtStart := command != "background"', self.windows)
        self.assertIn('app.desktopShowRequested = showAtStart', self.windows)
        self.assertIn('desktop_show_deferred_until_ready', self.windows)
        self.assertIn('showDesktopClient()', self.macos)
        self.assertIn('application.setActivationPolicy(.regular)', self.macos)
        self.assertNotIn('application.setActivationPolicy(.accessory)',
                         self.macos)

    def test_both_platforms_expose_the_same_operator_actions(self):
        native_actions = (
            '打开云端工作台',
            '本机设置',
            '重新启动执行器',
            '配对这台电脑',
            '检查更新',
            '打开日志目录',
            '退出 Xynigo',
        )
        for action in native_actions:
            self.assertIn(action, self.windows)
            self.assertIn(action, self.macos)
        for action in ('状态总览', '采购助手数据源', '诊断与维护',
                       '使用飞书授权登录', '本机任务明细'):
            self.assertIn(action, self.desktop)
        self.assertIn('task-details', self.desktop)

    def test_both_platforms_manage_the_executor_and_discover_ports(self):
        for source in (self.windows, self.macos):
            for marker in (
                    '--no-browser',
                    'XYNIGO_LAUNCHER_TOKEN',
                    'executor-status.json',
                    'executor-control/shutdown',
                    'view=localsettings',
                    '127.0.0.1',
                    '/desktop/',
            ):
                self.assertIn(marker, source)
        self.assertIn('for port := start; port < start+10; port++',
                      self.windows)
        self.assertIn('probe(port + 1, lastPort, completion)', self.macos)

    def test_close_keeps_client_available_in_tray_or_menu_bar(self):
        self.assertIn('app.mw.SetVisible(false)', self.windows)
        self.assertIn('walk.NewNotifyIcon', self.windows)
        self.assertIn('windowShouldClose', self.macos)
        self.assertIn('sender.orderOut(nil)', self.macos)
        self.assertIn('NSStatusBar.system.statusItem', self.macos)

    def test_both_platforms_publish_native_update_transitions_to_web_ui(self):
        for source in (self.windows, self.macos):
            self.assertIn('publishUpdateState', source)
            self.assertIn('setUpdateStatus', source)
            self.assertIn('更新请求已接受，页面将实时显示处理进度', source)
        self.assertIn('安装包已通过校验，请在系统“安装器”中确认安装。',
                      self.macos)


if __name__ == '__main__':
    unittest.main()
