# -*- coding: utf-8 -*-
from io import BytesIO
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
import zipfile

from openpyxl import Workbook

os.environ.setdefault(
    'XYNIGO_PROXY_LINK', 'https://proxy.example.test/{region}')

from purchase_tool.env_batch import (
    BatchEnvOrchestrator, EnvBatchError, RES_POOL, ResumeStateStore,
    batch_fingerprint, build_batch_plan, build_env_create_body,
    choose_resolution, format_remark, load_vendor_xlsx,
    mapping_workbook_bytes, parse_vendor_workbook)
from purchase_tool.hub_api import HubStudioApi


def workbook_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def demo_rows():
    return [
        ['alpha@example.com', 'secret-pass-a',
         'https://codes.example.test/get?orderNo=a1b2c3',
         '[{"name":"sid","value":"secret-cookie-a"}]'],
        ['beta@example.com', 'secret-pass-b',
         'https://codes.example.test/get?orderNo=d4e5f6',
         '[{"name":"sid","value":"secret-cookie-b"}]'],
    ]


class FakeHub(object):
    def __init__(self, fail_cookie_once=False):
        self.envs = []
        self.calls = []
        self.fail_cookie_once = fail_cookie_once

    def env_list(self, _tag):
        return [dict(item) for item in self.envs]

    def env_create(self, body):
        self.calls.append(('create', body['containerName']))
        self.envs.append({
            'containerName': body['containerName'],
            'containerCode': str(9000 + len(self.envs)),
            'serialNumber': 1100 + len(self.envs),
            'remark': '',
        })
        return {}

    def env_import_cookie(self, code, cookie_text):
        self.calls.append(('cookie', str(code), cookie_text))
        if self.fail_cookie_once:
            self.fail_cookie_once = False
            raise RuntimeError('cookie import failed')
        return {}

    def container_add_account(self, code, email, password, site):
        self.calls.append(('account', str(code), email, password, site))
        return {}

    def env_update(self, code, name, remark):
        self.calls.append(('remark', str(code), name, remark))
        for env in self.envs:
            if str(env['containerCode']) == str(code):
                env['remark'] = remark
        return {}


