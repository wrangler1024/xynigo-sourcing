# -*- coding: utf-8 -*-
import json
import time
import unittest

from purchase_tool.resource_center import (
    FeishuResourceReader, ProxyCheckError, ProxyCheckJob, ProxyEndpoint,
    ResourceCenterService, ResourceSourceConfig, _stable_id)


class FakeHub(object):
    def env_list(self):
        return [
            {
                'containerCode': 100001,
                'containerName': '墨西哥一店',
                'serialNumber': 7,
                'tagNames': ['店铺组'],
            },
            {
                'containerCode': 100002,
                'containerName': '未归属环境',
                'serialNumber': 8,
                'tagNames': ['待整理'],
            },
        ]

    def open_container_codes(self):
        return {'100001'}


class FakeReader(object):
    def __init__(self):
        self.loaded_asset_ids = []

    def list_shops(self):
        return [{
            'shop_id': 'shop_public_1',
            'shop_name': '墨西哥一店',
            'platform': 'SHEIN',
            'site': 'MX',
            'owner': '店务甲',
            'manager': '主管甲',
            'shop_status': '在售+已动销',
            'shop_type': '半托',
        }]

    def list_proxy_rows(self):
        return [{
            'asset_id': _stable_id('ip', 'Webshare', '203.0.113.9', 1080),
            'host': '203.0.113.9',
            'port': 1080,
            'shop_name': '墨西哥一店',
            'department': '店务部',
            'shop_type': '半托',
            'platform': 'SHEIN',
            'browser': 'HubStudio',
            'env_serial': '7',
            'source': 'Webshare',
            'asset_status': '使用中',
            'remark': '',
        }]

    def list_proxy_metadata(self):
        return {('203.0.113.9', 1080): {
            'protocol': 'SOCKS5', 'country': 'MX',
            'provider_status': '已绑定', 'provider_valid': True,
        }}

    def load_proxy_endpoints(self, asset_ids, protocol_by_asset=None):
        self.loaded_asset_ids = list(asset_ids)
        return [ProxyEndpoint(
            asset_id=asset_ids[0], host='203.0.113.9', port=1080,
            protocol=(protocol_by_asset or {}).get(asset_ids[0], 'SOCKS5'),
            username='sensitive-user', password='sensitive-password')]


class AlwaysOkChecker(object):
    def check(self, endpoint, timeout=8):
        return {
            'ok': True, 'code': 'ok', 'message': '代理可用，出口 IP 一致',
            'latencyMs': 18, 'exitIpMasked': '203.***.***.9',
        }


class RetryChecker(object):
    def __init__(self):
        self.calls = 0

    def check(self, endpoint, timeout=8):
        self.calls += 1
        if self.calls == 1:
            raise ProxyCheckError('proxy_unreachable', '代理端口拒绝连接')
        return {
            'ok': True, 'code': 'ok', 'message': '代理可用',
            'latencyMs': 21, 'exitIpMasked': '203.***.***.9',
        }


def wait_job(job):
    deadline = time.time() + 2
    while job.snapshot()['running'] and time.time() < deadline:
        time.sleep(0.01)
    if job.snapshot()['running']:
        raise AssertionError('proxy job did not finish')


