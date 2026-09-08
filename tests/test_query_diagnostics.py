"""Synthetic-only diagnostic coverage; no browser or business API is accessed."""
import json
from types import SimpleNamespace

import pytest

import purchase_tool.shein_query as query
from purchase_tool.cloud_auth import LocalAuthError
from purchase_tool.hub_api import HubApiError
from purchase_tool.operation_executor import LocalOperationExecutor
from test_operation_executor import FakeRpc


def test_failed_close_preserves_first_and_last_codes_without_vendor_text():
    class Hub:
        def browser_stop(self, code):
            raise HubApiError('password=NEVER-UPLOAD-VENDOR-TEXT',
                              'hubstudio_system_resources_insufficient', api_code='-10008')
    job = query.QueryOrchestrator(Hub())
    job._environment_serial_by_code['synthetic-container'] = '1001'
    assert job._stop_browser_and_confirm('synthetic-container') is False
    snapshot = job.snapshot()
    details = snapshot['diagnostics']['closeChecks'][0]
    assert details['firstErrorOperation'] == 'browser_stop'
    assert details['firstApiCode'] == '-10008'
    assert details['stopAttempts'] == 1 and details['statusChecks'] == 0
    assert snapshot['diagnostics']['pendingCloseCount'] == 1
    assert details['confirmed'] is False
    assert 'NEVER-UPLOAD' not in json.dumps(snapshot['diagnostics'])
    assert 'synthetic-container' not in json.dumps(snapshot['diagnostics'])


def test_close_timeout_and_later_confirmation_keep_correct_last_state(monkeypatch):
    class Clock:
        now = 1000.0
        def time(self): return self.now
        def sleep(self, seconds): self.now += seconds
    class Hub:
        closed = False
        def browser_stop(self, code): pass
        def browser_lifecycle_status(self, code, timeout=None):
            return {'state': 'closed' if self.closed else 'closing', 'data': {}}
    clock, hub = Clock(), Hub()
    monkeypatch.setattr(query, 'time', SimpleNamespace(time=clock.time, sleep=clock.sleep))
    job = query.QueryOrchestrator(hub)
    job._environment_serial_by_code['synthetic'] = '1001'
    assert not job._stop_browser_and_confirm('synthetic')
    check = job.snapshot()['diagnostics']['closeChecks'][0]
    assert check['elapsedMs'] == 60000 and check['state'] == 'closing'
    assert check['statusChecks'] == 60 and check['stopSent'] is True
    hub.closed = True
    assert job._wait_for_pending_closes(clock.now + 10)
    check = job.snapshot()['diagnostics']['closeChecks'][0]
    assert check['confirmed'] is True and check['state'] == 'closed'
    assert job.snapshot()['diagnostics']['pendingCloseCount'] == 0


@pytest.mark.parametrize('negotiated', [False, True])
def test_single_retry_only_uploads_selected_rows_and_negotiated_diagnostics(negotiated):
    diagnostic = {'schemaVersion': 1, 'resourceConstrained': False,
                  'effectiveConcurrency': 2, 'pendingCloseCount': 0,
                  'closeChecks': [{'environmentSerial': '1001', 'confirmed': True,
                      'state': 'closed', 'stopSent': True, 'elapsedMs': 5,
                      'stopAttempts': 1, 'statusChecks': 1, 'rawMessage': 'DO-NOT-UPLOAD'},
                      {'environmentSerial': '1002', 'confirmed': False}]}
    rpc = FakeRpc('/api/progress', [{
        'running': False, 'rows': [
            {'serial': '1001', 'state': 'ok'},
            *[{'serial': str(n), 'state': 'fail', 'screenshotState': 'ok'}
              for n in range(1002, 1024)]], 'diagnostics': diagnostic}])
    events = []
    payload = {'runKey': 'synthetic-single-retry', 'queryMode': 'single_retry',
               'site': 'MX', 'environmentSerials': ['1001']}
    if negotiated:
        payload['diagnosticsVersion'] = 1
    outcome, code, summary = LocalOperationExecutor(rpc).execute(
        'logistics.query.v1', payload, lambda **event: events.append(event))
    assert outcome == 'succeeded' and summary['totalCount'] == summary['successCount'] == 1
    assert summary['failedCount'] == 0
    assert [r['environmentSerial'] for r in events[0]['snapshot']['rows']] == ['1001']
    assert ('diagnostics' in events[0]['snapshot']) is negotiated
    assert 'DO-NOT-UPLOAD' not in json.dumps(events)
    assert not any('screenshot' in item['path'] for item in rpc.calls)
    if negotiated:
        assert len(events[0]['snapshot']['diagnostics']['closeChecks']) == 1


def test_persistent_progress_rejection_finishes_local_work_then_reports_failure():
    rpc = FakeRpc('/api/progress', [
        {'running': True, 'rows': [{'serial': '1001', 'state': 'running'}]},
        {'running': False, 'rows': [{'serial': '1001', 'state': 'ok'}]},
    ])
    def rejected(**event):
        raise LocalAuthError('executor_progress_snapshot_invalid', 'SENSITIVE-ERROR', 422)
    outcome, code, summary = LocalOperationExecutor(rpc, sleep_fn=lambda _: None).execute(
        'logistics.query.v1', {'runKey': 'synthetic-rejected-progress',
            'queryMode': 'initial', 'site': 'MX', 'environmentSerials': ['1001']}, rejected)
    assert not rpc.snapshots  # local work was allowed to finish
    assert outcome == 'failed' and code == 'executor_progress_rejected'
    assert summary['successCount'] == 1
    assert 'SENSITIVE-ERROR' not in json.dumps(summary)


def test_diagnostic_buffer_is_bounded_and_resets_between_runs():
    job = query.QueryOrchestrator(object())
    for n in range(30):
        job._environment_serial_by_code[str(n)] = str(1000 + n)
        job._record_close_diagnostic(str(n), confirmed=n != 0)
    checks = job.snapshot()['diagnostics']['closeChecks']
    assert len(checks) == 20
    assert checks[0]['environmentSerial'] == '1000'
    job._prepare_run(['1000'], 'MX', fresh=False)
    assert job.snapshot()['diagnostics']['closeChecks'] == []
