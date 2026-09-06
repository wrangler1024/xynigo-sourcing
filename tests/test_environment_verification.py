"""Synthetic regressions for desktop-owned sampling and live IP progress."""
import base64
from io import BytesIO
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
import pytest

import purchase_tool.main as main
import test_env_config as config_tests
from test_env_web import FakeHub, TEST_TAG, runtime_config
from test_operation_executor import FakeRpc
from purchase_tool.env_batch import BatchEnvOrchestrator, BackupEnvOrchestrator
from purchase_tool.operation_executor import LocalOperationExecutor


@pytest.mark.parametrize('mode', ['bound', 'backup', 'test'])
@pytest.mark.parametrize('configured', [0, 1, 3])
def test_http_start_uses_local_count_not_legacy_all_request(mode, configured):
    case = config_tests.ConfigRouteTests()
    case.setUp()
    try:
        state = main.STATE
        state.cfg.update(runtime_config(), verifySampleCount=configured)
        state.tasks = SimpleNamespace(begin=lambda *_:'synthetic-run',
                                      reserve=lambda *_:None, finish=lambda *_:None)
        state._hub_reads = SimpleNamespace(invalidate=lambda:None)
        hub = FakeHub()
        starts = []
        hub.browser_start = lambda code, **_: starts.append(code) or {'ip':'203.0.113.10'}
        hub.browser_stop = lambda code: None
        # Skip only creation writes. Use real job acceptance, sample selection,
        # IP probe loop and progress callbacks, with a synthetic Hub adapter.
        def created(runner):
            state.cfg['verifySampleCount'] = 10  # edits cannot change this attempt
            for i, row in enumerate(runner.rows):
                row.state = 'done'
                row.container_code = str(i + 1000)
                row.serial_number = i + 1000
            runner._persist()
            return runner.rows

        if mode == 'bound':
            job = main.EnvBatchJob(lambda:hub, lambda:state.cfg)
            state.env_job = job
            workbook = Workbook()
            for i in range(100):
                workbook.active.append([f'fixture{i}@example.test', 'synthetic',
                                       f'https://codes.example.test/get?orderNo={i:08x}', '[]'])
            data = BytesIO()
            workbook.save(data)
            plan = job.parse('fixture.xlsx', base64.b64encode(data.getvalue()).decode())
            path = '/api/envbatch/start'
            body = {'planId':plan['planId'], 'assignment':'34:新刚,33:志恒,33:康德'}
            runner_type = BatchEnvOrchestrator
        else:
            job = main.BackupEnvJob(lambda:hub, lambda:state.cfg)
            state.backup_job = job
            path = '/api/envbatch/backup/start'
            body = {'buyer':'新刚', 'count':10, 'type':'测试' if mode == 'test' else '备用'}
            runner_type = BackupEnvOrchestrator
        with patch.object(runner_type, 'run', created), patch(
                'purchase_tool.env_batch.lookup_ip_country',
                return_value={'countryCode':'MX', 'country':'Mexico'}):
            response = case._post_json(path, {
                **body, 'purchaseDate':'20260906', 'site':'MX',
                'environmentGroup':TEST_TAG, 'confirmWrite':True,
                'verifySampleCount':100,  # legacy/tampered browser value
            })
            assert response['started']
            deadline = time.monotonic() + 5
            while job.snapshot()['running'] and time.monotonic() < deadline:
                time.sleep(.01)
            snap = job.snapshot()
            assert not snap['running'] and not snap['fatalError'], snap
            assert len(starts) == configured
            assert snap['verifySampleCount'] == configured
            assert snap['ipCheckTotal'] == snap['ipCheckDone'] == configured
            assert snap['summary']['ipOk'] == snap['summary']['ipTotal'] == configured
        if mode == 'bound':
            # Retry both one row and a failed subset through the HTTP boundary.
            old_codes = set(starts)
            for retry_path, selection in [('/api/envbatch/retry-row', [10]),
                                          ('/api/envbatch/retry-failed', [20, 21])]:
                for i in selection:
                    job.runner.rows[i].state = 'failed'
                state.cfg['verifySampleCount'] = configured
                starts.clear()
                def repaired(row):
                    row.state = 'done'
                    job.runner._persist()
                body = {'verifySampleCount':100}
                if retry_path.endswith('retry-row'):
                    body['accountId'] = job.runner.rows[selection[0]].account.account_id
                with patch.object(job.runner, '_run_one', repaired), patch(
                        'purchase_tool.env_batch.lookup_ip_country',
                        return_value={'countryCode':'MX'}):
                    assert case._post_json(retry_path, body)['started']
                    deadline = time.monotonic() + 5
                    while job.snapshot()['running'] and time.monotonic() < deadline:
                        time.sleep(.01)
                assert len(starts) == min(configured, len(selection))
                assert set(starts).issubset({str(1000+i) for i in selection})
                assert not set(starts) & old_codes
                assert not job.snapshot()['fatalError']
            job._clear_sensitive()
    finally:
        case.tearDown()