class ResourceCenterTests(unittest.TestCase):
    def service(self, checker=None):
        return ResourceCenterService(
            lambda: FakeHub(), lambda: None, reader=FakeReader(),
            checker=checker or AlwaysOkChecker(), cache_ttl=60)

    def test_reconcile_returns_only_masked_identifiers_and_explicit_binding(self):
        service = self.service()
        stores = service.stores_snapshot()
        self.assertEqual(stores['stats']['total'], 1)
        self.assertEqual(stores['stats']['hubBound'], 1)
        self.assertEqual(stores['stats']['hubOrphans'], 1)
        row = stores['rows'][0]
        self.assertEqual(row['mappingState'], 'bound')
        self.assertEqual(row['hubRuntimeStatus'], '运行中')
        self.assertEqual(row['proxyAddressMasked'], '203.***.***.9:***80')
        self.assertEqual(row['containerCodeMasked'], '***0001')
        encoded = json.dumps(stores, ensure_ascii=False)
        self.assertNotIn('203.0.113.9', encoded)
        self.assertNotIn('sensitive-password', encoded)

    def test_proxy_check_uses_explicit_selection_and_never_returns_credentials(self):
        service = self.service()
        asset_id = service.proxies_snapshot()['rows'][0]['assetId']
        self.assertEqual(service.start_proxy_checks([asset_id]), 1)
        wait_job(service.check_job)
        snapshot = service.proxy_check_snapshot()
        self.assertEqual(snapshot['normal'], 1)
        self.assertEqual(service.reader.loaded_asset_ids, [asset_id])
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn('sensitive-user', encoded)
        self.assertNotIn('sensitive-password', encoded)
        refreshed = service.proxies_snapshot()
        self.assertEqual(refreshed['rows'][0]['checkStatus'], '正常')
        self.assertEqual(refreshed['rows'][0]['historyCount'], 1)

    def test_proxy_snapshot_exposes_business_taxonomy_without_fake_inventory(self):
        snapshot = self.service().proxies_snapshot()
        row = snapshot['rows'][0]
        self.assertEqual(row['proxyTypeCode'], 'static_datacenter')
        self.assertEqual(row['proxyType'], '静态数据中心 IP')
        self.assertEqual(row['provider'], 'Webshare')
        self.assertEqual(row['usageScenario'], '绑定店铺环境')
        self.assertEqual(row['acquisitionMode'], '静态资产台账')
        self.assertTrue(row['healthCheckSupported'])

        catalog = {item['typeCode']: item for item in snapshot['typeCatalog']}
        self.assertEqual(set(catalog), {
            'dynamic_residential', 'static_datacenter', 'static_residential'})
        self.assertEqual(catalog['dynamic_residential']['provider'], '711')
        self.assertEqual(
            catalog['dynamic_residential']['usageScenario'], '采购场景')
        self.assertEqual(
            catalog['dynamic_residential']['acquisitionMode'], 'API 动态提取')
        self.assertIsNone(catalog['dynamic_residential']['assetCount'])
        self.assertEqual(
            catalog['dynamic_residential']['inventorySummary'],
            '动态提取，不计固定库存')
        self.assertEqual(catalog['static_datacenter']['assetCount'], 1)
        self.assertEqual(
            catalog['static_residential']['usageScenario'], '暂无使用场景')
        self.assertIsNone(catalog['static_residential']['assetCount'])

        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn('rotgbapi.711proxy.com', encoded)

    def test_dynamic_711_row_is_classified_but_cannot_load_webshare_credentials(self):
        class DynamicReader(FakeReader):
            def list_proxy_rows(self):
                return [{
                    'asset_id': _stable_id(
                        'ip', '711', '198.51.100.7', 10080),
                    'host': '198.51.100.7',
                    'port': 10080,
                    'shop_name': '',
                    'department': '采购部',
                    'shop_type': '',
                    'platform': 'SHEIN',
                    'browser': 'HubStudio',
                    'env_serial': '',
                    'source': '711',
                    'asset_status': '动态提取',
                    'remark': '',
                }]

            def list_proxy_metadata(self):
                return {}

        service = ResourceCenterService(
            lambda: FakeHub(), lambda: None, reader=DynamicReader(),
            checker=AlwaysOkChecker(), cache_ttl=60)
        snapshot = service.proxies_snapshot()
        row = snapshot['rows'][0]
        self.assertEqual(row['proxyTypeCode'], 'dynamic_residential')
        self.assertEqual(row['usageScenario'], '采购场景')
        self.assertEqual(row['acquisitionMode'], 'API 动态提取')
        self.assertIn('白名单', row['accessRequirement'])
        self.assertFalse(row['healthCheckSupported'])
        with self.assertRaisesRegex(ValueError, '不支持固定资产本机检测'):
            service.start_proxy_checks([row['assetId']])

    def test_proxy_job_retries_once_and_checks_duplicate_endpoint_once(self):
        checker = RetryChecker()
        endpoints = [
            ProxyEndpoint('ip_a', '203.0.113.9', 1080, 'SOCKS5', 'u', 'p'),
            ProxyEndpoint('ip_b', '203.0.113.9', 1080, 'SOCKS5', 'u', 'p'),
        ]
        job = ProxyCheckJob(lambda _ids: list(endpoints), checker=checker)
        job.start(['ip_a', 'ip_b'], concurrency=10, timeout=8)
        wait_job(job)
        snapshot = job.snapshot()
        self.assertEqual(checker.calls, 2)
        self.assertEqual(snapshot['normal'], 2)
        self.assertTrue(all(row['conflict'] for row in snapshot['rows']))
        self.assertTrue(all(row['attempts'] == 2 for row in snapshot['rows']))

    def test_exports_are_bom_csv_and_never_include_proxy_secrets(self):
        service = self.service()
        store_csv = service.store_export().decode('utf-8-sig')
        proxy_csv = service.proxy_export().decode('utf-8-sig')
        self.assertIn('墨西哥一店', store_csv)
        self.assertIn('203.***.***.9:***80', proxy_csv)
        self.assertIn('静态数据中心 IP', proxy_csv)
        self.assertIn('绑定店铺环境', proxy_csv)
        self.assertNotIn('203.0.113.9', proxy_csv)
        self.assertNotIn('sensitive-password', store_csv + proxy_csv)

    def test_provider_fallback_does_not_claim_occupancy_is_unused(self):
        class ProviderOnlyReader(FakeReader):
            def list_proxy_rows(self):
                raise RuntimeError('ordinary sheet has no permission')

        service = ResourceCenterService(
            lambda: FakeHub(), lambda: None, reader=ProviderOnlyReader(),
            checker=AlwaysOkChecker(), cache_ttl=60)
        snapshot = service.proxies_snapshot()
        self.assertEqual(snapshot['stats']['total'], 1)
        self.assertEqual(snapshot['stats']['unused'], 0)
        self.assertEqual(snapshot['stats']['inUse'], 0)
        self.assertFalse(snapshot['rows'][0]['occupancyKnown'])


