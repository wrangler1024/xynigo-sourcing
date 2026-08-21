# -*- coding: utf-8 -*-
import unittest

from purchase_tool.lark_links import (
    LarkLinkError, parse_lark_base_link, resolve_lark_ledger_link)


class FakeWikiClient(object):
    def __init__(self, node=None, error=None):
        self.node = node or {
            'obj_type': 'bitable', 'obj_token': 'bascnPublicSafeExample'}
        self.error = error
        self.calls = []

    def get_wiki_node(self, token):
        self.calls.append(token)
        if self.error:
            raise self.error
        return dict(self.node)


class LarkLinkTests(unittest.TestCase):
    def test_direct_base_link_resolves_locally(self):
        url = ('https://public-safe.feishu.cn/base/'
               'bascnPublicSafeExample?table=tblPublicSafeExample'
               '&view=vewPublicSafeExample')
        ref = parse_lark_base_link(url)
        self.assertEqual(ref.kind, 'base')
        target = resolve_lark_ledger_link(url)
        self.assertEqual(target.base_token, 'bascnPublicSafeExample')
        self.assertEqual(target.table_id, 'tblPublicSafeExample')
        self.assertEqual(target.source_kind, 'base')

    def test_larksuite_host_is_supported(self):
        target = resolve_lark_ledger_link(
            'https://public-safe.larksuite.com/base/'
            'bascnPublicSafeExample?table=tblPublicSafeExample')
        self.assertEqual(target.source_kind, 'base')

    def test_wiki_link_uses_one_read_only_node_lookup(self):
        client = FakeWikiClient()
        target = resolve_lark_ledger_link(
            'https://public-safe.feishu.cn/wiki/'
            'wikcnPublicSafeExample?table=tblPublicSafeExample', client)
        self.assertEqual(client.calls, ['wikcnPublicSafeExample'])
        self.assertEqual(target.base_token, 'bascnPublicSafeExample')
        self.assertEqual(target.source_kind, 'wiki')

    def test_wiki_non_bitable_is_rejected(self):
        client = FakeWikiClient({
            'obj_type': 'docx', 'obj_token': 'docxPublicSafeExample'})
        with self.assertRaisesRegex(LarkLinkError, '不是多维表格'):
            resolve_lark_ledger_link(
                'https://public-safe.feishu.cn/wiki/'
                'wikcnPublicSafeExample?table=tblPublicSafeExample', client)

    def test_rejects_untrusted_or_ambiguous_links(self):
        bad = (
            'http://public-safe.feishu.cn/base/'
            'bascnPublicSafeExample?table=tblPublicSafeExample',
            'https://example.test/base/'
            'bascnPublicSafeExample?table=tblPublicSafeExample',
            'https://public-safe.feishu.cn/share/base/view/demo',
            'https://public-safe.feishu.cn/base/bascnPublicSafeExample',
            'https://public-safe.feishu.cn/base/'
            'bascnPublicSafeExample?table=tblOneExample&table=tblTwoExample',
            'https://public-safe.feishu.cn:bad/base/'
            'bascnPublicSafeExample?table=tblPublicSafeExample',
        )
        for url in bad:
            with self.subTest(url=url), self.assertRaises(LarkLinkError):
                parse_lark_base_link(url)


if __name__ == '__main__':
    unittest.main()
