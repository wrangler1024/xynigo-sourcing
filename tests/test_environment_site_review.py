"""Synthetic regression cases for accidental cross-site environment creation."""
import base64
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import time
from unittest.mock import patch

import pytest
from openpyxl import Workbook

from purchase_tool.env_batch import environment_site_review, ResumeStateStore
from purchase_tool.main import EnvBatchJob
from test_env_web import FakeHub, TEST_PROXY, TEST_US_TAG


@pytest.mark.parametrize('filename,hints,conflict', [
    ('mx-20.xlsx', ['MX'], True), ('20MX.xlsx', ['MX'], True),
    ('美国买家号.xlsx', ['US'], False), ('墨西哥20.xlsx', ['MX'], True),
    ('US-10 (1).xlsx', ['US'], False), ('MX_US.xlsx', ['MX', 'US'], True),
    ('music-custom-business.xlsx', [], False), ('buyers.xlsx', [], False),
])
def test_filename_hints_are_bounded_not_account_identity(filename, hints, conflict):
    result = environment_site_review(filename, 'US', 0)
    assert result['filenameSiteHints'] == hints
    assert result['filenameSiteConflict'] is conflict
    assert result['siteConfirmationRequired'] is conflict


def mixed_workbook():
    workbook = Workbook()
    workbook.active.append(['buyer@example.test', 'synthetic-password',
        'https://vendor.example/api?orderNo=abc123',
        '[{"domain":".shein.com.mx"},{"domain":".us.shein.com"}]'])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return base64.b64encode(output.getvalue()).decode('ascii')


def test_local_start_cannot_consume_unconfirmed_or_wrong_site_plan(tmp_path):
    hub = FakeHub()
    hub.groups = [TEST_US_TAG]
    job = EnvBatchJob(lambda: hub, lambda: {'purchaseTag': TEST_US_TAG, 'proxyLink': TEST_PROXY})
    plan = job.parse('mx-20.xlsx', mixed_workbook(), site='US')
    assert plan['mixedSiteCookieCount'] == 1
    assert plan['siteConfirmationRequired'] is True
    assert plan['filenameSiteConflict'] is True
    common = dict(site='US', environment_group=TEST_US_TAG)
    for confirmation in ({}, {'confirmed_site': 'MX'}, {'confirmed_site': 'US'},
                         {'confirmed_site': 'US', 'confirm_filename_site_mismatch': 'true'},
                         {'confirmed_site': 'US', 'confirm_filename_site_mismatch': 1}):
        with pytest.raises(ValueError):
            job.start(plan['planId'], '1:新刚', '20260908', confirm_write=True,
                      **common, **confirmation)
        assert plan['planId'] in job.pending
        assert not job.running
        assert hub.calls == [], 'rejected requests must not even reach HubStudio preflight'
    with pytest.raises(ValueError):
        job.preview(plan['planId'], '1:新刚', '20260908', **common)
    with patch('purchase_tool.main.ResumeStateStore', lambda batch_id: ResumeStateStore(batch_id, tmp_path)):
        job.start(plan['planId'], '1:新刚', '20260908', confirm_write=True,
                  verify_sample_count=0, confirmed_site='US',
                  confirm_filename_site_mismatch=True, **common)
        deadline = time.monotonic() + 5
        while job.running and time.monotonic() < deadline:
            time.sleep(.02)
    assert not job.running
    assert job.snapshot()['summary']['done'] == 1
    assert ('account', 'US') in hub.calls


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_real_site_review_handlers_reset_and_block_stale_confirmations():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/fixtures/environment_site_review_ui.cjs')],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
