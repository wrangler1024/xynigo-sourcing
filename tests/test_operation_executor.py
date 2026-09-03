# -*- coding: utf-8 -*-
import base64
import hashlib
import threading

from purchase_tool.operation_executor import LocalOperationExecutor


def response(body, status=200):
    return {
        'httpStatus': status,
        'responseType': 'json',
        'contentType': 'application/json',
        'body': body,
    }


class FakeRpc(object):
    def __init__(self, progress_path, snapshots):
        self.progress_path = progress_path
        self.snapshots = list(snapshots)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        path = payload['path']
        if payload['method'] == 'GET' and path == self.progress_path:
            return response(self.snapshots.pop(0))
        return response({'started': True})


class CloudPlanRpc(FakeRpc):
    def __call__(self, payload):
        self.calls.append(payload)
        path = payload['path']
        if payload['method'] == 'POST' and path == '/api/envbatch/cloud-plan':
            return response({
                'cloudPlanId': payload['body']['cloudPlanId'],
                'planId': 'local-hydrated-plan-0001',
                'count': 1,
            })
        if payload['method'] == 'GET' and path == self.progress_path:
            return response(self.snapshots.pop(0))
        return response({'started': True})


def test_bound_environment_task_reports_rows_and_uses_explicit_group():
    rpc = FakeRpc('/api/envbatch/progress', [
        {
            'running': True,
            'phase': 'creating',
            'rows': [{
                'accountId': 'a' * 64,
                'emailMasked': 'bu***01@example.test',
                'buyer': '新刚',
                'envName': 'XG-MX-0901-001',
                'containerCode': 'container-001',
                'serialNumber': '101',
                'state': 'running',
                'completedSteps': ['env_created'],
            }],
            'ipChecks': [],
        },
        {
            'running': False,
            'phase': 'completed',
            'rows': [{
                'accountId': 'a' * 64,
                'emailMasked': 'bu***01@example.test',
                'buyer': '新刚',
                'envName': 'XG-MX-0901-001',
                'containerCode': 'container-001',
                'serialNumber': '101',
                'state': 'done',
                'completedSteps': ['env_created', 'done'],
            }, {
                'accountId': 'b' * 64,
                'emailMasked': 'bu***02@example.test',
                'buyer': '新刚',
                'envName': 'XG-MX-0901-002',
                'state': 'failed',
                'completedSteps': [],
                'errorStep': 'env_created',
                'error': 'HubStudio 超时',
            }],
            'ipChecks': [{
                'envName': 'XG-MX-0901-001',
                'ip': '203.0.113.10',
                'country': 'Mexico',
                'ok': True,
            }],
            'summary': {
                'total': 2, 'done': 1, 'failed': 1,
                'ipOk': 1, 'ipTotal': 1,
            },
        },
    ])
    events = []
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    outcome, code, summary = executor.execute(
        'environment.create-bound.v1', {
            'runKey': 'environment-run-0001',
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
            'planRef': 'plan-local-0001',
            'totalCount': 2,
            'verifySampleCount': 1,
            'assignments': [{'purchaserLabel': '新刚', 'count': 2}],
        }, lambda **event: events.append(event))

    assert outcome == 'succeeded'
    assert code == 'environment_partial_failure'
    assert summary['runStatus'] == 'partial_failure'
    assert summary['successCount'] == 1
    assert summary['failedCount'] == 1
    start = rpc.calls[0]
    assert start['path'] == '/api/envbatch/start'
    assert start['body']['environmentGroup'] == 'MX采购'
    assert start['body']['operationRunKey'] == 'environment-run-0001'
    assert start['body']['assignment'] == '2:新刚'
    assert events[-1]['snapshot']['rows'][0]['ipVerified'] is True


