"""Synthetic refund timeline and negotiation cases; never contact a buyer."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from purchase_tool.after_sale_phase import (
    STEP_TITLES, PHASE_ORDER, TRACK_STATE_JS, classify_phase_state,
    latest_rejection_reason, read_refund_phase, phase_note,
)
from purchase_tool.operation_executor import LocalOperationExecutor

TITLES = ['Solicitud de reembolso aceptada', 'Reseña de SHEIN/Vendedor',
          'Procesamiento de reembolsos de SHEIN', 'Reembolso de SHEIN exitoso',
          'Reembolso procesado por su institución financiera']


def state(index=1, detail='En revisión (vendedor/SHEIN). Resultado en 7 días', supplement=False):
    return {'orderNo':'ORDER', 'refundBillId':'12345', 'amounts':['$MXN12.34'],
            'steps':[{'title':t, 'state':'finish' if i<index else 'active' if i==index else 'wait',
                      'visible':i<3 or i==index, 'detail':detail if i==index else '',
                      'canSupplement':supplement if i==index else False} for i,t in enumerate(TITLES)]}


def history(events, bill='12345'):
    return 'HISTORIAL DE NEGOCIACIÓN\nCódigo del Reembolso: '+bill+'\n'+events


def test_review_without_24_hour_countdown_and_collapsed_future_steps():
    raw=state(); result=classify_phase_state(raw)
    assert result['phase']=='reviewing' and result['countdown']==''
    for step in raw['steps']:step['visible']=True
    assert classify_phase_state(raw)==result
    raw['steps'][1]['detail']='SHEIN/Vendedor está en revisión. Termina en 23:48:25'
    assert classify_phase_state(raw)['countdown']=='23:48:25'


@pytest.mark.parametrize('supplement,expected', [(True,'evidence_required'),(False,'review_failed')])
def test_failed_review_requires_an_explicit_supplement_action(supplement,expected):
    result=classify_phase_state(state(detail='Revisión fallida (vendedor/SHEIN)',supplement=supplement))
    assert result['phase']==expected and result['phaseEvidence']['step']==2
    assert '具体原因尚未取得' in phase_note(result)
    assert classify_phase_state(state(supplement=True)) is None


@pytest.mark.parametrize('index',range(5))
def test_five_distinct_stages(index):
    result=classify_phase_state(state(index))
    assert result['phase']==PHASE_ORDER[index]
    assert result['phaseEvidence']['step']==index+1


def test_all_finished_is_financial_institution_processed():
    raw=state()
    for step in raw['steps']:step['state']='finish'
    assert classify_phase_state(raw)['phase']=='bank_processed'


@pytest.mark.parametrize('mutate',[
    lambda s:s['steps'].pop(),
    lambda s:s['steps'][2].update(state='active'),
    lambda s:s['steps'][0].update(state='wait'),
    lambda s:s['steps'][4].update(state='finish'),
    lambda s:s['steps'][1].update(visible=False),
    lambda s:s['steps'][1].update(detail=''),
    lambda s:s['steps'][1].update(title='Unknown review step'),
    lambda s:s['steps'][1].update(state=''),
])
def test_incomplete_and_conflicting_nodes_are_not_a_successful_read(mutate):
    raw=state();mutate(raw)
    assert classify_phase_state(raw) is None
    assert classify_phase_state('Procesamiento de reembolsos de SHEIN') is None


def test_negotiation_reason_must_belong_to_latest_event_and_same_refund():
    failed='Vendedor\nRevisión fallida por vendedor/SHEIN\nReembolso rechazado: Comprobantes insuficientes\n16 Sep 2026 21:30:53\n'
    submitted='Usuario\nEnviar solicitud de reembolso\n16 Sep 2026 13:55:39\n'
    assert latest_rejection_reason(history(failed+submitted),'12345')=='Comprobantes insuficientes'
    assert latest_rejection_reason(history(submitted+failed),'12345')=='Comprobantes insuficientes'
    resubmitted='Usuario\nSubir comprobante\n17 Sep 2026 01:00:00\n'
    assert latest_rejection_reason(history(failed+resubmitted),'12345')==''
    assert latest_rejection_reason(history(failed,bill='54321'),'12345')==''
    assert latest_rejection_reason(history(failed+'Vendedor\nRevisión pendiente'),'12345')==''


def page_for(raw):
    page=Mock();page.url='https://www.shein.com.mx/orders/refundLabel/ORDER?refund_bill_id=12345'
    page.js_evaluate.return_value=deepcopy(raw)
    return page


def test_phase_read_is_bound_to_the_refund_and_ignores_old_rejection_on_active_review():
    page=page_for(state())
    result=read_refund_phase(page,('ORDER','12345'),timeout=0)
    assert result['phase']=='reviewing'
    page.native_click_point.assert_not_called()
    with pytest.raises(RuntimeError,match='身份'):
        read_refund_phase(page,('ORDER','54321'),timeout=0)


def test_failed_review_reads_reason_without_uploading_evidence():
    raw=state(detail='Revisión fallida (vendedor/SHEIN)',supplement=True)
    page=page_for(raw)
    text=history('Vendedor\nRevisión fallida por vendedor/SHEIN\nReembolso rechazado: Comprobantes insuficientes\n16 Sep 2026 21:30:53')
    def evaluate(js):
        if js==TRACK_STATE_JS:return deepcopy(raw)
        if '.btn-info button' in js:return {'x':1,'y':2}
        return text
    page.js_evaluate.side_effect=evaluate
    result=read_refund_phase(page,('ORDER','12345'),timeout=0)
    assert result['phase']=='evidence_required'
    assert result['phaseEvidence']['reasonSource']=='negotiation_history'
    assert '凭证不足' in result['note']
    page.native_click_point.assert_called_once_with(1,2)
    page.press_key.assert_called_once_with('Escape','Escape',27)


def test_evidence_survives_both_executor_projections_without_whole_page_data():
    result=classify_phase_state(state())
    result['phaseEvidence']['wholePage']='must not upload'
    tracking=LocalOperationExecutor._after_sale_track_rows([dict(result,refundBillId='12345',status='ok')])[0]
    claim=LocalOperationExecutor._after_sale_rows([{'orderNo':'ORDER','status':'ok',
        'refunds':[dict(result,refundBillId='12345')]}])[0]
    assert tracking['phaseEvidence']==claim['refunds'][0]['phaseEvidence']
    assert 'wholePage' not in tracking['phaseEvidence']


def test_five_node_review_exception_ui():
    import subprocess
    from pathlib import Path
    result=subprocess.run(['node','tests/fixtures/after_sale_phase_ui.cjs'],
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_new_receipt_observation_clears_stale_failed_review_details():
    from purchase_tool.after_sale_claim import AfterSaleClaimer
    c=AfterSaleClaimer(None)
    old=classify_phase_state(state(detail='Revisión fallida (vendedor/SHEIN)',supplement=True))
    old['phaseEvidence'].update(reason='Comprobantes insuficientes',reasonSource='negotiation_history')
    c._record_refund('ORDER',dict(old,refundBillId='12345',refundPath='Cuenta original de pago',
        refundAccount='****1234',detailsNote='凭证不足',source='existing'))
    assert '凭证不足' in c._claim_rows['ORDER']['refunds'][0]['detailsNote']
    current=classify_phase_state(state())
    c._record_refund('ORDER',dict(current,refundBillId='12345',detailsNote='',source='existing'))
    record=c._claim_rows['ORDER']['refunds'][0]
    assert record['phase']=='reviewing' and record['detailsNote']==''
    assert record['phaseEvidence']['reason']==''


@pytest.mark.parametrize('unparsed', [
    'Usuario\nSubir comprobante\n',
    'Usuario\nSubir comprobante\n17 September 2026 01:00:00\n',
    'Otro actor\nNueva revisión\n17 Sep 2026 01:00:00\n',
])
def test_unparsed_new_event_never_reuses_old_rejection_reason(unparsed):
    failed='Vendedor\nRevisión fallida\nReembolso rechazado: Comprobantes insuficientes\n16 Sep 2026 21:30:53\n'
    assert latest_rejection_reason(history(unparsed+failed),'12345')==''
    assert latest_rejection_reason(history(failed+unparsed),'12345')==''


def test_conflicting_bill_in_history_discards_reason_and_known_metadata_is_allowed():
    failed='Vendedor\nRevisión fallida\nReembolso rechazado: Comprobantes insuficientes\n16 Sep 2026 21:30:53'
    text=history('Plazo de la solicitud: 16 Sep 2026 13:55:40\nVer detalles >\n'+failed)
    assert latest_rejection_reason(text,'12345')=='Comprobantes insuficientes'
    assert latest_rejection_reason(text+'\nCódigo del Reembolso: 99999','12345')==''


def test_dom_reader_ignores_disabled_supplement_controls_and_keeps_reason_lines():
    import json
    import subprocess
    from pathlib import Path
    result=subprocess.run(['node','tests/fixtures/after_sale_phase_dom.cjs'],
        input=json.dumps({'js':TRACK_STATE_JS,'titles':TITLES}),
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    states=json.loads(result.stdout)
    assert classify_phase_state(states[0])['phase']=='evidence_required'
    for raw in states[1:]:
        classified=classify_phase_state(raw)
        assert classified['phase']=='review_failed'
        assert classified['phaseEvidence']['reason']=='Comprobantes insuficientes'
