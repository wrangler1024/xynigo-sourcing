from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CLIENT = ROOT / 'packaging/windows/launcher/main.go'
MACOS_CLIENT = ROOT / 'packaging/macos/desktop_client.swift'


class DesktopClientParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.windows = WINDOWS_CLIENT.read_text(encoding='utf-8')
        cls.macos = MACOS_CLIENT.read_text(encoding='utf-8')

    def test_both_platforms_have_a_visible_primary_window(self):
        for marker in (
                'Xynigo 桌面客户端',
                'XYNIGO DESKTOP',
                '打开桌面客户端',
        ):
            with self.subTest(platform='windows', marker=marker):
                self.assertIn(marker, self.windows)
            with self.subTest(platform='macos', marker=marker):
                self.assertIn(marker, self.macos)
        self.assertIn('MainWindow{', self.windows)
        self.assertIn('NSWindow(', self.macos)
        self.assertIn('showAtStart := command != "background"', self.windows)
        self.assertIn('showDesktopClient()', self.macos)
        self.assertIn('application.setActivationPolicy(.regular)', self.macos)
        self.assertNotIn('application.setActivationPolicy(.accessory)',
                         self.macos)

    def test_both_platforms_expose_the_same_operator_actions(self):
        actions = (
            '打开云端工作台',
            '本机设置',
            '重新启动执行器',
            '配对这台电脑',
            '检查更新',
            '打开日志目录',
            '退出 Xynigo',
        )
        for action in actions:
            with self.subTest(platform='windows', action=action):
                self.assertIn(action, self.windows)
            with self.subTest(platform='macos', action=action):
                self.assertIn(action, self.macos)

    def test_both_platforms_manage_the_executor_and_discover_ports(self):
        for source in (self.windows, self.macos):
            for marker in (
                    '--no-browser',
                    'XYNIGO_LAUNCHER_TOKEN',
                    'executor-status.json',
                    'executor-control/shutdown',
                    'view=localsettings',
                    '127.0.0.1',
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


if __name__ == '__main__':
    unittest.main()
