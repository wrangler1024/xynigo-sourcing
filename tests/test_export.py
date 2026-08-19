# -*- coding: utf-8 -*-
import io
import unittest
import zipfile
from xml.etree import ElementTree

from purchase_tool.main import EXPORT_HEAD, export_bytes


def result_row(**overrides):
    row = {
        'serial': '1001', 'envName': '采购测试', 'state': 'ok',
        'orderNo': 'GSH1TEST', 'orderTime': '2026-08-18 01:05:40',
        'amount': '$MXN100.00', 'status': 'Enviado', 'statusCn': '已发货',
        'tracks': ['49350000001206'], 'pkgs': ['PKG1'], 'carrier': 'IMILE',
        'kanDan': False, 'riskOrder': False, 'ip': '127.0.0.1', 'error': '',
        'time': '2026-08-18 21:01:02',
        'screenshotState': 'ok', 'screenshotFile': '环境1975_物流尾号1206.jpg',
        'screenshotError': '', 'screenshotSizeKb': 95,
        'screenshotWidth': 1008, 'screenshotHeight': 535,
    }
    row.update(overrides)
    return row


class ExportTests(unittest.TestCase):
    def test_export_head_contains_tracking_screenshot(self):
        self.assertIn('物流轨迹截图', EXPORT_HEAD)

    def test_xlsx_embeds_screenshot_without_external_image_dependency(self):
        # 导出器不解码 JPEG；真实像素由 Chrome 生成、尺寸由 CDP 提供。
        jpeg = b'\xff\xd8\xff\xe0test-jpeg\xff\xd9'
        data, name, mime = export_bytes(
            [result_row()], 'xlsx', lambda _serial: jpeg)
        self.assertTrue(name.endswith('.xlsx'))
        self.assertIn('spreadsheetml', mime)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            self.assertTrue(any(x.startswith('xl/media/image')
                                and x.endswith('.jpeg') for x in names))
            workbook_xml = archive.read('xl/workbook.xml').decode('utf-8')
            self.assertNotIn('name="物流轨迹截图"', workbook_xml)
            sheet_xml = archive.read(
                'xl/worksheets/sheet1.xml').decode('utf-8')
            self.assertIn('<drawing', sheet_xml)
            drawing_xml = archive.read(
                'xl/drawings/drawing1.xml').decode('utf-8')
            self.assertIn('twoCellAnchor', drawing_xml)
            self.assertIn('<from><col>9</col>', drawing_xml)

            sheet_root = ElementTree.fromstring(sheet_xml)
            ns = {'x': 'http://schemas.openxmlformats.org/'
                       'spreadsheetml/2006/main'}
            screenshot_col = next(
                col for col in sheet_root.findall('.//x:col', ns)
                if col.attrib.get('min') == '10')
            self.assertLessEqual(float(screenshot_col.attrib['width']), 18)
            row_two = next(
                row for row in sheet_root.findall('.//x:row', ns)
                if row.attrib.get('r') == '2')
            self.assertLessEqual(float(row_two.attrib['ht']), 49)
            styles_xml = archive.read('xl/styles.xml').decode('utf-8')
            self.assertIn('style="thin"', styles_xml)
            self.assertIn('B7C9E2', styles_xml)

    def test_query_time_header_identifies_site_timezone(self):
        self.assertEqual(EXPORT_HEAD[-1], '查询时间（站点）')

    def test_csv_includes_screenshot_status_field(self):
        data, name, _mime = export_bytes([result_row()], 'csv')
        text = data.decode('utf-8-sig')
        self.assertTrue(name.endswith('.csv'))
        self.assertIn('物流轨迹截图', text)
        self.assertIn('查看截图', text)

    def test_risk_order_export_is_not_reported_as_success(self):
        data, _name, _mime = export_bytes([
            result_row(
                status='Risk verification', statusCn='风险订单/待验证',
                tracks=[], carrier='', riskOrder=True,
                screenshotState='none')
        ], 'csv')
        text = data.decode('utf-8-sig')
        self.assertIn('风险订单（待验证）', text)
        self.assertIn('风险订单待验证，无物流', text)


if __name__ == '__main__':
    unittest.main()
