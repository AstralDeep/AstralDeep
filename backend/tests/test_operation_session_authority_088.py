"""Tests for unregistered continuation authority
(backend/orchestrator/session_authority.py, auth.py, web_auth.py) through real
AstralPlane and JWT policy: incarnation binding, expiry, refresh causality, and
session isolation across replacement.
"""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import time
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
import httpx
from astralplane.repositories.assignment_models import (
    AssignmentDefinition, AssignmentOperationAuthority, AssignmentOperationSpec,
)
from astralplane.repositories.history import SessionExecutionObservation

from orchestrator import auth, session_authority as sa, web_auth
from persistent_agents.runtime_values import digest, thaw
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import runtime as runtime
from tests.test_request_session_authority_088 import signing_key as signing_key
from tests.test_request_session_authority_088 import REAL_EXCHANGE


def create_operation(fixture, runtime, *, expiry_seconds=300, deadline_seconds=None):
    store, owner, sid, _, _ = fixture
    state = store.capture_execution_reference(owner_id=owner, session_id=sid).state
    hard_expiry = datetime.fromtimestamp(state.credential.hard_expires_at, timezone.utc)
    observation = SessionExecutionObservation(
        credential=state.credential, started_at=state.observed_at,
        valid_until=min(state.observed_at + timedelta(seconds=15), hard_expiry))
    identity = str(uuid4())
    with runtime.transaction() as tx:
        runtime.repositories.history.sessions.bound_request_execution_waits(tx)
        return runtime.repositories.assignments.create_operation(
            tx, owner_id=owner, assignment_id=identity, origin_namespace="fixture",
            caller_key=identity, command_digest=digest(identity), authority=observation,
            definition=AssignmentDefinition(
                name="Synthetic continuation", instructions="Read one public source.", source={},
                allowed_tools=(), consented_scopes=(), offline_grant_id=None,
                limits={"max_retries": 1, "max_concurrent_tasks": 1, "max_depth": 1,
                        "max_tasks": 2, "model_calls": 2, "tool_calls": 2,
                        "tokens": 100, "elapsed_ms": 1000}),
            operation=AssignmentOperationSpec("chat", AssignmentOperationAuthority(
                owner, "interactive", "session_incarnation", state.credential.incarnation_id,
                min(state.observed_at + timedelta(seconds=expiry_seconds), hard_expiry)),
                state.observed_at + timedelta(seconds=expiry_seconds if deadline_seconds is None else deadline_seconds), "none"))


def refresh(fixture, runtime, record, **changes):
    values = {"owner_id": record.owner_id, "assignment_id": record.assignment_id,
              "sessions": fixture[0], "plane_runtime": runtime}
    values.update(changes)
    return asyncio.run(sa.refresh_operation_execution_authority(**values))


def test_continuation_selects_original_incarnation_and_keeps_private_claims(fixture, runtime):
    store, owner, sid, _, seen = fixture
    record = create_operation(fixture, runtime)
    other = uuid4().hex
    store.create(other, user_id=owner, access_token="synthetic-other-access",
                 refresh_token="synthetic-other-refresh", hard_max_seconds=3600)
    try:
        before = get_session_record(runtime, other)
        result = refresh(fixture, runtime, record)
        assert result.record == record
        assert result.plane_runtime is runtime
        assert result.observation.credential.incarnation_id == record.operation["authority"]["reference_id"]
        assert result.observation.credential.session_id == sid
        assert result.claims["sub"] == owner and result.claims["sid"] != sid
        assert result.subject_token and result.claims.get("_raw_token") is None
        assert "synthetic" not in repr(result) and result.subject_token not in repr(result)
        claims = result.claims
        claims["sub"] = "changed"
        claims["realm_access"]["roles"].clear()
        assert result.claims["sub"] == owner and result.claims["realm_access"]["roles"] == ["user"]
        assert len(seen) == 1 and seen[0][0] == "synthetic-initial-refresh"
        assert get_session_record(runtime, other) == before
    finally:
        store.delete(other)


