"""请求顺序与任务级进度；历史退款结果不能让新回访提前完成。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from xynigo_auth.models import AfterSaleRefundTracking, ExecutorTask
from test_after_sale_claim import (
    _e2e_setup, _lease_and_start, _track_row, AS_CAPABILITIES, CLIENT_VERSION,
)
from test_executor_channel import CSRF, device_headers, heartbeat


def test_track_progress_is_per_task_ordered_and_not_prefilled_by_old_results(tmp_path):
    for web, device, ids, database in _e2e_setup(tmp_path):
        credential = ids['credential']
        heartbeat(device, credential, capabilities=AS_CAPABILITIES, client_version=CLIENT_VERSION)
        items = [{'environmentSerial': 'SYNTHENV', 'orderNo': 'SYNTH'+key,
                  'refundBillId': 'BILL'+key} for key in ['Z', 'A', 'M']]

        def create(key):
            res = web.post('/v1/after-sale/track', headers=CSRF, json={
                'executorId': ids['executorId'], 'idempotencyKey': key, 'items': items})
            assert res.status_code == 202, res.text
            return res.json()['data']['taskId']

        def snapshot(task):
            res = web.get('/v1/after-sale/track/'+task)
            assert res.status_code == 200, res.text
            return res.json()['data']['summary']

        task = create('synthetic-first-track')
        with database.session_factory() as session:
            model = session.get(ExecutorTask, uuid.UUID(task))
            for item in items:
                session.add(AfterSaleRefundTracking(
                    tenant_id=model.tenant_id, refund_bill_id=item['refundBillId'],
                    order_no=item['orderNo'], environment_serial='SYNTHENV',
                    phase='refunded', phase_label='已退款', amount='99.00',
                    last_status='ok', checked_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
            session.commit()
        fresh = snapshot(task)
        assert fresh['progressCompleted'] == 0
        assert fresh['progressTotal'] == 3
        assert [r['refundBillId'] for r in fresh['rows']] == ['BILLZ', 'BILLA', 'BILLM']
        assert all(r['status'] == 'queued' and not r['amount'] for r in fresh['rows'])
        _, token = _lease_and_start(device, credential, expect_type='after.sale.track.v1')

        def report(task_id, lease, rows, done):
            return device.post('/v1/executor-channel/tasks/'+task_id+'/progress',
                headers=device_headers(credential), json={
                    'leaseToken': lease, 'phase': 'after_sale.track.running',
                    'current': done, 'total': 3, 'snapshot': {'rows': rows}})

        waiting = [{**item, 'status': 'running' if item['refundBillId']=='BILLZ' else 'queued'}
                   for item in reversed(items)]
        assert report(task, token, waiting, 0).status_code == 200
        assert snapshot(task)['progressCompleted'] == 0
        done_rows = [_track_row(item['refundBillId'], order_no=item['orderNo'], serial='SYNTHENV')
                     for item in items]
        assert report(task, token, done_rows[:1], 1).status_code == 200
        mid = snapshot(task)
        assert mid['progressCompleted'] == 1
        assert [r['status'] for r in mid['rows']] == ['ok', 'queued', 'queued']
        assert report(task, token, list(reversed(done_rows)), 3).status_code == 200
        final = device.post('/v1/executor-channel/tasks/'+task+'/finish',
            headers=device_headers(credential), json={
                'leaseToken': token, 'outcome': 'succeeded', 'resultCode':'after_sale_track_completed',
                'resultSummary': {'runStatus':'completed','phase':'after_sale.track.completed',
                    'progressCompleted':3,'progressTotal':3,'totalCount':3,'successCount':3,
                    'failedCount':0,'stoppedCount':0,'phaseCounts':{'reviewing':3},
                    'errorCode':'','errorSummary':''}})
        assert final.status_code == 200, final.text
        second = create('synthetic-second-track')
        assert snapshot(second)['progressCompleted'] == 0
        _, token2 = _lease_and_start(device, credential, expect_type='after.sale.track.v1')
        changed = [{**items[0], 'status':'fail','errorSummary':'synthetic failure'},
                   {**items[1], 'status':'stopped'}, {**items[2], 'status':'stopped'}]
        assert report(second, token2, changed, 3).status_code == 200
        assert snapshot(second)['progressCompleted'] == 1
        assert snapshot(second)['stoppedCount'] == 2
        # 同一退款单的另一任务已覆盖全局记录，首批仍保留自己的顺序与结果。
        first = snapshot(task)
        assert first['progressCompleted'] == 3
        assert [r['refundBillId'] for r in first['rows']] == ['BILLZ','BILLA','BILLM']
        assert all(r['status']=='ok' for r in first['rows'])
        # 禁止单号/环境串行、同退款单重复上报影响计数。
        bad = report(second, token2, [{**items[0], 'status':'ok', 'orderNo':'OTHER'}], 1)
        assert bad.status_code == 422
        bad = report(second, token2, [changed[0], changed[0]], 2)
        assert bad.status_code == 422
        assert snapshot(second)['progressCompleted'] == 1
        break


def test_claim_snapshot_and_history_follow_request_order(tmp_path):
    for web, device, ids, _database in _e2e_setup(tmp_path):
        credential = ids['credential']
        heartbeat(device, credential, capabilities=AS_CAPABILITIES, client_version=CLIENT_VERSION)
        items = [{'environmentSerial': env, 'orderNo': order}
                 for env, order in [('A','SYNTHZ'),('B','SYNTHA'),('A','SYNTHM')]]
        created = web.post('/v1/operation-runs/after-sale-claim', headers=CSRF, json={
            'executorId':ids['executorId'], 'idempotencyKey':'synthetic-claim-order', 'items':items})
        assert created.status_code == 202, created.text
        run_id = created.json()['data']['runId']
        task, token = _lease_and_start(device, credential, expect_type='after.sale.claim.v1')
        progress = device.post('/v1/executor-channel/tasks/'+task+'/progress',
            headers=device_headers(credential), json={
                'leaseToken':token, 'phase':'after_sale.claim.running','current':0,'total':3,
                'snapshot':{'rows':[{**item,'status':'queued'} for item in reversed(items)]}})
        assert progress.status_code == 200, progress.text
        for suffix in ['/'+run_id, '/history/'+run_id]:
            result = web.get('/v1/operation-runs/after-sale-claim'+suffix)
            assert result.status_code == 200, result.text
            assert [r['orderNo'] for r in result.json()['data']['rows']] == ['SYNTHZ','SYNTHA','SYNTHM']
        break
