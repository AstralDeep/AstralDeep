"""Service authorization with repository and current authority test doubles."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astralplane.repositories.assignment_models import AssignmentRecord
from persistent_agents.models import AssignmentError, CreateAssignmentRequest
from persistent_agents.service import AssignmentService, public_record
from persistent_agents.tests.test_models import create_payload


def make_record(assignment_id, owner_id, definition):
    return AssignmentRecord(assignment_id=assignment_id, owner_id=owner_id, definition=definition,
        instruction_revision=1, control_epoch=1, state_version=1, lifecycle="active", phase="waiting",
        next_wake_at=None, wake_reason="created", wake_generation=1, checkpoint={}, tasks=(), usage={},
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


class MemoryStore:
    """Test-only replay seam; Plane integration tests own transactional guarantees."""
    def __init__(self):
        self.records = {}
        self.receipts = {}
        self.calls = []

    async def call(self, method, **kwargs):
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


@pytest.fixture
def service(monkeypatch):
    store = MemoryStore()
    permissions = SimpleNamespace(list_disabled_agents=Mock(return_value=[]),
                                  get_tool_scope=Mock(return_value="tools:read"))
    grants = SimpleNamespace(capture=Mock(return_value="grant-1"), is_valid=Mock(return_value=True))
    orch = SimpleNamespace(tool_permissions=permissions, offline_grants=grants,
        web_sessions=SimpleNamespace(latest_refresh_token_for=Mock(return_value="never-print-token")),
        history=SimpleNamespace(get_chat=Mock(return_value={"id": "chat-1"})), ui_sessions={})
    monkeypatch.setattr("persistent_agents.service.eligible_tool_pairs", lambda *a, **kw:
                        [("web-research-1", SimpleNamespace(id="fetch_page"))])
    monkeypatch.setattr("persistent_agents.service.validate_egress_url", lambda url: None)
    return AssignmentService(orch, store, enabled=True,
                             phi_gate=SimpleNamespace(contains_phi=Mock(return_value=False)))


@pytest.mark.asyncio
async def test_create_replay_no_duplicate_grant_and_owner_projection(service):
    model = CreateAssignmentRequest.model_validate(create_payload())
    first = await service.create("owner", {"sub": "owner"}, model)
    again = await service.create("owner", {"sub": "owner"}, model)
    assert first == again
    assert service.orch.offline_grants.capture.call_count == 1
    assert first.definition.offline_grant_id == "grant-1"
    assert first.definition.consented_scopes == ("tools:read",)
    assert first.definition.allowed_tools == ("web-research-1:fetch_page",)
    projected = public_record(first)
    assert "offline_grant_id" not in projected["definition"]
    assert "owner_id" not in projected
    assert projected["cost_status"] == "unpriced"
    assert await service.list("other", {"sub": "other"}) == ()
    with pytest.raises(AssignmentError, match="assignment_not_found"):
        await service.get("other", {"sub": "other"}, first.assignment_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{"sub": "someone"}, {"sub": "owner", "act": {"sub": "agent"}},
    {"sub": "owner", "machine_turn_class": "scheduled_job"}, {}])
async def test_nonhuman_or_wrong_owner_refused(service, claims):
    with pytest.raises(AssignmentError):
        await service.create("owner", claims, CreateAssignmentRequest.model_validate(create_payload()))
    assert not service.store.records


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["consent", "phi", "grant", "source", "money", "conversation"])
async def test_activation_failures_create_no_running_assignment(service, monkeypatch, reason):
    payload = create_payload()
    if reason == "consent":
        payload["consent"] = False
    elif reason == "phi":
        service.phi_gate.contains_phi.return_value = True
    elif reason == "grant":
        service.orch.web_sessions.latest_refresh_token_for.return_value = None
    elif reason == "source":
        monkeypatch.setattr("persistent_agents.service.eligible_tool_pairs", lambda *a, **kw: [])
    elif reason == "money":
        payload["limits"] = {"daily": {"spend_micro_units": 0, "currency": "USD"},
                             "lifetime": {"spend_micro_units": 0, "currency": "USD"}}
    elif reason == "conversation":
        payload["conversation_id"] = "someone-elses-chat"
        service.orch.history.get_chat.return_value = None
    with pytest.raises(AssignmentError):
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    assert not service.store.records


@pytest.mark.asyncio
async def test_create_reused_submission_changed_body_conflicts(service):
    payload = create_payload()
    await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    payload["instructions"] = "Changed authority"
    with pytest.raises(AssignmentError, match="submission_conflict"):
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))


@pytest.mark.asyncio
async def test_live_execution_rechecks_grant_and_source_arguments(service):
    record = await service.create("owner", {"sub": "owner"},
                                  CreateAssignmentRequest.model_validate(create_payload()))
    current = await service.validate_execution("owner", {"sub": "owner"}, record)
    assert len(current["permission_digest"]) == 64
    service.orch.offline_grants.is_valid.return_value = False
    with pytest.raises(AssignmentError, match="authorization_required"):
        await service.validate_execution("owner", {"sub": "owner"}, record)


@pytest.mark.asyncio
async def test_disabled_service_and_unsafe_reader_fail_closed(service):
    service.enabled = False
    with pytest.raises(AssignmentError, match="disabled"):
        await service.list("owner", {"sub": "owner"})
    service.enabled = True
    service.orch.tool_permissions.get_tool_scope.return_value = "tools:write"
    with pytest.raises(AssignmentError, match="source_not_read_only"):
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))


@pytest.mark.asyncio
async def test_registered_reader_requires_trusted_bound(service, monkeypatch):
    payload = create_payload()
    payload["source"] = {"profile": "registered_reader", "agent_id": "reader-1", "tool_name": "read",
                         "arguments": {"folder": "inbox"}}
    payload["allowed_tools"] = [{"agent_id": "reader-1", "tool_name": "read"}]
    monkeypatch.setattr("persistent_agents.service.eligible_tool_pairs", lambda *a, **kw:
                        [("reader-1", SimpleNamespace(id="read"))])
    model = CreateAssignmentRequest.model_validate(payload)
    with pytest.raises(AssignmentError, match="tool_bound_unavailable"):
        await service.create("owner", {"sub": "owner"}, model)
    service.orch.persistent_tool_bounds = {"reader-1:read": {
        "model_calls": 0, "tool_calls": 1, "tokens": 0, "elapsed_ms": 10_000}}
    assert (await service.create("owner", {"sub": "owner"}, model)).definition.source["profile"] == "registered_reader"


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
async def test_service_read_bounds_and_terminal_deletion(service):
    from dataclasses import replace
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    assert await service.activity("owner", {"sub": "owner"}, record.assignment_id) == ()
    assert await service.proposals("owner", {"sub": "owner"}, record.assignment_id) == ()
    assert await service.actions("owner", {"sub": "owner"}, record.assignment_id,
                                 after_id=record.assignment_id) == ()
    assert await service.events("owner", {"sub": "owner"}, record.assignment_id,
                                after_id=record.assignment_id) == ()
    for method in (service.actions, service.events):
        with pytest.raises(AssignmentError, match="not_found"):
            await method("owner", {"sub": "owner"}, record.assignment_id, after_id="bad")
        with pytest.raises(AssignmentError, match="not_found"):
            await method("other", {"sub": "other"}, record.assignment_id)
    assert await service.tasks("owner", {"sub": "owner"}, record.assignment_id) == ()
    assert await service.list("owner", {"sub": "owner"}, after_id=record.assignment_id) == (record,)
    for limit in (True, 0, 101):
        with pytest.raises(AssignmentError, match="page_invalid"):
            await service.list("owner", {"sub": "owner"}, limit=limit)
    with pytest.raises(AssignmentError, match="cursor_invalid"):
        await service.activity("owner", {"sub": "owner"}, record.assignment_id, after_sequence=-1)
    with pytest.raises(AssignmentError, match="not_found"):
        await service.get("owner", {"sub": "owner"}, "invalid")
    with pytest.raises(AssignmentError, match="not_found"):
        await service.validate_execution("owner", {"sub": "owner"}, replace(record, owner_id="other"))
    with pytest.raises(AssignmentError, match="control_invalid"):
        await service.delete("owner", {"sub": "owner"}, record.assignment_id, expected_control_epoch=False)
    call = service.store.call

    async def delete(method, **kwargs):
        if method == "delete_for_owner":
            assert kwargs["expected_control_epoch"] == 1
            return True
        return await call(method, **kwargs)
    service.store.call = delete
    assert await service.delete("owner", {"sub": "owner"}, record.assignment_id, expected_control_epoch=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["phi", "permission", "grant_capture", "grant_check", "destination", "egress"])
async def test_prerequisite_failure_never_activates_or_leaks_diagnostics(service, monkeypatch, seam):
    payload = create_payload()
    error = RuntimeError("PRIVATE token and source text")
    if seam == "phi":
        service.phi_gate.contains_phi.side_effect = error
    elif seam == "permission":
        service.orch.tool_permissions.list_disabled_agents.side_effect = error
    elif seam == "grant_capture":
        service.orch.offline_grants.capture.side_effect = error
    elif seam == "grant_check":
        service.orch.offline_grants.is_valid.return_value = False
    elif seam == "destination":
        payload["conversation_id"] = "chat-1"
        service.orch.history.get_chat.side_effect = error
    elif seam == "egress":
        monkeypatch.setattr("persistent_agents.service.validate_egress_url", Mock(side_effect=error))
    with pytest.raises(AssignmentError) as caught:
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    assert "PRIVATE" not in str(caught.value)
    assert not service.store.records


@pytest.mark.asyncio
async def test_optional_trusted_quote_and_operator_delegation_posture(service, monkeypatch):
    from shared.feature_flags import flags
    payload = create_payload()
    payload["limits"] = {"daily": {"currency": "USD", "spend_micro_units": 10},
                         "lifetime": {"currency": "USD", "spend_micro_units": 100}, "max_depth": 1}
    monkeypatch.setattr(flags, "is_enabled", lambda name: False)
    with pytest.raises(AssignmentError, match="delegation_disabled"):
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    monkeypatch.setattr(flags, "is_enabled", lambda name: True)
    service.quote_provider = AsyncMock(return_value={})
    with pytest.raises(AssignmentError, match="cost_quote_unavailable"):
        await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    service.quote_provider = lambda *args: {"reviewed": "trusted-coverage"}
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(payload))
    assert record.definition.cost_quote_coverage["reviewed"] == "trusted-coverage"
    assert public_record(record)["cost_status"] == "capped"
    service.enabled = None
    assert await service.list("owner", {"sub": "owner"}) == (record,)


@pytest.mark.asyncio
async def test_runtime_permission_scope_grant_and_model_request_errors(service):
    from dataclasses import replace
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    service.orch.offline_grants.is_valid.side_effect = RuntimeError("private")
    with pytest.raises(AssignmentError, match="authorization_required"):
        await service.validate_execution("owner", {"sub": "owner"}, record)
    service.orch.offline_grants.is_valid.side_effect = None
    with pytest.raises(AssignmentError, match="scope_changed"):
        await service.validate_execution("owner", {"sub": "owner"},
            replace(record, definition=replace(record.definition, consented_scopes=())))
    service.orch.tool_permissions.get_tool_scope.return_value = "unknown:scope"
    with pytest.raises(AssignmentError, match="scope_unavailable"):
        await service.validate_execution("owner", {"sub": "owner"}, record)
    service.orch.tool_permissions.get_tool_scope.return_value = "tools:read"
    await service.validate_execution("owner", {"sub": "owner"}, record,
                                    SimpleNamespace(request={"kind": "model", "messages": []}))
    with pytest.raises(AssignmentError, match="action_invalid"):
        await service.validate_execution("owner", {"sub": "owner"}, record,
                                        SimpleNamespace(request={"kind": "arbitrary"}))
    with pytest.raises(AssignmentError, match="tool_outside_consent"):
        await service.validate_execution("owner", {"sub": "owner"}, record,
            SimpleNamespace(request={"kind": "tool", "agent_id": "other", "tool_name": "write"}))


@pytest.mark.asyncio
async def test_api_auth_owner_mutations_pagination_and_safe_errors(service):
    from fastapi import FastAPI, HTTPException
    from httpx import ASGITransport, AsyncClient
    from orchestrator.auth import get_current_user_payload, require_user_id
    from persistent_agents.api import assignment_router
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(persistent_assignments=service)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "owner"}
    app.include_router(assignment_router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post("/api/persistent-agents", json=create_payload())
        assert response.status_code == 201
        assignment_id = response.json()["assignment"]["assignment_id"]
        base = f"/api/persistent-agents/{assignment_id}"
        for path in ("/api/persistent-agents?limit=1", base, base + "/activity", base + "/tasks",
                     base + "/actions", base + "/events"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert "grant-1" not in response.text
        assert (await client.get("/api/persistent-agents?limit=0")).status_code == 422
        assert (await client.get("/api/persistent-agents/not-a-uuid")).status_code == 404
        invalid = {**create_payload(), "refresh_token": "PRIVATE"}
        response = await client.post("/api/persistent-agents", json=invalid)
        assert response.status_code == 422 and "PRIVATE" not in response.text
        def unauthenticated():
            raise HTTPException(401, "private error")
        app.dependency_overrides[require_user_id] = unauthenticated
        response = await client.get("/api/persistent-agents")
        assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
        assert "private" not in response.text
        app.dependency_overrides[require_user_id] = lambda: "owner"
        del app.state.orchestrator
        response = await client.get("/api/persistent-agents")
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_owner_evidence_reads_paginate_and_hide_source_and_dispatch_capabilities(service):
    from uuid import uuid4

    from astralplane.repositories.assignment_models import (
        AssignmentActionIntent,
        AssignmentActionRecord,
        AssignmentResourceAmount,
        AssignmentSourceEvent,
    )
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from orchestrator.auth import get_current_user_payload, require_user_id
    from persistent_agents.api import assignment_router

    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    action = AssignmentActionRecord(str(uuid4()), record.assignment_id, "owner",
        AssignmentActionIntent("action-key", {"kind": "tool"}, "a" * 64, AssignmentResourceAmount(),
                               "b" * 64, "c" * 64, downstream_key="PRIVATE_KEY"), 1, 1, "succeeded",
        attempts=({"attempt_id": "attempt", "state": "succeeded", "dispatch_token": "PRIVATE_TOKEN"},))
    event = AssignmentSourceEvent(str(uuid4()), "PRIVATE_SOURCE", "PRIVATE_ITEM", "PRIVATE_REVISION",
                                  "d" * 64, "e" * 64, {"text": "PRIVATE_CONTENT"}, "completed", "f" * 64)
    original = service.store.call

    async def call(method, **kwargs):
        if method in ("list_actions", "list_events"):
            assert kwargs["owner_id"] == "owner" and kwargs["limit"] == 1
            return () if kwargs.get("after_id") else (action if method == "list_actions" else event,)
        return await original(method, **kwargs)

    service.store.call = call
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(persistent_assignments=service)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "owner"}
    app.include_router(assignment_router)
    base = f"/api/persistent-agents/{record.assignment_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        for name, identity in (("actions", action.action_id), ("events", event.event_id)):
            response = await client.get(base + "/" + name + "?limit=1")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert "PRIVATE" not in response.text
            assert response.json()["next_cursor"] == identity
            final = await client.get(base + "/" + name + "?limit=1&after_id=" + identity)
            assert final.json() == {name: [], "next_cursor": None}
            assert (await client.get(base + "/" + name + "?limit=101")).status_code == 422
        app.dependency_overrides[require_user_id] = lambda: "other"
        app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "other"}
        assert (await client.get(base + "/events")).status_code == 404
        assert (await client.get(base + "/actions")).status_code == 404


@pytest.mark.asyncio
async def test_api_revision_control_and_pending_approval_return_durable_identity(service):
    from uuid import uuid4

    from astralplane.repositories.assignment_models import (
        AssignmentActionIntent,
        AssignmentActionRecord,
        AssignmentControlResult,
        AssignmentResourceAmount,
    )
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from orchestrator.auth import get_current_user_payload, require_user_id
    from persistent_agents.api import assignment_router
    from persistent_agents.tests.test_controls import control_payload
    record = await service.create("owner", {"sub": "owner"}, CreateAssignmentRequest.model_validate(create_payload()))
    result = AssignmentControlResult(record, True, (), ())
    service.revise = AsyncMock(return_value=result)
    service.control = AsyncMock(return_value=result)
    intent = AssignmentActionIntent("approval", {"kind": "tool"}, "a" * 64,
        AssignmentResourceAmount(tool_calls=1), "b" * 64, "c" * 64)
    action = AssignmentActionRecord(str(uuid4()), record.assignment_id, "owner", intent, 1, 1, "approved",
        attempts=({"attempt_id": "a", "state": "reserved", "dispatch_token": "PRIVATE"},))
    service.decide = AsyncMock(return_value=action)
    app = FastAPI()
    app.state.orchestrator = SimpleNamespace(persistent_assignments=service)
    app.dependency_overrides[require_user_id] = lambda: "owner"
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "owner"}
    app.include_router(assignment_router)
    base = f"/api/persistent-agents/{record.assignment_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(base, json={**create_payload(), **control_payload()})
        assert response.status_code == 200 and response.json()["applied"] is True
        response = await client.post(base + "/pause", json=control_payload())
        assert response.status_code == 200
        service.control.return_value = record
        response = await client.post(base + "/run-now", json=control_payload())
        assert response.status_code == 202 and response.json()["assignment"]["assignment_id"] == record.assignment_id
        approval = {**control_payload(), "decision": "approve", "request_digest": "a" * 64}
        response = await client.post(base + f"/approvals/{action.action_id}/decision", json=approval)
        assert response.status_code == 202
        assert response.json()["action"]["action_id"] == action.action_id
        assert "PRIVATE" not in response.text
        service.decide.return_value = {"state": "succeeded", "action_id": action.action_id, "token": "PRIVATE"}
        response = await client.post(base + f"/approvals/{action.action_id}/decision", json=approval)
        assert response.status_code == 200 and "PRIVATE" not in response.text
