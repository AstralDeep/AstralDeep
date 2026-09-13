"""Durable lease observations never accept in-memory transaction authority."""
import pytest

from orchestrator.work_admission import AdmissionClass, StaleExecutionFenceError
from tests.test_work_admission import _FakeClock, _coordinator, _request


@pytest.mark.parametrize('transaction', [None, object()])
def test_memory_facade_refuses_durable_lease_observation_without_altering_legacy(transaction):
    coordinator = _coordinator(_FakeClock())
    accepted = coordinator.submit(_request('guidance-read'))
    claim = coordinator.claim_operation(AdmissionClass.INTERACTIVE, accepted.operation_id)
    before = coordinator.assert_current_execution(claim.fence)
    with pytest.raises(StaleExecutionFenceError):
        coordinator.assert_current_execution_lease(claim.fence, transaction=transaction)
    assert coordinator.assert_current_execution(claim.fence) == before
