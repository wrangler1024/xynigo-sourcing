# -*- coding: utf-8 -*-
"""店铺结算巡检：纯函数与批次编排契约测试（不触达真实浏览器）。"""
import threading
import time
import unittest

from purchase_tool import store_finance_inspect as m
from purchase_tool.store_finance_inspect import (
    StoreFinanceInspector,
    date_after,
    money_after,
    parse_sms_key,
)


class MoneyParsingTests(unittest.TestCase):
    TEXT = (
        '我的收入 已结算记录 下次结算金额 MXN 13,610.45 日期:2026-09-14 '
        '累计未结算金额 MXN 31,816.46 最近打款金额 -MXN 11,734.75'
    )

    def test_money_after_parses_grouped_amount(self):
        self.assertEqual(money_after(self.TEXT, '下次结算金额'), 13610.45)
        self.assertEqual(money_after(self.TEXT, '累计未结算金额'), 31816.46)

    def test_money_after_missing_label_returns_none(self):
        self.assertIsNone(money_after(self.TEXT, '不存在的标签'))

    def test_date_after_expects_iso_day(self):
        self.assertEqual(date_after(self.TEXT, '下次结算金额'), '2026-09-14')
        self.assertIsNone(date_after(self.TEXT, '累计未结算金额'))

    def test_parse_sms_key_from_remark(self):
        remark = ('12095195435----'
                  + m.otp_sms_url('c' * 32))
        self.assertEqual(parse_sms_key(remark), 'c' * 32)
        self.assertIsNone(parse_sms_key('没有接码链接的备注'))

    def test_extract_code_validates_account_tail(self):
        extract = StoreFinanceInspector._extract_code
        sms = ('{"code":200,"data": "[SHEIN]Login verification code: '
               '780542, login account: GS*****90"}')
        self.assertEqual(extract(sms, '90'), '780542')
        self.assertIsNone(extract(sms, '11'))
        self.assertIsNone(extract('{"code":201,"msg":"No data"}', '90'))


class _FakeHub(object):
    """最小 HubStudio 适配替身：无网络、无浏览器。"""

    def __init__(self, serials):
        self._serials = [str(s) for s in serials]
        self.started = []
        self.stopped = []

    def env_list(self):
        return [{
            'containerCode': s,
            'containerName': '店铺' + s,
            'accounts': [{'accountName': 'GS' + s.zfill(4)}],
            'remark': 'x----' + m.otp_sms_url('a' * 32),
        } for s in self._serials]

    def open_container_codes(self):
        return set()

    def browser_start(self, serial, headless=True):
        self.started.append(serial)
        return {'debuggingPort': 0}

    def browser_stop(self, serial):
        self.stopped.append(serial)

    def account_list(self, name):
        return [{'accountName': name, 'accountPassword': 'secret'}]


class BatchStateTests(unittest.TestCase):
    def _inspector(self, serials, inspect_one=None):
        hub = _FakeHub(serials)
        inspector = StoreFinanceInspector(hub, concurrency=2)
        if inspect_one is not None:
            inspector._inspect_one = inspect_one
        return inspector, hub

    def test_batch_completes_all_rows_and_reports_running_flag(self):
        serials = ['11', '12', '13']

        def stub_inspect_one(serial, env, headless):
            time.sleep(0.05)
            row = inspector._base_row(serial, env, 'ok', 'auto')
            inspector._publish(serial, row)
            return row

        inspector, hub = self._inspector(serials, stub_inspect_one)
        inspector.start_batch(serials)
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        self.assertFalse(snap['running'])
        statuses = {r['environmentSerial']: r['status']
                    for r in snap['rows']}
        self.assertEqual(set(statuses.values()), {'ok'})
        self.assertEqual(len(snap['rows']), len(serials))

    def test_stop_marks_remaining_rows_as_stopped(self):
        serials = ['21', '22', '23', '24']

        def stub_inspect_one(serial, env, headless):
            inspector._stop_event.set()  # 第一家跑完即请求停止
            row = inspector._base_row(serial, env, 'ok', 'auto')
            row['durationSeconds'] = 1
            inspector._publish(serial, row)
            return row

        inspector, hub = self._inspector(serials, stub_inspect_one)
        inspector.start_batch(serials)
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        statuses = [r['status'] for r in snap['rows']]
        self.assertEqual(statuses.count('stopped'), 3)
        self.assertEqual(statuses.count('ok'), 1)

    def test_duplicate_start_is_rejected_while_running(self):
        serials = ['31']
        release = threading.Event()

        def stub_inspect_one(serial, env, headless):
            release.wait(2)
            row = inspector._base_row(serial, env, 'ok', 'auto')
            inspector._publish(serial, row)
            return row

        inspector, hub = self._inspector(serials, stub_inspect_one)
        inspector.start_batch(serials)
        try:
            again = inspector.start_batch(serials)
            self.assertTrue(again.get('error'))
        finally:
            release.set()
            inspector.request_stop()


if __name__ == '__main__':
    unittest.main()
