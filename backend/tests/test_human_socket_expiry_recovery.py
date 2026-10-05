"""Exercises expired registered JWT recovery through authenticated socket ingress
and the issued-session repository. Denials stay correlated and never replay private
reads or writes across a socket, owner, policy, or credential replacement.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from jose import jwt
import pytest

from orchestrator import auth, human_request_authority as human
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_socket_ingress_088 import (
    metadata as metadata, ingress as ingress, surface as surface, command as command,
    context as context, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, registered, send,
)

pytestmark = pytest.mark.asyncio


def set_registration_clock(state, monkeypatch, *, offset=1):
    instant = datetime.fromtimestamp(state.orch.ui_sessions[state.socket]["exp"] + offset,
                                     timezone.utc)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(human, "datetime", ExpiredClock)
    monkeypatch.setattr(jwt, "datetime", ExpiredClock)


@pytest.mark.parametrize("action,payload", [
    ("chrome_user_skill_save", {}),
    ("chrome_user_skill_edit", {"attempt_write": False}),
    ("chrome_open", {"surface": "guidance"}),
    ("chrome_open", {"surface": "agent_authoring"}),
])
async def test_verified_expired_registration_refuses_then_requests_normal_recovery(
    metadata, fixture, monkeypatch, action, payload,
):
    state, calls, audits = metadata
    await registered(state)
    refreshes = len(fixture[-1])
    registration = state.orch.ui_sessions[state.socket]
    set_registration_clock(state, monkeypatch)
    generation = send(state, action, **payload)
    await state.barrier()
    frame = next(value for value in state.frames if str(value.request_generation) == generation)
    delivered = state.socket.payloads()
    refusal = next(value for value in delivered if value.get("submission_id") == str(frame.submission_id))
    assert refusal["type"] == "error" and refusal["accepted"] is False
    assert refusal["code"] == "operation_failed" and refusal["retryable"] is False
    recovery = [value for value in delivered if value.get("type") == "auth_required"]
    assert recovery == [{"type": "auth_required", "reason": "expired"}]
    assert delivered.index(refusal) < delivered.index(recovery[0])
    assert calls == [] and audits == [] and frame.human_request is None
    assert not state.orch._connection_contexts[id(state.socket)].ingress
    assert state.orch.ui_sessions[state.socket] is registration
    assert not any(value.get("type") == "operation_status" for value in delivered)
    assert len(fixture[-1]) == refreshes
    assert "Private instructions" not in str(refusal) and state.token not in str(delivered)


@pytest.mark.parametrize("failure", [
    "malformed", "signature", "future_token", "issuer", "issuer_type", "client",
    "client_type", "owner", "roles", "mcp_audience", "actor", "claims", "generic_verifier",
    "mock", "missing_cookie", "forged_cookie", "query_token", "origin", "revoked", "foreign_session",
])
async def test_expiry_recovery_does_not_reclassify_invalid_or_revoked_authority(
    metadata, fixture, runtime, monkeypatch, failure,
):
    state, calls, audits = metadata
    await registered(state)
    refreshes = len(fixture[-1])
    registration = state.orch.ui_sessions[state.socket]
    original_expiry = registration["exp"]
    if failure in {"issuer", "issuer_type", "client", "client_type", "roles", "mcp_audience", "actor"}:
        changes = {
            "issuer": {"iss": "https://foreign.invalid/realm"},
            "issuer_type": {"iss": True},
            "client": {"azp": "foreign-client"},
            "client_type": {"azp": True},
            "roles": {"realm_access": {"roles": []}},
            "mcp_audience": {"aud": "astral-mcp"},
            "actor": {"act": {"sub": "machine"}},
        }[failure]
        raw = fixture[3](exp=original_expiry, **changes)
        registration.update(jwt.get_unverified_claims(raw), _raw_token=raw)
    elif failure == "malformed":
        registration["_raw_token"] = "invalid.synthetic.jwt"
    elif failure == "signature":
        parts = state.token.split(".")
        parts[-1] = ("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]
        registration["_raw_token"] = ".".join(parts)
    elif failure == "future_token":
        registration["_raw_token"] = fixture[3](exp=original_expiry + 600)
    elif failure == "owner":
        registration["_raw_token"] = fixture[3](sub="foreign-owner", exp=original_expiry)
    elif failure == "claims":
        registration["preferred_username"] = "unsigned-change"
    elif failure == "generic_verifier":
        async def invalid(_):
            raise HTTPException(status_code=401, detail="Invalid token")
        monkeypatch.setattr(auth, "verify_production_token", invalid)
    elif failure == "mock":
        monkeypatch.setenv("USE_MOCK_AUTH", "true")
    elif failure in {"missing_cookie", "forged_cookie", "origin"}:
        kind = b"origin" if failure == "origin" else b"cookie"
        state.socket.scope["headers"] = [header for header in state.socket.scope["headers"]
                                          if header[0] != kind]
        if failure != "missing_cookie":
            state.socket.scope["headers"].append((kind,
                b"https://foreign.invalid" if failure == "origin" else b"astral_session=forged.bad"))
    elif failure == "query_token":
        state.socket.scope["query_string"] = b"token=private-untrusted-token"
    elif failure == "revoked":
        await asyncio.to_thread(fixture[0].delete, fixture[2])
    elif failure == "foreign_session":
        record = get_session_record(runtime, fixture[2])
        await asyncio.to_thread(replace_session_record, runtime, replace(record, owner_id=str(uuid4())))
    set_registration_clock(state, monkeypatch)
    generation = send(state)
    await state.barrier()
    frame = next(value for value in state.frames if str(value.request_generation) == generation)
    delivered = state.socket.payloads()
    assert any(value.get("submission_id") == str(frame.submission_id)
               and value.get("accepted") is False for value in delivered)
    assert not any(value.get("type") in {"auth_required", "operation_status"} for value in delivered)
    assert frame.human_request is None and calls == [] and audits == []
    assert len(fixture[-1]) == refreshes


@pytest.mark.parametrize("change", [
    "registration", "owner", "token", "context", "generation", "pending_registration",
    "closing", "unregistered", "policy", "message", "deadline", "until", "boundary",
    "revoked", "replaced", "refresh",
])
async def test_recovery_rechecks_original_socket_and_issued_session_after_jwt_wait(
    metadata, fixture, runtime, monkeypatch, change,
):
    state, calls, audits = metadata
    await registered(state)
    refreshes = len(fixture[-1])
    set_registration_clock(state, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    original = human.ExpiredHumanSocketRequest.__init__

    def capture(self, pending):
        original(self, pending)
        captured.append(pending)

    async def held_keys(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return signing_key_value

    from shared.jwks_cache import get_jwks
    signing_key_value = await get_jwks("unused", token=state.token)
    monkeypatch.setattr(human.ExpiredHumanSocketRequest, "__init__", capture)
    monkeypatch.setattr("shared.jwks_cache.get_jwks", held_keys)
    generation = send(state)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        pending = captured[0]
        context = state.orch._connection_contexts[id(state.socket)]
        if change == "registration":
            state.orch.ui_sessions[state.socket] = deepcopy(state.orch.ui_sessions[state.socket])
        elif change in {"owner", "token"}:
            state.orch.ui_sessions[state.socket]["sub" if change == "owner" else "_raw_token"] = "changed"
        elif change == "context":
            state.orch._connection_contexts[id(state.socket)] = object()
        elif change == "generation":
            context.connection_generation = uuid4()
        elif change == "pending_registration":
            context.work_registrations_pending = 1
        elif change in {"closing", "unregistered"}:
            setattr(context, "closing" if change == "closing" else "registered", change == "closing")
        elif change == "policy":
            monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "changed-policy")
        elif change == "message":
            pending.message["payload"]["fields"]["name"] = "Changed after capture"
        elif change == "deadline":
            pending.deadline = time.monotonic() - 1
        elif change == "until":
            pending.until = human.datetime.now(timezone.utc) - timedelta(seconds=1)
        elif change == "boundary":
            pending.boundary.close()
        elif change == "revoked":
            await asyncio.to_thread(fixture[0].delete, fixture[2])
        elif change == "replaced":
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        else:
            await asyncio.to_thread(fixture[0].update_tokens, fixture[2],
                access_token=fixture[3](), refresh_token="synthetic-later-refresh")
        release.set()
        if change not in {"closing", "unregistered", "context", "pending_registration"}:
            await state.barrier()
        else:
            await state.arrived(lambda value: value.get("type") == "error" and value.get("accepted") is False)
            async with asyncio.timeout(5):
                while not pending.closed:
                    await asyncio.sleep(0)
        frame = next(value for value in state.frames if str(value.request_generation) == generation)
        assert pending.closed and frame.human_request is None and calls == [] and audits == []
        assert not any(value.get("type") == "auth_required" for value in state.socket.payloads())
        assert len(fixture[-1]) == refreshes
    finally:
        release.set()


async def test_expired_refusal_never_replays_but_fresh_registration_allows_explicit_retry(
    metadata, fixture, monkeypatch,
):
    from tests.test_human_socket_ingress_088 import terminal

    state, calls, _ = metadata
    await registered(state)
    refreshes = len(fixture[-1])
    with monkeypatch.context() as clock:
        set_registration_clock(state, clock)
        generation = send(state)
        await state.barrier()
    assert calls == []
    refused = next(value for value in state.frames if str(value.request_generation) == generation)
    state.token = fixture[3]()
    await registered(state)
    assert calls == []
    retried = send(state, native=True)
    assert (await terminal(state))["state"] == "completed"
    assert len(calls) == 1 and refused.human_request is None
    assert retried != generation and len(fixture[-1]) == refreshes


async def test_registration_role_merge_matches_signed_first_party_claims(metadata, fixture, monkeypatch):
    state, calls, _ = metadata
    state.token = fixture[3](resource_access={"astral-web": {"roles": ["user"]}})
    await registered(state)
    refreshes = len(fixture[-1])
    registration = state.orch.ui_sessions[state.socket]
    registration["realm_access"]["roles"].extend(["user"])
    set_registration_clock(state, monkeypatch)
    send(state)
    await state.barrier()
    assert [value for value in state.socket.payloads() if value.get("type") == "auth_required"] == [
        {"type": "auth_required", "reason": "expired"},
    ]
    assert calls == [] and len(fixture[-1]) == refreshes


@pytest.mark.parametrize("replace_session", [False, True])
async def test_expiry_during_capture_retains_original_issued_session_fence(
    metadata, fixture, runtime, monkeypatch, replace_session,
):
    state, calls, _ = metadata
    await registered(state)
    refreshes = len(fixture[-1])
    set_registration_clock(state, monkeypatch, offset=-1)
    original = human._HumanSocketRequest.capture_session
    captured = []

    async def expiring(pending):
        await original(pending)
        captured.append(pending)
        set_registration_clock(state, monkeypatch)
        if replace_session:
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        pending.assert_socket()

    monkeypatch.setattr(human._HumanSocketRequest, "capture_session", expiring)
    send(state)
    await state.barrier()
    expected = [] if replace_session else [{"type": "auth_required", "reason": "expired"}]
    assert [value for value in state.socket.payloads() if value.get("type") == "auth_required"] == expected
    assert captured[0].closed and calls == [] and len(fixture[-1]) == refreshes


async def test_replacement_during_refusal_send_cannot_request_authentication_for_new_registration(
    metadata, monkeypatch,
):
    state, calls, _ = metadata
    await registered(state)
    set_registration_clock(state, monkeypatch)
    original = state.orch._safe_send

    async def replaced(socket, raw):
        sent = await original(socket, raw)
        if json.loads(raw).get("accepted") is False:
            state.orch.ui_sessions[socket] = deepcopy(state.orch.ui_sessions[socket])
        return sent

    monkeypatch.setattr(state.orch, "_safe_send", replaced)
    send(state)
    await state.barrier()
    assert calls == []
    assert not any(value.get("type") == "auth_required" for value in state.socket.payloads())


async def test_cancellation_during_expiry_proof_closes_unadmitted_capture(metadata, monkeypatch):
    state, calls, _ = metadata
    await registered(state)
    set_registration_clock(state, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    original = human.ExpiredHumanSocketRequest.__init__

    def capture(self, pending):
        original(self, pending)
        captured.append(pending)

    async def held_keys(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise AssertionError("Cancelled proof resumed")

    monkeypatch.setattr(human.ExpiredHumanSocketRequest, "__init__", capture)
    monkeypatch.setattr("shared.jwks_cache.get_jwks", held_keys)
    context = state.orch._connection_contexts[id(state.socket)]
    message = {"type": "ui_event", "action": "chrome_user_skill_save", "payload": {},
        "submission_id": str(uuid4()), "request_generation": str(uuid4()),
        "connection_generation": state.connection_generation}
    task = asyncio.create_task(state.orch._enqueue_connection_frame(context, json.dumps(message), message))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert captured[0].closed and calls == [] and not context.ingress
        assert not any(value.get("type") == "auth_required" for value in state.socket.payloads())
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
