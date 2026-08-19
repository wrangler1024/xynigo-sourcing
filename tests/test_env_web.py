# -*- coding: utf-8 -*-
import base64
from io import BytesIO
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

from openpyxl import Workbook

os.environ.setdefault(
    'XYNIGO_PROXY_LINK', 'https://proxy.example.test/{region}')

from purchase_tool.env_batch import ResumeStateStore
from purchase_tool.main import EnvBatchJob


TEST_TAG = 'MX-Purchase'
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
    def __init__(self):
        self.envs = []
        self.groups = [TEST_TAG]
        self.calls = []

    def group_list(self):
        return list(self.groups)

    def env_list(self, tag):
        self.calls.append(('list', tag))
        return [dict(item) for item in self.envs]

    def env_create(self, body):
        self.calls.append(('create', dict(body)))
        self.envs.append({
            'containerName': body['containerName'],
            'containerCode': '9001', 'serialNumber': 1003, 'remark': ''})
        return {}

    def env_import_cookie(self, _code, _cookie_text):
        self.calls.append(('cookie',))
        return {}

    def container_add_account(self, _code, _email, _password, _site):
        self.calls.append(('account',))
        return {}

    def env_update(self, code, _name, remark):
        self.calls.append(('remark',))
        for env in self.envs:
            if env['containerCode'] == str(code):
                env['remark'] = remark
        return {}


class EnvWebJobTests(unittest.TestCase):
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
                    parsed['planId'], '1:甲', '20260819',
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
            job.start(parsed['planId'], '1:甲', '20260819')

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
                parsed['planId'], '1:甲', '20260819',
                verify_sample_count=0, confirm_write=True)
        self.assertIn(parsed['planId'], job.pending)
        self.assertFalse(any(call[0] in {
            'create', 'cookie', 'account', 'remark'} for call in hub.calls))

        hub.groups = [TEST_TAG]
        cfg['proxyLink'] = ''
        with self.assertRaisesRegex(ValueError, '动态代理'):
            job.start(
                parsed['planId'], '1:甲', '20260819',
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
                    parsed['planId'], '1:甲', '20260819',
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
            job.preview(parsed['planId'], '1:甲', '20260819')
        self.assertFalse(any(call[0] == 'list' for call in hub.calls))
        self.assertIn(parsed['planId'], job.pending)

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


if __name__ == '__main__':
    unittest.main()