def test_retry_selection_only_probes_eligible_retry_rows():
    hub = FakeHub()
    starts = []
    hub.browser_start = lambda code, **_: starts.append(code) or {'ip':'203.0.113.10'}
    hub.browser_stop = lambda code: None
    job = main.EnvBatchJob(lambda:hub, runtime_config)
    runner = BatchEnvOrchestrator(hub, purchase_tag=TEST_TAG,
                                  proxy_link=runtime_config()['proxyLink'])
    def row(code, state='done'):
        return SimpleNamespace(state=state, container_code=code,
                               env_name=code, account=SimpleNamespace(buyer='新刚'))
    old, retried, failed = row('old'), row('retried'), row('failed', 'failed')
    runner.rows = [old, retried, failed]
    job.runner = runner
    job.verify_sample_count = 3
    with patch('purchase_tool.env_batch.lookup_ip_country',
               return_value={'countryCode':'MX'}):
        job._verify_retry_ips([retried, failed])
    assert starts == ['retried']
    assert job.snapshot()['ipCheckTotal'] == 1


@pytest.mark.parametrize('extended', [False, True])
def test_executor_transmits_actual_plan_only_to_opted_in_cloud(extended):
    snap = {'running':False, 'phase':'completed', 'rows':[],
            'verifySampleCount':1, 'ipCheckTotal':1}
    rpc = FakeRpc('/api/envbatch/backup/progress', [snap])
    events = []
    LocalOperationExecutor(rpc).execute('environment.create-backup.v1', {
        'runKey':'synthetic-run', 'site':'MX', 'totalCount':10,
        'environmentGroup':TEST_TAG, 'purchaseDate':'20260906',
        'buyerLabel':'新刚', 'mode':'backup', 'verifySampleCount':10,
        **({'ipVerificationProgress':1} if extended else {}),
    }, lambda **event:events.append(event))
    snapshot = events[-1]['snapshot']
    assert ('ipVerification' in snapshot) == extended
    if extended:
        assert snapshot['ipVerification'] == {'requestedCount':1, 'totalCount':1}


def test_real_web_snapshot_does_not_invent_total_or_send_all_count():
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
const start = html.indexOf('function renderEnvironmentVerificationSetting(');
const end = html.indexOf('async function cloudEnvironmentSnapshotWithHistory', start);
const label = {};
const ctx = {cloudRunElapsed:()=>0, cloudRunHubConnected:()=>true, $:()=>label};
vm.runInNewContext(html.slice(start,end),ctx);
ctx.renderEnvironmentVerificationSetting(1);
assert(label.textContent.includes('抽检 1 个'));
ctx.renderEnvironmentVerificationSetting(0);
assert(label.textContent.includes('不抽检'));
const rows = Array.from({length:100},(_,i)=>({accountRef:`synthetic-${i}`,
  status:'success',ipVerified:i<30?true:null}));
const run = {rows, totalCount:100, phase:'environment.ip_checking', terminal:false,
  ipOkCount:0, ipTotalCount:0, ipCheckTotal:100};
let snap = ctx.cloudEnvironmentLegacySnapshot(run);
assert.equal(ctx.environmentIpProgressText(snap),'出口 IP 检测中：30/100');
assert.equal(snap.summary.ipOk,30); assert.equal(snap.summary.ipTotal,30);
delete run.ipCheckTotal;
snap = ctx.cloudEnvironmentLegacySnapshot(run);
assert.equal(snap.ipCheckTotal,null);
assert(ctx.environmentIpProgressText(snap).includes('计划数待确认'));
assert.equal(ctx.environmentIpProgressText({phase:'ip_checking',ipCheckDone:0,ipCheckTotal:1}),
  '出口 IP 检测中：0/1');
assert.equal(ctx.environmentIpProgressText({phase:'completed',ipCheckDone:1,ipCheckTotal:1}),
  '出口 IP 检测完成：1/1');
assert.equal(ctx.environmentIpProgressText({phase:'stopped',ipCheckDone:1,ipCheckTotal:3}),
  '出口 IP 检测已结束：1/3');
const submit = html.slice(html.indexOf("$('btnEnvStart').onclick"),
  html.indexOf("$('btnEnvStart').onclick")+9000);
assert(!submit.includes('verifySampleCount:'));
assert(!html.includes("$('envVerify').checked"));
'''
    subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1],
                   capture_output=True, text=True, check=True)
