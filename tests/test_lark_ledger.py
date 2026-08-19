# -*- coding: utf-8 -*-
import unittest

from purchase_tool.buyer_register import BuyerRegistrationTask
from purchase_tool.lark_ledger import LarkLedgerError, LarkLedgerSink


class LarkLedgerTests(unittest.TestCase):
    def test_build_payload_uses_confirmed_field_types(self):
        task = BuyerRegistrationTask(
            email='buyer@example.com', shein_password='shein-pass',
            outlook_password='mail-pass', env_serial='1002',
            record_id='rec_demo', buyer='甲')
        env = {'serialNumber': 1002, 'containerName': '注册测试-MX-006'}
        cookie = [{'name': 'sid', 'value': 'secret', 'domain': '.example.com'}]
        payload = LarkLedgerSink().build_payload(task, env, cookie)
        self.assertEqual(payload['账号状态'], '已绑定')
        self.assertEqual(payload['环境序号'], 1002)
        self.assertIsInstance(payload['Cookie'], str)
        self.assertEqual(payload['采购员'], '甲')

    def test_legacy_table_id_is_mx_only(self):
        sink = LarkLedgerSink(base_token='base_demo', table_id='tbl_mx')
        mx = BuyerRegistrationTask(
            email='mx@example.com', shein_password='shein-pass',
            outlook_password='mail-pass',
            env_serial='1002', record_id='rec_mx', site='MX')
        us = BuyerRegistrationTask(
            email='us@example.com', shein_password='shein-pass',
            outlook_password='mail-pass',
            env_serial='1003', record_id='rec_us', site='US')
        self.assertEqual(sink._table_id(mx), 'tbl_mx')
        with self.assertRaisesRegex(LarkLedgerError,
                                    'XYNIGO_LARK_TABLE_ID_US'):
            sink._table_id(us)

    def test_site_table_mapping_selects_matching_table(self):
        sink = LarkLedgerSink(
            base_token='base_demo',
            table_ids={'MX': 'tbl_mx', 'US': 'tbl_us'})
        us = BuyerRegistrationTask(
            email='us@example.com', shein_password='shein-pass',
            outlook_password='mail-pass',
            env_serial='1003', record_id='rec_us', site='US')
        self.assertEqual(sink._table_id(us), 'tbl_us')

    def test_rejects_unknown_table_mapping_site(self):
        with self.assertRaisesRegex(LarkLedgerError, '不支持的台账站点'):
            LarkLedgerSink(table_ids={'CA': 'tbl_ca'})


if __name__ == '__main__':
    unittest.main()
