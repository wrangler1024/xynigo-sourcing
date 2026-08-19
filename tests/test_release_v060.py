# -*- coding: utf-8 -*-
"""Release contract tests for Xynigo Sourcing v0.6.0."""

from pathlib import Path
import unittest

from purchase_tool import __version__


class ReleaseV060Tests(unittest.TestCase):
    def test_version_and_packaging_are_aligned(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.6.0')
        script = (root / '组装Windows绿色包.sh').read_text(encoding='utf-8')
        self.assertIn('v${VERSION}.zip', script)
        self.assertIn('Xynigo Sourcing v%s 启动中', script)
        self.assertTrue((root / 'packaging' / 'windows' /
                         'update-helper.ps1').is_file())
        mac_script = (root / '组装macOS绿色包.sh').read_text(
            encoding='utf-8')
        self.assertIn('Xynigo_Sourcing_macOS_', mac_script)
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
        self.assertIn('Xynigo Sourcing v0.6.0', html)
        self.assertIn('Xyni, GO!', html)
        self.assertIn('id="sidebarToggle"', html)
        self.assertIn('id="btnRetryFail"', html)
        self.assertIn('cfgHideEnvName', html)
        self.assertIn("activeModule !== 'query'", html)
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         '采购工具买家号入库模板.xlsx').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-logo.png').is_file())


if __name__ == '__main__':
    unittest.main()
