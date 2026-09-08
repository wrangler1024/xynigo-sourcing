"""Read-only XYP2 tool contract, using synthetic remarks only."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from purchase_tool.procurement_import import decode_xyp2_remark, ProcurementImportError


def remark(**changes):
    payload = {'d': 'mx', 'c': 'MXN', 'i': [
        ['SYNTH-A', '123456789', 'SYNTH-SKU-A', '27_447', 'Black', 'M', 20, .5, 10.25, 2],
        ['SYNTH-B', '123456790', 'SYNTH-SKU-B', '', 'Gray', 'L', 30, 0, 16.5, 1],
    ], 'r': 3}
    payload.update(changes)
    return '[XYP2]' + json.dumps(payload, ensure_ascii=False) + '[/XYP2]'


class Xyp2ParserTests(unittest.TestCase):
    def test_readable_purchase_fields_and_reference_total(self):
        result = decode_xyp2_remark('普通备注\n' + remark() + '\n合成测试')
        self.assertEqual((result['siteName'], result['currency'], result['detailCount'],
                          result['quantityCount'], result['guideTotal']), ('墨西哥站', 'MXN', 2, 3, 37))
        self.assertEqual(result['roundingAmount'], 3)
        self.assertEqual(result['items'][0]['mainSpec'], 'Black')
        self.assertIn('goods_id=123456789&skucode=SYNTH-SKU-A', result['items'][0]['purchaseLink'])
        self.assertTrue(result['items'][0]['purchaseLink'].startswith('https://www.shein.com.mx/'))
        self.assertNotIn('source_text', json.dumps(result))
        self.assertNotIn('普通备注', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('orderNo', result)

    def test_us_site_decimal_sum_and_missing_optional_fields(self):
        payload = json.loads(remark()[6:-7])
        payload.update(d='us', c='USD')
        payload.pop('r')
        payload['i'][0][8:10] = [.1, 3]
        payload['i'][1][8:10] = [.2, 1]
        payload['i'][1][5:8] = ['', None, None]
        result = decode_xyp2_remark('[XYP2]' + json.dumps(payload) + '[/XYP2]')
        self.assertEqual(result['guideTotal'], .5)
        self.assertIsNone(result['roundingAmount'])
        self.assertIsNone(result['items'][1]['originalPrice'])
        self.assertEqual(result['items'][1]['secondarySpec'], '')
        self.assertEqual(len(result['warnings']), 1)
        self.assertTrue(result['items'][0]['purchaseLink'].startswith('https://us.shein.com/'))

    def test_duplicate_identical_blocks_warn_and_conflicts_fail(self):
        result = decode_xyp2_remark(remark() + remark())
        self.assertEqual(result['detailCount'], 2)
        self.assertEqual(result['quantityCount'], 3)
        self.assertIn('相同', result['warnings'][0])
        with self.assertRaisesRegex(ProcurementImportError, '不同 XYP2'):
            decode_xyp2_remark(remark() + remark(r=9))

    def test_invalid_input_never_returns_partial_list(self):
        for value in ['', None, {}, '[XYP2]{', '[XYP2]{broken}[/XYP2]',
                      remark(d='unknown'), remark() + '[XYP2]{', 'A' * 20001]:
            with self.subTest(value=str(value)[:30]), self.assertRaises(ProcurementImportError):
                decode_xyp2_remark(value)
        payload = json.loads(remark()[6:-7])
        payload['i'][1][9] = 0
        with self.assertRaisesRegex(ProcurementImportError, '第 2 条明细'):
            decode_xyp2_remark('[XYP2]' + json.dumps(payload) + '[/XYP2]')

    def test_json_errors_locate_inner_json_position(self):
        with self.assertRaisesRegex(ProcurementImportError, '第 2 行、第 1 列'):
            decode_xyp2_remark('[XYP2]{\nbroken}[/XYP2]')

    def test_numbers_outside_accurate_browser_range_fail(self):
        for price, qty in [(10, 10**18), (1e100, 1)]:
            payload = json.loads(remark()[6:-7])
            payload['i'][0][8:10] = [price, qty]
            with self.assertRaisesRegex(ProcurementImportError, '范围'):
                decode_xyp2_remark('[XYP2]' + json.dumps(payload) + '[/XYP2]')

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_real_ui_handlers_and_copy_formats(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(['node', 'tests/fixtures/xyp2_parser_ui.cjs'], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_navigation_and_local_cloud_routes_are_connected(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'src/purchase_tool/web/index.html').read_text()
        secondary = html[html.index('id="secondaryTabs"'):html.index('<div class="nav-spacer">')]
        self.assertIn('data-parent="assistant" data-module="xyp2parser"', secondary)
        self.assertIn("defaultModule: 'xyp2parser'", html)
        self.assertIn("$('xyp2ParserPanel').classList.toggle('hidden', module !== 'xyp2parser')", html)
        self.assertIn("cloudFetchJson('/v1/assistant/xyp2/parse', opts)", html)
        main = (root / 'src/purchase_tool/main.py').read_text()
        self.assertIn("'/api/assistant/xyp2/parse': 'assistant.access'", main)
        self.assertEqual(html, (root / 'cloud/auth-service/src/xynigo_auth/web/index.html').read_text())
