"""Tests for orchestrator/work_subscriptions.py against real Plane: framework-authorized
subscribe/list/cancel of completion subscriptions, authority/ownership/duplicate refusals,
and the dispatcher that wakes parked targets through the real runner completion, framework
stop, and owner stop/delete paths with audited, redrivable fires.
"""

from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import (
    AssignmentEpisodeCompletion,
)

from orchestrator.work_subscriptions import (
    completion_event_key, completion_event_source,
)
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import digest


_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")
_LIMITS = {"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1, "max_tasks": 2,
           "model_calls": 2, "tool_calls": 2, "tokens": 100, "elapsed_ms": 1000}


def _identity_of(record_or_id):
    return record_or_id if isinstance(record_or_id, str) else record_or_id.assignment_id


def _terminal_completion(record, label):
    return AssignmentEpisodeCompletion(
        expected_state_version=record.state_version, checkpoint=record.checkpoint,
        completion_digest=digest(["subscription-fire", label, record.assignment_id]),
        completed=True, terminal_outcome="completed", next_wake_at=None,
        wake_reason="assignment_finished")


@pytest.mark.asyncio
async def test_event_key_helpers_reject_malformed_ids():
    with pytest.raises(AssignmentError, match="work_control_invalid"):
        completion_event_key("not-a-uuid")
    identifier = str(uuid4())
    assert completion_event_source(f"operation:{identifier}:completed") == identifier
    assert completion_event_source(f"owner:{identifier}") is None
    assert completion_event_source("operation:not-a-uuid:completed") is None
    assert completion_event_source(None) is None


