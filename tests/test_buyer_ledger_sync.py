# -*- coding: utf-8 -*-
import copy
import json
import unittest

from purchase_tool.buyer_ledger_sync import (
    BuyerLedgerSyncError, BuyerLedgerSyncService, validate_unified_schema)
from purchase_tool.env_batch import BatchPlanItem, BuyerAccount
from purchase_tool.lark_openapi import LarkApiError


def ledger_fields():
    types = {
        '站点': 'SingleSelect', '邮箱账号': 'Text', '密码': 'Text',
        '接码Key链接': 'Text', 'Cookie': 'Text', '号商购买单号': 'Text',
        '购买日期': 'DateTime', '账号状态': 'SingleSelect',
        '绑定环境': 'Text', '环境序号': 'Number', '采购员': 'SingleSelect',
        '绑定时间': 'DateTime', '首次登录日期': 'DateTime',
    }
    result = []
    for name, ui_type in types.items():
        field = {'field_name': name, 'ui_type': ui_type, 'property': {}}
        if name == '站点':
            field['property']['options'] = [{'name': 'MX'}, {'name': 'US'}]
        elif name == '账号状态':
            field['property']['options'] = [
                {'name': value} for value in
                ('未绑定', '已绑定', '已登录', '异常', '封号', '停用')]
        elif name == '采购员':
            field['property']['options'] = [
                {'name': value} for value in
                ('新刚', '志恒', '康德', '宇航')]
        result.append(field)
    return result


def plan_row(index=1, site='MX', email=None, order=None, buyer='新刚'):
    account = BuyerAccount(
        row_number=index,
        email=email or 'buyer%d@example.test' % index,
        password='password-%d' % index,
        key_url='https://codes.example.test/get?id=%d' % index,
        cookie_text='[{"name":"sid","value":"cookie-%d"}]' % index,
        order_no=order or 'vendor-order-%d' % index,
        buyer=buyer)
    return BatchPlanItem(
        account=account,
        env_name='%s-%s-0821-%03d' % (
            {'新刚': 'XG', '志恒': 'ZH', '康德': 'KD', '宇航': 'YH'}[buyer],
            site, index),
        serial_number=2000 + index,
        completed_steps={'done'}, state='done',
        binding_time='2026-08-21 10:%02d:00' % index)


class FakeLark(object):
    def __init__(self, records=None, fail_emails=(), corrupt_readback=()):
        self.fields = ledger_fields()
        self.records = copy.deepcopy(records or [])
        self.fail_emails = set(fail_emails)
        self.corrupt_readback = set(corrupt_readback)
        self.create_calls = []
        self.update_calls = []

    def list_fields(self):
        return copy.deepcopy(self.fields)

    def list_records(self, field_names=None):
        result = copy.deepcopy(self.records)
        for record in result:
            if record['fields'].get('邮箱账号') in self.corrupt_readback:
                record['fields']['绑定环境'] = 'CORRUPTED-ENV'
        if field_names:
            allowed = set(field_names)
            for record in result:
                record['fields'] = {
                    key: value for key, value in record['fields'].items()
                    if key in allowed}
        return result

    def batch_create(self, field_maps):
        fields = copy.deepcopy(field_maps[0])
        self.create_calls.append(fields)
        if fields['邮箱账号'] in self.fail_emails:
            raise LarkApiError(
                'timeout password=%s cookie=%s' %
                (fields['密码'], fields['Cookie']), retryable=True)
        record = {
            'record_id': 'rec-%d' % (len(self.records) + 1),
            'fields': fields,
        }
        self.records.append(record)
        return [copy.deepcopy(record)]

    def batch_update(self, updates):
        self.update_calls.append(copy.deepcopy(updates))
        index = {record['record_id']: record for record in self.records}
        for record_id, fields in updates:
            index[record_id]['fields'].update(copy.deepcopy(fields))
        return [{'record_id': record_id} for record_id, _fields in updates]

    def get_record(self, record_id):
        record = copy.deepcopy(next(
            item for item in self.records if item['record_id'] == record_id))
        email = record['fields'].get('邮箱账号')
        if email in self.corrupt_readback:
            record['fields']['绑定环境'] = 'CORRUPTED-ENV'
        return record


def existing_record(row, site='MX', **overrides):
    fields = {
        '站点': site,
        '邮箱账号': row.account.email,
        '号商购买单号': row.account.order_no,
        '购买日期': 1787241600000,
        '账号状态': '已绑定',
        '绑定环境': row.env_name,
        '环境序号': row.serial_number,
        '采购员': row.account.buyer,
        '绑定时间': 1787277660000,
    }
    fields.update(overrides)
    return {'record_id': 'rec-existing', 'fields': fields}


