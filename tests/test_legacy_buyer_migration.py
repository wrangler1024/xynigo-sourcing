# -*- coding: utf-8 -*-
import unittest

from purchase_tool.legacy_buyer_migration import SOURCE_FIELDS, full_snapshot


class LegacyBuyerMigrationTests(unittest.TestCase):
    def test_source_projection_includes_all_legacy_fields_and_credentials(self):
        self.assertEqual(len(SOURCE_FIELDS), 23)
        self.assertIn('密码', SOURCE_FIELDS)
        self.assertIn('Cookie', SOURCE_FIELDS)
        self.assertIn('接码Key链接', SOURCE_FIELDS)
        self.assertIn('异常记录', SOURCE_FIELDS)
        self.assertIn('累计下单数', SOURCE_FIELDS)

    def test_full_snapshot_maps_credentials_and_business_fields(self):
        records = [{'record_id': 'rec-private', 'fields': {
            '站点': 'US',
            '邮箱账号': 'buyer@example.test',
            '密码': 'must-not-leave-source',
            'Cookie': 'cookie-must-not-leave-source',
            '接码Key链接': 'https://secret.example.test',
            '账号状态': '未绑定',
            '凭证状态': '验证通过',
            '来源类型': '号商采购',
            '号商名称': '测试号商',
            '入库批次': 'batch-safe',
            '购买日期': 1787500800000,
            '采购员': '采购员甲',
            '号商购买单号': 'safe-order-source',
            '绑定环境': 'US-PUR-001',
            '环境分组名': '采购环境-US',
            '环境序号': 12,
            '累计下单数': 3,
            '异常记录': '合成异常记录',
            '备注': '合成备注',
            '账号ID': 'NO.012',
            '迁移状态': '正常',
            '操作人': [{'name': '合成操作员'}],
            '创建人': [{'name': '合成创建人'}],
            '绑定时间': '2026-08-26T10:00:00+08:00',
            '首次登录日期': '2026-08-26T10:05:00+08:00',
            '最后使用日期': '2026-08-26T11:00:00+08:00',
            '创建时间': '2026-08-25T09:00:00+08:00',
        }}]
        result = full_snapshot(records)
        self.assertEqual(result['invalidCount'], 0)
        self.assertEqual(len(result['accounts']), 1)
        account = result['accounts'][0]
        self.assertEqual(account['displayLabel'], 'bu***@example.test')
        self.assertEqual(account['credentialStatus'], 'ready')
        self.assertEqual(account['availabilityStatus'], 'available')
        self.assertTrue(account['sourceOrderRef'].startswith('sha256:'))
        self.assertEqual(account['credentials']['accountIdentifier'],
                         'buyer@example.test')
        self.assertEqual(account['credentials']['password'],
                         'must-not-leave-source')
        self.assertEqual(account['credentials']['cookie'],
                         'cookie-must-not-leave-source')
        self.assertEqual(account['businessProfile']['bindingEnvironment'],
                         'US-PUR-001')
        self.assertEqual(account['businessProfile']['sourcePurchaseOrderNo'],
                         'safe-order-source')
        self.assertEqual(account['businessProfile']['sourceOperators'],
                         ['合成操作员'])
        self.assertNotIn('record_id', repr(result))

    def test_safe_snapshot_reports_duplicates_without_guessing(self):
        records = [
            {'fields': {
                '站点': 'MX', '邮箱账号': 'same@example.test',
                '账号状态': '未绑定', '号商购买单号': 'order-a'}},
            {'fields': {
                '站点': 'MX', '邮箱账号': 'same@example.test',
                '账号状态': '未绑定', '号商购买单号': 'order-b'}},
            {'fields': {
                '站点': 'US', '邮箱账号': 'other@example.test',
                '账号状态': '未绑定', '号商购买单号': 'order-a'}},
        ]
        result = full_snapshot(records)
        self.assertEqual(result['duplicateAccountCount'], 1)
        self.assertEqual(result['duplicateOrderCount'], 1)
        self.assertEqual(len(result['accounts']), 1)


if __name__ == '__main__':
    unittest.main()
