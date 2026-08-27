# -*- coding: utf-8 -*-
"""Static Web contracts for the cloud local-executor download entry."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "src" / "purchase_tool" / "web" / "index.html"


class LocalExecutorDownloadWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_system_navigation_and_global_status_entry_are_real_features(self):
        self.assertIn(
            'data-parent="system" data-module="localexecutor" '
            'data-cloud-only="1"',
            self.html,
        )
        self.assertLess(
            self.html.index('data-module="localexecutor"'),
            self.html.index('data-module="localsettings"'),
        )
        self.assertIn('id="localExecutorEntry"', self.html)
        self.assertRegex(
            self.html,
            r"localexecutor:\s*\{[\s\S]*?label: '本地执行器', primary: 'system'",
        )
        self.assertIn(
            "$('localExecutorEntry').onclick = () => openFeatureTab('localexecutor')",
            self.html,
        )

    def test_download_panel_uses_cloud_release_catalog_and_platform_choice(self):
        for element_id in (
            "localExecutorPanel",
            "localExecutorPlatform",
            "localExecutorReleaseVersion",
            "localExecutorAssetName",
            "localExecutorAssetSize",
            "localExecutorInstallMode",
            "localExecutorSignature",
            "localExecutorAssetSha",
            "localExecutorDownload",
            "localExecutorInstallGuide",
            "btnLocalExecutorRefreshRelease",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn(
            "return cloudFetchJson('/v1/local-executor/releases/latest', opts)",
            self.html,
        )
        self.assertIn("function detectLocalExecutorPlatform()", self.html)
        self.assertIn("'windows-x86_64'", self.html)
        self.assertIn("'macos-arm64'", self.html)
        self.assertIn(
            "download.href = downloadAllowed ? String(asset.downloadUrl) : '#'",
            self.html,
        )

    def test_standard_installers_require_platform_signatures_and_truthful_launchers(self):
        for text in (
            '标准安装包 · 原生启动器',
            '标准安装包 · 状态中心与托盘',
            '下载标准安装包',
            '未签名 · 禁止团队分发',
            'Developer ID Installer 签名、Apple 公证和 stapling 校验',
            'Windows 标准安装包当前不可用，且发布清单没有可用绿色版兜底资产',
            '后台执行器驻留系统托盘，不再依赖黑色命令窗口',
            '应用会打开 Terminal 运行本地执行器',
        ):
            self.assertIn(text, self.html)
        self.assertIn('info.developerIdInstallerSigned', self.html)
        self.assertIn('info.notarized', self.html)
        self.assertIn('info.stapled', self.html)
        self.assertIn('trustedStandardInstaller', self.html)

    def test_standard_download_has_a_green_xynigo_exe_fallback(self):
        for source in (
            'greenFallback',
            'resolveLocalExecutorDownloadAsset',
            'localExecutorUsingGreenFallback',
            '标准安装包当前不可用，已自动切换',
            '下载绿色版执行器（Xynigo.exe）',
            '绿色版 · Xynigo.exe 状态中心',
            'Xynigo.exe --pair',
            '双击“Xynigo.exe”打开品牌状态中心',
        ):
            self.assertIn(source, self.html)
        self.assertIn('info.authenticodeTimestamped', self.html)
        self.assertIn('info.publisher', self.html)

    def test_cloud_install_flow_keeps_installation_explicit_and_uses_real_heartbeat(self):
        for text in (
            "用户确认后下载 · 不静默安装",
            "下载不等于安装成功",
            "当前页面不会猜测本机状态",
            "设备状态来自真实心跳",
            "主动长轮询",
            "不会自动运行程序",
            "Gatekeeper、SmartScreen",
        ):
            self.assertIn(text, self.html)
        self.assertIn("$('updateCheck').hidden = CLOUD_WEB_MODE", self.html)
        self.assertIn(
            "$('localExecutorEntry').hidden = !CLOUD_WEB_MODE || "
            "!hasFeatureAccess('localexecutor')",
            self.html,
        )
        self.assertNotRegex(
            self.html,
            r"localExecutorDownload[^\n]{0,300}(?:已安装|已连接)",
        )

    def test_p1_device_pairing_and_config_controls_call_cloud_apis(self):
        for element_id in (
            "localExecutorDeviceList",
            "btnLocalExecutorPair",
            "btnLocalExecutorLaunch",
            "btnLocalExecutorOpenPair",
            "localExecutorPairingCode",
            "localExecutorPairingStatus",
            "btnLocalExecutorRevoke",
            "btnLocalExecutorReadConfig",
            "btnLocalExecutorSaveConfig",
            "executorConfigSafeParallel",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        for endpoint in (
            "/v1/executors",
            "/v1/executors/pairing-codes",
            "/v1/executor-tasks/",
        ):
            self.assertIn(endpoint, self.html)
        self.assertIn("expectedRevision:localExecutorConfigRevision", self.html)
        self.assertIn("config_revision_conflict", self.html)
        self.assertIn("window.location.href = 'xynigo://start'", self.html)
        self.assertIn(
            'window.location.href = `xynigo://pair?code=${encodeURIComponent(code)}`',
            self.html,
        )
        self.assertIn('payload.pairingRequestId', self.html)
        self.assertIn('/api/local-executor/pairing-codes/${localExecutorPairingRequestId}', self.html)
        self.assertIn('在 Mac 上打开 Xynigo 并配对', self.html)
        self.assertIn('已配对并收到真实心跳', self.html)
        self.assertIn('长期设备凭证不会进入此链接', self.html)
        self.assertIn("网页不会通过协议直接执行采购任务", self.html)
        self.assertIn("代理提取链接、Cookie、账号密码和飞书凭证不会", self.html)
        self.assertIn('data-module="localsettings" data-local-only="1"', self.html)


if __name__ == "__main__":
    unittest.main()