class ReaderColumnSafetyTests(unittest.TestCase):
    def test_proxy_list_skips_credential_columns_until_check(self):
        calls = []

        class FakeClient(object):
            def list_records(self, field_names=None):
                return []

            def get_spreadsheet_values(self, _token, range_a1):
                calls.append(range_a1)
                if range_a1.endswith('A1:B5000'):
                    return [['IP地址', '端口'], ['203.0.113.9', 1080]]
                if range_a1.endswith('E1:J5000'):
                    return [['其他名称', '店铺中文名', '部门', '店铺属性', '电商平台', '指纹浏览器'],
                            ['', '墨西哥一店', '店务部', '半托', 'SHEIN', 'HubStudio']]
                if range_a1.endswith('L1:R5000'):
                    return [['IP:端口', '窗口序号', '代理来源', 'IP状态', '记录来源', '录入时间', '备注'],
                            ['', 7, 'Webshare', '使用中', '手工', '', '']]
                if range_a1.endswith('A2:D5000'):
                    return [['203.0.113.9', 1080, 'secret-user', 'secret-password']]
                raise AssertionError(range_a1)

        reader = FeishuResourceReader(
            lambda: None,
            config=ResourceSourceConfig(),
            client_factory=lambda **_kwargs: FakeClient())
        rows = reader.list_proxy_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls, [
            'a82543!A1:B5000', 'a82543!E1:J5000', 'a82543!L1:R5000'])
        self.assertNotIn('username', rows[0])
        asset_id = rows[0]['asset_id']
        endpoints = reader.load_proxy_endpoints([asset_id])
        self.assertEqual(endpoints[0].username, 'secret-user')
        self.assertEqual(calls[-1], 'a82543!A2:D5000')


if __name__ == '__main__':
    unittest.main()
