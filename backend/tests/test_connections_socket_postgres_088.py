"""Exercises Connections issuance and revocation through registered socket ingress,
the current human boundary, real Plane credentials, and chrome rendering.
"""

import asyncio
from copy import deepcopy
import json
import re
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest

from orchestrator import auth, chrome_events, human_request_authority as human
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.projection_surfaces import connections
from persistent_agents.models import AssignmentError
from tests.helpers.session_consent_088 import consent_from_store
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_work_surface_ingress_088 import (
    ingress as ingress, surface as surface, command as command, context as context,
    fixture as fixture, runtime as runtime, service as service, signing_key as signing_key,
    registered,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def connected(ingress, runtime, service, monkeypatch):
    state = ingress
    state.connection_view_offsets = {}
    orch = state.orch
    orch.audit_repo = service.audit
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=runtime, repositories=runtime.repositories))
    boundary = human.HumanRequestBoundary(orch)
    orch.human_request_boundary = boundary
    orch.framework_credentials = FrameworkCredentialService(
        plane_runtime=runtime, sessions=runtime.repositories.history.sessions, audit=service.audit)
    state.socket.scope["headers"].append((b"origin", b"https://app.invalid"))
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "framework_credentials", True)

    async def configured(*_):
        return True

    monkeypatch.setattr(orch, "llm_configured_for", configured)
    try:
        yield state, orch.framework_credentials
    finally:
        boundary.close()
        for key in list(connections._PENDING_SECRETS):
            if key[0] == id(state.socket):
                connections._PENDING_SECRETS.pop(key)


def send(state, action, payload):
    generation = str(uuid4())
    state.connection_view_offsets[generation] = len(state.socket.payloads())
    state.socket.feed(json.dumps({"type": "ui_event", "action": action,
        "submission_id": str(uuid4()), "request_generation": generation,
        "connection_generation": state.connection_generation, "payload": payload}))
    return generation


async def terminal(state, generation):
    return await state.arrived(lambda value: value.get("type") == "operation_status"
        and value.get("terminal") is True and value.get("request_generation") == generation)


def issue_payload(**changes):
    return {"fields": {"name": "Socket-owned key", "scopes": ["operations.read"],
        "expires_in_seconds": 300, "max_admissions": 1, **changes}}


def views(state, generation):
    return [value for value in state.socket.payloads()[state.connection_view_offsets[generation]:]
        if value.get("type") == "chrome_render"]


def seed(credentials, fixture, *, owner=None, sid=None):
    owner, sid = owner or fixture[1], sid or fixture[2]
    return credentials.issue(owner_id=owner,
        caller=consent_from_store(fixture[0], owner, sid).observation,
        name="Existing owner key", scopes=["operations.read"], expires_in_seconds=300,
        max_admissions=1)


async def test_current_cookie_socket_issues_one_time_secret_then_revokes(connected, fixture, service):
    state, credentials = connected
    await registered(state)
    generation = send(state, "chrome_connection_issue", issue_payload())
    assert (await terminal(state, generation))["state"] == "completed"
    assert len(credentials.list(owner_id=fixture[1])) == 1
    rendered = views(state, generation)
    assert len(rendered) == 1 and "Your new key" in rendered[0]["html"]
    match = re.search(r"afk_[A-Za-z0-9_-]+\.[0-9a-f-]+\.[A-Za-z0-9_-]+", rendered[0]["html"])
    assert match is not None
    secret = match.group()
    caller = credentials.resolve_bearer(secret)
    assert caller is not None and caller.owner_id == fixture[1]
    rows = credentials.list(owner_id=fixture[1])
    assert len(rows) == 1 and rows[0]["max_admissions"] == 1
    assert rows[0]["consumed_admissions"] == 0 and secret not in repr(rows)
    assert not any(key[0] == id(state.socket) for key in connections._PENDING_SECRETS)
    generation = send(state, "chrome_connection_revoke", {"credential_id": caller.credential_id})
    assert (await terminal(state, generation))["state"] == "completed"
    assert "Key revoked" in views(state, generation)[0]["html"]
    assert secret not in views(state, generation)[0]["html"]
    assert credentials.resolve_bearer(secret) is None
    audit, _ = service.audit.list_for_user(fixture[1])
    actions = [row.action_type for row in audit]
    assert actions.count("auth.framework_issue") == actions.count("auth.framework_revoke") == 1
    assert service.audit.verify_chain(fixture[1]) is None


@pytest.mark.parametrize("action", ["chrome_connection_issue", "chrome_connection_revoke"])
async def test_bare_institutional_socket_requires_session_only_for_issue(connected, fixture, action):
    state, credentials = connected
    row, secret = seed(credentials, fixture)
    state.socket.scope["headers"] = [(key, value) for key, value in state.socket.scope["headers"]
        if key not in {b"cookie", b"origin"}]
    await registered(state)
    payload = issue_payload() if action.endswith("issue") else {"credential_id": row["credential_id"]}
    generation = send(state, action, payload)
    outcome = await terminal(state, generation)
    assert outcome["state"] == ("failed" if action.endswith("issue") else "completed")
    rows = credentials.list(owner_id=fixture[1])
    assert len(rows) == 1 and rows[0]["revoked"] is action.endswith("revoke")
    assert (credentials.resolve_bearer(secret) is None) is action.endswith("revoke")


