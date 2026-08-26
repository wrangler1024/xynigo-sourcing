# -*- coding: utf-8 -*-
import base64
import copy
from io import BytesIO
import unittest

from openpyxl import Workbook

from purchase_tool.buyer_library import (
    BuyerLibraryError, BuyerLibraryJob, BuyerLibraryService,
    DatabaseBuyerLibraryService)
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
    def test_database_runtime_list_requests_and_maps_complete_rows(self):
        calls = []

        def requester(path, method, payload):
            calls.append((path, method, payload))
            return {'ok': True, 'data': {
                'counts': {
                    'total': 3, 'available': 1, 'reserved': 1,
                    'in_use': 1, 'cleanup_pending': 0,
                    'post_payment_hold': 0, 'manual_review': 0,
                    'disabled': 0,
                },
                'rows': [{
                    'accountId': '00000000-0000-0000-0000-000000000001',
                    'accountRef': 'sha256-buyer-safe-0001',
                    'displayLabel': 'bu***1@example.test',
                    'credentials': {
                        'accountIdentifier': 'buyer1@example.test',
                        'phoneNumber': '+1-555-0101',
                        'password': 'synthetic-password-1',
                        'cookie': 'synthetic-cookie-1',
                        'verificationKey': 'synthetic-key-1',
                        'verificationKeyLink': 'https://example.test/key/1',
                        'loginLink': 'https://example.test/login/1',
                    },
                    'businessProfile': {
                        'sourcePurchaseOrderNo': 'SYNTHETIC-ORDER-1',
                        'environmentSequence': 1,
                    },
                    'site': 'US', 'status': 'available',
                    'sourceAvailabilityStatus': 'available',
                    'credentialStatus': 'ready', 'source': 'vendor_import',
                    'sourceVendorLabel': '测试号商',
                    'sourceBatchRef': 'batch-safe',
                    'sourcePurchaseDate': '2026-08-26',
                    'operatorLabel': '采购员甲',
                    'hubEnvironment': {'ref': 'hub-safe', 'name': 'US-SAFE-001'},
                    'baseSyncStatus': 'completed',
                    'baseSyncedAt': '2026-08-26T10:00:00+00:00',
                }],
                'hasMore': False,
            }}

        result = DatabaseBuyerLibraryService(requester).list_public(
            'US', 'available', 100)
        self.assertEqual(result['source'], 'postgresql')
        self.assertEqual(result['counts']['bound'], 1)
        self.assertEqual(result['rows'][0]['status'], '可用')
        self.assertEqual(result['rows'][0]['email'], 'buyer1@example.test')
        self.assertEqual(result['rows'][0]['password'], 'synthetic-password-1')
        self.assertEqual(result['rows'][0]['baseSyncStatus'], 'completed')
        self.assertIn('status=available', calls[0][0])
        self.assertIn('includeCredentials=true', calls[0][0])
        self.assertEqual(calls[0][1], 'GET')

    def test_database_import_sends_credentials_for_server_side_encryption(self):
        calls = []

        def requester(path, method, payload):
            calls.append((path, method, copy.deepcopy(payload)))
            if path.endswith('/preflight'):
                return {'ok': True, 'data': {
                    'ready': True, 'conflictCount': 0, 'conflicts': []}}
            return {'ok': True, 'data': {
                'receivedCount': 1, 'createdCount': 1,
                'updatedCount': 0, 'unchangedCount': 0}}

        service = DatabaseBuyerLibraryService(requester)
        item = account()
        result = service.import_accounts(
            [item], 'MX', '测试号商', 'batch-safe', '2026-08-26',
            confirm_write=True)
        self.assertEqual(result['created'], 1)
        snapshot = calls[-1][2]
        self.assertEqual(snapshot['source'], 'vendor_import')
        self.assertEqual(snapshot['accounts'][0]['displayLabel'], item.safe_email)
        self.assertEqual(snapshot['accounts'][0]['credentialStatus'], 'unverified')
        credentials = snapshot['accounts'][0]['credentials']
        self.assertEqual(credentials['accountIdentifier'], item.email)
        self.assertEqual(credentials['password'], item.password)
        self.assertEqual(credentials['cookie'], item.cookie_text)
        self.assertEqual(credentials['verificationKeyLink'], item.key_url)
        self.assertEqual(
            snapshot['accounts'][0]['businessProfile']['sourcePurchaseOrderNo'],
            item.order_no)

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

    def test_job_preview_is_complete_and_commit_is_explicit(self):
        client = FakeClient()
        job = BuyerLibraryJob(lambda: BuyerLibraryService(client))
        result = job.parse(
            'vendor.xlsx', workbook_base64(), 'MX', '测试号商',
            'batch-a', '2026-08-24')
        self.assertTrue(result['libraryReady'])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['preview'][0]['email'], 'newbuyer@example.test')
        self.assertEqual(result['preview'][0]['password'], 'local-secret')
        self.assertIn('cookie-secret', result['preview'][0]['cookie'])
        with self.assertRaisesRegex(BuyerLibraryError, '二次确认'):
            job.commit(result['planId'])


if __name__ == '__main__':
    unittest.main()
