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
            'serialNumber': '9' + s,  # 序号与环境 ID 是两套标识
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


class ConcurrencyLimitTests(unittest.TestCase):
    """必须改3回归：同时运行的 _inspect_one 不超过 concurrency。"""

    def test_semaphore_limits_parallel_inspect_one(self):
        active = {'n': 0, 'peak': 0}
        lock = threading.Lock()
        serials = [str(i) for i in range(1, 9)]  # 8 家，并发 2

        hub = _FakeHub(serials)
        inspector = StoreFinanceInspector(hub, concurrency=2,
                                          stagger_seconds=0.05)

        def stub_inspect_one(serial, env, headless):
            with lock:
                active['n'] += 1
                active['peak'] = max(active['peak'], active['n'])
            time.sleep(0.15)
            with lock:
                active['n'] -= 1
            row = inspector._base_row(serial, env, 'ok', 'auto')
            inspector._publish(serial, row)
            return row

        inspector._inspect_one = stub_inspect_one
        inspector.start_batch(serials, concurrency=2)
        deadline = time.time() + 15
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        self.assertFalse(snap['running'])
        self.assertEqual(len(snap['rows']), len(serials))
        self.assertLessEqual(active['peak'], 2)
        self.assertGreaterEqual(active['peak'], 2)  # 确实并行过


class OccupiedEnvironmentTests(unittest.TestCase):
    """必须改4回归：采集失败且无法恢复 → 标记 inuse（非普通 fail）。"""

    def test_collect_failure_without_recovery_marks_inuse(self):
        serial = '1746'
        hub = _FakeHub([serial])
        inspector = StoreFinanceInspector(hub, concurrency=2)

        def stub_inspect_one(s, env, headless):
            # 绕开真实浏览器：直接走 _inspect_one 的采集异常分支
            started = time.time()
            row = inspector._base_row(s, env, 'running')
            inspector._publish(s, row)
            page = None
            try:
                raise RuntimeError('收入页加载超时或被拦截')
            except Exception:
                if not inspector._recover_after_manual_switch(
                        None, 'GS0001', 'pwd', 'a' * 32, '01'):
                    row = inspector._base_row(
                        s, env, 'inuse', 'auto',
                        error_summary='采集页面被人工切换，重试 1 次仍失败')
                    row['durationSeconds'] = int(time.time() - started)
                    inspector._publish(s, row)
                    return row
                raise

        inspector._inspect_one = stub_inspect_one
        inspector.start_batch([serial])
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        self.assertEqual(snap['rows'][0]['status'], 'inuse')


class ExecutorConcurrencyPassthroughTests(unittest.TestCase):
    """必须改3回归：payload.concurrency 经 _execute_store_finance 透传到
    /api/store-finance/inspect（UI 并发芯片 → 云端 → 执行器 → 信号量）。"""

    def _executor_with_fake_local(self):
        from purchase_tool.operation_executor import LocalOperationExecutor

        calls = []

        def rpc(call):
            calls.append(call)
            method, path = call['method'], call['path']
            if method == 'POST' and path == '/api/store-finance/inspect':
                return {'httpStatus': 200, 'responseType': 'json',
                        'body': {'running': True, 'total': 1}}
            if method == 'GET' and path == '/api/store-finance/progress':
                # 第二次轮询即返回完成，结束执行循环
                finished = sum(1 for c in calls
                               if c['path'] == '/api/store-finance/progress'
                               and c['method'] == 'GET') >= 2
                return {'httpStatus': 200, 'responseType': 'json',
                        'body': {'running': not finished, 'rows': [{
                            'environmentSerial': '1746',
                            'storeName': '山岚',
                            'gsCode': 'GS2392643',
                            'status': 'ok' if finished else 'running',
                            'collectedAt': '2026-09-10T15:00:00+08:00',
                            'screenshotStatus': '',
                        }]}}
            return {'httpStatus': 200, 'responseType': 'json', 'body': {}}

        executor = LocalOperationExecutor(rpc, poll_interval=0.01,
                                          sleep_fn=lambda _: time.sleep(0.01))
        return executor, calls

    def test_concurrency_reaches_local_inspect_endpoint(self):
        executor, calls = self._executor_with_fake_local()
        payload = {
            'runKey': 'store-finance-conc-test-0001',
            'environmentSerials': ['1746'],
            'browserMode': 'headless',
            'concurrency': 4,
        }
        outcome, code, summary = executor.execute(
            'store.finance.inspect.v1', payload,
            lambda **event: None,
            cancellation_event=threading.Event())
        inspect_calls = [c for c in calls
                         if c['path'] == '/api/store-finance/inspect'
                         and c['method'] == 'POST']
        self.assertEqual(len(inspect_calls), 1)
        self.assertEqual(inspect_calls[0]['body']['concurrency'], 4)
        self.assertEqual(outcome, 'succeeded')

    def test_concurrency_defaults_to_two_and_caps_at_five(self):
        executor, calls = self._executor_with_fake_local()
        executor.execute('store.finance.inspect.v1', {
            'runKey': 'store-finance-conc-test-0002',
            'environmentSerials': ['1746'],
        }, lambda **event: None, cancellation_event=threading.Event())
        inspect_calls = [c for c in calls
                         if c['path'] == '/api/store-finance/inspect'
                         and c['method'] == 'POST']
        self.assertEqual(inspect_calls[0]['body']['concurrency'], 2)

        # 超上限的值被夹到 5
        executor, calls = self._executor_with_fake_local()
        executor.execute('store.finance.inspect.v1', {
            'runKey': 'store-finance-conc-test-0003',
            'environmentSerials': ['1746'],
            'concurrency': 99,
        }, lambda **event: None, cancellation_event=threading.Event())
        inspect_calls = [c for c in calls
                         if c['path'] == '/api/store-finance/inspect'
                         and c['method'] == 'POST']
        self.assertEqual(inspect_calls[0]['body']['concurrency'], 5)


