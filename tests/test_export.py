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

    def test_xlsx_stores_screenshot_as_copyable_in_cell_image(self):
        # 导出器不解码 JPEG；真实像素由 Chrome 生成、尺寸由 CDP 提供。
        jpeg = b'\xff\xd8\xff\xe0test-jpeg\xff\xd9'
        data, name, mime = export_bytes(
            [result_row()], 'xlsx', lambda _serial: jpeg)
        self.assertTrue(name.endswith('.xlsx'))
        self.assertIn('spreadsheetml', mime)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            required = {
                'xl/media/image1.jpeg', 'xl/metadata.xml',
                'xl/richData/richValueRel.xml',
                'xl/richData/rdrichvalue.xml',
                'xl/richData/rdrichvaluestructure.xml',
                'xl/richData/rdRichValueTypes.xml',
                'xl/richData/_rels/richValueRel.xml.rels',
            }
            self.assertTrue(required.issubset(names))
            self.assertFalse(any(name.startswith('xl/drawings/')
                                 for name in names))
            self.assertEqual(archive.read('xl/media/image1.jpeg'), jpeg)

            sheet_xml = archive.read('xl/worksheets/sheet1.xml')
            sheet_root = ElementTree.fromstring(sheet_xml)
            ns = {'x': 'http://schemas.openxmlformats.org/'
                       'spreadsheetml/2006/main'}
            image_cell = sheet_root.find('.//x:c[@r="J2"]', ns)
            self.assertIsNotNone(image_cell)
            self.assertEqual(image_cell.attrib['t'], 'e')
            self.assertEqual(image_cell.attrib['vm'], '1')
            self.assertEqual(image_cell.find('x:v', ns).text, '#VALUE!')
            self.assertNotIn('drawing', sheet_xml.decode('utf-8'))
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

            metadata_root = ElementTree.fromstring(
                archive.read('xl/metadata.xml'))
            metadata_type = metadata_root.find('.//x:metadataType', ns)
            self.assertEqual(metadata_type.attrib['name'], 'XLRICHVALUE')
            self.assertEqual(metadata_type.attrib['copy'], '1')
            self.assertEqual(metadata_type.attrib['pasteAll'], '1')
            self.assertEqual(metadata_type.attrib['pasteValues'], '1')
            self.assertEqual(metadata_root.find(
                'x:valueMetadata', ns).attrib['count'], '1')

            rich_ns = {
                'r': ('http://schemas.microsoft.com/office/'
                      'spreadsheetml/2017/richdata')}
            structure = ElementTree.fromstring(
                archive.read('xl/richData/rdrichvaluestructure.xml'))
            self.assertEqual(structure.find('r:s', rich_ns).attrib['t'],
                             '_localImage')
            keys = [item.attrib['n']
                    for item in structure.findall('.//r:k', rich_ns)]
            self.assertEqual(
                keys, ['_rvRel:LocalImageIdentifier', 'CalcOrigin'])
            values = ElementTree.fromstring(
                archive.read('xl/richData/rdrichvalue.xml'))
            self.assertEqual(
                [item.text for item in values.findall('.//r:v', rich_ns)],
                ['0', '5'])

            relationships = ElementTree.fromstring(archive.read(
                'xl/richData/_rels/richValueRel.xml.rels'))
            package_ns = {
                'p': ('http://schemas.openxmlformats.org/package/'
                      '2006/relationships')}
            image_rel = relationships.find('p:Relationship', package_ns)
            self.assertEqual(image_rel.attrib['Target'],
                             '../media/image1.jpeg')
            self.assertNotIn('TargetMode', image_rel.attrib)

            workbook_rels = ElementTree.fromstring(
                archive.read('xl/_rels/workbook.xml.rels'))
            rel_types = {
                item.attrib['Type']
                for item in workbook_rels.findall(
                    'p:Relationship', package_ns)
            }
            self.assertIn(
                'http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/sheetMetadata', rel_types)
            self.assertIn(
                'http://schemas.microsoft.com/office/2022/10/'
                'relationships/richValueRel', rel_types)

            content_types = ElementTree.fromstring(
                archive.read('[Content_Types].xml'))
            content_ns = {
                'c': ('http://schemas.openxmlformats.org/package/'
                      '2006/content-types')}
            defaults = {
                item.attrib['Extension']: item.attrib['ContentType']
                for item in content_types.findall('c:Default', content_ns)
            }
            self.assertEqual(defaults['jpeg'], 'image/jpeg')
            overrides = {
                item.attrib['PartName']: item.attrib['ContentType']
                for item in content_types.findall('c:Override', content_ns)
            }
            self.assertEqual(
                overrides['/xl/richData/rdrichvalue.xml'],
                'application/vnd.ms-excel.rdrichvalue+xml')
            self.assertEqual(
                overrides['/xl/richData/richValueRel.xml'],
                'application/vnd.ms-excel.richvaluerel+xml')
            types_xml = archive.read(
                'xl/richData/rdRichValueTypes.xml').decode('utf-8')
            self.assertIn('mc:Ignorable="x"', types_xml)
            self.assertIn('xmlns:x=', types_xml)

    def test_xlsx_assigns_one_rich_value_to_each_image_cell(self):
        first = b'\xff\xd8first\xff\xd9'
        second = b'\xff\xd8second\xff\xd9'
        rows = [
            result_row(serial='1001'),
            result_row(serial='1002'),
            result_row(serial='1003', screenshotState='fail',
                       screenshotError='脱敏失败'),
        ]
        images = {'1001': first, '1002': second}
        data, _name, _mime = export_bytes(
            rows, 'xlsx', lambda serial: images.get(serial))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            sheet_root = ElementTree.fromstring(
                archive.read('xl/worksheets/sheet1.xml'))
            ns = {'x': 'http://schemas.openxmlformats.org/'
                       'spreadsheetml/2006/main'}
            self.assertEqual(
                sheet_root.find('.//x:c[@r="J2"]', ns).attrib['vm'], '1')
            self.assertEqual(
                sheet_root.find('.//x:c[@r="J3"]', ns).attrib['vm'], '2')
            self.assertNotIn(
                'vm', sheet_root.find('.//x:c[@r="J4"]', ns).attrib)
            self.assertEqual(archive.read('xl/media/image1.jpeg'), first)
            self.assertEqual(archive.read('xl/media/image2.jpeg'), second)
            metadata_root = ElementTree.fromstring(
                archive.read('xl/metadata.xml'))
            self.assertEqual(metadata_root.find(
                'x:valueMetadata', ns).attrib['count'], '2')
            rich_ns = {
                'r': ('http://schemas.microsoft.com/office/'
                      'spreadsheetml/2017/richdata')}
            values = ElementTree.fromstring(
                archive.read('xl/richData/rdrichvalue.xml'))
            rich_values = [
                [item.text for item in value.findall('r:v', rich_ns)]
                for value in values.findall('r:rv', rich_ns)
            ]
            self.assertEqual(rich_values, [['0', '5'], ['1', '5']])

    def test_query_time_header_identifies_site_timezone(self):
        self.assertEqual(EXPORT_HEAD[-1], '查询时间（站点）')

    def test_csv_includes_screenshot_status_field(self):
        data, name, _mime = export_bytes([result_row()], 'csv')
        text = data.decode('utf-8-sig')
        self.assertTrue(name.endswith('.csv'))
        self.assertIn('物流轨迹截图', text)
        self.assertIn('查看截图', text)

    def test_quick_xlsx_export_skips_screenshot_reader(self):
        calls = []

        def reader(serial):
            calls.append(serial)
            raise AssertionError('快速导出不应读取截图')

        data, name, _mime = export_bytes(
            [result_row()], 'xlsx', reader, include_screenshots=False)
        self.assertEqual(calls, [])
        self.assertIn('无截图', name)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertFalse(any(
                item.startswith('xl/media/') for item in archive.namelist()))
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(data), read_only=True)
        self.assertEqual(workbook.active['J2'].value, '已生成（未导出）')
        workbook.close()

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
