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
            "localExecutorGreenDownload",
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
            "const defaultPlatform = release.platforms['windows-x86_64']",
            self.html,
        )
        self.assertIn('团队默认显示 ${selectedLabel}', self.html)
        self.assertIn('Mac 用户可手动切换', self.html)
        self.assertIn(
            "download.href = downloadAllowed ? String(asset.downloadUrl) : '#'",
            self.html,
        )
        self.assertIn(
            "trustedStandardInstaller || internalUnsignedTest",
            self.html,
        )
        self.assertIn(
            "download.download = downloadAllowed ? String(asset.assetName || '') : ''",
            self.html,
        )
        download_markup = re.search(
            r'<a class="btn primary local-executor-download"[^>]+>',
            self.html,
        )
        self.assertIsNotNone(download_markup)
        self.assertIn(" download", download_markup.group(0))
        self.assertNotIn("target=", download_markup.group(0))
        green_markup = re.search(
            r'<a class="btn local-executor-download local-executor-green-download"[^>]+>',
            self.html,
        )
        self.assertIsNotNone(green_markup)
        self.assertIn(" download", green_markup.group(0))
        self.assertNotIn("target=", green_markup.group(0))

    def test_standard_installers_require_platform_signatures_and_truthful_launchers(self):
        for text in (
            '标准安装包 · 原生启动器',
            '标准安装包 · 状态中心与托盘',
            '下载标准安装包',
            '未签名 · 禁止团队分发',
            'Developer ID Installer 签名、Apple 公证和 stapling 校验',
            'Windows 标准安装包当前不可用，且发布清单没有可用绿色版兜底资产',
        ):
            self.assertIn(text, self.html)
        self.assertIn('info.developerIdInstallerSigned', self.html)
        self.assertIn('info.notarized', self.html)
        self.assertIn('info.stapled', self.html)
        self.assertIn('trustedStandardInstaller', self.html)
        self.assertIn('internalUnsignedTest', self.html)
        self.assertIn('内部未签名测试包 · 需手动确认系统安全提示', self.html)
        self.assertIn('下载前请核对 SHA-256', self.html)
        self.assertIn('已通过系统入口开始下载内部测试安装包', self.html)

    def test_standard_download_has_a_green_xynigo_exe_fallback(self):
        for source in (
            'greenFallback',
            'resolveLocalExecutorDownloadAsset',
            'localExecutorUsingGreenFallback',
            'localExecutorGreenDownloadAsset',
            '标准安装包当前不可用，已自动切换',
            '下载绿色版执行器（Xynigo.exe）',
            '下载绿色包（兜底）',
            '绿色版 · Xynigo.exe 状态中心',
            'Xynigo.exe --pair',
        ):
            self.assertIn(source, self.html)
        self.assertIn('info.authenticodeTimestamped', self.html)
        self.assertIn('info.publisher', self.html)
        self.assertIn(
            "greenDownload.href = greenDownloadAllowed ? String(greenAsset.downloadUrl) : '#'",
            self.html,
        )
        self.assertIn(
            'greenDownload.hidden = !greenDownloadAllowed || selection.usingGreenFallback',
            self.html,
        )
        self.assertIn('SHA-256：${greenAsset.sha256}', self.html)

    def test_cloud_install_flow_keeps_installation_explicit_and_uses_real_heartbeat(self):
        for text in (
            "用户确认后下载 · 不静默安装",
            "当前页面不会猜测本机状态",
            "HTTPS 长轮询",
            "不会自动运行程序",
            "Gatekeeper、SmartScreen",
        ):
            self.assertIn(text, self.html)
        self.assertNotIn('id="updateCheck"', self.html)
        self.assertIn(
            "$('localExecutorEntry').hidden = !CLOUD_WEB_MODE || "
            "!hasFeatureAccess('localexecutor')",
            self.html,
        )
        self.assertNotRegex(
            self.html,
            r"localExecutorDownload[^\n]{0,300}(?:已安装|已连接)",
        )

    def test_p1_device_pairing_and_summary_controls_call_cloud_apis(self):
        for element_id in (
            "localExecutorDeviceList",
            "btnLocalExecutorPair",
            "btnLocalExecutorLaunch",
            "btnLocalExecutorOpenPair",
            "localExecutorPairingCode",
            "localExecutorPairingStatus",
            "btnLocalExecutorRevoke",
            "btnLocalExecutorRefreshSummary",
            "btnLocalExecutorOpenSettings",
            "executorSummarySafeParallel",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.html)
        for endpoint in (
            "/v1/executors",
            "/v1/executors/pairing-codes",
            "/runtime-summary",
        ):
            self.assertIn(endpoint, self.html)
        self.assertNotIn("expectedRevision:localExecutorConfigRevision", self.html)
        self.assertNotIn('id="btnLocalExecutorSaveConfig"', self.html)
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
        self.assertIn("云端仅展示执行器主动上报的严格摘要", self.html)
        self.assertIn('data-module="localsettings" data-local-only="1"', self.html)

    def test_cloud_executor_config_is_strict_read_only_summary(self):
        card_start = self.html.index('<div class="card-title">设备配置摘要')
        card_end = self.html.index(
            '<div class="local-executor-config-actions">', card_start)
        card = self.html[card_start:card_end]
        payload_start = self.html.index('function applyLocalExecutorConfigSummary')
        payload_end = self.html.index(
            'function updateLocalExecutorSummary()', payload_start)
        payload = self.html[payload_start:payload_end]
        for required in (
            'executorSummaryHubPort',
            'executorSummaryConcurrency',
            'executorSummaryEnvWorkers',
            'executorSummaryVerifyCount',
            'executorSummarySafeParallel',
            'executorSummaryIntegrations',
            'executorSummaryDataSources',
        ):
            self.assertIn(required, card)
            self.assertIn(required, payload)
        for legacy in (
            'executorConfigPurchaseSite',
            'executorConfigMxTag',
            'executorConfigUsTag',
            'executorConfigBuyerPlan',
            'purchaseSite',
            'purchaseTags',
            'importBuyerPlan',
        ):
            self.assertNotIn(legacy, card)
            self.assertNotIn(legacy, payload)
        self.assertIn('所有修改都在 Windows/macOS 桌面客户端完成', card)
        self.assertIn('摘要不可用', card)
        self.assertIn(
            'grid-template-columns: minmax(112px,1.3fr) '
            'repeat(3,minmax(76px,1fr)) minmax(108px,1.2fr)', self.html)
        self.assertIn(
            'gap: 16px; align-items: stretch', self.html)
        self.assertNotIn('<input', card)
        self.assertNotIn('<select', card)
        self.assertNotIn('localExecutorInstallGuide', self.html)
        self.assertNotIn('安装与连接说明', self.html)
        actions_start = self.html.index(
            '<div class="local-executor-config-actions">', card_start)
        actions_end = self.html.index('</div>', actions_start)
        self.assertIn(
            'id="localExecutorConfigState"',
            self.html[actions_start:actions_end],
        )

    def test_executor_summary_handles_stale_and_legacy_devices(self):
        for source in (
            'config.summary.v2',
            'payload.configSummaryStale',
            '摘要不可用：所选设备版本过旧',
            '不能把缺少摘要误判为未配置',
            '修改配置请在对应电脑的 Xynigo 桌面客户端完成',
        ):
            self.assertIn(source, self.html)
        for removed in (
            'data-executor-number-step',
            'validateLocalExecutorConfigInputs',
            'updateLocalExecutorConfigDirtyState',
            'startLocalExecutorConfigTask',
        ):
            self.assertNotIn(removed, self.html)

    def test_topbar_hub_status_uses_cloud_executor_heartbeat(self):
        self.assertIn('function renderCloudExecutorHubStatus()', self.html)
        self.assertIn("item.connectivity === 'online'", self.html)
        self.assertIn("device.hubStatus === 'ready'", self.html)
        self.assertIn("pill.textContent = 'HubStudio 已连接'", self.html)
        self.assertNotIn('🟢 HubStudio 已连接', self.html)
        self.assertIn('🟠 执行器在线 · HubStudio 未连接', self.html)
        self.assertIn('☁ 本地执行器均离线', self.html)
        self.assertNotIn('☁ 本机执行器离线', self.html)

    def test_topbar_executor_status_refreshes_outside_executor_page(self):
        self.assertIn(
            "if (authReady && CLOUD_WEB_MODE && hasFeatureAccess('localexecutor'))",
            self.html,
        )
        self.assertIn(
            "if (!CLOUD_WEB_MODE || !authReady || localExecutorDeviceLoading) return;",
            self.html,
        )
        self.assertNotIn(
            "activeModule === 'localexecutor' && authReady && CLOUD_WEB_MODE",
            self.html,
        )

    def test_topbar_executor_status_does_not_expose_device_count(self):
        self.assertIn("? '本地执行器在线'", self.html)
        self.assertIn("activeDevices.length ? '本地执行器离线'", self.html)
        self.assertIn(": '本地执行器待连接'", self.html)
        self.assertNotIn(
            "? `本地执行器 · ${onlineDevices.length} 台在线`",
            self.html,
        )

    def test_online_executor_badge_matches_connected_pill_style(self):
        match = re.search(
            r"\.local-executor-entry\.online\s*\{([^}]+)\}", self.html
        )
        self.assertIsNotNone(match)
        style = match.group(1)
        for declaration in (
            "padding: 8px 11px",
            "border: 0",
            "border-radius: 999px",
            "gap: 7px",
            "background: var(--green-100)",
            "color: var(--green)",
            "font-size: 12px",
            "font-weight: 800",
        ):
            self.assertIn(declaration, style)
        self.assertIn(
            ".local-executor-entry.online .local-executor-entry-dot "
            "{ width: 12px; height: 12px; background: var(--green); "
            "box-shadow: none; }",
            self.html,
        )
        self.assertIn(
            '#hubStatus.pill.ok::before { content: ""; flex: 0 0 auto; '
            'width: 12px; height: 12px; border-radius: 50%; '
            'background: var(--green); }',
            self.html,
        )


if __name__ == "__main__":
    unittest.main()
