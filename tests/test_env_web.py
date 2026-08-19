# -*- coding: utf-8 -*-
import base64
from io import BytesIO
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

from purchase_tool.env_batch import ResumeStateStore
from purchase_tool.main import BackupEnvJob, EnvBatchJob


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

        tsv, _name = job.tsv_export()
        text = tsv.decode('utf-8-sig')
        self.assertIn('web-secret-pass', text)
        self.assertIn('web-secret-cookie', text)
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