def test_bound_environment_task_hydrates_encrypted_cloud_plan_before_start():
    rpc = CloudPlanRpc('/api/envbatch/progress', [{
        'running': False,
        'phase': 'completed',
        'rows': [{
            'accountId': 'a' * 64,
            'emailMasked': 'bu***01@example.test',
            'buyer': '新刚',
            'envName': 'XG-MX-0901-001',
            'state': 'done',
            'completedSteps': ['done'],
        }],
        'summary': {'total': 1, 'done': 1, 'failed': 0},
    }])
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    account = {
        'rowNumber': 2,
        'email': 'buyer1@example.test',
        'password': 'sensitive-password',
        'keyUrl': 'https://vendor.example/api?orderNo=00000001',
        'cookie': '[{"name":"session","value":"sensitive"}]',
        'orderNo': '00000001',
    }
    account_ref = hashlib.sha256(
        account['email'].strip().casefold().encode('utf-8')).hexdigest()

    outcome, code, _summary = executor.execute(
        'environment.create-bound.v1', {
            'runKey': 'environment-run-cloud-plan-0001',
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
            'cloudPlanId': 'cloud-plan-0001',
            'planAccounts': [account],
            'totalCount': 1,
            'verifySampleCount': 0,
            'assignments': [{'purchaserLabel': '新刚', 'count': 1}],
            'plannedEnvironmentNames': [{
                'accountRef': account_ref,
                'environmentName': 'XG-MX-0901-101',
            }],
        }, lambda **_event: None)

    assert outcome == 'succeeded'
    assert code == 'environment_completed'
    assert rpc.calls[0]['path'] == '/api/envbatch/cloud-plan'
    assert rpc.calls[0]['body']['cloudPlanId'] == 'cloud-plan-0001'
    assert rpc.calls[0]['body']['accounts'] == [account]
    assert rpc.calls[1]['path'] == '/api/envbatch/start'
    assert rpc.calls[1]['body']['planId'] == 'local-hydrated-plan-0001'
    assert rpc.calls[1]['body']['plannedEnvironmentNames'] == [{
        'accountRef': account_ref,
        'environmentName': 'XG-MX-0901-101',
    }]


def test_backup_environment_task_honors_cooperative_cancellation():
    rpc = FakeRpc('/api/envbatch/backup/progress', [
        {
            'running': True,
            'phase': 'creating',
            'rows': [{
                'envName': 'XG-MX-BF-001',
                'state': 'running',
            }],
            'ipChecks': [],
        },
        {
            'running': False,
            'phase': 'stopped',
            'stopRequested': True,
            'rows': [{
                'envName': 'XG-MX-BF-001',
                'state': 'stopped',
                'error': '安全停止：未开始执行',
            }],
            'ipChecks': [],
            'summary': {'total': 1, 'stopped': 1},
        },
    ])
    cancellation = threading.Event()
    cancellation.set()
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    outcome, code, summary = executor.execute(
        'environment.create-backup.v1', {
            'runKey': 'environment-run-0002',
            'mode': 'backup',
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
            'buyerLabel': '新刚',
            'totalCount': 1,
            'verifySampleCount': 0,
            'assignments': [],
        }, lambda **_event: None,
        cancellation_event=cancellation)

    assert outcome == 'succeeded'
    assert code == 'environment_cancelled'
    assert summary['runStatus'] == 'cancelled'
    assert any(call['path'] == '/api/envbatch/backup/stop'
               for call in rpc.calls)


def test_environment_stop_reports_cleanup_and_ip_failure_reasons():
    rpc = FakeRpc('/api/envbatch/progress', [{
        'running': False,
        'phase': 'stopped',
        'stopRequested': True,
        'rows': [{
            'accountId': 'a' * 64,
            'emailMasked': 'bu***01@example.test',
            'buyer': '新刚',
            'envName': 'XG-US-0901-001',
            'containerCode': '132725138',
            'serialNumber': '101',
            'state': 'rolled_back',
            'completedSteps': ['env_created', 'done'],
            'createdInRun': True,
            'cleanupStatus': 'deleted',
        }],
        'ipChecks': [{
            'envName': 'XG-US-0901-001',
            'ip': '', 'country': '', 'ok': False,
            'errorCode': 'hub_ip_missing',
            'error': 'HubStudio 已启动环境，但未返回出口 IP',
        }],
        'summary': {
            'total': 1, 'done': 0, 'stopped': 1, 'failed': 0,
            'cleanupTotal': 1, 'cleanupDone': 1, 'cleanupFailed': 0,
        },
    }])
    events = []
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)

    outcome, code, summary = executor.execute(
        'environment.create-bound.v1', {
            'runKey': 'environment-run-cleanup-0001',
            'site': 'US', 'purchaseDate': '20260901',
            'environmentGroup': 'US采购', 'planRef': 'plan-local-0001',
            'totalCount': 1, 'verifySampleCount': 1,
            'cleanupBlockedAccountRefs': ['a' * 64],
            'assignments': [{'purchaserLabel': '新刚', 'count': 1}],
        }, lambda **event: events.append(event))

    assert outcome == 'succeeded'
    assert code == 'environment_cancelled'
    assert summary['cleanupDone'] == 1
    assert rpc.calls[0]['body']['cleanupBlockedAccountRefs'] == ['a' * 64]
    row = events[-1]['snapshot']['rows'][0]
    assert row['status'] == 'stopped'
    assert row['createdInRun'] is True
    assert row['cleanupStatus'] == 'deleted'
    assert row['ipErrorCode'] == 'hub_ip_missing'
    assert '未返回出口 IP' in row['ipErrorSummary']


