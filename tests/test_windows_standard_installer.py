# -*- coding: utf-8 -*-
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import purchase_tool.main as main_module
from purchase_tool.executor_protocol import (
    ExecutorProtocolError,
    parse_executor_protocol_uri,
)
from purchase_tool.instance_guard import ExecutorInstanceGuard, WINDOWS_MUTEX_NAME
from purchase_tool.updater import UpdateCoordinator


ROOT = Path(__file__).resolve().parents[1]


class ExecutorProtocolTests(unittest.TestCase):
    def test_only_low_risk_actions_are_accepted(self):
        self.assertEqual(
            parse_executor_protocol_uri('xynigo://start'),
            {'action': 'start'},
        )
        self.assertEqual(
            parse_executor_protocol_uri('xynigo://wake/'),
            {'action': 'wake'},
        )
        self.assertEqual(
            parse_executor_protocol_uri('xynigo://pair?code=ABCD-EFGH'),
            {'action': 'pair', 'pairingCode': 'ABCD-EFGH'},
        )

    def test_business_actions_credentials_and_ambiguous_parameters_are_rejected(self):
        invalid = (
            'https://example.test/start',
            'xynigo://purchase?order=1',
            'xynigo://start?token=' + ('x' * 40),
            'xynigo://start?ticket=' + ('x' * 40),
            'xynigo://pair?code=ABCD-EFGH&code=JKLM-NPQR',
            'xynigo://pair?code=ABCI-1234',
            'xynigo://user:password@start',
            'xynigo://start:bad-port',
            'xynigo://start#fragment',
        )
        for uri in invalid:
            with self.subTest(uri=uri):
                with self.assertRaises(ExecutorProtocolError):
                    parse_executor_protocol_uri(uri)

    def test_single_instance_handle_is_closed_once(self):
        closed = []
        guard = ExecutorInstanceGuard(True, 42, closed.append)
        guard.close()
        guard.close()
        self.assertEqual(closed, [42])
        self.assertTrue(WINDOWS_MUTEX_NAME.startswith('Local\\'))


class WindowsStandardInstallerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installer = (
            ROOT / 'packaging/windows/xynigo-standard-installer.nsi'
        ).read_text(encoding='utf-8')
        cls.launcher = (
            ROOT / 'packaging/windows/Xynigo.cmd'
        ).read_text(encoding='utf-8')
        cls.pair_launcher = (
            ROOT / 'packaging/windows/配对本地执行器.cmd'
        ).read_text(encoding='utf-8')
        cls.builder = (
            ROOT / '组装Windows标准安装包.sh'
        ).read_text(encoding='utf-8')
        cls.green_builder = (
            ROOT / '组装Windows绿色包.sh'
        ).read_text(encoding='utf-8')
        cls.gui_launcher = (
            ROOT / 'packaging/windows/launcher/main.go'
        ).read_text(encoding='utf-8')
        cls.desktop_ui = (
            ROOT / 'src/purchase_tool/web/desktop.js'
        ).read_text(encoding='utf-8')
        cls.signer = (
            ROOT / 'packaging/windows/sign-windows-artifacts.ps1'
        ).read_text(encoding='utf-8')

    def test_installer_is_per_user_and_does_not_force_autostart(self):
        self.assertIn('RequestExecutionLevel user', self.installer)
        self.assertIn('InstallDir "$LOCALAPPDATA\\Programs\\${APP_NAME}"', self.installer)
        self.assertIn('WriteRegStr HKCU', self.installer)
        self.assertNotIn('WriteRegStr HKLM', self.installer)
        self.assertNotIn('CurrentVersion\\Run', self.installer)
        self.assertNotIn('RequestExecutionLevel admin', self.installer)

    def test_protocol_is_registered_only_as_a_validated_launcher(self):
        self.assertIn('Software\\Classes\\xynigo', self.installer)
        self.assertIn('"URL Protocol" ""', self.installer)
        self.assertIn('Xynigo.exe$\\" --protocol', self.installer)

    def test_versioned_runtime_and_uninstall_preserve_user_data(self):
        self.assertIn('$INSTDIR\\versions\\${APP_RUNTIME_ID}', self.installer)
        self.assertIn('FileWrite $0 "${APP_RUNTIME_ID}"', self.installer)
        self.assertNotIn('$INSTDIR\\versions\\${APP_VERSION}', self.installer)
        self.assertIn('current-version.txt', self.installer)
        self.assertNotIn('RMDir /r "$INSTDIR"', self.installer)
        for name in ('config.json', '查询日志', '运行数据', 'imports'):
            self.assertIn(name, self.installer)
            self.assertNotIn('Delete "$INSTDIR\\%s"' % name, self.installer)
            self.assertNotIn('RMDir /r "$INSTDIR\\%s"' % name, self.installer)
        self.assertIn('"/MIGRATEDIR="', self.installer)
        self.assertIn('MigrateGreenPackageData', self.installer)
        self.assertIn('robocopy.exe', self.installer)
        self.assertIn('/XO /XN /XC', self.installer)

    def test_launchers_pin_data_root_and_pairing_stays_explicit(self):
        self.assertIn('XYNIGO_DATA_DIR=%CD%', self.launcher)
        self.assertIn('XYNIGO_INSTALL_MODE=standard', self.launcher)
        self.assertIn('XYNIGO_RUNTIME_ID=%XYNIGO_ACTIVE_VERSION%', self.launcher)
        self.assertIn('current-version.txt', self.launcher)
        self.assertIn('run.py" %*', self.launcher)
        self.assertIn('set /p XYNIGO_PAIR_CODE=', self.pair_launcher)
        self.assertIn('Xynigo.cmd" pair', self.pair_launcher)

    def test_branded_installer_desktop_webview_and_tray_are_first_class(self):
        for source in (
            'MUI_WELCOMEFINISHPAGE_BITMAP',
            'MUI_HEADERIMAGE_BITMAP',
            'MigrationPageCreate',
            '迁移旧版数据',
            'Xynigo.exe',
            '启动 Xynigo 桌面客户端',
        ):
            self.assertIn(source, self.installer)
        self.assertIn(
            'Delete "$SMPROGRAMS\\${APP_NAME}\\本地执行器状态中心.lnk"',
            self.installer,
        )
        for source in (
            'Xynigo 本地执行器',
            'edge.NewChromium()',
            'browser.Embed(uintptr(app.webHost.Handle()))',
            '/desktop/',
            'platform=windows',
            'walk.NewNotifyIcon',
            '打开桌面客户端',
            '打开云端工作台',
            '打开本机设置',
            'view=localsettings',
            '配对这台电脑',
            '重新启动执行器',
            '检查更新',
            '更新到 v',
            'ProgressBar{AssignTo: &app.updateProgress',
            'DownloadPercent',
            '下载 %d%%',
            '验证设备身份',
            '建立安全通道',
            '秒后自动重试',
            '连接进度 1/3',
            '连接进度 2/3',
            '上次在线：',
            'case "verifying"',
            'case "installing"',
            '安装最新构建',
            '/executor-control/update/check',
            '/executor-control/update/install',
            '打开日志目录',
            '退出 Xynigo',
            '/executor-status.json',
        ):
            self.assertIn(source, self.gui_launcher)
        self.assertIn('Icon:       "xynigo-x.ico"', self.gui_launcher)
        self.assertIn('使用飞书授权登录', self.desktop_ui)
        self.assertIn('/api/auth/start', self.desktop_ui)
        self.assertIn('/api/local-config/data-sources', self.desktop_ui)
        self.assertIn("'onlineUpdate': True", self.builder)
        self.assertIn(
            "'onlineUpdateFlow': "
            "'authenticated_download_sha256_silent_installer'",
            self.builder)
        self.assertTrue((ROOT / 'packaging/windows/branding/'
                         'installer-welcome.bmp').is_file())
        self.assertTrue((ROOT / 'packaging/windows/branding/'
                         'installer-header.bmp').is_file())

    def test_pairing_is_single_flight_and_python_output_is_utf8(self):
        for source in (
            'pairButton     *walk.PushButton',
            'pairInFlight',
            'AssignTo: &app.pairButton',
            'case "pair-device":',
            'app.startPair(code)',
            'if app.pairInFlight || app.exiting',
            'app.pairButton.SetEnabled(false)',
            'defer app.finishPair()',
            'PYTHONUTF8=1',
            'PYTHONIOENCODING=utf-8',
        ):
            self.assertIn(source, self.gui_launcher)
        self.assertNotIn(
            'go app.performPair(app.pairEdit.Text())',
            self.gui_launcher,
        )

    def test_desktop_v3_is_webview2_full_width_and_state_driven(self):
        for source in (
            'Size:       Size{Width: 1360, Height: 790}',
            'MinSize:    Size{Width: 1080, Height: 650}',
            'browser.MessageCallback = app.handleWebMessage',
            'WebView2-v2',
            'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
            '--disable-gpu',
            'desktop-ready',
            'desktopReadyWait',
            'desktop_navigation_attempt',
            'Windows 原生兼容模式 · 后台执行器保持运行',
            'desktop_watchdog_native_fallback',
            'app.browser.Hide()',
            'app.pairPanel.SetVisible(true)',
            'desktopShowRequested bool',
            'app.desktopShowRequested = showAtStart',
            'desktop_show_deferred_until_ready',
            'revealStatusCenterIfRequested',
            '在浏览器中打开完整界面',
            'open-desktop-browser',
            'retry-desktop',
            'AssignTo: &app.pairPanel',
            '尚未配对 · 配对码 5 分钟内有效且只能使用一次',
            'app.setPairingVisible(false)',
            'app.setPairingVisible(true)',
            '● 本地执行器在线',
            '● 等待设备配对',
            '● 正在执行 %d 个任务',
            '云端：',
            'HubStudio：已连接',
            '打开桌面客户端',
        ):
            self.assertIn(source, self.gui_launcher)
        self.assertIn('Visible:    false,', self.gui_launcher)
        self.assertNotIn(
            'if showAtStart {\n\t\tapp.showStatusCenter()',
            self.gui_launcher,
        )
        self.assertIn('grid-template-columns:repeat(4', (
            ROOT / 'src/purchase_tool/web/desktop.css'
        ).read_text(encoding='utf-8'))

    def test_webview2_runtime_is_checked_before_install(self):
        self.assertIn(
            '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
            self.installer,
        )
        self.assertIn('WebView2Missing', self.installer)
        self.assertIn(
            'https://developer.microsoft.com/microsoft-edge/webview2/',
            self.installer,
        )

    def test_same_version_upgrade_replaces_launcher_and_preserves_user_data(self):
        launcher_output = (
            'File /oname=Xynigo.exe "${STANDARD_GUI_LAUNCHER}"'
        )
        self.assertIn('SetOutPath "$INSTDIR"', self.installer)
        self.assertIn('SetOverwrite on', self.installer)
        self.assertIn(launcher_output, self.installer)
        self.assertIn('SetOverwrite off', self.installer)
        self.assertIn("'APP_RUNTIME_ID': os.environ['RUNTIME_ID']", self.builder)
        self.assertIn('RUNTIME_ID="${VERSION}-${RUNTIME_REVISION}"', self.builder)
        self.assertIn("'runtimeId': os.environ['RUNTIME_ID']", self.builder)
        self.assertNotIn('Delete "$INSTDIR\\config.json"', self.installer)
        self.assertNotIn('RMDir /r "$INSTDIR\\运行数据"', self.installer)

    def test_standard_installer_has_verified_online_upgrade_handoff(self):
        self.assertIn('/ONLINEUPDATE=', self.installer)
        self.assertIn('Function .onInstSuccess', self.installer)
        self.assertIn('Exec \'"$INSTDIR\\Xynigo.exe" --show\'', self.installer)
        self.assertIn(
            'taskkill.exe" /IM Xynigo.exe /F', self.installer)
        self.assertIn('XYNIGO_RUNTIME_ID="+runtimeID', self.gui_launcher)

    def test_builder_marks_unsigned_artifact_as_not_release_eligible(self):
        self.assertIn("'authenticodeSigned': False", self.builder)
        self.assertIn("'releaseEligible': False", self.builder)
        self.assertIn("'installMode': 'standard_per_user'", self.builder)
        self.assertIn("'statusCenter': True", self.builder)
        self.assertIn("'trayMenu': True", self.builder)
        self.assertIn("'desktopUI': 'webview2'", self.builder)
        self.assertIn("'webViewRuntime': 'evergreen'", self.builder)
        self.assertIn("'launcherFile': 'Xynigo.exe'", self.builder)
        self.assertIn('makensis is required', self.builder)

    def test_green_package_has_portable_status_center_launcher(self):
        for source in (
            'packaging/windows/build-launcher.sh "$STAGE/Xynigo.exe"',
            "'launcherFile': 'Xynigo.exe'",
            "'statusCenter': True",
            "'trayMenu': True",
            "'installMode': 'green_package'",
            "'Xynigo.exe', 'xynigo-logo.png', 'xynigo-x.ico'",
            '双击“Xynigo.exe”打开桌面客户端',
            '绿色版不注册 xynigo:// 系统协议',
            "'desktopUI': 'webview2'",
            "'webViewRuntime': 'evergreen'",
            'XYNIGO_BUILD_LABEL',
        ):
            self.assertIn(source, self.green_builder)
        self.assertIn('resolveRuntime(app.root)', self.gui_launcher)
        self.assertIn('return root, "green", nil', self.gui_launcher)

    def test_real_signing_gate_requires_trust_and_timestamp_verification(self):
        for source in (
            'XYNIGO_WINDOWS_SIGNING_PFX_BASE64',
            '/fd SHA256',
            '/tr $timestampUrl',
            '/td SHA256',
            'verify /pa /all /v',
            'TimeStamperCertificate',
            'Status -ne "Valid"',
        ):
            self.assertIn(source, self.signer)
        self.assertNotIn('New-SelfSignedCertificate', self.signer)

    def test_standard_install_without_online_client_requires_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = UpdateCoordinator(
                directory,
                '0.12.10',
                environ={'XYNIGO_INSTALL_MODE': 'standard'},
            )
        snapshot = coordinator.snapshot()
        self.assertFalse(snapshot['enabled'])
        self.assertIn('覆盖安装一次', snapshot['message'])


