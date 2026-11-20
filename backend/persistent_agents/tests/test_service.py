"""Verifies owner-scoped assignment reads, replay, retired execution and Plane errors."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from astralplane.repositories.assignment_models import AssignmentRecord
from persistent_agents.models import AssignmentError


def make_record(assignment_id, owner_id, definition):
    return AssignmentRecord(assignment_id=assignment_id, owner_id=owner_id, definition=definition,
        instruction_revision=1, control_epoch=1, state_version=1, lifecycle="active", phase="waiting",
        next_wake_at=None, wake_reason="created", wake_generation=1, checkpoint={}, tasks=(), usage={},
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.receipts = {}
        self.calls = []
        self.plane_runtime = object()
        owner = self

        class Repository:
            def __getattr__(self, method):
                return lambda transaction, **kwargs: owner._call(method, **kwargs)

        self.repository = Repository()

    async def transaction(self, callback, *, bound_session_waits=False):
        return callback(None, self.repository)

    async def call(self, method, **kwargs):
        return self._call(method, **kwargs)

    def _call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        key = (kwargs["owner_id"], kwargs.get("assignment_id"))
        if method == "get_submission_receipt":
            if kwargs["command"] != "create" or key not in self.receipts:
                return None
            if self.receipts[key] != kwargs["submission_digest"]:
                raise AssignmentError("assignment_submission_conflict")
            return self.records[key]
        if method == "get_assignment":
            return self.records.get(key)
        if method == "list_assignments":
            return tuple(record for (owner, _), record in self.records.items() if owner == key[0])
        if method == "create_assignment":
            if key in self.receipts and self.receipts[key] != kwargs["submission_digest"]:
                raise AssignmentError("assignment_submission_conflict")
            self.receipts[key] = kwargs["submission_digest"]
            if key not in self.records:
                self.records[key] = make_record(key[1], key[0], kwargs["definition"])
            return self.records[key]
        if method in ("list_actions", "list_activity", "list_events"):
            return ()
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_store_transaction_contract_and_bounded_errors():
    from astralplane.errors import PlaneError
    from astralplane.repositories import (
        RepositoryConflictError,
        RepositoryValidationError,
    )
    from persistent_agents.store import AssignmentStore
    repo = SimpleNamespace(get_assignment=Mock(return_value="record"))
    runtime = SimpleNamespace(repositories=SimpleNamespace(assignments=repo))
    transaction = object()

    async def run(callback):
        return callback(transaction)
    adapter = SimpleNamespace(run_in_transaction=AsyncMock(side_effect=run), close=Mock())
    store = AssignmentStore(plane_runtime=runtime, async_runtime=adapter)
    assert await store.call("get_assignment", owner_id="owner") == "record"
    repo.get_assignment.assert_called_once_with(transaction, owner_id="owner")
    for error, code in [(RepositoryConflictError("hidden", code="assignment_stale"), 409),
                        (RepositoryValidationError("hidden"), 422),
                        (PlaneError("hidden", code="assignment_not_found"), 404),
                        (PlaneError("hidden", code="assignment_capacity"), 429),
                        (PlaneError("hidden"), 503),
                        (AssignmentError("assignment_denied", 403), 403)]:
        adapter.run_in_transaction.side_effect = error
        with pytest.raises(AssignmentError) as caught:
            await store.call("get_assignment")
        assert caught.value.status_code == code
        assert "hidden" not in str(caught.value)
    with pytest.raises(AssignmentError, match="contract_unavailable"):
        await store.call("_private")
    store.close()
    adapter.close.assert_called_once()
    with pytest.raises(AssignmentError):
        AssignmentStore()
    with pytest.raises(AssignmentError):
        AssignmentStore(plane_runtime=SimpleNamespace(repositories=object()))
    orch = SimpleNamespace(plane_repository_source=SimpleNamespace(
        plane_runtime=runtime, plane_repositories=runtime.repositories))
    assert AssignmentStore(orch).repository is repo


@pytest.mark.asyncio
async def test_retired_create_preserves_owner_receipts_and_refuses_new_work_before_tool_checks():
    import hashlib
    from uuid import UUID
    from persistent_agents.models import CreateAssignmentRequest, digest
    from persistent_agents.service import AssignmentService
    from persistent_agents.tests.test_models import create_payload

    request = CreateAssignmentRequest.model_validate(create_payload())
    store = MemoryStore()
    service = AssignmentService(object(), store=store, enabled=True, phi_gate=object())
    claims = {"sub": "owner"}
    with pytest.raises(AssignmentError, match="turn_engine_retired") as refused:
        await service.create("owner", claims, request)
    assert refused.value.status_code == 410
    assert [call[0] for call in store.calls] == ["get_submission_receipt"]

    assignment_id = str(UUID(bytes=hashlib.sha256(f"owner\0{request.submission_id}".encode()).digest()[:16], version=4))
    key = ("owner", assignment_id)
    retained = object()
    store.receipts[key] = digest(request.model_dump())
    store.records[key] = retained
    assert await service.create("owner", claims, request) is retained
    with pytest.raises(AssignmentError, match="assignment_owner_required"):
        await service.create("other", claims, request)
    assert [call[0] for call in store.calls] == ["get_submission_receipt", "get_submission_receipt"]

@pytest.mark.parametrize("command", ["revise", "resume", "run-now"])
async def test_owner_cannot_reactivate_a_retained_assignment(command):
    from persistent_agents.models import ControlRequest, ReviseAssignmentRequest
    from persistent_agents.service import AssignmentService
    from persistent_agents.tests.test_models import create_payload

    store = MemoryStore()
    identity = str(uuid4())
    store.records["owner", identity] = SimpleNamespace(owner_id="owner", instruction_revision=1, control_epoch=1)
    service = AssignmentService(object(), store=store, enabled=True, phi_gate=object())
    control = {"expected_instruction_revision": 1, "expected_control_epoch": 1}
    with pytest.raises(AssignmentError, match="turn_engine_retired") as refused:
        if command == "revise":
            request = ReviseAssignmentRequest.model_validate({**create_payload(), **control})
            await service.revise("owner", {"sub": "owner"}, identity, request)
        else:
            request = ControlRequest(submission_id=str(uuid4()), **control)
            await service.control("owner", {"sub": "owner"}, identity, command, request)
    assert refused.value.status_code == 410
    assert [call[0] for call in store.calls] == ["get_assignment", "get_submission_receipt"]


@pytest.mark.parametrize("decision", ["approve", "decline"])
async def test_pending_action_allows_an_audited_decline_and_refuses_execution(decision):
    from persistent_agents.models import ApprovalDecisionRequest
    from persistent_agents.service import AssignmentService

    owner = "owner"
    claims = {"sub": owner}
    identity, action_id = str(uuid4()), str(uuid4())
    socket = object()
    request = ApprovalDecisionRequest(decision=decision, submission_id=str(uuid4()),
        expected_instruction_revision=1, expected_control_epoch=1, request_digest="a" * 64)
    record = SimpleNamespace(assignment_id=identity, owner_id=owner, instruction_revision=1, control_epoch=1)
    proposal = SimpleNamespace(state="proposed", intent=SimpleNamespace(
        request_digest=request.request_digest, permission_digest="b" * 64, precondition_digest="c" * 64))
    declined = SimpleNamespace(state="declined")
    store = SimpleNamespace(call=AsyncMock(side_effect=[record, proposal, declined]))
    service = AssignmentService(SimpleNamespace(ui_sessions={socket: claims}), store=store,
                                enabled=True, phi_gate=object())
    service._audit = AsyncMock()
    call = service.decide(owner, claims, identity, action_id, request, interaction=socket)
    if decision == "approve":
        with pytest.raises(AssignmentError, match="turn_engine_retired"):
            await call
        assert store.call.await_count == 2
        service._audit.assert_not_awaited()
    else:
        assert await call is declined
        assert store.call.await_args.args == ("decide_action",)
        assert store.call.await_args.kwargs["decision"].permission_digest == "b" * 64
        service._audit.assert_awaited_once_with(claims, decision, record)
