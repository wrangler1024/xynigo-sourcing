# -*- coding: utf-8 -*-
import copy
import json
import unittest

from purchase_tool.buyer_register import BuyerRegistrationTask
from purchase_tool.lark_ledger import LarkLedgerError, LarkLedgerSink


class FakeLark(object):
    def __init__(self):
        types = {
            '站点': 'SingleSelect', '账号状态': 'SingleSelect',
            '绑定环境': 'Text', '环境序号': 'Number',
            '绑定时间': 'DateTime', '首次登录日期': 'DateTime',
            'Cookie': 'Text', '采购员': 'SingleSelect',
        }
        self.fields = [
            {'field_name': name, 'ui_type': kind}
            for name, kind in types.items()]
        self.records = {
            'rec_demo': {'record_id': 'rec_demo', 'fields': {}}
        }
        self.updates = []

    def list_fields(self):
        return copy.deepcopy(self.fields)

    def batch_update(self, updates):
        self.updates.append(copy.deepcopy(updates))
        for record_id, fields in updates:
            self.records[record_id]['fields'].update(copy.deepcopy(fields))

    def get_record(self, record_id):
        return copy.deepcopy(self.records[record_id])


class LarkLedgerTests(unittest.TestCase):
    def task(self, record_id='rec_demo', site='MX'):
        return BuyerRegistrationTask(
            email='buyer@example.com', shein_password='shein-pass',
            outlook_password='mail-pass', env_serial='1002',
            record_id=record_id, buyer='新刚', site=site)

    def test_build_payload_uses_unified_table_field_types(self):
        env = {'serialNumber': 1002, 'containerName': 'XG-MX-0821-006'}
        cookie = [{'name': 'sid', 'value': 'secret',
                   'domain': '.example.test'}]
        payload = LarkLedgerSink(FakeLark()).build_payload(
            self.task(), env, cookie)
        self.assertEqual(payload['站点'], 'MX')
        self.assertEqual(payload['账号状态'], '已绑定')
        self.assertEqual(payload['环境序号'], 1002)
        self.assertIsInstance(payload['绑定时间'], int)
        self.assertIsInstance(payload['Cookie'], str)
        self.assertEqual(payload['采购员'], '新刚')

    def test_known_record_updates_through_openapi_and_reads_back(self):
        client = FakeLark()
        sink = LarkLedgerSink(client)
        result = sink(
            self.task(),
            {'serialNumber': 1002, 'containerName': 'XG-MX-0821-006'},
            '[{"name":"sid","value":"cookie-demo"}]')
        self.assertEqual(result, {'updated': True})
        self.assertEqual(len(client.updates), 1)
        rendered = json.dumps(result)
        self.assertNotIn('rec_demo', rendered)

    def test_preflight_is_read_only(self):
        client = FakeLark()
        self.assertEqual(LarkLedgerSink(client).preflight(), {'ready': True})
        self.assertEqual(client.updates, [])

    def test_missing_record_id_never_creates_or_guesses(self):
        client = FakeLark()
        with self.assertRaisesRegex(LarkLedgerError, 'record_id'):
            LarkLedgerSink(client)(
                self.task(record_id=''),
                {'serialNumber': 1002, 'containerName': 'XG-MX-0821-006'},
                '[]')
        self.assertEqual(client.updates, [])

    def test_schema_mismatch_blocks_write(self):
        client = FakeLark()
        client.fields = [field for field in client.fields
                         if field['field_name'] != 'Cookie']
        with self.assertRaisesRegex(LarkLedgerError, 'Cookie'):
            LarkLedgerSink(client)(
                self.task(),
                {'serialNumber': 1002, 'containerName': 'XG-MX-0821-006'},
                '[]')
        self.assertEqual(client.updates, [])


if __name__ == '__main__':
    unittest.main()
