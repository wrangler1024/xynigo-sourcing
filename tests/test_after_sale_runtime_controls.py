"""合成环境验证：模式透传、并发上限、同环境隔离与停止。"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from purchase_tool.after_sale_claim import AfterSaleClaimer
from purchase_tool.operation_executor import LocalOperationExecutor


@pytest.mark.parametrize('mode', ['scan', 'claim', 'track'])
@pytest.mark.parametrize('browser, expected', [(None, True), ('headless', True), ('visible', False)])
def test_batch_resolves_explicit_browser_mode(mode, browser, expected):
    claimer = AfterSaleClaimer(None)
    callback = Mock()
    setattr(claimer, '_run_' + mode, callback)
    claimer._run_batch(mode, [], browser)
    callback.assert_called_once_with([], expected)


@pytest.mark.parametrize('value', [0, 6, True, '2', 2.5, None])
def test_invalid_concurrency_never_starts_a_batch(value):
    claimer = AfterSaleClaimer(None)
    with pytest.raises(ValueError):
        claimer.start_scan(['synthetic'], concurrency=value)
    assert not claimer.snapshot()['running']


@pytest.mark.parametrize('mode', ['scan', 'claim', 'track'])
def test_each_batch_uses_two_environment_workers_and_keeps_orders_serial(mode):
    hub = SimpleNamespace(env_list=lambda: [
        {'serialNumber': s, 'containerCode': 'id-' + s} for s in ('A', 'B', 'C')])
    claimer = AfterSaleClaimer(hub, stagger_seconds=0, order_stagger=(0, 0))
    active = set()
    seen = []
    lock = threading.Lock()
    first_pair = threading.Barrier(2, timeout=3)
    peak = [0]

    def work(serial, key):
        with lock:
            assert serial not in active
            active.add(serial)
            peak[0] = max(peak[0], len(active))
        if key in ('A1', 'B1'):
            first_pair.wait()
        time.sleep(.01)
        with lock:
            seen.append(key)
            active.remove(serial)

    if mode == 'scan':
        claimer._scan_one = lambda serial, env, headless: work(serial, serial + '1')
        items = ['A', 'B', 'C']
    else:
        items = [{'environmentSerial': s, 'orderNo': s + str(i),
                  'refundBillId': s + str(i)} for s in ('A', 'B', 'C') for i in (1, 2)]
        claimer._open_env = lambda env, serial, headless: (serial, False)
        if mode == 'claim':
            claimer._claim_one = lambda page, serial, item: work(serial, item['orderNo'])
        else:
            claimer._track_one = lambda page, item: work(page, item['orderNo'])
    with patch('purchase_tool.after_sale_claim.random.uniform', return_value=0):
        getattr(claimer, '_run_' + mode)(items, True)
    assert peak[0] == 2
    assert len(seen) == len(items)
    if mode != 'scan':
        for serial in ('A', 'B', 'C'):
            assert seen.index(serial + '1') < seen.index(serial + '2')


def test_aliases_of_same_environment_do_not_overlap():
    claimer = AfterSaleClaimer(None)
    active, peak = [0], [0]
    def work():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(.02)
        active[0] -= 1
    claimer._run_environment_jobs([
        (alias, {'containerCode': 'same-id'}, work, ()) for alias in ('serial', 'name')])
    assert peak[0] == 1


def test_stop_does_not_start_waiting_environments():
    claimer = AfterSaleClaimer(None)
    claimer._concurrency = 1
    visited = []
    def first():
        visited.append('first')
        claimer.request_stop()
    claimer._run_environment_jobs([
        ('a', {}, first, ()), ('b', {}, lambda: visited.append('second'), ())])
    assert visited == ['first']


@pytest.mark.parametrize('task, route, key', [
    ('scan', 'scan', 'serials'), ('claim', 'submit', 'items'), ('track', 'track', 'items')])
@pytest.mark.parametrize('options', [{}, {'browserMode': 'visible', 'concurrency': 3}])
def test_bridge_and_real_http_handler_keep_runtime_options(task, route, key, options):
    from purchase_tool import main
    callbacks = {name: Mock(return_value={'running': False})
                 for name in ('start_scan', 'start_submit', 'start_track')}
    state = SimpleNamespace(after_sale=SimpleNamespace(**callbacks))
    requests = []
    def rpc(request):
        if request['method'] == 'GET':
            return {'responseType': 'json', 'httpStatus': 200, 'body': {'running': False, 'rows': [],
                                              'claimRows': [], 'trackRows': []}}
        handler = main.Handler.__new__(main.Handler)
        handler.path = request['path']
        handler._body = lambda **kwargs: request['body']
        handler._require_same_origin = lambda: None
        handler._internal_executor_rpc_allowed = lambda: False
        handler._require_auth = lambda path: {}
        output = []
        handler._json = lambda value, status=200: output.append((value, status))
        with patch.object(main, 'STATE', state):
            handler.do_POST()
        requests.append(request)
        value, status = output[0]
        return {'responseType': 'json', 'httpStatus': status, 'body': value}
    payload = dict(options, environmentSerials=['A'], items=[{
        'environmentSerial': 'A', 'orderNo': 'SYNTH1', 'refundBillId': 'SYNTHBILL'}])
    LocalOperationExecutor(rpc, sleep_fn=lambda _: None).execute(
        'after.sale.' + task + '.v1', payload, lambda **event: None, threading.Event())
    callback = callbacks['start_' + route]
    assert callback.call_args.args[1] == options.get('browserMode', 'headless')
    assert callback.call_args.kwargs['concurrency'] == options.get('concurrency', 2)


def test_headless_request_overrides_legacy_visible_config():
    claimer = AfterSaleClaimer(None, headless=False)
    claimer._run_scan = Mock()
    claimer._run_batch('scan', [], 'headless')
    claimer._run_scan.assert_called_once_with([], True)


@pytest.mark.parametrize('concurrency', [1, 2, 3, 5])
def test_selected_concurrency_is_the_worker_limit(concurrency):
    claimer = AfterSaleClaimer(None)
    claimer._concurrency = concurrency
    barrier = threading.Barrier(concurrency, timeout=3)
    active, peak = [0], [0]
    lock = threading.Lock()
    def work():
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        barrier.wait()
        time.sleep(.01)
        with lock:
            active[0] -= 1
    claimer._run_environment_jobs([(str(i), {}, work, ()) for i in range(concurrency * 2)])
    assert peak[0] == concurrency


def test_web_runtime_controls_use_shared_settings_for_all_three_tasks():
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('node'):
        pytest.skip('Node.js is required for UI checks')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests/fixtures/after_sale_runtime_ui.cjs')],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('concurrency', [1, 2, 3, 5])
def test_start_captures_selected_limit_for_worker(concurrency):
    claimer = AfterSaleClaimer(None)
    seen = []
    finished = threading.Event()
    def scan(items, headless):
        seen.append((claimer._concurrency, headless))
        finished.set()
    claimer._run_scan = scan
    claimer.start_scan(['SYNTH'], browser_mode='headless', concurrency=concurrency)
    assert finished.wait(2)
    assert seen == [(concurrency, True)]
