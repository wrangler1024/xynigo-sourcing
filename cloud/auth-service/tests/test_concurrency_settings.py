import uuid
import pytest
from pydantic import ValidationError
from xynigo_auth.operation_contract import StoreFinanceRunCreateBody
from xynigo_auth.executor_contract import ExecutorConfigWriteBody


@pytest.mark.parametrize('value', [1, 4, 10, 0, True, '2', 2.5, None, 2, 3, 5])
def test_finance_and_desktop_writes_share_strict_choices(value):
    valid = type(value) is int and value in (2,3,5)
    factories = [lambda: StoreFinanceRunCreateBody(
        idempotencyKey='synthetic-concurrency', executorId=uuid.uuid4(),
        environmentSerials=['SYNTH-A'], concurrency=value)]
    for key in ('concurrency','envCreateWorkers'):
        factories.append(lambda key=key: ExecutorConfigWriteBody.validate_safe_config({key:value}))
    for create in factories:
        if valid:
            create()
        else:
            with pytest.raises((ValidationError, ValueError)):
                create()