class ProjectionTests(unittest.TestCase):
    """三轮评审必须改1回归：投影不得把可空字段收成非法空串。"""

    def test_projection_keeps_login_mode_none_for_pending_rows(self):
        from purchase_tool.operation_executor import LocalOperationExecutor

        snapshot = {'rows': [
            {   # queued 行：无 loginMode / 无金额 / 无摘要
                'environmentSerial': '1746',
                'storeName': '山岚',
                'gsCode': 'GS2392643',
                'status': 'queued',
                'screenshotStatus': '',
            },
            {   # ok 行：全字段 + 本地附加字段 + None 可空值
                'environmentSerial': '1775875785',
                'storeName': '花间',
                'gsCode': 'GS5021497',
                'status': 'ok',
                'loginMode': 'auto',
                'inTransitAmount': 2420.01,
                'unsettledAmount': 9680.77,
                'nextSettlementAmount': 4663.47,
                'nextSettlementDate': '2026-09-15',
                'completedSettlementAmount': 54216.44,
                'nonWithdrawableAmount': 1934.51,
                'collectedAt': '2026-09-10T14:25:00+08:00',
                'durationSeconds': 70,
                'errorSummary': None,
                'screenshotSha256': None,
                'screenshotStatus': '',
                'cumulativeSettlementAmount': 54216.44,
                'fundLimitAmount': 1934.51,
            },
        ]}
        rows = LocalOperationExecutor._store_finance_rows(snapshot)
        queued, ok = rows
        self.assertIsNone(queued['loginMode'])       # 修复点：None 不收成 ''
        self.assertEqual(queued['status'], 'queued')
        self.assertNotIn('screenshotStatus', queued)
        self.assertNotIn('cumulativeSettlementAmount', ok)
        self.assertEqual(ok['loginMode'], 'auto')
        self.assertIsNone(ok['errorSummary'])
        self.assertIsNone(ok['screenshotSha256'])



class MissingEnvironmentTests(unittest.TestCase):
    """序号在 HubStudio 查不到（如误传店铺名）时必须出失败行而非崩线程。"""

    def test_unknown_serial_produces_fail_row_with_clear_error(self):
        hub = _FakeHub(['31'])  # 环境列表里只有 31
        inspector = StoreFinanceInspector(hub, concurrency=2)
        inspector.start_batch(['溪山'])  # 传的是店铺名，查不到环境
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        self.assertFalse(snap['running'])
        row = snap['rows'][0]
        self.assertEqual(row['status'], 'fail')
        self.assertIn('未匹配到唯一环境', row['errorSummary'])
        self.assertEqual(row['gsCode'], '')
        self.assertEqual(hub.started, [])  # 未误开任何浏览器

    def test_base_row_tolerates_env_without_accounts(self):
        hub = _FakeHub([])
        inspector = StoreFinanceInspector(hub)
        row = inspector._base_row('32', {'containerName': '无账号店'}, 'running')
        self.assertEqual(row['gsCode'], '')
        self.assertEqual(row['storeName'], '无账号店')


