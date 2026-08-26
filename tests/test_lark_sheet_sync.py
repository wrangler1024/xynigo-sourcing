# -*- coding: utf-8 -*-
"""Ordinary Feishu Sheet gateway tests; every write uses a fake runner."""
import json
from pathlib import Path
import subprocess
import unittest

from purchase_tool.lark_sheet_sync import (
    LarkCliSheetsGateway, LarkSheetSyncError,
    _cell_background_color, _cell_contains_image, _cell_contains_link,
    _parse_annotated_csv, parse_lark_sheet_url)


def ok(data):
    return subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({'ok': True, 'identity': 'user', 'data': data}),
        stderr='')


class FakeCliRunner:
    def __init__(self):
        self.calls = []
        self.payloads = []

    def __call__(self, argv, cwd=None, **kwargs):
        self.calls.append((list(argv), cwd))
        command = argv[2]
        if command == '+workbook-info':
            return ok({'revision': 3, 'sheets': [{
                'resource_type': 'sheet', 'sheet_id': 'sheetA',
                'sheet_name': '采购分单协作区', 'row_count': 200,
                'column_count': 34, 'is_hidden': False,
            }]})
        if command == '+csv-get':
            return ok({
                'revision': 3, 'has_more': False,
                'annotated_csv': (
                    '[row=1] A,B\n'
                    '[row=7] one,"two\nlines"\n'),
            })
        if command == '+cells-get':
            cell_range = argv[argv.index('--range') + 1]
            if cell_range.startswith('A'):
                return ok({
                    'has_more': False,
                    'ranges': [{
                        'row_indices': [7, 8],
                        'cells': [
                            [{'cell_styles': {
                                'background_color': '#E8F7F7'}}],
                            [{}],
                        ],
                    }],
                })
            if cell_range.startswith('N'):
                return ok({
                    'has_more': False,
                    'ranges': [{
                        'row_indices': [7, 8],
                        'cells': [
                            [{'rich_text': [{
                                'type': 'link', 'text': '打开采购链接',
                                'link': 'https://example.com/one'}]}],
                            [{'value': 'https://example.com/two'}],
                        ],
                    }],
                })
            return ok({
                'has_more': False,
                'ranges': [{
                    'row_indices': [7, 8],
                    'cells': [
                        [{'value': '订单商品图'}],
                        [{'value': [{'type': 'embed-image',
                                     'file_token': 'img-token'}]}],
                    ],
                }],
            })
        if command == '+cells-set-image':
            image_name = argv[argv.index('--image') + 1]
            if not cwd or not Path(cwd, image_name).is_file():
                raise AssertionError('temporary image is missing')
            return ok({'revision': 4, 'file_token': 'img-token'})
        if command == '+table-put':
            source = argv[argv.index('--sheets') + 1]
            if not cwd or not source.startswith('@./'):
                raise AssertionError('table payload must use a private file')
            payload = json.loads(
                Path(cwd, source[3:]).read_text(encoding='utf-8'))
            self.payloads.append((command, payload))
            table = payload['sheets'][0]
            if table.get('header') is not False:
                raise AssertionError('table write must not repeat the header')
            if table.get('mode') == 'append':
                if table['allow_overwrite'] is not False:
                    raise AssertionError('append must protect existing cells')
            elif table.get('mode') == 'overwrite':
                if table['allow_overwrite'] is not True:
                    raise AssertionError('typed overwrite must be explicit')
            else:
                raise AssertionError('table write mode must be explicit')
            return ok({'updated_rows_count': len(table['data'])})
        if command == '+batch-update':
            source = argv[argv.index('--operations') + 1]
            if (('--yes' not in argv and '--dry-run' not in argv)
                    or not cwd or not source.startswith('@./')):
                raise AssertionError(
                    'header structure batch must be previewed/confirmed and private')
            operations = json.loads(
                Path(cwd, source[3:]).read_text(encoding='utf-8'))
            self.payloads.append((command, operations))
            if not operations:
                raise AssertionError('header operation batch is empty')
            return ok({'succeeded': len(operations)})
        if command == '+dim-move':
            return ok({'revision': 5})
        if command in {'+styles-put', '+cells-set'}:
            flag = '--styles' if command == '+styles-put' else '--writes'
            source = argv[argv.index(flag) + 1]
            if not cwd or not source.startswith('@./'):
                raise AssertionError('write payload must use a private file')
            payload = json.loads(
                Path(cwd, source[3:]).read_text(encoding='utf-8'))
            self.payloads.append((command, payload))
            if command == '+styles-put' and not payload.get('styles'):
                raise AssertionError('style payload is empty')
            if command == '+cells-set' and not payload:
                raise AssertionError('cell writes are empty')
            return ok({'revision': 5})
        raise AssertionError('unexpected command: ' + command)


