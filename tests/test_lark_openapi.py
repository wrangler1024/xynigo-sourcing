# -*- coding: utf-8 -*-
import json
import unittest

from purchase_tool.lark_credentials import LarkCredentials
from purchase_tool.lark_openapi import (
    LarkHttpResponse, LarkOpenApiClient)


def response(payload, status=200):
    return LarkHttpResponse(
        status, json.dumps(payload, ensure_ascii=False).encode('utf-8'))


class FakeTransport(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({
            'method': method, 'url': url, 'headers': dict(headers),
            'body': body, 'timeout': timeout,
        })
        return self.responses.pop(0)


class LarkOpenApiTests(unittest.TestCase):
    def client(self, responses, sleep_fn=lambda _seconds: None):
        transport = FakeTransport(responses)
        client = LarkOpenApiClient(
            lambda: LarkCredentials('cli_public_demo', 'private-secret-demo'),
            'base_public_demo', 'tbl_public_demo', transport=transport,
            origin='https://open.feishu.test', sleep_fn=sleep_fn,
            clock=lambda: 1000)
        return client, transport

    def test_token_is_cached_in_memory_and_never_added_to_url(self):
        client, transport = self.client([
            response({'code': 0, 'tenant_access_token': 'tenant-secret',
                      'expire': 7200}),
            response({'code': 0, 'data': {'items': [], 'has_more': False}}),
            response({'code': 0, 'data': {'items': [], 'has_more': False}}),
        ])
        client.list_fields()
        client.list_fields()
        self.assertEqual(sum(call['url'].endswith('/internal')
                             for call in transport.calls), 1)
        for call in transport.calls:
            self.assertNotIn('tenant-secret', call['url'])
        self.assertEqual(
            transport.calls[1]['headers']['Authorization'],
            'Bearer tenant-secret')

    def test_field_and_record_pagination_follow_page_token(self):
        client, transport = self.client([
            response({'code': 0, 'tenant_access_token': 'tenant-secret',
                      'expire': 7200}),
            response({'code': 0, 'data': {
                'items': [{'field_name': '站点'}], 'has_more': True,
                'page_token': 'next-field'}}),
            response({'code': 0, 'data': {
                'items': [{'field_name': '邮箱账号'}], 'has_more': False}}),
            response({'code': 0, 'data': {
                'items': [{'record_id': 'rec-1'}], 'has_more': True,
                'page_token': 'next-record'}}),
            response({'code': 0, 'data': {
                'items': [{'record_id': 'rec-2'}], 'has_more': False}}),
        ])
        self.assertEqual(len(client.list_fields()), 2)
        self.assertEqual(len(client.list_records(['站点', '邮箱账号'])), 2)
        urls = [call['url'] for call in transport.calls]
        self.assertTrue(any('page_token=next-field' in url for url in urls))
        self.assertTrue(any('page_token=next-record' in url for url in urls))
        self.assertTrue(any('field_names=' in url for url in urls))

    def test_batch_payload_uses_official_records_shape(self):
        client, transport = self.client([
            response({'code': 0, 'tenant_access_token': 'tenant-secret',
                      'expire': 7200}),
            response({'code': 0, 'data': {
                'records': [{'record_id': 'rec-created'}]}}),
            response({'code': 0, 'data': {
                'records': [{'record_id': 'rec-created'}]}}),
        ])
        client.batch_create([{'站点': 'MX', '邮箱账号': 'demo@example.test'}])
        client.batch_update([('rec-created', {'账号状态': '已绑定'})])
        create_body = json.loads(transport.calls[1]['body'])
        update_body = json.loads(transport.calls[2]['body'])
        self.assertEqual(create_body, {'records': [{
            'fields': {'站点': 'MX', '邮箱账号': 'demo@example.test'}}]})
        self.assertEqual(update_body, {'records': [{
            'record_id': 'rec-created',
            'fields': {'账号状态': '已绑定'}}]})
        self.assertNotIn('create_records', create_body)
        self.assertNotIn('update_records', update_body)

    def test_wiki_node_lookup_does_not_require_a_saved_base_target(self):
        transport = FakeTransport([
            response({'code': 0, 'tenant_access_token': 'tenant-secret',
                      'expire': 7200}),
            response({'code': 0, 'data': {'node': {
                'obj_type': 'bitable',
                'obj_token': 'bascnPublicSafeExample'}}}),
        ])
        client = LarkOpenApiClient(
            lambda: LarkCredentials('cli_public_demo', 'private-secret-demo'),
            '', '', transport=transport, origin='https://open.feishu.test',
            clock=lambda: 1000)
        node = client.get_wiki_node('wikcnPublicSafeExample')
        self.assertEqual(node['obj_type'], 'bitable')
        self.assertIn('/open-apis/wiki/v2/spaces/get_node?',
                      transport.calls[1]['url'])
        self.assertIn('token=wikcnPublicSafeExample',
                      transport.calls[1]['url'])

    def test_rate_limit_retries_without_leaking_credentials(self):
        sleeps = []
        client, _transport = self.client([
            response({'code': 0, 'tenant_access_token': 'tenant-secret',
                      'expire': 7200}),
            response({'code': 1254291, 'msg':
                      'password=private-secret-demo buyer@example.test'}),
            response({'code': 0, 'data': {'items': [], 'has_more': False}}),
        ], sleep_fn=sleeps.append)
        self.assertEqual(client.list_fields(), [])
        self.assertEqual(sleeps, [1])


if __name__ == '__main__':
    unittest.main()