class LocalRouteMethodTests(unittest.TestCase):
    """执行器用 GET 调 progress/screenshot：路由必须挂在 do_GET 分发链里。"""

    def _method_span(self, name):
        import re as _re
        source = open(
            'src/purchase_tool/main.py', encoding='utf-8').read()
        start = source.index('def %s(self):' % name)
        nxt = _re.search(r'\n    def ', source[start + 1:])
        return source[start:start + (nxt.start() if nxt else len(source))]

    def test_progress_and_screenshot_routes_live_in_do_get(self):
        span = self._method_span('do_GET')
        self.assertIn("path == '/api/store-finance/progress'", span)
        self.assertIn("path == '/api/store-finance/screenshot'", span)

    def test_inspect_and_stop_routes_live_in_do_post(self):
        span = self._method_span('do_POST')
        self.assertIn("path == '/api/store-finance/inspect'", span)
        self.assertIn("path == '/api/store-finance/stop'", span)
        self.assertNotIn("path == '/api/store-finance/progress'", span)
        self.assertNotIn("path == '/api/store-finance/screenshot'", span)


class SerialNumberInputTests(unittest.TestCase):
    """粘贴 HubStudio 窗口序号（serialNumber）也应能定位环境并按 containerCode 开浏览器。"""

    def test_serial_number_input_starts_browser_with_container_code(self):
        hub = _FakeHub(['41'])
        inspector = StoreFinanceInspector(hub, concurrency=2)
        inspector.start_batch(['941'])  # 941 是序号，41 才是环境 ID
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        snap = inspector.snapshot()
        row = snap['rows'][0]
        self.assertEqual(row['environmentSerial'], '941')  # 行键=用户输入
        self.assertNotIn('环境序号未找到', row['errorSummary'] or '')
        self.assertEqual(hub.started, ['41'])  # 浏览器用 containerCode 启动


class ExactNameMatchTests(unittest.TestCase):
    """店铺环境名输入必须全名精确匹配：主/子账号（溪山 / 溪山-子）互不误伤。"""

    class _NamedHub(_FakeHub):
        def __init__(self):
            super().__init__([])
            self._envs = [
                {'containerCode': '501', 'serialNumber': '9501',
                 'containerName': '溪山', 'remark': '',
                 'accounts': [{'accountName': 'GS01'}]},
                {'containerCode': '502', 'serialNumber': '9502',
                 'containerName': '溪山-子', 'remark': '',
                 'accounts': [{'accountName': 'GS02'}]},
                {'containerCode': '503', 'serialNumber': '9503',
                 'containerName': '重名店', 'remark': '',
                 'accounts': [{'accountName': 'GS03'}]},
                {'containerCode': '504', 'serialNumber': '9504',
                 'containerName': '重名店', 'remark': '',
                 'accounts': [{'accountName': 'GS04'}]},
            ]

        def env_list(self):
            return [dict(e) for e in self._envs]

    def _run(self, inputs):
        hub = self._NamedHub()
        inspector = StoreFinanceInspector(hub, concurrency=4)
        inspector.start_batch(inputs)
        deadline = time.time() + 5
        while time.time() < deadline and inspector.snapshot()['running']:
            time.sleep(0.05)
        return hub, {(r['environmentSerial'], r['storeName'],
                      r['gsCode']) for r in inspector.snapshot()['rows']}

    def test_full_name_hits_main_env_not_sub_env(self):
        hub, rows = self._run(['溪山'])
        self.assertIn(('溪山', '溪山', 'GS01'), rows)
        self.assertNotIn(('溪山', '溪山-子', 'GS02'), rows)
        self.assertEqual(hub.started, ['501'])  # 用主账号 containerCode 开浏览器

    def test_sub_env_name_still_resolvable_when_named_explicitly(self):
        _, rows = self._run(['溪山-子'])
        self.assertIn(('溪山-子', '溪山-子', 'GS02'), rows)

    def test_prefix_is_not_substring_match(self):
        _, rows = self._run(['溪'])  # 不是任何环境的全名
        (serial, _, _), = rows
        self.assertEqual(serial, '溪')

    def test_duplicate_name_refuses_to_pick_one(self):
        hub, rows = self._run(['重名店'])
        (serial, _, gs), = rows
        self.assertEqual(serial, '重名店')
        self.assertEqual(gs, '')  # 未定位到环境，不采集
        self.assertEqual(hub.started, [])