class EnvBatchTests(unittest.TestCase):
    def test_repository_sample_matches_contract(self):
        repo = Path(__file__).resolve().parents[1]
        sample = repo / 'examples' / 'buyer-import-template.xlsx'
        _path, accounts = load_vendor_xlsx(sample, allow_repo_sample=True)
        self.assertEqual(len(accounts), 5)
        self.assertTrue(all(account.cookie_text.startswith('[')
                            for account in accounts))
        self.assertTrue(all(account.order_no for account in accounts))

    def test_strict_parser_rejects_header_and_extra_business_column(self):
        with self.assertRaises(EnvBatchError):
            parse_vendor_workbook(BytesIO(workbook_bytes([
                ['邮箱', '密码', '接码Key', 'Cookie']
            ])))
        row = demo_rows()[0] + ['unexpected']
        with self.assertRaises(EnvBatchError):
            parse_vendor_workbook(BytesIO(workbook_bytes([row])))

    def test_parser_preserves_cookie_string_exactly(self):
        cookie = ' [ {"name": "sid", "value": "abc"} ] '
        row = ['exact@example.com', 'pass',
               'https://codes.example.test/?orderNo=abcdef', cookie]
        account = parse_vendor_workbook(
            BytesIO(workbook_bytes([row])))[0]
        self.assertEqual(account.cookie_text, cookie)

    def test_assignment_and_daily_continuation_are_per_buyer(self):
        accounts = parse_vendor_workbook(BytesIO(workbook_bytes(demo_rows())))
        existing = [
            {'containerName': '采购-甲-MX-0819-003'},
            {'containerName': '采购-乙-MX-0819-009'},
        ]
        plan = build_batch_plan(
            accounts, '1:甲,1:乙', existing, purchase_date='20260819')
        self.assertEqual(plan[0].env_name, '采购-甲-MX-0819-004')
        self.assertEqual(plan[1].env_name, '采购-乙-MX-0819-010')

    def test_existing_order_remark_makes_completed_rerun_idempotent(self):
        accounts = parse_vendor_workbook(BytesIO(workbook_bytes(demo_rows()[:1])))
        existing = [{
            'containerName': '采购-甲-MX-0819-007',
            'containerCode': '7007', 'serialNumber': 1006,
            'remark': '邮箱接码:https://redacted | 单号:a1b2c3 | 采购员:甲 | 购买:20260819',
        }]
        plan = build_batch_plan(
            accounts, '1:甲', existing, purchase_date='20260819')
        self.assertEqual(plan[0].state, 'done')
        self.assertEqual(plan[0].env_name, '采购-甲-MX-0819-007')
        self.assertEqual(plan[0].completed_steps,
                         {'env_created', 'cookie_imported', 'account_bound',
                          'remarked', 'done'})

    def test_resolution_pool_tracks_weights_and_caps_size(self):
        rng = random.Random(20260819)
        counts = {pair[:2]: 0 for pair in RES_POOL}
        for _index in range(10000):
            resolution = choose_resolution(rng)
            counts[resolution] += 1
            self.assertLessEqual(resolution[0], 2560)
            self.assertLessEqual(resolution[1], 1440)
        for width, height, weight in RES_POOL:
            observed = counts[(width, height)] / 10000.0
            self.assertLess(abs(observed - weight), 0.025)
        body = build_env_create_body(
            '采购-甲-MX-0819-001', rng=rng,
            proxy_link='https://proxy.example.test/{region}')
        self.assertEqual(body['coreVersion'], 148)
        self.assertEqual(body['advancedBo']['languageType'], 0)

    def test_hub_cookie_import_requires_and_preserves_string(self):
        api = HubStudioApi()
        calls = []
        api._post = lambda path, body: calls.append((path, body)) or {}
        cookie = '[{"name":"sid","value":"unchanged"}]'
        api.env_import_cookie(123, cookie)
        self.assertEqual(calls[0], (
            '/env/import-cookie',
            {'containerCode': '123', 'cookie': cookie}))
        with self.assertRaises(TypeError):
            api.env_import_cookie(123, [])

    def test_failed_step_resumes_without_repeating_completed_steps(self):
        source = workbook_bytes(demo_rows()[:1])
        accounts = parse_vendor_workbook(BytesIO(source))
        hub = FakeHub(fail_cookie_once=True)
        with tempfile.TemporaryDirectory() as tmp:
            store = ResumeStateStore(
                batch_fingerprint(source, '1:甲', 'MX', '20260819'), tmp)
            first = BatchEnvOrchestrator(
                hub, purchase_date='20260819', state_store=store,
                sleep_fn=lambda _seconds: None)
            first.prepare(accounts, '1:甲')
            first.run()
            self.assertEqual(first.rows[0].state, 'failed')
            self.assertEqual(first.rows[0].error_step, 'cookie_imported')
            self.assertTrue(store.path.exists())
            state_text = store.path.read_text(encoding='utf-8')
            for secret in ('secret-pass-a', 'secret-cookie-a',
                           'codes.example.test', 'alpha@example.com'):
                self.assertNotIn(secret, state_text)
            if os.name != 'nt':
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

            second = BatchEnvOrchestrator(
                hub, purchase_date='20260819', state_store=store,
                sleep_fn=lambda _seconds: None)
            second.prepare(accounts, '1:甲')
            second.run()
            self.assertEqual(second.rows[0].state, 'done')
            self.assertEqual(sum(call[0] == 'create' for call in hub.calls), 1)
            self.assertEqual(sum(call[0] == 'cookie' for call in hub.calls), 2)
            self.assertEqual(sum(call[0] == 'account' for call in hub.calls), 1)
            self.assertEqual(sum(call[0] == 'remark' for call in hub.calls), 1)
            self.assertFalse(store.path.exists())

    def test_remark_and_mapping_export_contract(self):
        accounts = parse_vendor_workbook(BytesIO(workbook_bytes(demo_rows()[:1])))
        plan = build_batch_plan(
            accounts, '1:甲', purchase_date='20260819')
        row = plan[0]
        row.serial_number = 1004
        row.binding_time = '2026-08-19 10:00:00'
        row.state = 'done'
        remark = format_remark(row.account, '20260819')
        self.assertIn('邮箱接码:', remark)
        self.assertIn('单号:a1b2c3', remark)
        self.assertIn('采购员:甲', remark)
        self.assertIn('购买:20260819', remark)

        output = mapping_workbook_bytes(plan)
        with zipfile.ZipFile(BytesIO(output)) as archive:
            text = ''.join(
                archive.read(name).decode('utf-8', errors='ignore')
                for name in archive.namelist() if name.endswith('.xml'))
        self.assertIn('绑定映射清单', text)
        self.assertIn('alpha@example.com', text)
        for secret in ('secret-pass-a', 'secret-cookie-a',
                       'codes.example.test'):
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
