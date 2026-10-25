"""Verifies repeated terminal observations preserve completed generations under the effect guard.
Real Plane transactions keep active uncertainty, stale executors and owner gates fail closed.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import uuid

import pytest

from orchestrator.work_admission import OperationState, StaleExecutionFenceError
from tests.test_work_reconciliation_postgres import (  # noqa: F401
    _FakeClock, _digest, _ledger, _node, _submit_and_claim,
    clean_database as clean_database_fixture,
    encryption_key as encryption_key_fixture, postgres_database,
)


@pytest.fixture
def completion_database(request):
    request.getfixturevalue("encryption_key_fixture")
    return request.getfixturevalue("clean_database_fixture")


def finish(node, fence, *, state=OperationState.COMPLETED):
    return node.terminalize(fence, state=state,
        terminal_code="fixture_failure" if state is OperationState.FAILED else None,
        safe_summary="Finished", retry_after_ms=None)


@pytest.mark.parametrize("state", [OperationState.COMPLETED, OperationState.FAILED])
def test_repeated_completion_observes_same_terminal_generation(completion_database, state):
    node = _node(completion_database, _FakeClock())
    ledger = _ledger(completion_database, node)
    node.set_completion_guard(ledger.completion_uncertain)
    _, claim = _submit_and_claim(node, "repeat", "owner-a")
    first = finish(node, claim.fence, state=state)
    assert finish(node, claim.fence, state=state) == first
    assert first.state is state and first.execution_lease_token is None


def test_uncertain_effect_cannot_be_promoted_by_repeated_completion(completion_database):
    node = _node(completion_database, _FakeClock())
    ledger = _ledger(completion_database, node)
    node.set_completion_guard(ledger.completion_uncertain)
    accepted, claim = _submit_and_claim(node, "uncertain", "owner-a")
    attempt = ledger.bind(claim.fence, target_kind="remote_machine", target_id="fixture-machine",
                          argument_digest=_digest("reviewed arguments"))
    first = finish(node, claim.fence)
    assert first.state is OperationState.RETRYABLE and first.terminal_code == "effect_uncertain"
    assert finish(node, claim.fence) == first
    assert ledger.read(partition="owner-a", operation_id=accepted.operation_id,
                       nonce=attempt.attempt_nonce).state == "bound"


@pytest.mark.parametrize("change", ["token", "generation", "missing", "other_owner"])
def test_stale_active_fence_cannot_finish_any_owner_operation(completion_database, change):
    node = _node(completion_database, _FakeClock())
    node.set_completion_guard(_ledger(completion_database, node).completion_uncertain)
    _, claim = _submit_and_claim(node, "current", "owner-a")
    _, other = _submit_and_claim(node, "other", "owner-b")
    fence = claim.fence
    if change == "token":
        fence = replace(fence, execution_lease_token=uuid.uuid4())
    elif change == "generation":
        fence = replace(fence, execution_generation=fence.execution_generation + 1)
    else:
        fence = replace(fence, operation_id=uuid.uuid4() if change == "missing" else other.fence.operation_id)
    with pytest.raises(StaleExecutionFenceError):
        finish(node, fence)
    for current in (claim, other):
        assert node.repository.get_operation_for_administration(current.operation.operation_id).state is OperationState.RUNNING


def test_terminal_observation_cannot_adopt_another_execution_generation(completion_database):
    node = _node(completion_database, _FakeClock())
    node.set_completion_guard(_ledger(completion_database, node).completion_uncertain)
    _, claim = _submit_and_claim(node, "old-generation", "owner-a")
    completed = finish(node, claim.fence)
    with pytest.raises(StaleExecutionFenceError):
        finish(node, replace(claim.fence, execution_generation=claim.fence.execution_generation + 1))
    assert node.repository.get_operation_for_administration(completed.operation_id) == completed


def test_concurrent_completions_have_one_transition_and_same_observation(completion_database):
    clock = _FakeClock()
    nodes = [_node(completion_database, clock), _node(completion_database, clock)]
    for node in nodes:
        node.set_completion_guard(_ledger(completion_database, node).completion_uncertain)
    _, claim = _submit_and_claim(nodes[0], "concurrent", "owner-a")
    ready = Barrier(2)
    def complete(node):
        ready.wait(timeout=5)
        return finish(node, claim.fence)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(complete, nodes))
    assert results[0] == results[1] and results[0].state is OperationState.COMPLETED
    assert results[0].state_revision == claim.operation.state_revision + 1


def test_terminal_observation_still_checks_its_owner_execution_gate(completion_database):
    node = _node(completion_database, _FakeClock())
    node.set_completion_guard(_ledger(completion_database, node).completion_uncertain)
    _, claim = _submit_and_claim(node, "owner-gate", "owner-a")
    completed = finish(node, claim.fence)
    def refuse(_transaction, owner, operation_id):
        assert owner.owner_user_id == "owner-a" and operation_id == completed.operation_id
        raise PermissionError("owner stopped")
    node.repository._execution_gate = refuse
    with pytest.raises(PermissionError, match="owner stopped"):
        finish(node, claim.fence)
    assert node.repository.get_operation_for_administration(completed.operation_id) == completed
