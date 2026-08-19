# -*- coding: utf-8 -*-
import base64
from io import BytesIO
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from openpyxl import Workbook

os.environ.setdefault(
    'XYNIGO_PROXY_LINK', 'https://proxy.example.test/{region}')

from purchase_tool.env_batch import ResumeStateStore
from purchase_tool.main import EnvBatchJob


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

    def env_list(self, _tag):
        return [dict(item) for item in self.envs]

    def env_create(self, body):
        self.envs.append({
            'containerName': body['containerName'],
            'containerCode': '9001', 'serialNumber': 1003, 'remark': ''})
        return {}

    def env_import_cookie(self, _code, _cookie_text):
        return {}

    def container_add_account(self, _code, _email, _password, _site):
        return {}

    def env_update(self, code, _name, remark):
        for env in self.envs:
            if env['containerCode'] == str(code):
                env['remark'] = remark
        return {}


class EnvWebJobTests(unittest.TestCase):
    def test_parse_returns_only_masked_plan(self):
        source = source_bytes()
        job = EnvBatchJob(lambda: FakeHub())
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
        job = EnvBatchJob(lambda: hub)
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

    def test_start_requires_explicit_confirmation(self):
        job = EnvBatchJob(lambda: FakeHub())
        parsed = job.parse(
            'vendor.xlsx', base64.b64encode(source_bytes()).decode('ascii'))
        with self.assertRaises(ValueError):
            job.start(parsed['planId'], '1:甲', '20260819')


if __name__ == '__main__':
    unittest.main()
