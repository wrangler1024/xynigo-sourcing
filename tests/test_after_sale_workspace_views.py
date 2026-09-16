"""Approved order workspace, using production markup and functions with synthetic data."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = {}
        self.duplicates = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        identifier = attrs.get('id')
        if identifier:
            if identifier in self.nodes:
                self.duplicates.append(identifier)
            self.nodes[identifier] = (attrs, [item[1] for item in self.stack if item[1]])
        if tag not in {'meta', 'link', 'input', 'img', 'br', 'hr', 'source', 'wbr'}:
            self.stack.append((tag, identifier))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def test_two_views_and_history_entries_have_the_correct_containers():
    html = (ROOT / 'src/purchase_tool/web/index.html').read_text(encoding='utf-8')
    parser = Elements()
    parser.feed(html)
    assert not parser.duplicates
    for tab, panel in [('asPendingTab', 'asPendingView'), ('asResultTab', 'asResultView')]:
        assert parser.nodes[tab][0]['aria-controls'] == panel
        assert parser.nodes[panel][0]['aria-labelledby'] == tab
        assert 'asOrderWorkspace' in parser.nodes[panel][1]
    for control in ('asScanTable', 'asPickClaimable', 'asScanExport', 'asSubmit'):
        assert 'asPendingView' in parser.nodes[control][1]
    for control in ('asClaimTable', 'asClaimHistory', 'asRetryFailed', 'asDirectSubmit', 'asClaimExport'):
        assert 'asResultView' in parser.nodes[control][1]
    assert 'asTrackCard' in parser.nodes['asTrackHistory'][1]
    assert 'asOrderWorkspace' not in parser.nodes['asTrackCard'][1]
    assert "$('asClaimHistory').onclick = asOpenClaimHistory;" in html
    assert "$('asTrackHistory').onclick = asOpenClaimHistory;" in html


def test_workspace_state_transitions_and_submission_guards():
    result = subprocess.run(
        ['node', str(ROOT / 'tests/fixtures/after_sale_workspace_ui.cjs')],
        cwd=ROOT, text=True, encoding='utf-8', capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