def test_recreated_identical_sid_cannot_continue_original_operation(fixture, runtime):
    record = create_operation(fixture, runtime)
    before = get_session_record(runtime, fixture[2])
    replacement = replace_session_record(runtime, before)
    assert replacement.incarnation_id != before.incarnation_id
    with pytest.raises(sa.SessionAuthorityUnavailable, match="session_authority_unavailable"):
        refresh(fixture, runtime, record)
    assert fixture[-1] == []
    assert get_session_record(runtime, fixture[2]) == replacement


@pytest.mark.parametrize("field,value", [
    ("owner_id", "other-owner"), ("assignment_id", str(uuid4())),
    ("assignment_id", "not-an-id"), ("plane_runtime", object()),
    ("sessions", object()),
])
def test_missing_or_unbound_authority_refuses_before_remote(fixture, runtime, field, value):
    record = create_operation(fixture, runtime)
    with pytest.raises(sa.SessionAuthorityUnavailable, match="session_authority_unavailable"):
        refresh(fixture, runtime, record, **{field: value})
    assert fixture[-1] == []


def test_original_operation_expiry_caps_observation(fixture, runtime):
    record = create_operation(fixture, runtime, expiry_seconds=8)
    result = refresh(fixture, runtime, record)
    assert result.observation.valid_until.isoformat() == record.operation["authority"]["expires_at"]


@pytest.mark.parametrize("mode", ["true", "1", "yes", " TRUE "])
def test_mock_mode_never_resolves_continuation(fixture, runtime, monkeypatch, mode):
    record = create_operation(fixture, runtime)
    monkeypatch.setenv("USE_MOCK_AUTH", mode)
    with pytest.raises(sa.SessionAuthorityUnavailable, match="session_authority_unavailable"):
        refresh(fixture, runtime, record)
    assert fixture[-1] == []


def set_operation(runtime, record, operation):
    runtime.execute("UPDATE persistent_assignment SET data=jsonb_set(data,'{operation}',%s::jsonb) WHERE id=%s",
                    (json.dumps(operation), record.assignment_id))


@pytest.mark.parametrize("change", ["legacy", "future", "scheduled", "delegation", "bad-reference", "expired", "deadline", "paused"])
def test_ineligible_stored_operation_never_refreshes(fixture, runtime, change):
    record = create_operation(fixture, runtime)
    operation = thaw(record.operation)
    if change == "legacy":
        operation["version"] = 1
        operation["authority"]["reference_kind"] = "session"
    elif change == "future":
        operation["version"] = 3
    elif change in {"scheduled", "delegation"}:
        operation["authority"].update(origin="scheduled" if change == "scheduled" else "interactive",
            reference_kind="offline_grant" if change == "scheduled" else "delegation")
    elif change == "bad-reference":
        operation["authority"]["reference_id"] = "not-an-incarnation"
    elif change in {"expired", "deadline"}:
        target = operation["authority"] if change == "expired" else operation
        target["expires_at" if change == "expired" else "deadline_at"] = "2000-01-01T00:00:00+00:00"
    else:
        runtime.execute("UPDATE persistent_assignment SET lifecycle='paused',data=jsonb_set(data,'{lifecycle}','\"paused\"'::jsonb) WHERE id=%s", (record.assignment_id,))
    if change != "paused":
        set_operation(runtime, record, operation)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert fixture[-1] == []


def test_task_deadline_and_original_db_sample_cap_observation(fixture, runtime):
    record = create_operation(fixture, runtime, deadline_seconds=8)
    result = refresh(fixture, runtime, record)
    assert result.observation.valid_until.isoformat() == record.operation["deadline_at"]
    assert result.observation.valid_until - result.observation.started_at < timedelta(seconds=15)


