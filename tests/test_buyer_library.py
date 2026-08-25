# -*- coding: utf-8 -*-
import base64
import copy
from io import BytesIO
import unittest

from openpyxl import Workbook

from purchase_tool.buyer_library import (
    BuyerLibraryError, BuyerLibraryJob, BuyerLibraryService)
from purchase_tool.env_batch import BuyerAccount, VENDOR_TEMPLATE_HEADERS


def library_fields():
    types = {
        '站点': 'SingleSelect', '邮箱账号': 'Text', '密码': 'Text',
        '接码Key链接': 'Text', 'Cookie': 'Text', '号商购买单号': 'Text',
        '购买日期': 'DateTime', '账号状态': 'SingleSelect',
        '来源类型': 'SingleSelect', '号商名称': 'Text', '入库批次': 'Text',
        '入库时间': 'DateTime', '凭证状态': 'SingleSelect',
        '绑定环境': 'Text', '环境序号': 'Number', '采购员': 'SingleSelect',
        'IP检测状态': 'SingleSelect',
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
        elif name == '来源类型':
            field['property']['options'] = [
                {'name': '号商采购'}, {'name': '自主注册'}]
        elif name == '凭证状态':
            field['property']['options'] = [
                {'name': '未验证'}, {'name': '验证通过'}, {'name': '验证失败'}]
        result.append(field)
    return result


def account(index=1):
    return BuyerAccount(
        row_number=index,
        email='buyer%d@example.test' % index,
        password='secret-password-%d' % index,
        key_url='https://codes.example.test/?orderNo=abcdef%d' % index,
        cookie_text='[{"name":"sid","value":"secret-cookie-%d"}]' % index,
        order_no='abcdef%d' % index)


class FakeClient(object):
    def __init__(self, records=None):
        self.fields = library_fields()
        self.records = copy.deepcopy(records or [])
        self.created = []

    def list_fields(self):
        return copy.deepcopy(self.fields)

    def list_records(self, field_names=None):
        result = copy.deepcopy(self.records)
        if field_names:
            allowed = set(field_names)
            for record in result:
                record['fields'] = {
                    key: value for key, value in record['fields'].items()
                    if key in allowed}
        return result

    def batch_create(self, field_maps):
        self.created.extend(copy.deepcopy(field_maps))
        return [{'record_id': 'rec-%d' % index}
                for index, _fields in enumerate(field_maps, start=1)]


def workbook_base64():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(VENDOR_TEMPLATE_HEADERS))
    sheet.append([
        'newbuyer@example.test', 'local-secret',
        'https://codes.example.test/?orderNo=1234abcd',
        '[{"name":"sid","value":"cookie-secret"}]',
    ])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return base64.b64encode(output.getvalue()).decode('ascii')


class BuyerLibraryTests(unittest.TestCase):
    def test_public_list_masks_credentials_and_maps_legacy_status(self):
        client = FakeClient([{'record_id': 'rec-secret', 'fields': {
            '站点': 'MX', '邮箱账号': 'alpha@example.test',
            '密码': 'must-not-return', 'Cookie': 'must-not-return',
            '接码Key链接': 'https://secret.example.test/?token=hidden',
            '账号状态': '未绑定', '来源类型': '号商采购',
            '号商名称': '测试号商', '入库批次': 'batch-a',
            '购买日期': 1787500800000,
        }}])
        result = BuyerLibraryService(client).list_public()
        self.assertEqual(result['counts']['available'], 1)
        self.assertEqual(result['rows'][0]['status'], '可用')
        self.assertEqual(result['rows'][0]['emailMasked'], 'al***@example.test')
        serialized = repr(result)
        for secret in ('must-not-return', 'token=hidden', 'rec-secret'):
            self.assertNotIn(secret, serialized)

    def test_preflight_blocks_duplicate_email_or_order(self):
        item = account()
        client = FakeClient([{'record_id': 'rec-existing', 'fields': {
            '站点': 'MX', '邮箱账号': item.email,
            '号商购买单号': item.order_no, '账号状态': '未绑定',
        }}])
        result = BuyerLibraryService(client).import_preflight([item], 'MX')
        self.assertFalse(result['ready'])
        self.assertEqual(result['conflicts'], 1)
        self.assertIn('邮箱已存在', result['rows'][0]['message'])
        self.assertNotIn(item.email, repr(result))

    def test_import_requires_confirmation_and_writes_source_metadata(self):
        item = account()
        client = FakeClient()
        service = BuyerLibraryService(client)
        with self.assertRaisesRegex(BuyerLibraryError, '二次确认'):
            service.import_accounts(
                [item], 'MX', '测试号商', 'batch-a', '2026-08-24')
        result = service.import_accounts(
            [item], 'MX', '测试号商', 'batch-a', '2026-08-24',
            confirm_write=True)
        self.assertEqual(result['created'], 1)
        self.assertEqual(client.created[0]['来源类型'], '号商采购')
        self.assertEqual(client.created[0]['账号状态'], '未绑定')
        self.assertEqual(client.created[0]['邮箱账号'], item.email)
        self.assertEqual(client.created[0]['接码Key链接'], {
            'text': item.key_url, 'link': item.key_url})

    def test_import_preflight_rejects_legacy_table_without_source_fields(self):
        client = FakeClient()
        client.fields = [field for field in client.fields
                         if field['field_name'] not in {
                             '来源类型', '号商名称', '入库批次',
                             '入库时间', '凭证状态'}]
        with self.assertRaisesRegex(BuyerLibraryError, '来源类型'):
            BuyerLibraryService(client).import_preflight([account()], 'MX')

    def test_job_preview_is_masked_and_commit_is_explicit(self):
        client = FakeClient()
        job = BuyerLibraryJob(lambda: BuyerLibraryService(client))
        result = job.parse(
            'vendor.xlsx', workbook_base64(), 'MX', '测试号商',
            'batch-a', '2026-08-24')
        self.assertTrue(result['libraryReady'])
        self.assertEqual(result['count'], 1)
        self.assertIn('ne***@example.test', result['preview'][0]['emailMasked'])
        self.assertNotIn('local-secret', repr(result))
        self.assertNotIn('cookie-secret', repr(result))
        with self.assertRaisesRegex(BuyerLibraryError, '二次确认'):
            job.commit(result['planId'])


if __name__ == '__main__':
    unittest.main()
