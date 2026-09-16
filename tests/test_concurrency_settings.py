"""Runtime write boundaries and read-only legacy configuration migration."""
import pytest
from purchase_tool.main import (default_config, updated_config,
    updated_executor_config, normalize_purchase_assistant_profiles,
    environment_worker_policy)
from purchase_tool.resource_center import ProxyCheckJob
from purchase_tool.store_finance_inspect import StoreFinanceInspector


@pytest.mark.parametrize('value', [2, 3, 5])
def test_both_settings_paths_preserve_valid_choices(value):
    for save in (updated_config, updated_executor_config):
        result = save(default_config(), dict(concurrency=value, envCreateWorkers=value))
        assert result['concurrency'] == result['envCreateWorkers'] == value


@pytest.mark.parametrize('value', [1, 4, 10, 0, True, '2', 2.5, None])
def test_old_config_normalizes_but_new_writes_reject(value):
    old = dict(concurrency=value, envCreateWorkers=value)
    normalized = normalize_purchase_assistant_profiles(old)
    assert normalized['concurrency'] == normalized['envCreateWorkers'] == 2
    assert old['concurrency'] is value
    for save in (updated_config, updated_executor_config):
        for key in old:
            with pytest.raises(ValueError):
                save(default_config(), {key:value})
    inspector = StoreFinanceInspector(None)
    with pytest.raises(ValueError):
        inspector.start_batch(['SYNTH-A'], concurrency=value)
    assert not inspector.snapshot()['running']
    # Validation must precede worker setup or endpoint/network access.
    with pytest.raises(ValueError):
        ProxyCheckJob.start(None, ['synthetic-proxy'], concurrency=value)


def test_defaults_and_effective_safety_caps():
    assert default_config()['envCreateWorkers'] == default_config()['concurrency'] == 2
    assert environment_worker_policy({'envCreateWorkers':5, 'safeParallelTasks':True}) == (5,2)
    assert environment_worker_policy({'envCreateWorkers':5, 'safeParallelTasks':False}) == (5,3)