def test_logistics_task_reports_incremental_terminal_result():
    rpc = FakeRpc('/api/progress', [
        {
            'running': True,
            'rows': [{
                'serial': '101', 'envName': 'XG-MX-001',
                'state': 'running',
            }, {
                'serial': '102', 'envName': 'XG-MX-002',
                'state': 'pending',
            }],
        },
        {
            'running': False,
            'rows': [{
                'serial': '101', 'envName': 'XG-MX-001',
                'state': 'ok', 'orderNo': 'order-001',
                'orderTime': '2026-09-01 10:20:30',
                'amount': '$MXN123.45',
                'status': 'Enviado', 'statusCn': '已发货',
                'stage': 'En tránsito.',
                'tracks': ['track-001'], 'pkgs': ['package-001'],
                'carrier': 'IMILE', 'kanDan': False,
                'riskOrder': False, 'riskMessage': '',
                'ip': '203.0.113.10', 'timeZone': 'America/Mexico_City',
                'utcOffsetMinutes': -360,
                'time': '2026-09-01 10:21:31',
                'screenshotState': 'ok',
            }, {
                'serial': '102', 'envName': 'XG-MX-002',
                'state': 'login', 'error': '登录失效',
            }],
        },
    ])
    events = []
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    outcome, code, summary = executor.execute(
        'logistics.query.v1', {
            'runKey': 'logistics-run-0001',
            'queryMode': 'initial',
            'site': 'MX',
            'environmentSerials': ['101', '102'],
        }, lambda **event: events.append(event))

    assert outcome == 'succeeded'
    assert code == 'logistics_partial_failure'
    assert summary['successCount'] == 1
    assert summary['failedCount'] == 1
    assert events[-1]['current'] == 2
    assert events[-1]['snapshot']['rows'][1]['status'] == 'login'
    completed = events[-1]['snapshot']['rows'][0]
    assert completed['orderTime'] == '2026-09-01 10:20:30'
    assert completed['amount'] == '$MXN123.45'
    assert completed['fulfillmentStage'] == 'En tránsito.'
    assert completed['ipAddress'] == '203.0.113.10'
    assert completed['queriedAt'] == '2026-09-01T10:21:31-06:00'
    assert completed['screenshotStatus'] == 'ok'


def test_logistics_system_failure_preserves_reason_code_and_fails_run():
    rpc = FakeRpc('/api/progress', [{
        'running': False,
        'fatalErrorCode': 'hubstudio_browser_core_missing',
        'fatalError': 'HubStudio 浏览器内核不存在',
        'rows': [{
            'serial': '101', 'envName': 'XG-MX-001',
            'state': 'fail',
            'error': '批次已终止：HubStudio 浏览器内核不存在',
        }],
    }])
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)

    outcome, code, summary = executor.execute(
        'logistics.query.v1', {
            'runKey': 'logistics-runtime-failure-0001',
            'queryMode': 'initial',
            'site': 'MX',
            'environmentSerials': ['101'],
        }, lambda **_event: None)

    assert outcome == 'failed'
    assert code == 'hubstudio_browser_core_missing'
    assert summary['runStatus'] == 'failed'
    assert summary['errorSummary'] == 'HubStudio 浏览器内核不存在'


def test_environment_preflight_failure_preserves_reason_code_and_summary():
    rpc = FakeRpc('/api/envbatch/progress', [{
        'running': False,
        'phase': 'failed',
        'fatalErrorCode': 'environment_preflight_failed',
        'fatalError': 'HubStudio 采购分组不存在',
        'rows': [],
        'summary': {},
    }])
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)

    outcome, code, summary = executor.execute(
        'environment.create-bound.v1', {
            'runKey': 'environment-preflight-failure-0001',
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
            'planRef': 'plan-local-0001',
            'totalCount': 1,
            'verifySampleCount': 0,
            'assignments': [{'purchaserLabel': '新刚', 'count': 1}],
        }, lambda **_event: None)

    assert outcome == 'failed'
    assert code == 'environment_preflight_failed'
    assert summary['runStatus'] == 'failed'
    assert summary['errorSummary'] == 'HubStudio 采购分组不存在'


