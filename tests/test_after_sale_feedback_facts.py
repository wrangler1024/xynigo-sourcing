"""Skipped orders keep facts and existing receipts; no write actions in these cases."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from purchase_tool import after_sale_claim as module
from purchase_tool.after_sale_claim import AfterSaleClaimer
from purchase_tool.operation_executor import LocalOperationExecutor
from purchase_tool.after_sale_phase import classify_phase_state
from test_after_sale_phase import state


def run_cards(cards, recover=None):
    claimer = AfterSaleClaimer(None, order_stagger=(0, 0))
    page = Mock()
    claimer._open_env = Mock(return_value=(page, True))
    claimer._stop_env = Mock()
    claimer._login_required = lambda _: False
    claimer._read_all_order_cards = lambda _: cards
    claimer._claim_one = Mock(side_effect=AssertionError('No-entry orders must not submit'))
    claimer._scan_pre_info = Mock(side_effect=AssertionError('No entry needs no eligibility write preparation'))
    if recover is not None:
        claimer._recover_receipts = recover(claimer)
    with patch.object(module, 'read_order_detail_facts', side_effect=RuntimeError('detail unavailable')):
        claimer._claim_env_one('900001', {'containerCode':'SYNTHENV', 'containerName':'Synthetic'}, True)
    return claimer


def card(order, status, detail=''):
    return {'text': f'Núm. de pedido {order}\n{status}\n$MXN12.34', 'hasEntry': False,
            'statusText':status, 'statusDetail':detail,
            'goodsImg':'https://img.ltwebstatic.com/synthetic.jpg',
            'goodsImages':['https://img.ltwebstatic.com/synthetic.jpg'],
            'itemCount':2, 'goodsItems':[{'name':'Synthetic product','quantity':2}]}


def test_skipped_order_retains_existing_refund_without_counting_as_new_submission():
    def recovery(claimer):
        def read(page, order, source):
            assert source == 'existing'
            record={'refundBillId':'900000000000001','phase':'reviewing','phaseLabel':'审核中',
                    'refundAccount':'****1234','refundPath':'Cuenta original de pago'}
            claimer._record_refund(order, dict(record, source=source))
            return [record], ''
        return Mock(side_effect=read)
    claimer=run_cards([card('SYNTHORDER', 'Procesamiento de reembolsos', 'En revisión vendedor/SHEIN.')],recovery)
    snap=claimer.snapshot();row=snap['claimRows'][0]
    assert row['status']=='skip' and row['goodsImg'].endswith('synthetic.jpg')
    assert row['itemCount']==2 and row['goodsItems'][0]['name']=='Synthetic product'
    assert row['refundBillId']=='900000000000001' and row['refundAccount']=='****1234'
    assert row['refunds'][0]['source']=='existing' and not row.get('submittedAt')
    assert '审核中' in row['note'] and row['operationCompletedAt']
    env=snap['claimEnvRows'][0];assert env['status']=='skip' and '审核中' in env['note']
    summary=LocalOperationExecutor._after_sale_environment_summary(1,[env],[row])
    assert summary['successCount']==0 and summary['skippedCount']==1
    assert summary['runStatus']=='completed'
    projected=LocalOperationExecutor._after_sale_rows([row],claim=True,discovered=True)[0]
    assert projected['goodsItems'][0]['quantity']==2 and projected['itemCount']==2
    assert projected['refunds'][0]['source']=='existing'
    claimer._claim_one.assert_not_called();claimer._scan_pre_info.assert_not_called()
    claimer._open_env.assert_called_once();claimer._stop_env.assert_called_once()


@pytest.mark.parametrize('platform,detail,expected',[
    ('Enviado','','运输中'),('Procesando','','备货中'),('No pagado','','待付款'),
    ('Cancelado','','已取消'),('Entregado','','已送达'),('Other','','具体原因待核对'),
])
def test_no_entry_reason_comes_from_actual_order_status(platform,detail,expected):
    claimer=run_cards([card('SYNTHORDER',platform,detail)])
    row=claimer.snapshot()['claimRows'][0]
    assert row['status']=='skip' and expected in row['note']
    claimer._claim_one.assert_not_called()


def test_receipt_failure_keeps_order_and_reason_without_inventing_refund():
    claimer=run_cards([card('SYNTHORDER','Procesamiento de reembolsos')],
                      lambda _:Mock(return_value=([], 'read failed')))
    row=claimer.snapshot()['claimRows'][0]
    assert row['status']=='skip' and not row.get('refundBillId')
    assert row['recoveryError']=='read failed' and '详情未完整取得' in row['note']
    assert row['goodsImages']


def test_absent_environment_and_duplicate_name_have_different_actionable_messages():
    hub=SimpleNamespace(env_list=lambda:[{'containerCode':'A','serialNumber':1,'containerName':'same'},
                                         {'containerCode':'B','serialNumber':2,'containerName':'same'}])
    claimer=AfterSaleClaimer(hub)
    index=claimer._env_index(['same','1','999'])
    assert index['1']['containerCode']=='A'
    with pytest.raises(RuntimeError,match='未找到匹配环境.*目标团队'):
        claimer._open_env(index.get('999'),'999',True)
    with pytest.raises(RuntimeError,match='多个同名环境.*序号'):
        claimer._open_env(index['same'],'same',True)


def test_refund_countdown_supports_split_rendered_digits():
    result=classify_phase_state(state(detail='En revisión\nTermina en\n08\n:\n17\n:\n18\nHistorial de negociación'))
    assert result['countdown']=='08:17:18'


def test_stopping_during_read_preserves_visible_unprocessed_orders_as_stopped():
    claimer=AfterSaleClaimer(None, order_stagger=(0, 0))
    claimer._open_env=Mock(return_value=(Mock(), True))
    claimer._stop_env=Mock()
    claimer._login_required=lambda _:False
    eligible=dict(card('SYNTHQUEUED', 'Entregado'), hasEntry=True)
    claimer._read_all_order_cards=lambda _:[eligible, card('SYNTHSKIP','Other')]
    claimer._claim_one=Mock(side_effect=AssertionError('Stopped before submit'))
    def stop_read(page, order):
        claimer._stop_event.set()
        return {}
    with patch.object(module, 'read_order_detail_facts', side_effect=stop_read):
        claimer._claim_env_one('900001', {'containerCode':'SYNTHENV'}, True)
    rows={row['orderNo']:row for row in claimer.snapshot()['claimRows']}
    assert rows['SYNTHQUEUED']['status']=='stopped'
    assert not any(row['status'] in ('queued','running') for row in rows.values())
    claimer._claim_one.assert_not_called()
    claimer._stop_env.assert_called_once()
