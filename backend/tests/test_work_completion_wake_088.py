"""Tests for the closed completion-continuation request contract
(backend/orchestrator/work_completion_wake.py): field validation, rejection of
untrusted event or authority fields, and the completion event key/outcome helpers.
"""

import importlib
from uuid import uuid4

import pytest
from astralplane.repositories import RepositoryConflictError
from persistent_agents.models import AssignmentError
from pydantic import ValidationError


def module():
    return importlib.import_module("orchestrator.work_completion_wake")


def values():
    return {
        "expected_revision": 1,
        "submission_id": str(uuid4()),
        "source_operation_id": str(uuid4()),
    }


def confirm_values():
    return {**values(), "proposal_id": uuid4().hex, "proposal_digest": "0" * 64}


def test_closed_proposal_and_confirm_request_contracts():
    proposal = values()
    assert module().WorkContinuationProposalRequest(**proposal).model_dump() == proposal
    confirm = confirm_values()
    assert module().WorkContinuationConfirmRequest(**confirm).model_dump() == confirm


@pytest.mark.parametrize("field,value", [
    ("expected_revision", True), ("expected_revision", 0),
    ("submission_id", "caller-key"), ("source_operation_id", "provider-operation"),
    ("source_operation_id", str(uuid4()).upper()), ("event_key", "completion:remote"),
    ("authority", {}), ("proposal_id", uuid4().hex + "ff"), ("proposal_id", str(uuid4())),
    ("proposal_id", "z" * 32), ("proposal_digest", "z" * 64),
])
def test_requests_refuse_untrusted_event_or_authority_fields(field, value):
    with pytest.raises(ValidationError):
        module().WorkContinuationConfirmRequest(**{**confirm_values(), field: value})


def test_completion_event_key_round_trip_and_refusals():
    source = str(uuid4())
    key = module().completion_event_key(source)
    assert key == "completion:" + source
    assert module().completion_event_source(key) == source
    assert module().completion_event_source("owner:" + source) is None
    assert module().completion_event_source("completion:not-a-uuid") is None
    assert module().completion_event_source("completion:" + source + ":extra") is None
    assert module().completion_event_source(None) is None
    with pytest.raises(AssignmentError):
        module().completion_event_key("short")


def test_source_read_returns_none_on_repository_conflict():
    class FakeRepository:
        def get_operation(self, tx, *, owner_id, assignment_id):
            raise RepositoryConflictError("conflict", code="assignment_version_unsupported")

    assert module()._source_read(object(), FakeRepository(), "owner", str(uuid4())) is None


def test_schedule_redrive_guards_and_bound():
    module()._redrive_pending.clear()
    module().schedule_redrive("", str(uuid4()))
    module().schedule_redrive("owner", "not-a-uuid")
    module().schedule_redrive(None, str(uuid4()))
    assert module()._redrive_pending == {}
    owner, source = "owner", str(uuid4())
    for _ in range(module()._REDRIVE_LIMIT):
        module().schedule_redrive(owner, str(uuid4()))
    assert len(module()._redrive_pending) == module()._REDRIVE_LIMIT
    oldest = next(iter(module()._redrive_pending))
    module().schedule_redrive(owner, source)
    assert oldest not in module()._redrive_pending
    assert (owner, source) in module()._redrive_pending
    module()._redrive_pending.clear()


def test_fire_completions_refuses_untrusted_arguments():
    class FakeRepository:
        def list_operations(self, tx, *, owner_id, limit, after_id):
            raise AssertionError("scan must not run for untrusted arguments")

    recorded = []
    source = str(uuid4())
    for arguments in (
        {"outcome": "running", "source_revision": 3},
        {"outcome": "completed", "source_revision": True},
        {"outcome": "completed", "source_revision": 0},
        {"outcome": "completed", "source_revision": None},
    ):
        assert module().fire_completions(object(), None, FakeRepository(),
                                         owner_id="owner", source_id=source,
                                         **arguments) == 0
    assert module().fire_completions(object(), None, FakeRepository(), owner_id="",
        source_id=source, source_revision=1, outcome="completed") == 0
    recorded.append(module().fire_completions(object(), None, FakeRepository(),
        owner_id="owner", source_id="short", source_revision=1, outcome="completed"))
    assert recorded == [0]


def test_redrive_reports_transaction_failures(monkeypatch):
    class FakeStore:
        async def transaction(self, callback, **kwargs):
            raise AssignmentError("work_control_unavailable", 503)

    class FakeAssignments:
        store = FakeStore()

    module()._redrive_pending.clear()
    module().schedule_redrive("owner", str(uuid4()))
    import asyncio
    result = asyncio.run(module().redrive_pending(FakeAssignments()))
    assert result == 0
    assert list(module()._redrive_pending.values()) == [1]
    module()._redrive_pending.clear()

    class BrokenStore:
        async def transaction(self, callback, **kwargs):
            raise RuntimeError("synthetic redrive failure")

    module().schedule_redrive("owner", str(uuid4()))
    assert asyncio.run(module().redrive_pending(
        type("B", (), {"store": BrokenStore()})())) == 0
    assert list(module()._redrive_pending.values()) == [1]
    module()._redrive_pending.clear()


def test_redrive_exhausts_and_drops_missing_sources():
    module()._redrive_pending.clear()
    key = ("owner", str(uuid4()))
    module()._redrive_pending[key] = module()._REDRIVE_ATTEMPTS - 1

    class FakeStore:
        async def transaction(self, callback, **kwargs):
            return -1

    import asyncio
    asyncio.run(module().redrive_pending(type("A", (), {"store": FakeStore()})()))
    assert module()._redrive_pending.get(key) == module()._REDRIVE_ATTEMPTS
    asyncio.run(module().redrive_pending(type("A", (), {"store": FakeStore()})()))
    assert key not in module()._redrive_pending


def test_wait_watermark_and_outcome_helpers_tolerate_untrusted_shapes():
    class Shape:
        def __init__(self, operation):
            self.operation = operation

    assert module()._wait(Shape(None)) is None
    assert module()._wait(Shape({"control": "broken"})) is None
    assert module()._wait(Shape({"control": {"wait": {"event_key": "x"}}})) is None
    assert module()._wait(Shape({"control": {"wait": {
        "event_key": "completion:" + str(uuid4()), "source_revision": 3}}})) is not None
    assert module()._watermark(Shape(None), "completion:x") == 0
    assert module()._watermark(
        Shape({"control": {"watermarks": {"completion:x": "bad"}}}), "completion:x") == 0
    assert module().terminal_outcome(None) is None
    assert module().terminal_outcome(object()) is None
    paused = Shape(None)
    paused.execution_profile = "persistent"
    paused.lifecycle = "stopped"
    assert module().terminal_outcome(paused) is None