async def test_foreign_credential_and_payload_owner_never_select_authority(connected, fixture):
    state, credentials = connected
    foreign_owner, foreign_sid = str(uuid4()), str(uuid4())
    fixture[0].create(foreign_sid, user_id=foreign_owner, access_token="synthetic-foreign-access",
        refresh_token="synthetic-foreign-refresh", hard_max_seconds=3600)
    try:
        row, secret = seed(credentials, fixture, owner=foreign_owner, sid=foreign_sid)
        await registered(state)
        generation = send(state, "chrome_connection_revoke", {
            "credential_id": row["credential_id"], "owner_id": foreign_owner})
        assert (await terminal(state, generation))["state"] == "completed"
        assert "That key no longer exists" in views(state, generation)[0]["html"]
        assert credentials.resolve_bearer(secret) is not None
        assert credentials.list(owner_id=fixture[1]) == []
        generation = send(state, "chrome_connection_issue", {
            **issue_payload(), "owner_id": foreign_owner})
        assert (await terminal(state, generation))["state"] == "completed"
        assert len(credentials.list(owner_id=fixture[1])) == 1
        assert len(credentials.list(owner_id=foreign_owner)) == 1
    finally:
        fixture[0].delete(foreign_sid)


@pytest.mark.parametrize("action", ["chrome_connection_issue", "chrome_connection_revoke"])
@pytest.mark.parametrize("loss", ["deleted", "replaced", "registration", "iam", "closed"])
async def test_original_authority_loss_during_admission_prevents_mutation(
    connected, fixture, runtime, monkeypatch, action, loss,
):
    state, credentials = connected
    row, _ = seed(credentials, fixture)
    before = credentials.list(owner_id=fixture[1])
    await registered(state)
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch._call_work_admission

    async def held(callback, *args, **kwargs):
        if getattr(callback, "__name__", "") == "_submit_connection_batch":
            entered.set()
            await release.wait()
        return await original(callback, *args, **kwargs)

    monkeypatch.setattr(state.orch, "_call_work_admission", held)
    payload = issue_payload() if action.endswith("issue") else {"credential_id": row["credential_id"]}
    generation = send(state, action, payload)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        frame = next(frame for frame in state.frames if str(frame.request_generation) == generation)
        assert frame.human_request is not None
        assert frame.human_request.method == "WS_WRITE"
        if loss == "deleted":
            await asyncio.to_thread(fixture[0].delete, fixture[2])
        elif loss == "replaced":
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        elif loss == "registration":
            state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
        elif loss == "iam":
            async def revoked(_token):
                raise HTTPException(401, "synthetic revoked institutional authority")
            monkeypatch.setattr(auth, "verify_production_token", revoked)
        else:
            state.orch._connection_contexts[id(state.socket)].registered = False
        release.set()
        assert (await terminal(state, generation))["state"] == "failed"
        assert credentials.list(owner_id=fixture[1]) == before
        assert views(state, generation) == []
    finally:
        release.set()


@pytest.mark.parametrize("action", ["chrome_connection_issue", "chrome_connection_revoke"])
@pytest.mark.parametrize("read_only", [False, True])
async def test_connections_mutations_refuse_missing_or_read_only_ingress(connected, fixture, action, read_only):
    from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT

    state, credentials = connected
    await registered(state)
    payload = issue_payload() if action.endswith("issue") else {"credential_id": str(uuid4())}
    pending = None
    if read_only:
        pending = human.capture_human_socket_request(state.orch.human_request_boundary,
            websocket=state.socket, context=state.orch._connection_contexts[id(state.socket)],
            message={"type": "ui_event", "action": "chrome_user_skill_edit", "payload": payload,
                "request_generation": str(uuid4()), "submission_id": str(uuid4()),
                "connection_generation": state.connection_generation})
        assert (await pending.authenticate())._method == "WS_READ"
    token = _CONNECTION_OPERATION_CONTEXT.set({"human_request": pending})
    try:
        with pytest.raises(AssignmentError, match="human_authentication_required"):
            await chrome_events.handle_chrome_event(state.orch, state.socket, action, payload, fixture[1])
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
        if pending is not None:
            pending.close()
    assert credentials.list(owner_id=fixture[1]) == []


async def test_socket_issuance_retains_closed_scope_validation(connected, fixture):
    state, credentials = connected
    await registered(state)
    generation = send(state, "chrome_connection_issue", issue_payload(scopes=["sudo"]))
    assert (await terminal(state, generation))["state"] == "completed"
    assert "Check the name, scopes, expiry and admissions" in views(state, generation)[0]["html"]
    assert credentials.list(owner_id=fixture[1]) == []