class WindowsStandardInstallerArtifactTests(unittest.TestCase):
    def test_local_compiled_artifact_metadata_when_present(self):
        metadata = ROOT / 'dist/Xynigo_Sourcing_Windows_Setup_v0.13.19.json'
        if not metadata.is_file():
            self.skipTest('standard installer artifact is built in packaging CI')
        import json
        payload = json.loads(metadata.read_text(encoding='utf-8'))
        installer = ROOT / 'dist' / payload['assetName']
        self.assertTrue(installer.is_file())
        self.assertEqual(payload['installMode'], 'standard_per_user')
        self.assertTrue(payload['runtimeId'].startswith(payload['version'] + '-'))
        self.assertFalse(payload['requiresElevation'])
        self.assertFalse(payload['autoStart'])
        self.assertFalse(payload['releaseEligible'])
        self.assertEqual(payload['protocol'], 'xynigo')
        self.assertTrue(payload['statusCenter'])
        self.assertTrue(payload['trayMenu'])
        if 'onlineUpdate' in payload:
            self.assertTrue(payload['onlineUpdate'])
        self.assertEqual(payload['launcherFile'], 'Xynigo.exe')
        if 'desktopUI' in payload:
            self.assertEqual(payload['desktopUI'], 'webview2')
        self.assertEqual(len(payload['sha256']), 64)
        self.assertGreater(payload['size'], 1_000_000)


