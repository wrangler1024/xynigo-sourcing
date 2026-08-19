# -*- coding: utf-8 -*-
"""Release contract tests for Xynigo Sourcing v0.6.3."""

from pathlib import Path
import unittest

from purchase_tool import __version__


class ReleaseV063Tests(unittest.TestCase):
    def test_version_and_packaging_are_aligned(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.6.3')
        script = (root / '组装Windows绿色包.sh').read_text(encoding='utf-8')
        self.assertIn('v${VERSION}.zip', script)
        self.assertIn('Xynigo Sourcing v%s 启动中', script)
        self.assertIn("os.environ.setdefault('XYNIGO_INSTALL_DIR', ROOT)", script)
        self.assertNotIn(
            'from purchase_tool.updater import check_for_updates_at_startup',
            script)
        self.assertTrue((root / 'packaging' / 'windows' /
                         'update-helper.ps1').is_file())
        mac_script = (root / '组装macOS绿色包.sh').read_text(
            encoding='utf-8')
        self.assertIn('Xynigo_Sourcing_macOS_', mac_script)
        mac_entry = (root / 'packaging' / 'macos' / 'entry.py').read_text(
            encoding='utf-8')
        self.assertIn("os.environ.setdefault('XYNIGO_INSTALL_DIR'", mac_entry)
        self.assertNotIn('check_for_updates_at_startup', mac_entry)
        self.assertTrue((root / 'packaging' / 'macos' /
                         'update-helper.sh').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' /
                         'updater.py').is_file())

    def test_web_bundle_contains_module_three_and_template(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'src' / 'purchase_tool' / 'web' / 'index.html').read_text(
            encoding='utf-8')
        self.assertIn('买家号建环境', html)
        self.assertIn('/api/envbatch/start', html)
        self.assertIn('/api/envbatch/preflight', html)
        self.assertIn('id="querySite"', html)
        self.assertIn('<option value="US">美国站 · us.shein.com</option>', html)
        self.assertIn('JSON.stringify({ serials, site })', html)
        self.assertIn("'Shipped': 'st-enviado'", html)
        self.assertIn("'Paid': 'st-procesando'", html)
        self.assertIn("'Risk verification': 'st-reembolsando'", html)
        self.assertIn('风险订单，待验证', html)
        self.assertIn('风险订单 <b id="cntRisk">0</b>', html)
        self.assertIn("if (r.riskOrder) cnt.risk++;", html)
        self.assertIn("else cnt.ok++;", html)
        self.assertIn("r.state === 'ok' && !r.riskOrder", html)
        self.assertIn("r.state === 'ok' && r.riskOrder", html)
        self.assertIn('id="cfgPurchaseTag"', html)
        self.assertIn('id="cfgProxyLink"', html)
        self.assertIn('type="password" id="cfgProxyLink"', html)
        self.assertIn('id="cfgProxyClear"', html)
        self.assertIn('proxyLink, proxyClear', html)
        self.assertIn('留空保留已有配置', html)
        self.assertNotIn('https://proxy.example.test', html)
        self.assertIn('Xynigo Sourcing v0.6.3', html)
        self.assertIn('Xyni, GO!', html)
        self.assertIn('Xynigo 品牌字标', html)
        self.assertIn('小犀与 Xynigo 完整品牌一体图形', html)
        self.assertIn('src="xynigo-logo.png"', html)
        self.assertIn('src="xynigo-logo.png?v=6"', html)
        self.assertNotIn('src="xynigo-mascot-x.png"', html)
        self.assertIn('跨境采购协同系统', html)
        self.assertNotIn('跨境代采协同系统', html)
        self.assertIn('src="xynigo-x.ico?v=3"', html)
        self.assertIn('href="/xynigo-x.png?v=5"', html)
        self.assertIn('href="/favicon.ico?v=5"', html)
        self.assertNotIn('class="logo-x"', html)
        self.assertIn('小犀提示', html)
        self.assertIn('品牌表达', html)
        self.assertIn('持续迭代中', html)
        self.assertIn('id="sidebarToggle"', html)
        self.assertIn('id="btnRetryFail"', html)
        self.assertIn('id="updateNotice"', html)
        self.assertIn('id="updateCheck"', html)
        self.assertIn('/api/update/check', html)
        self.assertIn('/api/update/status', html)
        self.assertIn('/api/update/prompt', html)
        self.assertIn('cfgHideEnvName', html)
        self.assertIn("activeModule !== 'query'", html)
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         '采购工具买家号入库模板.xlsx').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-logo.png').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-mascot-x.png').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-x.ico').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-x.png').is_file())
        ico = (root / 'src' / 'purchase_tool' / 'web' /
               'xynigo-x.ico').read_bytes()
        self.assertEqual(ico[:4], b'\x00\x00\x01\x00')
        self.assertGreaterEqual(int.from_bytes(ico[4:6], 'little'), 7)
        main_py = (root / 'src' / 'purchase_tool' / 'main.py').read_text(
            encoding='utf-8')
        self.assertIn("self._file(X_ICON_ICO, 'image/x-icon')", main_py)
        self.assertNotIn('FAVICON_ICO', main_py)


if __name__ == '__main__':
    unittest.main()
