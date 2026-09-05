"""Offline regression tests: serial lookup and per-environment routing."""
from unittest.mock import Mock, patch

import pytest

from purchase_tool.shein_query import QueryOrchestrator, resolve_environment_site


def test_domain_takes_precedence_over_old_name_and_group():
    assert resolve_environment_site(
        {'containerName': 'SYN-MX-0905-001', 'tagName': '墨西哥买家号注册'},
        [{'url': 'https://us.shein.com/user/orders/list'}],
    ) == 'US'


@pytest.mark.parametrize('name,expected', [
    ('采购-合成-MX-0905-001', 'MX'), ('SYN-US-0905-001', 'US'),
])
def test_blank_browser_uses_explicit_environment_marker(name, expected):
    assert resolve_environment_site(
        {'containerName': name}, [{'url': 'about:blank'}]) == expected


@pytest.mark.parametrize('env,pages', [
    ({'containerName': '未标记环境', 'tagName': '墨西哥买家号注册'}, []),
    ({'containerName': 'SYN-MX-US-001'}, []),
    ({'containerName': '未标记环境'}, [{'url': 'https://us.shein.com.example.test'}]),
    ({'containerName': 'SYN-MX-001'}, [
        {'url': 'https://us.shein.com'}, {'url': 'https://www.shein.com.mx'}]),
])
def test_ambiguous_site_never_defaults_to_mx(env, pages):
    with pytest.raises(ValueError):
        resolve_environment_site(env, pages)


def test_mixed_batch_routes_each_environment_and_isolates_unknown_row():
    hub = Mock()
    hub.open_container_codes.return_value = set()
    job = QueryOrchestrator(hub, concurrency=2, settle_seconds=0, env_interval=0)
    environments = {
        str(i): {'containerCode': str(i), 'containerName': '未标记环境',
                 'tagName': '墨西哥买家号注册'} for i in range(1, 4)
    }
    pages = {}
    domains = {1: 'https://us.shein.com', 2: 'https://www.shein.com.mx', 3: 'about:blank'}

    def client(port):
        page = Mock()
        page.url = ''
        page._evaluate.side_effect = lambda expr: 'UTC' if 'resolvedOptions' in expr else 0
        pages[port] = page
        cdp = Mock()
        cdp.list_pages.return_value = [{'url': domains[port]}]
        cdp.new_page.return_value = page
        return cdp

    def parse_list(text, site):
        return {'orderNo': 'SYN' + site, 'orderTime': '', 'amount': '',
                'status': 'Paid', 'statusCn': '已支付', 'stage': ''}

    with patch.object(job, '_start_browser', side_effect=lambda code: {'debuggingPort': int(code)}), \
            patch.object(job, '_stop_browser_and_confirm', return_value=True) as stop, \
            patch.object(job, '_read_text_stable', return_value=''), \
            patch('purchase_tool.shein_query.CdpClient', side_effect=client), \
            patch('purchase_tool.shein_query.parse_list_page', side_effect=parse_list), \
            patch('purchase_tool.shein_query.parse_detail_page', return_value={'tracks': []}):
        job._run(['1', '2', '3'], environments, site='AUTO')
    rows = {row['serial']: row for row in job.rows}
    assert [(rows[s]['site'], rows[s]['state']) for s in ('1', '2')] == [('US', 'ok'), ('MX', 'ok')]
    assert rows['3']['state'] == 'fail'
    assert '无法识别' in rows['3']['error']
    assert rows['3']['site'] == 'AUTO'
    assert rows['3']['time'] == ''
    assert pages[1].goto.call_args_list[0].args[0] == 'https://us.shein.com/user/orders/list'
    assert pages[2].goto.call_args_list[0].args[0] == 'https://www.shein.com.mx/user/orders/list'
    assert not pages[3].goto.called
    assert stop.call_count == 3
    assert job.snapshot()['site'] == 'AUTO'
    job.close()


def test_mixed_retry_keeps_each_rows_resolved_site():
    job = QueryOrchestrator(Mock(), env_interval=0)
    job.hub.open_container_codes.return_value = set()
    job.rows = [job._blank_row('1', 'US'), job._blank_row('2', 'MX')]
    for row in job.rows:
        row['state'] = 'fail'
    with patch('purchase_tool.shein_query.threading.Thread') as thread:
        assert job.requery_failed({'1': {}, '2': {}}) == 2
        args = thread.call_args.kwargs['args']
    with patch.object(job, '_query_one') as query:
        job._run(*args)
    assert {call.args[1]: call.args[-1] for call in query.call_args_list} == {'1': 'US', '2': 'MX'}


@pytest.mark.parametrize('mode,path', [
    ('initial', '/api/query'), ('single_retry', '/api/requery'),
    ('failed_retry', '/api/query'),
])
def test_executor_forwards_auto_routing_for_every_query_mode(mode, path):
    from test_operation_executor import FakeRpc
    from purchase_tool.operation_executor import LocalOperationExecutor
    rpc = FakeRpc('/api/progress', [{'running': False, 'rows': [
        {'serial': '1001', 'state': 'fail', 'error': 'synthetic unknown site'},
    ]}])
    LocalOperationExecutor(rpc, sleep_fn=lambda _: None).execute(
        'logistics.query.v1', {'runKey': 'synthetic-auto-routing',
                               'queryMode': mode, 'environmentSerials': ['1001']},
        lambda **_: None,
    )
    assert rpc.calls[0]['path'] == path
    assert rpc.calls[0]['body']['site'] == 'AUTO'


def test_cloud_single_retry_discards_legacy_manual_site():
    job = QueryOrchestrator(Mock(), env_interval=0)
    row = job._blank_row('1', 'MX')
    row.update(state='fail', utcOffsetMinutes=-360, time='2026-09-05 01:00:00')
    job.rows = [row]
    with patch('purchase_tool.shein_query.threading.Thread') as thread:
        job.requery('1', site='AUTO')
        assert thread.call_args.kwargs['args'][3] == 'AUTO'
    assert row['site'] == 'AUTO'
    assert row['utcOffsetMinutes'] is None
    assert row['time'] == ''
