# -*- coding: utf-8 -*-
from io import BytesIO
import copy
import unittest

from openpyxl import Workbook

from purchase_tool.env_batch import MAPPING_HEADERS, parse_vendor_workbook
from purchase_tool.ledger_backfill import (
    LedgerBackfillError, LedgerBackfillService, MappingRow,
    ensure_buyer_options, parse_mapping_workbook, validate_schema)


def workbook_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def accounts():
    rows = [
        ['alpha@example.com', 'pass-a',
         'https://codes.example.test/?orderNo=a1b2c3',
         '[{"name":"sid","value":"cookie-a"}]'],
        ['beta@example.com', 'pass-b',
         'https://codes.example.test/?orderNo=d4e5f6',
         '[{"name":"sid","value":"cookie-b"}]'],
    ]
    return parse_vendor_workbook(BytesIO(workbook_bytes(rows)))


def mapping_rows():
    return [
        MappingRow(2, 'alpha@example.com', '采购-甲-MX-0819-001',
                   1004, '甲', '2026-08-19 10:00:00', '完成'),
        MappingRow(3, 'beta@example.com', '采购-乙-MX-0819-001',
                   1005, '乙', '2026-08-19 10:05:00', '完成'),
    ]


def fields():
    result = []
    types = {
        '邮箱账号': 'text', '密码': 'text', '接码Key链接': 'text',
        '号商购买单号': 'text', '账号状态': 'select', '采购员': 'select',
        'Cookie': 'text', '备注': 'text', '绑定环境': 'text',
        '环境序号': 'number', '绑定时间': 'datetime', '操作人': 'user',
    }
    for index, (name, kind) in enumerate(types.items()):
        field = {'id': 'fld%d' % index, 'name': name, 'type': kind}
        if name == '采购员':
            field.update({'multiple': False,
                          'options': [{'name': '甲'}, {'name': '乙'}]})
        result.append(field)
    return result


class FakeClient(object):
    def __init__(self):
        self.fields = fields()
        self.records = [{
            '_record_id': 'rec-alpha',
            '邮箱账号': 'alpha@example.com',
            '账号状态': '已绑定',
            '绑定环境': '采购-甲-MX-0819-001',
            '环境序号': 1004,
            '绑定时间': '2026-08-19 10:00:00',
            '采购员': '甲',
            '操作人': [{'id': 'ou_test'}],
        }]
        self.created_payloads = []
        self.updated_payloads = []
        self.option_updates = []

    def list_fields(self):
        return copy.deepcopy(self.fields)

    def list_records(self):
        return copy.deepcopy(self.records)

    def batch_create(self, create_records):
        self.created_payloads.extend(copy.deepcopy(create_records))
        ids = []
        for item in create_records:
            record_id = 'rec-%d' % (len(self.records) + 1)
            ids.append(record_id)
            record = {'_record_id': record_id}
            record.update(copy.deepcopy(item))
            self.records.append(record)
        return ids

    def batch_update(self, update_records):
        self.updated_payloads.append(copy.deepcopy(update_records))
        index = {item['_record_id']: item for item in self.records}
        for record_id, patch in update_records.items():
            index[record_id].update(copy.deepcopy(patch))

    def update_select_options(self, field, missing):
        self.option_updates.append((field['name'], list(missing)))
        for item in self.fields:
            if item['name'] == field['name']:
                item['options'].extend({'name': name} for name in missing)


class LedgerBackfillTests(unittest.TestCase):
    def test_mapping_parser_requires_exact_headers(self):
        rows = [list(MAPPING_HEADERS),
                ['alpha@example.com', '采购-甲-MX-0819-001', 1004, '甲',
                 '2026-08-19 10:00:00', '完成']]
        parsed = parse_mapping_workbook(BytesIO(workbook_bytes(rows)))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].serial_number, 1004)
        with self.assertRaises(LedgerBackfillError):
            parse_mapping_workbook(BytesIO(workbook_bytes([
                ['邮箱', '环境', '序号']
            ])))

    def test_schema_and_buyer_options_are_preflighted(self):
        client = FakeClient()
        validate_schema(client.list_fields())
        buyer_field = next(item for item in client.fields
                           if item['name'] == '采购员')
        buyer_field['options'] = [{'name': '甲'}]
        with self.assertRaises(LedgerBackfillError):
            ensure_buyer_options(client, client.list_fields(), ['甲', '乙'])
        ensure_buyer_options(
            client, client.list_fields(), ['甲', '乙'], allow_create=True)
        self.assertEqual(client.option_updates, [('采购员', ['乙'])])

    def test_apply_is_idempotent_and_never_overwrites_existing_credentials(self):
        client = FakeClient()
        service = LedgerBackfillService(
            client, operator_open_id='ou_test', remark='20260819批次')
        first = service.apply(accounts(), mapping_rows())
        self.assertEqual(first['created'], 1)
        self.assertEqual(first['updated'], 1)
        self.assertEqual(first['skippedComplete'], 1)
        self.assertEqual(client.created_payloads[0]['邮箱账号'],
                         'beta@example.com')
        self.assertNotIn('密码', client.records[0])

        second = service.apply(accounts(), mapping_rows())
        self.assertEqual(second['created'], 0)
        self.assertEqual(second['updated'], 0)
        self.assertEqual(second['skippedComplete'], 2)

    def test_failed_mapping_row_is_imported_but_not_marked_bound(self):
        client = FakeClient()
        rows = mapping_rows()
        rows[1] = MappingRow(
            3, 'beta@example.com', '', None, '乙', '',
            '失败:cookie_imported')
        service = LedgerBackfillService(client, operator_open_id='ou_test')
        result = service.apply(accounts(), rows)
        self.assertEqual(result['failedRows'], 1)
        beta = next(item for item in client.records
                    if item['邮箱账号'] == 'beta@example.com')
        self.assertEqual(beta['账号状态'], '未绑定')
        self.assertNotIn('绑定环境', beta)

    def test_original_and_mapping_email_sets_must_match(self):
        service = LedgerBackfillService(FakeClient(), operator_open_id='ou_test')
        with self.assertRaises(LedgerBackfillError):
            service.dry_run(accounts(), mapping_rows()[:1])


if __name__ == '__main__':
    unittest.main()
