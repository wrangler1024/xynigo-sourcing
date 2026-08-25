# -*- coding: utf-8 -*-
import base64
import csv
from io import BytesIO, StringIO
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from openpyxl import Workbook

os.environ.setdefault(
    'XYNIGO_PROXY_LINK', 'https://proxy.example.test/{region}')

from purchase_tool.env_batch import BatchPlanItem, BuyerAccount, ResumeStateStore
from purchase_tool.main import BackupEnvJob, EnvBatchJob, ledger_tsv_bytes


TEST_TAG = 'MX-Purchase'
TEST_US_TAG = 'US-Purchase'
TEST_PROXY = 'https://proxy.example.test/{region}'


def runtime_config():
    return {'purchaseTag': TEST_TAG, 'proxyLink': TEST_PROXY}


def source_bytes():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        'web@example.com', 'web-secret-pass',
        'https://codes.example.test/get?orderNo=abc123',
        '[{"name":"sid","value":"web-secret-cookie"}]'])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def result_row(site='MX'):
    return SimpleNamespace(
        state='done',
        account=SimpleNamespace(
            email='paste@example.com', password='paste-secret',
            key_url='https://codes.example.test/get?orderNo=paste123',
            order_no='order-paste-001', buyer='新刚',
            cookie_text='[{"name":"sid","value":"paste-cookie"}]'),
        env_name='XG-%s-0820-001' % site,
        serial_number=2001,
        binding_time='2026-08-20 15:30:00')


def tsv_rows(data):
    return list(csv.reader(
        StringIO(data.decode('utf-8-sig')), dialect='excel-tab'))


class LedgerPasteTsvTests(unittest.TestCase):
    def test_mx_tsv_is_headerless_and_matches_unified_table_from_site(self):
        rows = tsv_rows(ledger_tsv_bytes(
            [result_row('MX')], 'MX', '20260820', TEST_TAG))
        self.assertEqual(rows, [[
            'MX', 'paste@example.com', 'paste-secret',
            'https://codes.example.test/get?orderNo=paste123',
            '[{"name":"sid","value":"paste-cookie"}]',
            'order-paste-001', '2026-08-20', '已绑定', 'XG-MX-0820-001',
            TEST_TAG, '2001', '新刚', '2026-08-20 15:30:00']])
        self.assertNotIn('站点', rows[0])

    def test_us_tsv_uses_same_unified_table_order(self):
        rows = tsv_rows(ledger_tsv_bytes(
            [result_row('US')], 'US', '20260820', TEST_US_TAG))
        self.assertEqual(rows, [[
            'US', 'paste@example.com', 'paste-secret',
            'https://codes.example.test/get?orderNo=paste123',
            '[{"name":"sid","value":"paste-cookie"}]',
            'order-paste-001', '2026-08-20', '已绑定', 'XG-US-0820-001',
            TEST_US_TAG, '2001', '新刚', '2026-08-20 15:30:00']])

    def test_tsv_rejects_invalid_purchase_date(self):
        with self.assertRaisesRegex(ValueError, 'YYYYMMDD'):
            ledger_tsv_bytes(
                [result_row('MX')], 'MX', '20260230', TEST_TAG)


class FakeHub(object):
    def __init__(self, create_delay=0.0):
        self.envs = []
        self.groups = [TEST_TAG]
        self.calls = []
        self.lock = threading.Lock()
        self.create_delay = create_delay

    def group_list(self):
        return list(self.groups)

    def env_list(self, tag=None):
        with self.lock:
            self.calls.append(('list', tag))
            return [dict(item) for item in self.envs
                    if not tag or item.get('tagName', TEST_TAG) == tag]

    def env_create(self, body):
        if self.create_delay:
            time.sleep(self.create_delay)
        with self.lock:
            self.calls.append(('create', dict(body)))
            self.envs.append({
                'containerName': body['containerName'],
                'containerCode': '9001', 'serialNumber': 1003,
                'tagName': body['tagName'], 'remark': ''})
        return {}

    def env_import_cookie(self, _code, _cookie_text):
        self.calls.append(('cookie',))
        return {}

    def container_add_account(self, _code, _email, _password, site):
        self.calls.append(('account', site))
        return {}

    def env_update(self, code, _name, remark):
        self.calls.append(('remark',))
        for env in self.envs:
            if env['containerCode'] == str(code):
                env['remark'] = remark
        return {}


