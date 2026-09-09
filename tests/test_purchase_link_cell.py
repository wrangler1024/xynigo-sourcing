"""Actual server auto-link representation and hostile/malformed alternatives."""
import pytest
from purchase_tool.purchase_link_cell import purchase_url_cell_matches

URL = 'https://example.test/item?goods_id=123&sku=blue#size=L&price=12.50'


@pytest.mark.parametrize('cell', [
    URL,
    {'value': URL},
    {'text': URL, 'type': 'text'},
    [{'cellPosition': None, 'type': 'url', 'text': URL, 'link': URL}],
    {'value': URL, 'rich_text': [{'type': 'link', 'text': URL, 'link': URL}]},
    [{'type': 'text', 'text': URL[:20]}, {'type': 'text', 'text': URL[20:]}],
])
def test_complete_purchase_url_survives_native_and_cli_representations(cell):
    assert purchase_url_cell_matches(cell, URL)


@pytest.mark.parametrize('cell', [
    None, {}, '', URL.split('#')[0],
    [{'type': 'url', 'text': '打开采购链接', 'link': URL}],
    [{'type': 'url', 'text': URL, 'link': URL.split('#')[0]}],
    [{'type': 'url', 'text': URL, 'link': 'https://other.example.test/'}],
    [{'type': 'url', 'text': URL}],
    {'value': URL, 'formula': '=HYPERLINK("https://other.example.test/")'},
    {'value': URL, 'rich_text': [{'text': URL, 'href': 'https://other.example.test/'}]},
    [{'type': 'link', 'text': URL[:20], 'link': URL},
     {'type': 'link', 'text': URL[20:], 'link': 'https://other.example.test/'}],
    {'value': URL, 'rich_text': [{'text': '不同显示内容'}]},
])
def test_incomplete_mismatched_or_ambiguous_purchase_links_are_rejected(cell):
    assert not purchase_url_cell_matches(cell, URL)