class LocalExecutorStatusContractTests(unittest.TestCase):
    def test_status_center_contract_is_safe_without_cloud_user_session(self):
        state = main_module.AppState.__new__(main_module.AppState)
        state.tasks = SimpleNamespace(snapshot=lambda: {
            'safeParallel': True,
            'tasks': [{
                'taskId': 'query-sensitive-id',
                'kind': 'query',
                'label': '订单物流查询',
                'state': 'running',
                'startedAt': '2026-09-03T06:30:00Z',
                'elapsedSec': 12,
                'resourceCount': 3,
            }],
        })
        state.updates = SimpleNamespace(snapshot=lambda: {
            'enabled': True,
            'state': 'available',
            'installMode': 'standard',
            'currentVersion': '0.12.10',
            'currentRuntimeId': '0.12.10-test',
            'latestVersion': '0.12.10',
            'latestRuntimeId': '0.12.10-test',
            'message': '发现可在线升级的新构建 v0.12.10',
            'stage': 'downloading',
            'downloadReceivedBytes': 5_000_000,
            'downloadTotalBytes': 10_000_000,
            'downloadPercent': 50,
            'downloadSpeedBytesPerSecond': 1_000_000,
            'downloadEtaSeconds': 5,
        })
        state.hub_status = lambda force=False: (True, 'raw hub response')
        with patch.object(
                main_module.ExecutorChannelStateStore, 'load', return_value={
                    'executorId': 'executor-internal-id',
                    'displayName': '采购电脑',
                    'platform': 'windows',
                    'architecture': 'x86_64',
                    'status': 'online',
                    'lastPollAt': '2026-08-27T00:00:00+00:00',
                    'lastErrorCode': '',
                    'connectionPhase': 'listening',
                    'connectionAttempt': 0,
                    'nextRetryAt': None,
                    'connectedAt': '2026-08-27T00:00:00+00:00',
                }):
            payload = state.local_executor_status()
        self.assertTrue(payload['executor']['running'])
        self.assertTrue(payload['executor']['paired'])
        self.assertTrue(payload['hubStudio']['connected'])
        self.assertEqual(payload['tasks']['activeCount'], 1)
        task = payload['tasks']['items'][0]
        self.assertEqual(task['label'], '订单物流查询')
        self.assertEqual(task['state'], 'running')
        self.assertEqual(task['startedAt'], '2026-09-03T06:30:00Z')
        self.assertEqual(task['elapsedSec'], 12)
        self.assertEqual(task['resourceCount'], 3)
        self.assertEqual(payload['update']['state'], 'available')
        self.assertEqual(payload['update']['latestVersion'], '0.12.10')
        self.assertEqual(payload['update']['stage'], 'downloading')
        self.assertEqual(payload['update']['downloadPercent'], 50)
        self.assertEqual(payload['update']['downloadTotalBytes'], 10_000_000)
        self.assertEqual(payload['cloudChannel']['phase'], 'listening')
        self.assertEqual(payload['cloudChannel']['attempt'], 0)
        self.assertEqual(
            payload['cloudChannel']['connectedAt'],
            '2026-08-27T00:00:00+00:00')
        encoded = __import__('json').dumps(payload, ensure_ascii=False)
        for forbidden in (
            'executor-internal-id', 'query-sensitive-id',
            'raw hub response', 'credential', 'password', 'cookie', 'token',
        ):
            self.assertNotIn(forbidden, encoded.casefold())


if __name__ == '__main__':
    unittest.main()
