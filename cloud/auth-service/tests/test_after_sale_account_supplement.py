"""Read-side account supplements never rewrite accepted submission evidence."""
import copy
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from xynigo_auth.models import AfterSaleClaimResult, AfterSaleClaimRun, AfterSaleRefundTracking, Tenant
from xynigo_auth.operation_service import after_sale_claim_snapshot, after_sale_tracking_snapshot
from xynigo_auth.executor_service import ExecutorChannelService, ExecutorServiceError
from test_after_sale_claim import _e2e_setup, _lease_and_start, AS_CAPABILITIES, CLIENT_VERSION
from test_executor_channel import CSRF, device_headers, heartbeat


def test_track_supplements_missing_card_without_mutating_submission_or_crossing_identity(tmp_path):
    for web,device,ids,database in _e2e_setup(tmp_path):
        credential=ids['credential']
        heartbeat(device,credential,capabilities=AS_CAPABILITIES,client_version=CLIENT_VERSION)
        products=['https://img.ltwebstatic.com/synthetic-a.jpg','https://img.ltwebstatic.com/synthetic-b.jpg']
        created=web.post('/v1/operation-runs/after-sale-claim',headers=CSRF,json={
            'executorId':ids['executorId'],'idempotencyKey':'account-supplement',
            'items':[{'orderNo':'SYNTH','environmentSerial':'ENV','goodsImages':products}]})
        assert created.status_code==202,created.text
        run_id=uuid.UUID(created.json()['data']['runId'])
        task,lease=_lease_and_start(device,credential,expect_type='after.sale.claim.v1')
        raw={'orderNo':'SYNTH','environmentSerial':'ENV','status':'ok','refundBillId':'12345',
             'operationCompletedAt':'2026-09-01T00:00:20Z',
             'refundPath':'Cuenta original de pago / 其他退款渠道（名称未取得）',
             'refunds':[{'refundBillId':'12345','source':'submit_response','refundAccount':'',
                'refundPath':'Cuenta original de pago / 其他退款渠道（名称未取得）','detailsNote':'退款账户未读取'}]}
        posted=device.post(f'/v1/executor-channel/tasks/{task}/progress',headers=device_headers(credential),json={
            'leaseToken':lease,'phase':'after_sale.claim.running','current':1,'total':1,'snapshot':{'rows':[raw]}})
        assert posted.status_code==200,posted.text
        with database.session_factory() as session:
            run=session.get(AfterSaleClaimRun,run_id)
            result=session.scalar(select(AfterSaleClaimResult).where(AfterSaleClaimResult.run_id==run_id))
            before=copy.deepcopy(result.refunds); completed=result.operation_completed_at
            other=Tenant(feishu_tenant_key='synthetic-other-tenant');session.add(other);session.flush()
            session.add(AfterSaleRefundTracking(tenant_id=other.id,refund_bill_id='12345',order_no='SYNTH',
                environment_serial='ENV',refund_account='****8888',checked_at=datetime.now(timezone.utc),last_status='ok'))
            session.flush()
            assert not after_sale_claim_snapshot(session,run)['rows'][0]['refundAccount'], 'never supplement another tenant'
            track=AfterSaleRefundTracking(tenant_id=run.tenant_id,refund_bill_id='12345',order_no='OTHER',
                environment_serial='ENV',refund_account='****1234',checked_at=datetime.now(timezone.utc),last_status='ok')
            session.add(track);session.flush()
            assert not after_sale_claim_snapshot(session,run)['rows'][0]['refundAccount']
            track.order_no='SYNTH';track.environment_serial='OTHER'
            session.flush()
            assert not after_sale_claim_snapshot(session,run)['rows'][0]['refundAccount']
            track.environment_serial='ENV';session.flush()
            view=after_sale_claim_snapshot(session,run)['rows'][0]
            assert view['refundAccount']=='****1234' and view['refundAccountSource']=='tracking'
            assert view['refundPath']=='Cuenta original de pago'
            assert view['refunds'][0]['refundAccountCheckedAt']
            assert not view['refunds'][0]['detailsNote']
            assert result.refunds==before and result.operation_completed_at==completed and result.status=='ok'
            assert not result.refund_account
            images=after_sale_tracking_snapshot(session,run.tenant_id,['12345'])['rows'][0]
            assert images['goodsImages']==products
            service=object.__new__(ExecutorChannelService);service.session=session
            task_row=SimpleNamespace(tenant_id=run.tenant_id,progress_summary={})
            for order,environment in [('OTHER','ENV'),('SYNTH','OTHER')]:
                item={'refundBillId':'12345','orderNo':order,'environmentSerial':environment}
                service._request_payload=lambda _task, item=item:{'items':[item]}
                with pytest.raises(ExecutorServiceError):
                    service._upsert_after_sale_track(task_row,{'rows':[dict(item,status='fail')]},datetime.now(timezone.utc))
                assert track.order_no=='SYNTH' and track.environment_serial=='ENV' and track.refund_account=='****1234'
                assert task_row.progress_summary=={}, 'invalid identity must not enter task progress either'
            item={'refundBillId':'12345','orderNo':'SYNTH','environmentSerial':'ENV'}
            service._request_payload=lambda _task:{'items':[item]}
            service._upsert_after_sale_track(task_row,{'rows':[dict(item,status='ok',refundAccount='',checkedAt='2026-09-17T01:00:00Z')]},datetime.now(timezone.utc))
            assert track.refund_account=='****1234', 'an empty later read cannot erase an existing masked card'
            # An existing receipt account is authoritative, even if tracking differs.
            result.refunds=[dict(before[0],refundAccount='****9999')];session.flush()
            assert after_sale_claim_snapshot(session,run)['rows'][0]['refundAccount']=='****9999'
        from xynigo_auth.after_sale_export import _EXPORT_SLOT
        assert _EXPORT_SLOT.acquire(blocking=False)
        try:
            busy=web.get(f'/v1/operation-runs/after-sale-claim/history/{run_id}/export')
            assert busy.status_code==429 and busy.headers['Retry-After']=='5'
        finally:
            _EXPORT_SLOT.release()
        break
