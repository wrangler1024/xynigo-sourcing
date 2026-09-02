# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
import tempfile
import unittest

from purchase_tool.data_migration import (
    DataMigrationError,
    migrate_green_data,
)
from purchase_tool.instance_guard import acquire_executor_instance_guard


ROOT = Path(__file__).resolve().parents[1]


class MacOSDataMigrationTests(unittest.TestCase):
    def make_green_package(self, root):
        source = Path(root) / 'Xynigo-Sourcing'
        source.mkdir()
        (source / 'VERSION.json').write_text(
            '{"version":"0.12.7"}', encoding='utf-8')
        (source / 'config.json').write_text(
            '{"serverPort":6873}', encoding='utf-8')
        (source / '运行数据').mkdir()
        (source / '运行数据' / 'keep.txt').write_text(
            'green', encoding='utf-8')
        return source

    def test_migration_copies_only_whitelist_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_green_package(directory)
            (source / 'secret.env').write_text('never-copy', encoding='utf-8')
            target = Path(directory) / 'standard-data'
            target.mkdir()
            (target / 'config.json').write_text(
                '{"serverPort":9999}', encoding='utf-8')
            result = migrate_green_data(source, target)
            self.assertEqual(
                (target / 'config.json').read_text(encoding='utf-8'),
                '{"serverPort":9999}',
            )
            self.assertEqual(
                (target / '运行数据' / 'keep.txt').read_text(encoding='utf-8'),
                'green',
            )
            self.assertFalse((target / 'secret.env').exists())
            self.assertEqual(result['copied'], 1)
            self.assertEqual(result['skipped'], 1)

    def test_migration_rejects_non_package_and_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            invalid = Path(directory) / 'invalid'
            invalid.mkdir()
            with self.assertRaises(DataMigrationError):
                migrate_green_data(invalid, target)
            source = self.make_green_package(directory)
            linked = source / '运行数据' / 'linked.txt'
            try:
                linked.symlink_to(source / 'config.json')
            except (OSError, NotImplementedError):
                self.skipTest('symbolic links are not available')
            with self.assertRaises(DataMigrationError):
                migrate_green_data(source, target)

    def test_migration_rejects_symbolic_link_in_standard_data_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_green_package(directory)
            target = Path(directory) / 'target'
            outside = Path(directory) / 'outside'
            target.mkdir()
            outside.mkdir()
            try:
                (target / '运行数据').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symbolic links are not available')
            with self.assertRaises(DataMigrationError):
                migrate_green_data(source, target)
            self.assertFalse((outside / 'keep.txt').exists())


class MacOSSingleInstanceTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX file locking is not used on Windows')
    def test_posix_lock_allows_only_one_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'executor.lock'
            first = acquire_executor_instance_guard(lock_path=lock_path)
            second = acquire_executor_instance_guard(lock_path=lock_path)
            try:
                self.assertTrue(first.acquired)
                self.assertFalse(second.acquired)
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
            finally:
                first.close()
                second.close()
            third = acquire_executor_instance_guard(lock_path=lock_path)
            try:
                self.assertTrue(third.acquired)
            finally:
                third.close()


class MacOSStandardInstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = (ROOT / '组装macOS标准安装包.sh').read_text(
            encoding='utf-8')
        cls.launcher = (ROOT / 'packaging/macos/desktop_client.swift').read_text(
            encoding='utf-8')
        cls.main = (ROOT / 'src/purchase_tool/main.py').read_text(
            encoding='utf-8')
        cls.desktop_ui = (
            ROOT / 'src/purchase_tool/web/desktop.js'
        ).read_text(encoding='utf-8')
        cls.start_script = (
            ROOT / 'packaging/macos/启动本地执行器.command'
        ).read_text(encoding='utf-8')
        cls.protocol_script = (
            ROOT / 'packaging/macos/协议启动.command'
        ).read_text(encoding='utf-8')
        cls.migration_script = (
            ROOT / 'packaging/macos/迁移绿色包数据.command'
        ).read_text(encoding='utf-8')

    def test_app_and_pkg_have_stable_identity_and_no_autostart(self):
        self.assertIn('/Applications/Xynigo Sourcing.app', self.builder)
        self.assertIn('icu.samforo.xynigo.sourcing', self.builder)
        self.assertIn("'CFBundleURLSchemes': ['xynigo']", self.builder)
        self.assertIn("'LSMinimumSystemVersion': '13.0'", self.builder)
        self.assertIn("'NSAllowsLocalNetworking': True", self.builder)
        for forbidden in ('LaunchAgents', 'LaunchDaemons', 'LoginItems'):
            self.assertNotIn('mkdir -p "$PACKAGE_ROOT/Library/%s' % forbidden,
                             self.builder)
        self.assertIn("'autoStart': False", self.builder)
        self.assertIn('BundleIsRelocatable', self.builder)
        self.assertIn("find \"$APP\" -name '._*' -type f -delete", self.builder)
        self.assertIn('RUNTIME_ID="${VERSION}-${RUNTIME_REVISION}"',
                      self.builder)
        self.assertIn("'runtimeRevision': os.environ['RUNTIME_REVISION']",
                      self.builder)
        self.assertIn("'runtimeId': os.environ['RUNTIME_ID']", self.builder)
        self.assertIn('/usr/bin/ditto -x "$PAYLOAD_ARCHIVE"', self.builder)
        self.assertIn(
            "find \"$VERIFIED_APP\" -name '._*' -print -quit",
            self.builder)

    def test_app_icon_uses_the_shared_x_brand_asset(self):
        self.assertIn(
            'SOURCE_ICON="src/purchase_tool/web/xynigo-x.png"',
            self.builder,
        )
        self.assertNotIn(
            'SOURCE_ICON="src/purchase_tool/web/xynigo-favicon.png"',
            self.builder,
        )

    def test_launcher_only_accepts_low_risk_protocol_and_fixed_scripts(self):
        self.assertIn('^xynigo://', self.launcher)
        self.assertNotIn('xynigo://purchase', self.launcher.lower())
        self.assertIn('URLComponents(url: url', self.launcher)
        self.assertIn('desktopPairPattern', self.launcher)
        self.assertIn('pairField.stringValue = code', self.launcher)
        self.assertNotIn('/bin/bash -c', self.launcher)

    def test_launcher_has_visible_desktop_window_and_menu_bar_entry(self):
        for marker in (
                'NSWindow(',
                'Xynigo 本地执行器',
                'WKWebView(',
                'WKScriptMessageHandler',
                '/desktop/',
                'URLQueryItem(name: "platform", value: "mac")',
                'styleMask: [.titled, .closable, .miniaturizable, .resizable]',
                'application.setActivationPolicy(.regular)',
                'NSStatusBar.system.statusItem',
                '打开桌面客户端',
                '打开本机设置',
                '打开云端工作台',
                '启动执行器',
                '配对这台电脑',
                '检查更新',
                'view=localsettings',
                'xynigo://(?:start/?|wake/?|settings/?|pair',
                'configuredServerPort()',
                '127.0.0.1',
        ):
            self.assertIn(marker, self.launcher)
        self.assertIn('使用飞书授权登录', self.desktop_ui)
        self.assertIn('/api/auth/start', self.desktop_ui)

    def test_launcher_manages_executor_without_terminal(self):
        for marker in (
                'private var childProcess: Process?',
                'process.arguments = ["--no-browser"]',
                'XYNIGO_LAUNCHER_TOKEN',
                'launcher-token-v1',
                'executor-control/ping',
                'orphan_takeover_started',
                'terminateOwnedListener',
                '/usr/sbin/lsof',
                'update_request_rejected',
                'executor-status.json',
                'probe(port + 1, lastPort, completion)',
                'executor-control/shutdown',
                'status.schemaVersion == 1',
                '本地执行器.log',
                '[.posixPermissions: 0o600]',
        ):
            self.assertIn(marker, self.launcher)
        direct_launch = self.launcher[
            self.launcher.index('private func startManagedExecutor()'):
            self.launcher.index('private func executorEnvironment')
        ]
        self.assertNotIn('Terminal', direct_launch)

    def test_standard_client_supports_authenticated_online_update(self):
        for marker in (
                'process.terminationStatus == 42',
                'waitForSystemInstaller(',
                'pollInstalledRuntime(',
                'skip-update-once',
                'relaunchInstalledUpdate()',
                '/usr/bin/open',
                '等待系统安装器完成更新'):
            self.assertIn(marker, self.launcher)
        update_handler = self.launcher[
            self.launcher.index('@objc private func handleUpdate()'):
            self.launcher.index('private func postControl(')
        ]
        self.assertIn('beginSheetModal(for: hostWindow)', update_handler)
        self.assertIn('cancelButton.keyEquivalent = "\\u{1b}"',
                      update_handler)
        self.assertNotIn('runModal()', update_handler)
        self.assertIn("sys.platform == 'darwin'", self.main)
        self.assertIn("'onlineUpdate': True", self.builder)
        self.assertIn(
            "'onlineUpdateFlow': "
            "'authenticated_download_sha256_system_installer'",
            self.builder)
        self.assertIn("'updateAutoRelaunch': True", self.builder)

    def test_standard_scripts_pin_data_root_and_disable_green_updater(self):
        for script in (self.start_script, self.protocol_script,
                       self.migration_script):
            self.assertIn(
                '$HOME/Library/Application Support/XynigoSourcing', script)
            self.assertIn('XYNIGO_INSTALL_MODE=standard', script)
        self.assertIn('protocol "$XYNIGO_PROTOCOL_URI"', self.protocol_script)
        self.assertNotIn('echo "$XYNIGO_PROTOCOL_URI"', self.protocol_script)

    def test_first_launch_offers_safe_green_data_migration(self):
        self.assertIn('首次启动 Xynigo 标准版', self.launcher)
        self.assertIn('迁移绿色包数据.command', self.launcher)
        self.assertIn('choose folder', self.migration_script)
        self.assertIn(' migrate "$SOURCE_DIR"', self.migration_script)

    def test_builder_marks_artifact_as_not_release_eligible(self):
        self.assertIn("'appSignature': 'adhoc'", self.builder)
        self.assertIn("'developerIdApplicationSigned': False", self.builder)
        self.assertIn("'developerIdInstallerSigned': False", self.builder)
        self.assertIn("'notarized': False", self.builder)
        self.assertIn("'releaseEligible': False", self.builder)
        self.assertIn("'requiresElevation': True", self.builder)
        self.assertIn("'statusCenter': True", self.builder)
        self.assertIn("'menuBar': True", self.builder)
        self.assertIn("'dockIcon': True", self.builder)
        self.assertIn("'desktopUI': 'wkwebview'", self.builder)
        self.assertIn("'executorLaunchMode': 'managed_child'", self.builder)
        self.assertIn('packaging/macos/desktop_client.swift', self.builder)
        self.assertIn('/usr/bin/swiftc -parse-as-library', self.builder)
        self.assertIn('-framework AppKit -framework WebKit', self.builder)