def test_logistics_task_uploads_each_available_screenshot_with_progress():
    jpeg = b'\xff\xd8synthetic\xff\xd9'

    class ScreenshotRpc(FakeRpc):
        def __call__(self, payload):
            self.calls.append(payload)
            if payload['method'] == 'GET' and payload['path'].startswith('/api/screenshot'):
                return {
                    'httpStatus': 200,
                    'responseType': 'base64',
                    'contentType': 'image/jpeg',
                    'bodyBase64': base64.b64encode(jpeg).decode('ascii'),
                }
            if payload['method'] == 'GET' and payload['path'] == self.progress_path:
                return response(self.snapshots.pop(0))
            return response({'started': True})

    rpc = ScreenshotRpc('/api/progress', [{
        'running': False,
        'rows': [{
            'serial': '101', 'envName': 'XG-MX-001', 'state': 'ok',
            'orderNo': 'order-001', 'status': 'Enviado', 'statusCn': '已发货',
            'screenshotState': 'ok', 'screenshotSizeKb': 1,
        }],
    }])
    events = []
    outcome, _code, _summary = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None
    ).execute(
        'logistics.query.v1', {
            'runKey': 'logistics-screenshot-0001', 'queryMode': 'initial',
            'site': 'MX', 'environmentSerials': ['101'],
        },
        lambda **event: events.append(event),
    )
    assert outcome == 'succeeded'
    assert events[-1]['snapshot']['screenshots'][0]['environmentSerial'] == '101'
    assert events[-1]['snapshot']['screenshots'][0]['size'] == len(jpeg)
    assert events[-1]['snapshot']['rows'][0]['screenshotSizeKb'] == 1


def test_environment_single_retry_filters_parent_rows_into_child_run():
    common_success = {
        'accountId': 'a' * 64,
        'emailMasked': 'ok***01@example.test',
        'buyer': '新刚',
        'envName': 'XG-MX-0901-001',
        'containerCode': 'container-001',
        'serialNumber': '101',
        'state': 'done',
        'completedSteps': ['done'],
    }
    retry_ref = 'b' * 64
    rpc = FakeRpc('/api/envbatch/progress', [
        {
            'running': True,
            'phase': 'creating',
            'rows': [common_success, {
                'accountId': retry_ref,
                'emailMasked': 'fa***02@example.test',
                'buyer': '新刚',
                'envName': 'XG-MX-0901-002',
                'state': 'running',
                'completedSteps': ['env_created'],
            }],
        },
        {
            'running': False,
            'phase': 'completed',
            'rows': [common_success, {
                'accountId': retry_ref,
                'emailMasked': 'fa***02@example.test',
                'buyer': '新刚',
                'envName': 'XG-MX-0901-002',
                'containerCode': 'container-002',
                'serialNumber': '102',
                'state': 'done',
                'completedSteps': ['done'],
            }],
        },
    ])
    events = []
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    outcome, code, summary = executor.execute(
        'environment.retry-row.v1', {
            'runKey': 'environment-retry-0001',
            'parentRunId': 'parent-environment-run-0001',
            'retryMode': 'single',
            'accountRefs': [retry_ref],
            'totalCount': 1,
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
        }, lambda **event: events.append(event))

    assert outcome == 'succeeded'
    assert code == 'environment_completed'
    assert summary['successCount'] == 1
    assert rpc.calls[0]['path'] == '/api/envbatch/retry-row'
    assert rpc.calls[0]['body']['accountId'] == retry_ref
    assert rpc.calls[0]['body']['operationRunKey'] == 'environment-retry-0001'
    assert len(events[-1]['snapshot']['rows']) == 1
    assert events[-1]['snapshot']['rows'][0]['accountRef'] == retry_ref


def test_environment_failed_retry_uses_batch_retry_endpoint():
    retry_ref = 'c' * 64
    rpc = FakeRpc('/api/envbatch/progress', [{
        'running': False,
        'phase': 'completed',
        'rows': [{
            'accountId': retry_ref,
            'emailMasked': 'fa***03@example.test',
            'buyer': '新刚',
            'envName': 'XG-MX-0901-003',
            'containerCode': 'container-003',
            'serialNumber': '103',
            'state': 'done',
            'completedSteps': ['done'],
        }],
    }])
    executor = LocalOperationExecutor(
        rpc, poll_interval=0.05, sleep_fn=lambda _seconds: None)
    outcome, code, _summary = executor.execute(
        'environment.retry-failed.v1', {
            'runKey': 'environment-retry-0002',
            'parentRunId': 'parent-environment-run-0001',
            'retryMode': 'failed',
            'accountRefs': [retry_ref],
            'totalCount': 1,
            'site': 'MX',
            'purchaseDate': '20260901',
            'environmentGroup': 'MX采购',
        }, lambda **_event: None)

    assert outcome == 'succeeded'
    assert code == 'environment_completed'
    assert rpc.calls[0]['path'] == '/api/envbatch/retry-failed'