class BuyerLedgerSyncTests(unittest.TestCase):
    def test_mx_and_us_create_in_same_unified_table(self):
        fake = FakeLark()
        service = BuyerLedgerSyncService(fake)
        mx = service.sync([plan_row(1, 'MX')], 'MX', '20260821')
        us = service.sync([plan_row(2, 'US')], 'US', '20260821')
        self.assertEqual(mx['created'], 1)
        self.assertEqual(us['created'], 1)
        self.assertEqual([call['站点'] for call in fake.create_calls],
                         ['MX', 'US'])
        rendered = json.dumps([mx, us], ensure_ascii=False)
        self.assertNotIn('record_id', rendered)
        self.assertNotIn('cookie-1', rendered)

    def test_retry_is_idempotently_confirmed_without_new_create(self):
        fake = FakeLark()
        service = BuyerLedgerSyncService(fake)
        row = plan_row(1)
        first = service.sync([row], 'MX', '20260821')
        second = service.sync([row], 'MX', '20260821')
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['confirmed'], 1)
        self.assertEqual(len(fake.create_calls), 1)

    def test_exact_unbound_row_updates_binding_without_overwriting_credentials(self):
        row = plan_row(1)
        record = existing_record(
            row,
            **{'账号状态': '未绑定', '绑定环境': '', '环境序号': None,
               '采购员': '', '绑定时间': None,
               '密码': 'keep-old-password',
               'Cookie': 'keep-old-cookie',
               '接码Key链接': 'https://keep.example.test'})
        fake = FakeLark([record])
        result = BuyerLedgerSyncService(fake).sync(
            [row], 'MX', '20260821')
        self.assertEqual(result['updated'], 1)
        stored = fake.records[0]['fields']
        self.assertEqual(stored['密码'], 'keep-old-password')
        self.assertEqual(stored['Cookie'], 'keep-old-cookie')
        self.assertEqual(stored['接码Key链接'], 'https://keep.example.test')

    def test_incomplete_bound_row_is_conflict_not_silently_repaired(self):
        row = plan_row(1)
        record = existing_record(
            row, **{'账号状态': '已绑定', '绑定环境': '',
                    '环境序号': None, '采购员': '', '绑定时间': None})
        fake = FakeLark([record])
        result = BuyerLedgerSyncService(fake).sync(
            [row], 'MX', '20260821')
        self.assertEqual(result['conflict'], 1)
        self.assertEqual(fake.create_calls, [])
        self.assertEqual(fake.update_calls, [])

    def test_dual_key_and_cross_site_conflicts_make_zero_writes(self):
        scenarios = []
        row = plan_row(1)
        scenarios.append(existing_record(
            row, **{'号商购买单号': 'different-order'}))
        scenarios.append(existing_record(
            row, **{'邮箱账号': 'different@example.test'}))
        scenarios.append(existing_record(row, site='US'))
        scenarios.append(existing_record(
            row, **{'绑定环境': 'XG-MX-0821-999'}))
        for record in scenarios:
            with self.subTest(record=record['fields']):
                fake = FakeLark([record])
                result = BuyerLedgerSyncService(fake).sync(
                    [row], 'MX', '20260821')
                self.assertEqual(result['conflict'], 1)
                self.assertEqual(fake.create_calls, [])
                self.assertEqual(fake.update_calls, [])

    def test_two_keys_pointing_to_different_records_is_conflict(self):
        row = plan_row(1)
        email_record = existing_record(
            row, **{'号商购买单号': 'another-order'})
        order_record = existing_record(
            row, **{'邮箱账号': 'another@example.test'})
        email_record['record_id'] = 'rec-by-email'
        order_record['record_id'] = 'rec-by-order'
        fake = FakeLark([email_record, order_record])
        result = BuyerLedgerSyncService(fake).sync(
            [row], 'MX', '20260821')
        self.assertEqual(result['conflict'], 1)
        self.assertEqual(fake.create_calls, [])
        self.assertEqual(fake.update_calls, [])

    def test_same_batch_duplicate_key_makes_zero_writes(self):
        first = plan_row(1)
        second = plan_row(2, email=first.account.email)
        fake = FakeLark()
        result = BuyerLedgerSyncService(fake).sync(
            [first, second], 'MX', '20260821')
        self.assertEqual(result['conflict'], 2)
        self.assertEqual(fake.create_calls, [])
        self.assertEqual(fake.update_calls, [])

    def test_partial_failure_preserves_success_and_redacts_error(self):
        first = plan_row(1)
        second = plan_row(2)
        fake = FakeLark(fail_emails={second.account.email})
        result = BuyerLedgerSyncService(fake).sync(
            [first, second], 'MX', '20260821')
        self.assertEqual(result['created'], 1)
        self.assertEqual(result['pending'], 1)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(second.account.password, rendered)
        self.assertNotIn(second.account.cookie_text, rendered)
        self.assertNotIn(second.account.email, rendered)

    def test_post_write_readback_mismatch_is_pending(self):
        row = plan_row(1)
        fake = FakeLark(corrupt_readback={row.account.email})
        result = BuyerLedgerSyncService(fake).sync(
            [row], 'MX', '20260821')
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['created'], 0)

    def test_preflight_blocks_existing_other_site_before_hub_write(self):
        row = plan_row(1)
        fake = FakeLark([existing_record(row, site='US')])
        row.state = 'pending'
        row.serial_number = None
        result = BuyerLedgerSyncService(fake).preflight_plan([row], 'MX')
        self.assertEqual(result['conflicts'], 1)
        self.assertNotIn('record_id', json.dumps(result))

    def test_schema_rejects_missing_options(self):
        fields = ledger_fields()
        site = next(field for field in fields if field['field_name'] == '站点')
        site['property']['options'] = [{'name': 'MX'}]
        with self.assertRaisesRegex(BuyerLedgerSyncError, 'MX/US'):
            validate_unified_schema(fields)

    def test_schema_rejects_polluted_status_options(self):
        fields = ledger_fields()
        status = next(
            field for field in fields if field['field_name'] == '账号状态')
        status['property']['options'].append({'name': 'unexpected-value'})
        with self.assertRaisesRegex(BuyerLedgerSyncError, '系统契约'):
            validate_unified_schema(fields)


if __name__ == '__main__':
    unittest.main()
