# -*- coding: utf-8 -*-
import unittest

from purchase_tool.buyer_register import BuyerRegistrationTask
from purchase_tool.lark_ledger import LarkLedgerSink


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


if __name__ == '__main__':
    unittest.main()
