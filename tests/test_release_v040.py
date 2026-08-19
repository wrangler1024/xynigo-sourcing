# -*- coding: utf-8 -*-
from pathlib import Path
import unittest

from purchase_tool import __version__


class ReleaseV040Tests(unittest.TestCase):
    def test_version_and_packaging_are_aligned(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.4.2')
        script = (root / '组装Windows绿色包.sh').read_text(encoding='utf-8')
        self.assertIn('v0.4.2.zip', script)
        self.assertIn('Xynigo Sourcing v0.4.2 启动中', script)

    def test_web_bundle_contains_module_three_and_template(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'src' / 'purchase_tool' / 'web' / 'index.html').read_text(
            encoding='utf-8')
        self.assertIn('模块三 · 买家号建环境', html)
        self.assertIn('/api/envbatch/start', html)
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         '采购工具买家号入库模板.xlsx').is_file())


if __name__ == '__main__':
    unittest.main()