class MacOSStandardInstallerArtifactTests(unittest.TestCase):
    def test_local_compiled_artifact_metadata_when_present(self):
        metadata = ROOT / 'dist/Xynigo_Sourcing_macOS_Standard_v0.13.13.json'
        if not metadata.is_file():
            self.skipTest('macOS standard installer is built in packaging CI')
        payload = json.loads(metadata.read_text(encoding='utf-8'))
        installer = ROOT / 'dist' / payload['assetName']
        self.assertTrue(installer.is_file())
        self.assertEqual(payload['platform'], 'macos-arm64')
        self.assertEqual(payload['installMode'], 'standard_system_application')
        self.assertTrue(payload['statusCenter'])
        self.assertTrue(payload['menuBar'])
        self.assertTrue(payload['dockIcon'])
        self.assertEqual(payload['executorLaunchMode'], 'managed_child')
        if 'onlineUpdate' in payload:
            self.assertTrue(payload['onlineUpdate'])
            self.assertTrue(payload['updateAutoRelaunch'])
        if 'desktopUI' in payload:
            self.assertEqual(payload['desktopUI'], 'wkwebview')
        self.assertTrue(payload['runtimeId'].startswith(
            payload['version'] + '-'))
        self.assertEqual(len(payload['runtimeRevision']), 12)
        self.assertTrue(payload['requiresElevation'])
        self.assertFalse(payload['autoStart'])
        self.assertFalse(payload['releaseEligible'])
        self.assertFalse(payload['notarized'])
        self.assertEqual(payload['protocol'], 'xynigo')
        self.assertEqual(len(payload['sha256']), 64)
        self.assertGreater(payload['size'], 1_000_000)


if __name__ == '__main__':
    unittest.main()
