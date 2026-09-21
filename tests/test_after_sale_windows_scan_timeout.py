"""A slow Windows lookup must never turn an unscanned batch into success."""
import threading
from unittest.mock import Mock

import pytest

from purchase_tool.after_sale_claim import AfterSaleClaimer
from purchase_tool.operation_executor import LocalOperationExecutor
from test_after_sale_claim import _FakeRpc


def test_lookup_failure_retains_every_environment_and_never_opens_browser():
    claimer = AfterSaleClaimer(None)
    def fail_lookup(_serials, on_progress):
        snapshot = claimer.snapshot()
        assert len(snapshot['rows']) == 3
        assert all(row['status'] == 'running' for row in snapshot['rows'])
        on_progress(1, 3)
        assert all('已找到 1/3' in row['errorSummary'] for row in claimer.snapshot()['rows'])
        raise TimeoutError('synthetic Hub timeout')
    claimer._env_index = fail_lookup
    claimer._run_environment_jobs = Mock(side_effect=AssertionError('must not scan'))
    claimer._run_batch('scan', ['SYNTH-A', 'SYNTH-B', 'SYNTH-C'], 'headless')
    snapshot = claimer.snapshot()
    assert snapshot['running'] is False
    assert len(snapshot['rows']) == 3
    assert all(row['status'] == 'fail' and 'synthetic Hub timeout' in row['errorSummary'] for row in snapshot['rows'])
    claimer._run_environment_jobs.assert_not_called()


@pytest.mark.parametrize('statuses,expected', [
    (['fail','fail'], 'failed'),
    (['ok','fail'], 'failed'),
    (['empty','skip'], 'succeeded'),
])
def test_final_outcome_reflects_environment_status(statuses, expected):
    rows = [{'environmentSerial': f'SYNTH-{i}', 'status': status,
             'orders': [], 'errorSummary': 'synthetic lookup timeout' if status == 'fail' else None}
            for i,status in enumerate(statuses)]
    rpc = _FakeRpc([{'running': False, 'rows': rows}])
    reports = []
    executor = LocalOperationExecutor(rpc, poll_interval=0.001, sleep_fn=lambda _s: None)
    outcome, code, summary = executor._execute_after_sale_scan(
        {'environmentSerials': [r['environmentSerial'] for r in rows]},
        lambda **event: reports.append(event), threading.Event())
    assert outcome == expected
    assert reports[-1]['current'] == len(rows)
    assert len(reports[-1]['snapshot']['rows']) == len(rows)
    if expected == 'failed':
        assert code == 'after_sale_scan_failed'
        assert summary['runStatus'] == 'failed'
        assert 'errorSummary' in summary
    else:
        assert code == 'after_sale_scan_completed'
        assert summary['claimableCount'] == 0