class LarkSheetSyncTests(unittest.TestCase):
    def test_accepts_only_official_ordinary_sheet_urls(self):
        reference = parse_lark_sheet_url(
            'https://tenant.feishu.cn/sheets/SheetToken123?from=copy#gid=1')
        self.assertEqual(
            reference.url,
            'https://tenant.feishu.cn/sheets/SheetToken123')
        with self.assertRaisesRegex(LarkSheetSyncError, '普通飞书电子表格'):
            parse_lark_sheet_url(
                'https://tenant.feishu.cn/base/BaseToken?table=tbl123')
        with self.assertRaisesRegex(LarkSheetSyncError, '官方 HTTPS'):
            parse_lark_sheet_url(
                'https://example.com/sheets/SheetToken123')

    def test_annotated_csv_uses_real_row_prefix_and_preserves_multiline_cell(self):
        rows = _parse_annotated_csv(
            '[row=1] A,B\n[row=9] one,"two\nlines"\n')
        self.assertEqual(rows[0], (1, ('A', 'B')))
        self.assertEqual(rows[1], (9, ('one', 'two\nlines')))

    def test_plain_label_is_not_mistaken_for_embedded_image(self):
        self.assertFalse(_cell_contains_image({'value': '订单商品图'}))
        self.assertTrue(_cell_contains_image({
            'value': [{'type': 'embed-image', 'file_token': 'img-token'}]}))

    def test_rich_links_and_background_colors_are_detected(self):
        link = {'rich_text': [{
            'type': 'link', 'text': '打开采购链接',
            'link': 'https://example.com/one'}]}
        self.assertTrue(_cell_contains_link(link, 'https://example.com/one'))
        self.assertFalse(_cell_contains_link(link, 'https://example.com/two'))
        self.assertEqual(_cell_background_color({
            'cell_styles': {'background_color': '#e8f7f7'}}), '#E8F7F7')

    def test_gateway_uses_user_identity_and_single_m_cell(self):
        runner = FakeCliRunner()
        gateway = LarkCliSheetsGateway(runner=runner, sleep_fn=lambda _: None)
        url = 'https://tenant.feishu.cn/sheets/SheetToken123'
        info = gateway.inspect(url)
        self.assertEqual(info['sheets'][0]['sheetId'], 'sheetA')
        table = gateway.read_table(url, 'sheetA')
        self.assertEqual(table.rows[0], (7, ('one', 'two\nlines')))
        presence = gateway.image_presence(url, 'sheetA', [7, 8])
        self.assertEqual(presence, {7: False, 8: True})
        gateway.set_image(url, 'sheetA', 7, b'jpeg', 'image/jpeg')
        write_argv = runner.calls[-1][0]
        self.assertIn('--as', write_argv)
        self.assertEqual(write_argv[write_argv.index('--range') + 1], 'M7')
        self.assertNotIn('--profile', write_argv)

        result = gateway.append_table_rows(
            url, '采购分单协作区', ['订单号', '金额'],
            [['00123', 12.5]], dtypes={'订单号': 'object', '金额': 'float64'},
            formats={'金额': '#,##0.00'})
        self.assertEqual(result['updated_rows_count'], 1)
        append_argv = runner.calls[-1][0]
        self.assertEqual(append_argv[2], '+table-put')
        self.assertIn('--as', append_argv)
        self.assertNotIn('00123', ' '.join(append_argv))

        backgrounds = gateway.row_backgrounds(url, 'sheetA', [7, 8])
        self.assertEqual(backgrounds, {7: '#E8F7F7', 8: ''})
        gateway.apply_row_presentation(
            url, '采购分单协作区',
            [{'start': 7, 'end': 8, 'color': '#E8F7F7'}], [(7, 8)], 52,
            last_column='AF')
        self.assertEqual(runner.calls[-1][0][2], '+styles-put')
        links = {
            7: 'https://example.com/one',
            8: 'https://example.com/two',
        }
        self.assertEqual(
            gateway.hyperlink_presence(url, 'sheetA', links),
            {7: True, 8: False})
        gateway.set_hyperlinks(url, 'sheetA', [(7, links[7])])
        self.assertEqual(runner.calls[-1][0][2], '+cells-set')

        normalized = gateway.normalize_collaboration_headers(
            url, 'sheetA', '采购分单协作区', [
                '分单标记', '采购员', '采购状态', '优先级',
                '销售订单号', '采购单号', '店铺', '运营', '收件人',
                '国家', '销售金额', '订单时间', '商品图片',
                '采购链接', '主规格', '次规格', '需求数量',
                '采购指导价', '采购备注', '系统订单键',
                '导入批次', '数据版本'], last_row=7)
        self.assertGreater(normalized['operations'], 0)
        self.assertEqual(runner.calls[-1][0][2], '+batch-update')
        self.assertIn('--yes', runner.calls[-1][0])
        self.assertIn('--dry-run', runner.calls[-2][0])
        gateway.apply_header_presentation(url, 'sheetA', '采购分单协作区', [{
            'start': 'A', 'end': 'Z', 'color': '#1B7280', 'note': '采购需求区',
        }, {
            'start': 'AA', 'end': 'AM', 'color': '#386FA4', 'note': '采购下单区',
        }, {
            'start': 'AN', 'end': 'AQ', 'color': '#2F855A', 'note': '系统追踪区',
        }])
        self.assertEqual(runner.calls[-1][0][2], '+cells-set')
        styles_payload = next(
            payload for command, payload in reversed(runner.payloads)
            if command == '+styles-put')
        self.assertEqual(styles_payload['styles'][0]['freeze'], {
            'rows': 1, 'cols': 5})

    def test_reorders_demand_columns_and_rewrites_split_date_as_typed_date(self):
        runner = FakeCliRunner()
        gateway = LarkCliSheetsGateway(runner=runner, sleep_fn=lambda _: None)
        url = 'https://tenant.feishu.cn/sheets/SheetToken123'
        headers = (
            '分单日期', '采购员', '采购状态', '优先级', '销售订单号',
            '包裹号', '店铺', '运营', '销售订单金额', '商品金额',
            '订单时间', '商品图片', '采购链接', '主规格', '次规格',
            '需求数量', '采购指导价', '收货人姓名', '收货人国家',
            '收货人州/省', '收货人城市', '地址1', '地址2', '邮编',
            '收货人电话', '采购备注', '系统订单键', '导入批次', '数据版本')
        desired = (
            '分单日期', '采购员', '销售订单号', '店铺', '运营', '包裹号',
            '采购状态', '优先级', '销售订单金额', '商品金额', '订单时间',
            '商品图片', '采购链接', '主规格', '次规格', '需求数量',
            '采购指导价', '收货人姓名', '收货人国家', '收货人州/省',
            '收货人城市', '地址1', '地址2', '邮编', '收货人电话', '采购备注')
        moved = gateway.reorder_collaboration_headers(
            url, 'sheetA', '采购分单协作区', headers, desired)
        self.assertGreater(moved['operations'], 0)
        self.assertTrue(all(call[0][2] == '+dim-move'
                            for call in runner.calls))

        final_headers = desired + ('系统订单键', '导入批次', '数据版本')
        normalized = gateway.normalize_date_column(
            url, '采购分单协作区', final_headers, (
                (7, ('2026-08-26 16:49:12',)),
                (8, ('2026-08-26',)),
            ))
        self.assertEqual(normalized['rows'], 2)
        date_payload = runner.payloads[-1][1]['sheets'][0]
        self.assertEqual(date_payload['mode'], 'overwrite')
        self.assertEqual(date_payload['start_cell'], 'A7')
        self.assertEqual(date_payload['data'], [
            ['2026-08-26'], ['2026-08-26']])
        self.assertEqual(date_payload['dtypes'], {
            '分单日期': 'datetime64[ns]'})
        self.assertEqual(date_payload['formats'], {
            '分单日期': 'yyyy-mm-dd'})

    def test_refuses_to_delete_split_batch_without_equal_import_batch(self):
        runner = FakeCliRunner()
        gateway = LarkCliSheetsGateway(runner=runner, sleep_fn=lambda _: None)
        with self.assertRaisesRegex(LarkSheetSyncError, '未被导入批次等值覆盖'):
            gateway.normalize_collaboration_headers(
                'https://tenant.feishu.cn/sheets/SheetToken123',
                'sheetA', '采购分单协作区',
                ('分单批次', '分单时间', '采购指导价', '收货人姓名',
                 '收货人国家', '收货人州/省', '收货人城市', '地址1',
                 '地址2', '邮编', '收货人电话', '销售订单金额',
                 '商品金额', '系统订单键', '导入操作人', '导入批次'),
                rows=((2, ('batch-a', '2026-08-26', '', '', '', '', '', '',
                           '', '', '', '', '', '', '', 'batch-b')),))
        self.assertEqual(runner.calls, [])


if __name__ == '__main__':
    unittest.main()