class FakeLedgerService(object):
    def __init__(self, preflight_conflicts=0, sync_states=('created',)):
        self.preflight_conflicts = preflight_conflicts
        self.sync_states = list(sync_states)
        self.preflight_calls = []
        self.sync_calls = []

    def preflight_plan(self, rows, site, environment_group):
        self.preflight_calls.append((len(rows), site, environment_group))
        return {
            'total': len(rows),
            'conflicts': self.preflight_conflicts,
            'rows': ([{'state': 'conflict'}]
                     if self.preflight_conflicts else []),
        }

    def sync(self, rows, site, purchase_date, environment_group):
        self.sync_calls.append(
            (len(rows), site, purchase_date, environment_group))
        state = self.sync_states.pop(0) if self.sync_states else 'confirmed'
        counts = {name: int(name == state) * len(rows) for name in (
            'created', 'updated', 'confirmed', 'conflict', 'pending')}
        return {
            'total': len(rows),
            **counts,
            'rows': [{
                'accountId': row.account.account_id,
                'rowNumber': row.account.row_number,
                'emailMasked': row.account.safe_email,
                'site': site,
                'state': state,
                'message': '',
            } for row in rows],
        }


class EnvWebJobTests(unittest.TestCase):
    @staticmethod
    def _other_group_duplicate():
        return {
            'containerName': 'XG-MX-0820-001',
            'containerCode': 'other-1', 'serialNumber': 2001,
            'tagName': 'Other-Purchase',
            'remark': ('邮箱接码:https://redacted | 单号:abc123 | '
                       '采购员:新刚 | 购买:20260820'),
        }

    def test_parse_returns_only_masked_plan(self):
        source = source_bytes()
        job = EnvBatchJob(lambda: FakeHub(), runtime_config)
        parsed = job.parse('vendor.xlsx', base64.b64encode(source).decode('ascii'))
        rendered = json.dumps(parsed, ensure_ascii=False)
        self.assertEqual(parsed['count'], 1)
        self.assertIn('we***@example.com', rendered)
        for secret in ('web@example.com', 'web-secret-pass',
                       'web-secret-cookie', 'codes.example.test'):
            self.assertNotIn(secret, rendered)

    def test_safe_parallel_mode_caps_environment_workers_at_three(self):
        cfg = {
            'purchaseTag': TEST_TAG,
            'proxyLink': TEST_PROXY,
            'envCreateWorkers': 9,
            'safeParallelTasks': True,
        }
        job = EnvBatchJob(lambda: FakeHub(), lambda: cfg)
        backup = BackupEnvJob(lambda: FakeHub(), lambda: cfg)
        self.assertEqual(job._runtime_config()['workers'], 3)
        self.assertEqual(backup._runtime_config()['workers'], 3)

    def test_apply_generates_safe_mapping_and_one_shot_tsv(self):
        source = source_bytes()
        hub = FakeHub()
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse('vendor.xlsx', base64.b64encode(source).decode('ascii'))
        with tempfile.TemporaryDirectory() as tmp:
            def store_factory(batch_id):
                return ResumeStateStore(batch_id, tmp)

            with patch('purchase_tool.main.ResumeStateStore', store_factory):
                job.start(
                    parsed['planId'], '1:新刚', '20260819',
                    verify_sample_count=0, confirm_write=True)
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)
        snap = job.snapshot()
        self.assertFalse(snap['running'])
        self.assertTrue(snap['mappingReady'])
        self.assertTrue(snap['tsvReady'])
        public = json.dumps(snap, ensure_ascii=False)
        for secret in ('web@example.com', 'web-secret-pass',
                       'web-secret-cookie', 'codes.example.test'):
            self.assertNotIn(secret, public)

        mapping, _name = job.mapping_export()
        with zipfile.ZipFile(BytesIO(mapping)) as archive:
            xml = ''.join(
                archive.read(name).decode('utf-8', errors='ignore')
                for name in archive.namelist() if name.endswith('.xml'))
        self.assertIn('web@example.com', xml)
        self.assertNotIn('web-secret-pass', xml)
        self.assertNotIn('web-secret-cookie', xml)

        tsv, name = job.tsv_export()
        text = tsv.decode('utf-8-sig')
        self.assertIn('web-secret-pass', text)
        self.assertIn('web-secret-cookie', text)
        self.assertEqual(len(tsv_rows(tsv)), 1)
        self.assertIn('统一表_MX_20260819_无表头_从站点列开始', name)
        with self.assertRaises(ValueError):
            job.tsv_export()

        create_body = next(call[1] for call in hub.calls
                           if call[0] == 'create')
        self.assertEqual(create_body['tagName'], TEST_TAG)
        self.assertEqual(create_body['linkCode'],
                         'https://proxy.example.test/MX')

    def test_start_requires_explicit_confirmation(self):
        job = EnvBatchJob(lambda: FakeHub(), runtime_config)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaises(ValueError):
            job.start(parsed['planId'], '1:新刚', '20260819')

    def test_resource_conflict_stops_before_plan_consumption_or_hub_write(self):
        hub = FakeHub()
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))

        def reject(_resources):
            raise RuntimeError('目标环境正被物流查询占用')

        with self.assertRaisesRegex(RuntimeError, '目标环境'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True,
                reserve_resources=reject)
        self.assertFalse(any(call[0] == 'create' for call in hub.calls))
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(job.running)

    def test_lark_write_requires_separate_confirmation(self):
        service = FakeLedgerService()
        job = EnvBatchJob(
            lambda: FakeHub(), runtime_config,
            ledger_sync_factory=lambda: service)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '单独二次确认'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True,
                write_lark_ledger=True, confirm_lark_write=False)
        self.assertIn(parsed['planId'], job.pending)
        self.assertEqual(service.preflight_calls, [])

    def test_lark_preflight_conflict_blocks_all_hub_writes(self):
        hub = FakeHub()
        service = FakeLedgerService(preflight_conflicts=1)
        job = EnvBatchJob(
            lambda: hub, runtime_config,
            ledger_sync_factory=lambda: service)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '已阻止建环境'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True,
                write_lark_ledger=True, confirm_lark_write=True)
        self.assertEqual(service.preflight_calls, [(1, 'MX', TEST_TAG)])
        self.assertEqual(service.sync_calls, [])
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

    def test_lark_partial_failure_retries_only_ledger(self):
        hub = FakeHub()
        service = FakeLedgerService(sync_states=('pending', 'confirmed'))
        job = EnvBatchJob(
            lambda: hub, runtime_config,
            ledger_sync_factory=lambda: service)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with tempfile.TemporaryDirectory() as tmp:
            def store_factory(batch_id):
                return ResumeStateStore(batch_id, tmp)

            with patch('purchase_tool.main.ResumeStateStore', store_factory):
                job.start(
                    parsed['planId'], '1:新刚', '20260819',
                    verify_sample_count=0, confirm_write=True,
                    write_lark_ledger=True, confirm_lark_write=True)
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)
                self.assertEqual(job.snapshot()['ledger']['pending'], 1)
                hub_writes = [call for call in hub.calls if call[0] in {
                    'create', 'cookie', 'account', 'remark'}]

                job.retry_ledger(confirm_lark_write=True)
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)

        snap = job.snapshot()
        self.assertEqual(snap['summary']['done'], 1)
        self.assertEqual(snap['ledger']['confirmed'], 1)
        self.assertEqual(len(service.sync_calls), 2)
        self.assertEqual(
            [call for call in hub.calls if call[0] in {
                'create', 'cookie', 'account', 'remark'}], hub_writes)

    def test_lark_retry_filters_confirmed_rows_and_preserves_totals(self):
        service = FakeLedgerService(sync_states=('confirmed',))
        job = EnvBatchJob(
            lambda: FakeHub(), runtime_config,
            ledger_sync_factory=lambda: service)
        rows = []
        for index in (1, 2):
            account = BuyerAccount(
                row_number=index,
                email='buyer%d@example.test' % index,
                password='public-test-password',
                key_url='https://codes.example.test/get?orderNo=order%d' % index,
                cookie_text='',
                order_no='order%d' % index,
                buyer='新刚')
            rows.append(BatchPlanItem(
                account=account,
                env_name='XG-MX-0821-%03d' % index,
                serial_number=3000 + index,
                completed_steps={'created', 'cookie', 'bound', 'remarked'},
                state='done', binding_time='2026-08-21 12:00:00'))
        job.runner = SimpleNamespace(
            rows=rows, site='MX', purchase_date='20260821',
            purchase_tag=TEST_TAG)
        job.ledger_enabled = True
        job.ledger_summary = {
            'enabled': True, 'running': False, 'total': 2,
            'created': 1, 'updated': 0, 'confirmed': 0,
            'conflict': 0, 'pending': 1, 'error': '',
            'rows': [
                {'accountId': rows[0].account.account_id,
                 'rowNumber': 1, 'emailMasked': rows[0].account.safe_email,
                 'site': 'MX', 'state': 'created', 'message': ''},
                {'accountId': rows[1].account.account_id,
                 'rowNumber': 2, 'emailMasked': rows[1].account.safe_email,
                 'site': 'MX', 'state': 'pending', 'message': 'timeout'},
            ],
        }
        job.retry_ledger(confirm_lark_write=True)
        deadline = time.time() + 5
        while job.snapshot()['running'] and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(
            service.sync_calls, [(1, 'MX', '20260821', TEST_TAG)])
        ledger = job.snapshot()['ledger']
        self.assertEqual(ledger['total'], 2)
        self.assertEqual(ledger['created'], 1)
        self.assertEqual(ledger['confirmed'], 1)
        self.assertEqual(ledger['pending'], 0)

    def test_completed_hub_batch_can_supplement_ledger_without_hub_writes(self):
        hub = FakeHub()
        service = FakeLedgerService(sync_states=('created',))
        job = EnvBatchJob(
            lambda: hub, runtime_config,
            ledger_sync_factory=lambda: service)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with tempfile.TemporaryDirectory() as tmp:
            def store_factory(batch_id):
                return ResumeStateStore(batch_id, tmp)

            with patch('purchase_tool.main.ResumeStateStore', store_factory):
                job.start(
                    parsed['planId'], '1:新刚', '20260819',
                    verify_sample_count=0, confirm_write=True,
                    write_lark_ledger=False)
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(
                    job.snapshot()['ledger']['supplementAvailable'])
                hub_writes = [call for call in hub.calls if call[0] in {
                    'create', 'cookie', 'account', 'remark'}]

                with self.assertRaisesRegex(ValueError, '单独二次确认'):
                    job.retry_ledger()
                result = job.retry_ledger(confirm_lark_write=True)
                self.assertEqual(result, {'mode': 'supplement', 'count': 1})
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)

        self.assertEqual(service.preflight_calls, [(1, 'MX', TEST_TAG)])
        self.assertEqual(
            service.sync_calls, [(1, 'MX', '20260819', TEST_TAG)])
        self.assertEqual(job.snapshot()['ledger']['created'], 1)
        self.assertFalse(job.snapshot()['ledger']['supplementAvailable'])
        self.assertEqual(
            [call for call in hub.calls if call[0] in {
                'create', 'cookie', 'account', 'remark'}], hub_writes)

    def test_supplement_preflight_conflict_blocks_lark_and_hub_writes(self):
        hub = FakeHub()
        service = FakeLedgerService(preflight_conflicts=1)
        job = EnvBatchJob(
            lambda: hub, runtime_config,
            ledger_sync_factory=lambda: service)
        row = result_row()
        account = BuyerAccount(
            row_number=1, email=row.account.email,
            password=row.account.password, key_url=row.account.key_url,
            cookie_text=row.account.cookie_text,
            order_no=row.account.order_no, buyer=row.account.buyer)
        job.runner = SimpleNamespace(
            rows=[BatchPlanItem(
                account=account, env_name=row.env_name,
                serial_number=row.serial_number,
                completed_steps={'created', 'cookie', 'bound', 'remarked'},
                state='done', binding_time=row.binding_time)],
            site='MX', purchase_date='20260820', purchase_tag=TEST_TAG)
        before = list(hub.calls)
        with self.assertRaisesRegex(ValueError, '已阻止补写'):
            job.retry_ledger(confirm_lark_write=True)
        self.assertEqual(service.sync_calls, [])
        self.assertEqual(hub.calls, before)
        self.assertFalse(job.ledger_enabled)

    def test_us_site_uses_us_group_proxy_name_and_account_binding(self):
        hub = FakeHub()
        hub.groups = [TEST_US_TAG]
        cfg = {
            'purchaseSite': 'US',
            'purchaseTags': {'MX': TEST_TAG, 'US': TEST_US_TAG},
            'proxyLink': TEST_PROXY,
        }
        job = EnvBatchJob(lambda: hub, lambda: cfg)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        preview = job.preview(
            parsed['planId'], '1:新刚', '20260819', site='US')
        self.assertEqual(preview[0]['envName'], 'XG-US-0819-001')
        with tempfile.TemporaryDirectory() as tmp:
            def store_factory(batch_id):
                return ResumeStateStore(batch_id, tmp)

            with patch('purchase_tool.main.ResumeStateStore', store_factory):
                job.start(
                    parsed['planId'], '1:新刚', '20260819',
                    verify_sample_count=0, confirm_write=True, site='US')
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)
        create_body = next(call[1] for call in hub.calls
                           if call[0] == 'create')
        account_call = next(call for call in hub.calls
                            if call[0] == 'account')
        self.assertEqual(create_body['tagName'], TEST_US_TAG)
        self.assertEqual(
            create_body['linkCode'], 'https://proxy.example.test/US')
        self.assertEqual(account_call[-1], 'US')
        tsv, name = job.tsv_export()
        row = tsv_rows(tsv)[0]
        self.assertIn('统一表_US_20260819_无表头_从站点列开始', name)
        self.assertEqual(row[0], 'US')
        self.assertEqual(row[4],
                         '[{"name":"sid","value":"web-secret-cookie"}]')
        self.assertEqual(
            row[3], 'https://codes.example.test/get?orderNo=abc123')
        self.assertEqual(row[-2], '新刚')

    def test_preflight_failure_preserves_plan_and_makes_zero_writes(self):
        source = source_bytes()
        hub = FakeHub()
        cfg = {'purchaseTag': TEST_TAG, 'proxyLink': TEST_PROXY}
        job = EnvBatchJob(lambda: hub, lambda: cfg)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source).decode('ascii'))

        hub.groups = []
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True)
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

        hub.groups = [TEST_TAG]
        # 空代理链接现回落内置默认（前期写死），用非法自定义链接验证代理阻断仍零写入
        cfg['proxyLink'] = 'https://proxy.example.test/{bad}'
        with self.assertRaisesRegex(ValueError, '占位符'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True)
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

        cfg['proxyLink'] = TEST_PROXY
        with tempfile.TemporaryDirectory() as tmp:
            def store_factory(batch_id):
                return ResumeStateStore(batch_id, tmp)

            with patch('purchase_tool.main.ResumeStateStore', store_factory):
                job.start(
                    parsed['planId'], '1:新刚', '20260819',
                    verify_sample_count=0, confirm_write=True)
                deadline = time.time() + 5
                while job.snapshot()['running'] and time.time() < deadline:
                    time.sleep(0.05)
        self.assertNotIn(parsed['planId'], job.pending)
        self.assertEqual(job.snapshot()['summary']['done'], 1)

    def test_missing_group_blocks_preview_before_env_list(self):
        hub = FakeHub()
        hub.groups = []
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse(
            'vendor.xlsx',
            base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            job.preview(parsed['planId'], '1:新刚', '20260819')
        self.assertFalse(any(call[0] == 'list' for call in hub.calls))
        self.assertIn(parsed['planId'], job.pending)

    def test_cross_group_duplicate_blocks_preview_and_preserves_plan(self):
        hub = FakeHub()
        hub.envs.append(self._other_group_duplicate())
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse(
            'vendor.xlsx',
            base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '其他分组'):
            job.preview(parsed['planId'], '1:新刚', '20260820')
        self.assertIn(parsed['planId'], job.pending)
        self.assertIn(('list', None), hub.calls)
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

    def test_cross_group_duplicate_blocks_start_before_plan_consumption(self):
        hub = FakeHub()
        hub.envs.append(self._other_group_duplicate())
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse(
            'vendor.xlsx',
            base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '其他分组'):
            job.start(
                parsed['planId'], '1:新刚', '20260820',
                verify_sample_count=0, confirm_write=True)
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(job.snapshot()['running'])
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

    def test_stale_sensitive_cleanup_cannot_clear_current_batch(self):
        job = EnvBatchJob(lambda: FakeHub(), runtime_config)
        old_account = SimpleNamespace(
            password='old-password', key_url='old-key',
            cookie_text='old-cookie')
        new_account = SimpleNamespace(
            password='new-password', key_url='new-key',
            cookie_text='new-cookie')
        old_runner = SimpleNamespace(rows=[SimpleNamespace(account=old_account)])
        new_runner = SimpleNamespace(rows=[SimpleNamespace(account=new_account)])

        with job.lock:
            job.runner = old_runner
            job.tsv_data = b'old'
        job._schedule_sensitive_cleanup(old_runner)
        stale_generation = job._sensitive_generation
        with job.lock:
            job._cancel_sensitive_cleanup_locked()
            job._wipe_runner_credentials(job.runner)
            job.runner = new_runner
            job.tsv_data = b'new'

        job._clear_sensitive(stale_generation, old_runner)
        self.assertEqual(job.tsv_data, b'new')
        self.assertEqual(new_account.password, 'new-password')
        self.assertEqual(old_account.password, '')


class BackupEnvJobTests(unittest.TestCase):
    def test_backup_preview_start_and_result_flow(self):
        hub = FakeHub()
        job = BackupEnvJob(lambda: hub, runtime_config)
        preview = job.preview('新刚', 2, '备用', '20260819')
        self.assertEqual(preview['names'],
                         ['XG-MX-0819-001', 'XG-MX-0819-002'])
        self.assertEqual(preview['remark'], '备用环境')
        self.assertEqual(preview['buyerCode'], 'XG')
        with self.assertRaises(ValueError):
            job.start('新刚', 2, '备用', '20260819')
        with self.assertRaisesRegex(ValueError, '1-25'):
            job.preview('新刚', 26, '备用', '20260819')
        with self.assertRaisesRegex(ValueError, '不在名单内'):
            job.preview('甲', 1, '备用', '20260819')
        job.start('新刚', 2, '备用', '20260819',
                  verify_sample_count=0, confirm_write=True)
        deadline = time.time() + 5
        while job.snapshot()['running'] and time.time() < deadline:
            time.sleep(0.05)
        snap = job.snapshot()
        self.assertEqual(snap['summary']['done'], 2)
        self.assertTrue(snap['resultReady'])
        # 备用模式零 Cookie、零绑号
        self.assertFalse(any(call[0] in {'cookie', 'account'}
                             for call in hub.calls))
        self.assertEqual(
            {env['containerName'] for env in hub.envs},
            {'XG-MX-0819-001', 'XG-MX-0819-002'})
        data, _name = job.result_export()
        text = data.decode('utf-8-sig')
        self.assertIn('XG-MX-0819-001', text)
        self.assertIn('环境名', text)
        self.assertNotIn(TEST_PROXY, text)
        self.assertNotIn('proxy.example.test', text)

    def test_backup_preflight_blocks_before_any_write(self):
        hub = FakeHub()
        hub.groups = []
        job = BackupEnvJob(lambda: hub, runtime_config)
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            job.start('新刚', 1, '备用', '20260819', confirm_write=True)
        self.assertFalse(any(call[0] in {'create', 'remark'}
                             for call in hub.calls))
        self.assertFalse(job.snapshot()['running'])
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            job.preview('新刚', 1, '备用', '20260819')

    def test_start_blocks_site_mismatch_before_consuming_plan(self):
        # 正式执行同步校验：US Cookie 账号选 MX 站 → 消费计划前整批拒收
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([
            'us@example.com', 'web-secret-pass',
            'https://codes.example.test/get?orderNo=abc123',
            '[{"name":"sid","domain":".us.shein.com"}]'])
        output = BytesIO()
        workbook.save(output)
        hub = FakeHub()
        job = EnvBatchJob(lambda: hub, runtime_config)
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(output.getvalue()).decode('ascii'))
        with self.assertRaisesRegex(ValueError, '不一致'):
            job.start(
                parsed['planId'], '1:新刚', '20260819',
                verify_sample_count=0, confirm_write=True, site='MX')
        self.assertIn(parsed['planId'], job.pending)   # 计划保留可改站重跑
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

    def test_backup_running_snapshot_has_rows_and_elapsed(self):
        hub = FakeHub(create_delay=0.25)
        job = BackupEnvJob(lambda: hub, runtime_config)
        job.start('新刚', 3, '备用', '20260819',
                  verify_sample_count=0, confirm_write=True)
        saw_running_with_rows = False
        deadline = time.time() + 5
        while time.time() < deadline:
            snap = job.snapshot()
            if snap['running'] and snap['rows']:
                saw_running_with_rows = True
                self.assertEqual(len(snap['rows']), 3)
            if not snap['running']:
                break
            time.sleep(0.05)
        self.assertTrue(saw_running_with_rows)
        final = job.snapshot()
        self.assertFalse(final['running'])
        self.assertEqual(final['summary']['done'], 3)

    def test_backup_uses_builtin_default_proxy_when_unset(self):
        from purchase_tool.env_batch import DEFAULT_PROXY_LINK
        hub = FakeHub()
        cfg = {'purchaseTags': {'MX': TEST_TAG}}   # 未配置 proxyLink
        job = BackupEnvJob(lambda: hub, lambda: cfg)
        job.start('新刚', 1, '备用', '20260819',
                  verify_sample_count=0, confirm_write=True)
        deadline = time.time() + 5
        while job.snapshot()['running'] and time.time() < deadline:
            time.sleep(0.05)
        create_body = next(call[1] for call in hub.calls
                           if call[0] == 'create')
        self.assertEqual(create_body['linkCode'],
                         DEFAULT_PROXY_LINK.replace('{region}', 'MX'))
        self.assertNotIn('{region}', create_body['linkCode'])

    def test_backup_us_site_uses_us_group_and_proxy_region(self):
        hub = FakeHub()
        hub.groups = [TEST_US_TAG]
        cfg = {
            'purchaseSite': 'US',
            'purchaseTags': {'MX': TEST_TAG, 'US': TEST_US_TAG},
            'proxyLink': TEST_PROXY,
        }
        job = BackupEnvJob(lambda: hub, lambda: cfg)
        job.start('志恒', 1, '测试', '20260819',
                  verify_sample_count=0, confirm_write=True, site='US')
        deadline = time.time() + 5
        while job.snapshot()['running'] and time.time() < deadline:
            time.sleep(0.05)
        create_body = next(call[1] for call in hub.calls
                           if call[0] == 'create')
        self.assertEqual(create_body['tagName'], TEST_US_TAG)
        self.assertEqual(create_body['linkCode'],
                         'https://proxy.example.test/US')
        self.assertEqual(hub.envs[0]['containerName'], 'ZH-US-测试-01')
        self.assertEqual(hub.envs[0]['remark'], '测试环境')


if __name__ == '__main__':
    unittest.main()
