"""Environment-discovered skip facts survive progress, final results and history."""
from test_after_sale_claim import _e2e_setup, _lease_and_start, AS_CAPABILITIES, CLIENT_VERSION
from test_executor_channel import CSRF, device_headers, heartbeat


def test_discovered_skip_product_and_refund_facts_roundtrip(tmp_path):
    for web, device, ids, database in _e2e_setup(tmp_path):
        credential=ids['credential']
        heartbeat(device,credential,capabilities=AS_CAPABILITIES,client_version=CLIENT_VERSION)
        response=web.post('/v1/operation-runs/after-sale-claim',headers=CSRF,json={
            'executorId':ids['executorId'],'idempotencyKey':'synthetic-feedback-facts',
            'environmentSerials':['900001']})
        assert response.status_code==202,response.text
        run=response.json()['data']['runId']
        task,lease=_lease_and_start(device,credential,expect_type='after.sale.claim.v1')
        facts={'goodsImg':'https://img.ltwebstatic.com/synthetic.jpg',
               'goodsImages':['https://img.ltwebstatic.com/synthetic.jpg'],
               'goodsItems':[{'goodsImg':'https://img.ltwebstatic.com/synthetic.jpg','name':'Synthetic','specification':'','quantity':2}],
               'itemCount':2,'deliveredAt':'01 Sep 2026 12:00:00'}
        row={'orderNo':'SYNTHORDER','environmentSerial':'900001','status':'skip',**facts,
             'note':'已有退款申请，审核中；本次跳过提交','operationCompletedAt':'2026-09-01T05:00:00Z',
             'refundBillId':'900000000000001','refunds':[{'refundBillId':'900000000000001',
              'source':'existing','phase':'reviewing','phaseLabel':'审核中',
              'refundPath':'Cuenta original de pago','refundAccount':'****1234'}]}
        env={'environmentSerial':'900001','status':'skip','entryCount':0,'submittedCount':0,'note':'已有退款申请'}
        progress=device.post(f'/v1/executor-channel/tasks/{task}/progress',headers=device_headers(credential),json={
            'leaseToken':lease,'phase':'after_sale.claim.running','current':1,'total':1,
            'snapshot':{'rows':[row],'environments':[env]}})
        assert progress.status_code==200,progress.text
        # Sparse legacy final frame must retain observed products, delivery and receipt.
        sparse={k:v for k,v in row.items() if k not in facts}
        final=device.post(f'/v1/executor-channel/tasks/{task}/finish',headers=device_headers(credential),json={
            'leaseToken':lease,'outcome':'succeeded','resultCode':'after_sale_completed',
            'resultSummary':{'runStatus':'completed','phase':'after_sale.completed','totalCount':1,
              'progressTotal':1,'progressCompleted':1,'successCount':0,'failedCount':0,'skippedCount':1,
              'stoppedCount':0,'uncertainCount':0,'rows':[sparse],'environments':[env]}})
        assert final.status_code==200,final.text
        for path in [f'/v1/operation-runs/after-sale-claim/{run}',f'/v1/operation-runs/after-sale-claim/history/{run}']:
            response=web.get(path);assert response.status_code==200,response.text
            data=response.json()['data'];saved=data['rows'][0]
            for key in facts:assert saved[key]==facts[key],(key,saved)
            assert saved['status']=='skip' and saved['refunds'][0]['source']=='existing'
            assert saved['operationCompletedAt'] and saved['note']==row['note']
        snapshot=web.get(f'/v1/operation-runs/after-sale-claim/{run}').json()['data']
        assert snapshot['successCount']==0 and snapshot['skippedCount']==1
        break