def test_fresh_jwt_expiry_caps_observation(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    expiry = time.time() + 8
    async def exchange(*args):
        return {"access_token": fixture[3](exp=expiry)}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    assert refresh(fixture, runtime, record).observation.valid_until.timestamp() == pytest.approx(expiry)


@pytest.mark.parametrize("changes", [
    {"sub": "other-owner"}, {"iss": "https://wrong.invalid"}, {"azp": "not-allowed"},
    {"realm_access": {"roles": []}}, {"act": {"sub": "agent"}}, {"aud": "astral-mcp"},
    {"exp": None}, {"exp": True}, {"exp": "9999999999"}, {"exp": 1}, {"exp": float("inf")},
])
def test_operation_refresh_uses_normal_signed_jwt_and_role_policy(fixture, runtime, monkeypatch, changes):
    record = create_operation(fixture, runtime)
    async def exchange(*args):
        fixture[-1].append("attempt")
        return {"access_token": fixture[3](**changes)}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(sa.SessionAuthorityUnavailable, match="^session_authority_unavailable$"):
        refresh(fixture, runtime, record)
    assert fixture[-1] == ["attempt"]


@pytest.mark.parametrize("change", ["replace", "delete", "rotate"])
def test_same_incarnation_lookup_and_state_capture_cannot_cross_replacement(fixture, runtime, monkeypatch, change):
    record = create_operation(fixture, runtime)
    store, _, sid, _, seen = fixture
    original = store._sessions.repository.get_execution_state
    after = []
    def between_reads(transaction, **kwargs):
        if change == "delete":
            store.delete(sid)
        elif change == "rotate":
            store.update_tokens(sid, access_token="rotated-during-read", refresh_token="rotated-refresh")
        else:
            replace_session_record(runtime, get_session_record(runtime, sid))
        after.append(get_session_record(runtime, sid))
        return original(transaction, **kwargs)
    monkeypatch.setattr(store._sessions.repository, "get_execution_state", between_reads)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert seen == [] and get_session_record(runtime, sid) == after[0]


@pytest.mark.parametrize("change", ["replace", "delete", "retire"])
def test_remote_result_does_not_adopt_or_resurrect_a_retired_session(fixture, runtime, monkeypatch, change):
    record = create_operation(fixture, runtime)
    store, owner, sid, token, seen = fixture
    after = []
    async def exchange(*args):
        seen.append("attempt")
        if change == "replace":
            after.append(replace_session_record(runtime, get_session_record(runtime, sid)))
        elif change == "delete":
            store.delete(sid)
        else:
            with runtime.transaction() as tx:
                runtime.repositories.assignments.retire_operations_for_owner(tx, owner_id=owner)
        return {"access_token": token(), "refresh_token": "late-old-result"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert seen == ["attempt"]
    if change == "replace":
        assert get_session_record(runtime, sid) == after[0]
    elif change == "delete":
        assert get_session_record(runtime, sid) is None


@pytest.mark.parametrize("change", ["credential", "state_version", "control_epoch", "instruction_revision", "authority", "deadline", "timestamp-only"])
def test_final_check_preserves_original_operation_and_session_generation(fixture, runtime, monkeypatch, signing_key, change):
    record = create_operation(fixture, runtime)
    mutated = []
    async def keys(*args, **kwargs):
        if change == "credential":
            fixture[0].update_tokens(fixture[2], access_token="newer-access", refresh_token="newer-refresh")
        elif change in {"authority", "deadline"}:
            operation = thaw(record.operation)
            if change == "authority":
                operation["authority"]["reference_id"] = str(uuid4())
            else:
                operation["deadline_at"] = (record.created_at + timedelta(seconds=200)).isoformat()
            set_operation(runtime, record, operation)
        elif change == "timestamp-only":
            # Not a real lease renewal — that also bumps state_version
            runtime.execute("UPDATE persistent_assignment SET data=jsonb_set(data,'{updated_at}',to_jsonb(%s::text)) WHERE id=%s",
                ((record.updated_at + timedelta(seconds=1)).isoformat(), record.assignment_id))
        else:
            assert change in {"state_version", "control_epoch", "instruction_revision"}
            physical = "state_version=state_version+1," if change == "state_version" else ""
            runtime.execute(f"UPDATE persistent_assignment SET {physical}data=jsonb_set(data,'{{{change}}}',to_jsonb((data->>'{change}')::bigint+1)) WHERE id=%s", (record.assignment_id,))
        with runtime.transaction() as tx:
            current = runtime.repositories.assignments.get_operation(tx,
                owner_id=record.owner_id, assignment_id=record.assignment_id).assignment
        if change in {"state_version", "control_epoch", "instruction_revision", "timestamp-only"}:
            field = "updated_at" if change == "timestamp-only" else change
            assert getattr(current, field) > getattr(record, field)
        elif change in {"authority", "deadline"}:
            assert current.operation != record.operation
        else:
            assert fixture[0].get(fixture[2])["access_token"] == "newer-access"
        mutated.append(True)
        return signing_key[1]
    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    if change == "timestamp-only":
        assert refresh(fixture, runtime, record).record == record
    else:
        with pytest.raises(sa.SessionAuthorityUnavailable):
            refresh(fixture, runtime, record)
    assert mutated == [True]


@pytest.mark.parametrize("failure", ["timeout", "transport", "malformed", "cancel"])
def test_unknown_remote_outcome_never_replays_or_returns_old_access(fixture, runtime, monkeypatch, failure):
    record = create_operation(fixture, runtime)
    async def exchange(*args):
        fixture[-1].append("attempt")
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise TimeoutError("PRIVATE")
        if failure == "transport":
            raise RuntimeError("PRIVATE")
        return {"access_token": "invalid PRIVATE token"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert fixture[-1] == ["attempt"] and fixture[0].get(fixture[2])["refresh_token"] == ""


@pytest.mark.parametrize("stage", ["operation_read", "session_read", "final_operation", "final_session"])
def test_each_database_boundary_is_bounded_while_blocker_remains(fixture, runtime, monkeypatch, signing_key, stage):
    record = create_operation(fixture, runtime)
    with ExitStack() as stack:
        def block():
            blocker = stack.enter_context(runtime.transaction())
            table = "web_session" if stage in {"session_read", "final_session"} else "persistent_assignment"
            blocker.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        if stage.startswith("final"):
            async def keys(*args, **kwargs):
                block()
                return signing_key[1]
            monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
        else:
            block()
        started = time.monotonic()
        with pytest.raises(sa.SessionAuthorityUnavailable):
            refresh(fixture, runtime, record)
        assert time.monotonic() - started < 3
        assert runtime._pool.snapshot.borrowed == 1
        with runtime.transaction() as probe:
            assert probe.fetch_one("SELECT 1 AS alive")["alive"] == 1
    assert len(fixture[-1]) == int(stage.startswith("final"))


def test_verifying_roles_copies_lists_and_never_logs_claims(fixture, caplog):
    payload = {"sub": "PRIVATE-SUBJECT", "private": "PRIVATE-TOKEN",
        "realm_access": {"roles": []}, "resource_access": {
            "astral-web": {"roles": ["user"]}, "account": {"roles": ["account-role"]}}}
    original = json.loads(json.dumps(payload))
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(auth.verify_user(payload)) == original
        assert auth._extract_roles(payload) == ["user", "account-role"]
    assert payload == original and "PRIVATE" not in caplog.text


def test_expired_original_db_observation_refuses_before_http(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    capture = fixture[0].capture_incarnation_execution_reference
    def expired(**kwargs):
        reference = capture(**kwargs)
        return replace(reference, state=replace(reference.state,
            observed_at=reference.state.observed_at - timedelta(seconds=16)))
    monkeypatch.setattr(fixture[0], "capture_incarnation_execution_reference", expired)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert fixture[-1] == []


def test_concurrent_continuations_never_share_or_replay_refresh_claim(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def exchange(*args):
            fixture[-1].append("attempt")
            entered.set()
            await release.wait()
            return {"access_token": fixture[3]()}
        monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
        kwargs = dict(owner_id=record.owner_id, assignment_id=record.assignment_id,
                      sessions=fixture[0], plane_runtime=runtime)
        first = asyncio.create_task(sa.refresh_operation_execution_authority(**kwargs))
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            with pytest.raises(sa.SessionAuthorityUnavailable):
                await sa.refresh_operation_execution_authority(**kwargs)
        finally:
            release.set()
        assert (await first).record == record
    asyncio.run(scenario())
    assert fixture[-1] == ["attempt"]


def test_session_hard_expiry_caps_current_observation(fixture, runtime):
    store, owner, sid, token, _ = fixture
    store.delete(sid)
    store.create(sid, user_id=owner, access_token=token(),
        refresh_token="synthetic-short-session", hard_max_seconds=8)
    record = create_operation(fixture, runtime)
    current = refresh(fixture, runtime, record).observation
    assert current.valid_until.timestamp() == current.credential.hard_expires_at


def test_operation_deadline_expiring_during_capture_prevents_http(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime, deadline_seconds=1)
    capture = fixture[0].capture_incarnation_execution_reference
    captured = []
    def after_deadline(**kwargs):
        time.sleep(1.05)
        value = capture(**kwargs)
        captured.append(value.state.observed_at)
        return value
    monkeypatch.setattr(fixture[0], "capture_incarnation_execution_reference", after_deadline)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert len(captured) == 1 and fixture[-1] == []


def test_second_session_guard_rechecks_db_time_after_operation_reread(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    expiry = int(time.time()) + 2
    async def exchange(*args):
        return {"access_token": fixture[3](exp=expiry), "refresh_token": "expiry-persisted"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    assignments = runtime.repositories.assignments
    get_operation = assignments.get_operation
    repository = runtime.repositories.history.sessions
    check = repository.assert_current_execution
    guarded = []
    reads = []
    def observed_guard(transaction, *, observation):
        if observation.valid_until.timestamp() == expiry:
            guarded.append("attempt")
        return check(transaction, observation=observation)
    def delayed_reread(transaction, **kwargs):
        result = get_operation(transaction, **kwargs)
        reads.append(result)
        if len(reads) == 2:
            assert guarded == ["attempt"]
            time.sleep(max(0, expiry - time.time()) + .05)
        return result
    monkeypatch.setattr(repository, "assert_current_execution", observed_guard)
    monkeypatch.setattr(assignments, "get_operation", delayed_reread)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert len(reads) == 2 and guarded == ["attempt", "attempt"]
    assert fixture[0].get(fixture[2])["refresh_token"] == "expiry-persisted"


def test_non_encrypted_capture_is_not_continuation_authority(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    monkeypatch.setattr(fixture[0], "_fernet", None)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert fixture[-1] == []


def test_real_http_exchange_persists_then_uses_production_iam(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    real_client, attempts = httpx.AsyncClient, []
    async def respond(request):
        assert runtime._pool.snapshot.borrowed == 0
        assert str(request.url) == "https://request-authority.invalid/realm/protocol/openid-connect/token"
        attempts.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": fixture[3](),
            "refresh_token": "real-http-rotation"})
    monkeypatch.setattr(web_auth.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", REAL_EXCHANGE)
    result = refresh(fixture, runtime, record)
    assert result.claims["sub"] == record.owner_id
    assert len(attempts) == 1 and attempts[0]["refresh_token"] == ["synthetic-initial-refresh"]
    assert fixture[0].get(fixture[2])["refresh_token"] == "real-http-rotation"


def test_overall_timeout_during_iam_never_returns_an_observation(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    cancelled = []
    async def stalled_keys(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    monkeypatch.setattr("shared.jwks_cache.get_jwks", stalled_keys)
    monkeypatch.setattr(sa, "_TIME_LIMIT_SECONDS", .2)
    with pytest.raises(sa.SessionAuthorityUnavailable):
        refresh(fixture, runtime, record)
    assert cancelled == [True] and len(fixture[-1]) == 1
    assert fixture[0].get(fixture[2])["refresh_token"] == "synthetic-rotated-refresh"


def test_cancelled_final_read_never_returns_authority_and_releases_worker(fixture, runtime, monkeypatch):
    record = create_operation(fixture, runtime)
    assignments = runtime.repositories.assignments
    read = assignments.get_operation
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    reads = []
    def held_reread(transaction, **kwargs):
        result = read(transaction, **kwargs)
        reads.append(result)
        if len(reads) == 2:
            entered.set()
            assert release.wait(3)
            completed.set()
        return result
    monkeypatch.setattr(assignments, "get_operation", held_reread)
    async def scenario():
        task = asyncio.create_task(sa.refresh_operation_execution_authority(
            owner_id=record.owner_id, assignment_id=record.assignment_id,
            sessions=fixture[0], plane_runtime=runtime))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert await asyncio.to_thread(completed.wait, 3)
    asyncio.run(scenario())
    assert len(reads) == 2 and runtime._pool.snapshot.borrowed == 0
