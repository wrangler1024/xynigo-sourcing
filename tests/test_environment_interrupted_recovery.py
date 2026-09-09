"""Interrupted procurement runs: reconcile identities before any Hub writes."""
import pytest

from purchase_tool.env_batch import (
    BatchEnvOrchestrator, BuyerAccount, EnvBatchError, ResumeStateStore,
    batch_fingerprint, format_environment_name, format_remark,
)
from test_env_batch import FakeHub, TEST_TAG, TEST_PROXY

DATE = '20260909'


def recovery(tmp_path, count=1):
    hub = FakeHub()
    hub.browser_lifecycle_status = lambda _code: {'state': 'closed'}
    accounts = [BuyerAccount(i, 'buyer%d@example.test' % i, 'synthetic-password',
        'https://vendor.example.test/%d' % i,
        '[{"domain":".shein.com.mx","name":"sid","value":"synthetic"}]',
        '%08d' % i, '新刚') for i in range(1, count + 1)]
    names = {a.account_id: format_environment_name('XG', 'MX', DATE, i, a.account_id)
             for i, a in enumerate(accounts, 1)}
    context = {'rows': [{'accountRef': a.account_id, 'environmentName': names[a.account_id],
                         'environmentRef': None, 'completedSteps': [], 'status': 'queued'}
                        for a in accounts],
               'originalAssignments': [{'purchaserLabel': '新刚', 'count': count}]}
    runner = BatchEnvOrchestrator(hub, TEST_TAG, TEST_PROXY, purchase_date=DATE,
        state_store=ResumeStateStore('synthetic-recovery', tmp_path),
        sleep_fn=lambda _: None, max_workers=1)
    return hub, accounts, names, context, runner


def live(hub, name, code='8001', remark='', group=TEST_TAG):
    env = {'containerName': name, 'containerCode': code, 'serialNumber': code,
           'remark': remark, 'tagName': group}
    hub.envs.append(env)
    return env


def prepare(accounts, names, context, runner):
    return runner.prepare(accounts, '%d:新刚' % len(accounts), planned_env_names=names,
                          trust_cloud_inventory=True, resume_context=context)


def test_resume_skips_completed_blocks_unknown_binding_and_creates_only_absent(tmp_path):
    hub, accounts, names, context, runner = recovery(tmp_path, 3)
    live(hub, names[accounts[0].account_id], remark=format_remark(accounts[0], DATE))
    live(hub, names[accounts[1].account_id], code='8002')
    rows = prepare(accounts, names, context, runner)
    runner.run()
    assert [r.state for r in rows] == ['done', 'failed', 'done']
    assert rows[0].recovered_existing
    assert rows[1].error_code == 'environment_resume_manual_review'
    assert [c[0] for c in hub.calls] == ['create', 'cookie', 'account', 'remark']
    assert hub.calls[0][1]['containerName'] == names[accounts[2].account_id]
    assert hub.env_list_calls == 2, 'resume must read full inventory even with a fresh cloud cache'
    runner.rollback_created_environments()
    assert {e['containerCode'] for e in hub.envs} == {'8001', '8002'}


@pytest.mark.parametrize('evidence', ['cloud', 'legacy'])
def test_confirmed_binding_only_writes_missing_remark(tmp_path, evidence):
    hub, accounts, names, context, runner = recovery(tmp_path)
    name = names[accounts[0].account_id]
    live(hub, name)
    steps = ['env_created', 'cookie_imported', 'account_bound']
    if evidence == 'cloud':
        context['rows'][0].update(environmentRef='8001', completedSteps=steps)
    else:
        legacy = BatchEnvOrchestrator(hub, TEST_TAG, TEST_PROXY, purchase_date=DATE)
        rows = legacy.prepare(accounts, '1:新刚', planned_env_names=names)
        rows[0].container_code = '8001'
        rows[0].completed_steps = set(steps)
        ResumeStateStore(batch_fingerprint(b'', '1:新刚', 'MX', DATE), tmp_path).save(rows, 'MX', DATE)
    rows = prepare(accounts, names, context, runner)
    runner.run()
    assert rows[0].state == 'done'
    assert not rows[0].created_in_run
    assert [c[0] for c in hub.calls] == ['remark']


@pytest.mark.parametrize('case', ['missing', 'changed_id', 'moved_group', 'wrong_remark', 'in_use'])
def test_uncertain_existing_identity_is_never_rebuilt_or_modified(tmp_path, case):
    hub, accounts, names, context, runner = recovery(tmp_path)
    name = names[accounts[0].account_id]
    context['rows'][0].update(environmentRef='8001', completedSteps=['env_created','cookie_imported','account_bound'])
    if case != 'missing':
        live(hub, name, code='9999' if case == 'changed_id' else '8001',
             group='US-Purchase' if case == 'moved_group' else TEST_TAG,
             remark='unrelated note' if case == 'wrong_remark' else '')
    if case == 'in_use':
        hub.browser_lifecycle_status = lambda _: {'state': 'open'}
    rows = prepare(accounts, names, context, runner)
    runner.run()
    assert rows[0].state == 'failed'
    assert rows[0].error_code == 'environment_resume_manual_review'
    assert not hub.calls


def test_duplicate_names_stop_entire_resume_before_writes(tmp_path):
    hub, accounts, names, context, runner = recovery(tmp_path)
    live(hub, names[accounts[0].account_id])
    live(hub, names[accounts[0].account_id], code='8002')
    with pytest.raises(EnvBatchError, match='同名'):
        prepare(accounts, names, context, runner)
    assert not hub.calls


def test_environment_appearing_after_reconcile_is_not_adopted(tmp_path):
    hub, accounts, names, context, runner = recovery(tmp_path)
    rows = prepare(accounts, names, context, runner)
    live(hub, names[accounts[0].account_id])
    runner.run()
    assert rows[0].error_code == 'environment_resume_manual_review'
    assert not hub.calls


def test_checkpoint_for_different_environment_does_not_prove_binding(tmp_path):
    hub, accounts, names, context, runner = recovery(tmp_path)
    live(hub, names[accounts[0].account_id])
    context['rows'][0].update(environmentRef='other-id', completedSteps=['account_bound'])
    rows = prepare(accounts, names, context, runner)
    runner.run()
    assert rows[0].state == 'failed'
    assert not hub.calls
