"""Bounded lookup and visible preparation without opening real browsers."""
import threading
from unittest.mock import Mock, patch

import pytest

from purchase_tool.after_sale_claim import AfterSaleClaimer
from purchase_tool.hub_api import HubApiError, HubStudioLocalApiAdapter


def test_lookup_stops_pagination_after_all_exact_identifiers_match():
    hub = HubStudioLocalApiAdapter()
    hub._post = Mock(return_value={'list': [
        {'serialNumber': '900001', 'containerCode': 'SYNTH-A'},
        {'serialNumber': '900002', 'containerCode': 'SYNTH-B'}], 'total': 20000})
    claimer = AfterSaleClaimer(hub)
    result = claimer._env_index(['900002', '900001'])
    assert result['900001']['containerCode'] == 'SYNTH-A'
    assert len(result) == 2 and hub._post.call_count == 1
    assert hub._post.call_args.kwargs['retries'] == 1
    assert hub._post.call_args.kwargs['timeout'] <= 10


def test_names_require_complete_search_and_duplicate_name_is_rejected():
    hub = HubStudioLocalApiAdapter()
    hub._post = Mock(side_effect=[
        {'list': [{'containerName': 'Synthetic', 'containerCode': 'A'}], 'total': 201},
        {'list': [{'containerName': 'Synthetic', 'containerCode': 'B'}], 'total': 201}])
    result = AfterSaleClaimer(hub)._env_index(['Synthetic'])
    assert '_resolutionError' in result['Synthetic']
    assert hub._post.call_count == 2


def test_page_budget_and_stop_prevent_more_requests():
    hub = HubStudioLocalApiAdapter()
    hub._post = Mock(return_value={'list': [{'serialNumber': '1'}], 'total': 999})
    with patch('purchase_tool.hub_api.time.monotonic', side_effect=[0, 0, 46]):
        with pytest.raises(HubApiError, match='超时'):
            list(hub.iter_environments())
    assert hub._post.call_count == 1
    stop = threading.Event(); stop.set()
    assert list(hub.iter_environments(stop_event=stop)) == []
    assert hub._post.call_count == 1


@pytest.mark.parametrize('stopped', [False, True])
def test_lookup_stage_is_published_before_request_and_failure_never_opens(stopped):
    claimer = AfterSaleClaimer(None)
    claimer._run_environment_jobs = Mock()
    def lookup(keys):
        snap = claimer.snapshot()
        assert len(snap['claimEnvRows']) == 2
        assert all('正在匹配 Hub 环境' in row['note'] for row in snap['claimEnvRows'])
        if stopped:
            claimer._stop_event.set()
            return {}
        raise TimeoutError('synthetic lookup timeout')
    claimer._env_index = lookup
    claimer._run_claim_environments(['900001', '900002'], True)
    snap = claimer.snapshot()
    assert not snap['claimRows']
    assert all(row['status'] == ('stopped' if stopped else 'fail') for row in snap['claimEnvRows'])
    assert all('尚未提交' in row['note'] for row in snap['claimEnvRows'])
    claimer._run_environment_jobs.assert_not_called()