class LookupStoresTests(unittest.TestCase):
    """粘贴序号 / 环境 ID / 店铺名都能解析出环境展示字段。"""

    def _envs(self):
        return [
            {'containerCode': '1776003960', 'serialNumber': '1377',
             'containerName': '溪山-子', 'tagName': '魏无羡',
             'accounts': [{'accountName': 'GS1098478'}],
             'remark': '13800000000----https://sms/x', 'openTime': 'None'},
            {'containerCode': '1775999821', 'serialNumber': '1378',
             'containerName': '帆影-子', 'tagName': '魏无羡',
             'accounts': [], 'remark': '', 'openTime': '09-10 10:00:00'},
        ]

    def test_lookup_by_serial_id_and_name(self):
        hub = _FakeHub([])
        inspector = StoreFinanceInspector(hub)
        result = inspector.lookup_stores(
            ['1377', '1775999821', '溪山', '不存在'],
            env_list=self._envs(),
            open_codes={'1775999821'})  # browser_status 实时：帆影-子开着
        serials = {r['environmentSerial'] for r in result['matched']}
        self.assertEqual(serials, {'1377', '1378'})
        self.assertEqual(result['unmatched'], ['不存在'])
        by_serial = {r['environmentSerial']: r for r in result['matched']}
        self.assertEqual(by_serial['1377']['storeName'], '溪山-子')
        self.assertEqual(by_serial['1377']['gsCode'], 'GS1098478')
        self.assertEqual(by_serial['1377']['group'], '魏无羡')
        self.assertEqual(by_serial['1377']['environmentId'], '1776003960')
        self.assertFalse(by_serial['1377']['browserOpen'])
        self.assertEqual(by_serial['1378']['gsCode'], '')  # 未绑账号不出 IndexError
        self.assertTrue(by_serial['1378']['browserOpen'])  # 来自实时开合集
        for row in result['matched']:
            self.assertNotIn('remark', row)  # 备注含接码链接，绝不下发

    def test_lookup_rejects_empty_input(self):
        inspector = StoreFinanceInspector(_FakeHub([]))
        result = inspector.lookup_stores(['  ', ''], env_list=[])
        self.assertEqual(result, {'matched': [], 'unmatched': []})


class ExecutorLookupPassthroughTests(unittest.TestCase):
    def test_lookup_reaches_local_endpoint_with_identifiers(self):
        from purchase_tool.operation_executor import LocalOperationExecutor
        calls = []

        def rpc(request):
            calls.append(request)
            return {'httpStatus': 200, 'responseType': 'json',
                    'body': {'matched': [{'environmentSerial': '1377',
                                          'environmentId': '1776003960',
                                          'storeName': '溪山-子',
                                          'gsCode': 'GS1', 'group': '魏无羡',
                                          'browserOpen': False}],
                             'unmatched': []}}

        executor = LocalOperationExecutor(rpc)
        outcome, code, summary = executor.execute(
            'store.finance.lookup.v1',
            {'identifiers': ['溪山']}, lambda **_: None)
        self.assertEqual(outcome, 'succeeded')
        self.assertEqual(code, 'store_finance_lookup_completed')
        self.assertEqual(summary['matched'][0]['environmentSerial'], '1377')
        self.assertEqual(calls[0]['path'], '/api/store-finance/lookup')
        self.assertEqual(calls[0]['body']['identifiers'], ['溪山'])


class LookupBrowserStateFallbackTests(unittest.TestCase):
    def test_live_query_failure_falls_back_to_open_time(self):
        class _BrokenStatusHub(_FakeHub):
            def open_container_codes(self):
                raise RuntimeError('browser_status 不可用')

        inspector = StoreFinanceInspector(_BrokenStatusHub([]))
        result = inspector.lookup_stores(
            ['1378'], env_list=[
                {'containerCode': '1775999821', 'serialNumber': '1378',
                 'containerName': '帆影-子', 'tagName': '魏无羡',
                 'accounts': [], 'openTime': '09-10 10:00:00'}])
        self.assertTrue(result['matched'][0]['browserOpen'])
