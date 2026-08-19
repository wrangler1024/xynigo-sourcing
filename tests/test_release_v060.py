# -*- coding: utf-8 -*-
"""Release contract tests for Xynigo Sourcing v0.6.1."""

from pathlib import Path
import unittest

from purchase_tool import __version__


class ReleaseV061Tests(unittest.TestCase):
    def test_version_and_packaging_are_aligned(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.6.1')
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
        self.assertIn('Xynigo Sourcing v0.6.1', html)
        self.assertIn('Xyni, GO!', html)
        self.assertIn('小犀与 X 一体图形', html)
        self.assertIn('小犀与 Xynigo 完整品牌一体图形', html)
        self.assertIn('src="xynigo-logo.png"', html)
        self.assertEqual(html.count('src="xynigo-mascot-x.png"'), 1)
        self.assertIn('src="xynigo-x.ico?v=3"', html)
        self.assertIn('href="xynigo-x.png?v=4"', html)
        self.assertIn('href="favicon.ico?v=4"', html)
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
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-favicon.png').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-favicon.ico').is_file())
        main_py = (root / 'src' / 'purchase_tool' / 'main.py').read_text(
            encoding='utf-8')
        self.assertIn("self._file(FAVICON_ICO, 'image/x-icon')", main_py)


if __name__ == '__main__':
    unittest.main()
