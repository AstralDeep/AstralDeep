"""Original-session control observations, without changing execution eligibility.

Normal signed JWT verification and all session/operation rows use the real host
and Plane paths. Only external JWKS/refresh responses are synthetic. These tests
do not activate a route, claim work, or treat an observation as a committed control.
"""
import asyncio
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import time
from uuid import uuid4

import pytest

from astralplane.repositories.history import SessionExecutionObservation

from orchestrator import session_authority as execution, web_auth
from orchestrator.work_submit_authority import authenticate_work_submission_request
from persistent_agents.runtime_values import digest, thaw
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_operation_session_authority_088 import create_operation, set_operation
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import request
from tests.test_request_session_authority_088 import runtime as runtime
from tests.test_request_session_authority_088 import signing_key as signing_key


def module():
    return importlib.import_module("orchestrator.work_continuation_authority")


def control(runtime, record, command="pause"):
    """Apply a real owner transition to one fixture operation."""
    with runtime.transaction() as tx:
        return runtime.repositories.assignments.apply_control(
            tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision,
            expected_control_epoch=record.control_epoch,
            expected_state_version=record.state_version, submission_id=str(uuid4()),
            submission_digest=digest(command), control=command).assignment


def current(runtime, record):
    with runtime.transaction() as tx:
        return runtime.repositories.assignments.get_operation(
            tx, owner_id=record.owner_id, assignment_id=record.assignment_id).assignment


async def context(fixture, runtime, **claims):
    """Authenticate a synthetic owner bearer through ordinary production IAM."""
    return await authenticate_work_submission_request(request(headers=[
        (b"authorization", ("Bearer " + fixture[3](**claims)).encode()),
        (b"content-type", b"application/json"),
    ]), sessions=fixture[0], plane_runtime=runtime)


async def resolve(fixture, runtime, record, command="resume", selected=None):
    selected = selected or await context(fixture, runtime)
    return await module().refresh_operation_control_authority(
        context=selected, original=record, command=command, sessions=fixture[0])


def test_execution_resolver_still_rejects_actual_paused_operation(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(execution.refresh_operation_execution_authority(
            owner_id=paused.owner_id, assignment_id=paused.assignment_id,
            sessions=fixture[0], plane_runtime=runtime))
    assert fixture[-1] == [] and current(runtime, paused) == paused


def test_resume_observation_is_private_and_does_not_resume_or_grant_execution(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    result = asyncio.run(resolve(fixture, runtime, paused))
    assert result.record == paused and result.command == "resume"
    assert result.plane_runtime is runtime
    assert result.observation.credential.incarnation_id == paused.operation["authority"]["reference_id"]
    assert result.observation.credential.session_id == fixture[2]
    assert result.claims["sub"] == fixture[1]
    assert not isinstance(result, execution.OperationExecutionAuthority)
    assert not hasattr(result, "subject_token")
    assert "synthetic" not in repr(result) and fixture[1] not in repr(result)
    detached = result.claims
    detached["realm_access"]["roles"].clear()
    assert result.claims["realm_access"]["roles"] == ["user"]
    with pytest.raises(FrozenInstanceError):
        result.command = "wake"
    assert len(fixture[-1]) == 1 and current(runtime, paused) == paused


def test_identical_sid_replacement_after_request_cannot_become_control_authority(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        selected = await context(fixture, runtime)
        replacement = replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        with pytest.raises(execution.SessionAuthorityUnavailable):
            await resolve(fixture, runtime, paused, selected=selected)
        assert get_session_record(runtime, fixture[2]) == replacement
    asyncio.run(scenario())
    assert fixture[-1] == [] and current(runtime, paused) == paused


@pytest.mark.parametrize("later_command", ["resume", "pause"])
def test_changed_operation_since_receipt_miss_refuses_before_refresh(fixture, runtime, later_command):
    paused = control(runtime, create_operation(fixture, runtime))
    later = control(runtime, paused, later_command)
    assert later.control_epoch > paused.control_epoch
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, paused))
    assert fixture[-1] == [] and current(runtime, paused) == later


def wait(fixture, runtime, record):
    """Construct a real worker event-wait row, not a new owner wait endpoint."""
    state = fixture[0].capture_execution_reference(
        owner_id=record.owner_id, session_id=fixture[2]).state
    observation = SessionExecutionObservation(state.credential, state.observed_at,
        state.observed_at + timedelta(seconds=15))
    with runtime.transaction() as tx:
        repo = runtime.repositories.assignments
        claim = repo.claim_operation_for_administration(tx, owner_id=record.owner_id,
            assignment_id=record.assignment_id, expected_state_version=record.state_version,
            worker_id="synthetic-control-fixture", authority=observation)
        return repo.set_event_wait(tx, fence=claim.fence,
            expected_state_version=claim.assignment.state_version,
            checkpoint={"schema_version": 1}, completion_digest=digest("fixture-wait"),
            event_key="synthetic-source-observation", source_revision=1)


def test_wake_observation_keeps_real_wait_and_does_not_accept_an_event(fixture, runtime):
    waiting = wait(fixture, runtime, create_operation(fixture, runtime))
    result = asyncio.run(resolve(fixture, runtime, waiting, command="wake"))
    assert result.command == "wake" and result.record == waiting
    assert not hasattr(result, "subject_token")
    assert current(runtime, waiting) == waiting
    assert waiting.operation["control"]["wake_receipts"] == {}
    assert len(fixture[-1]) == 1


@pytest.mark.parametrize("state,command", [
    ("active", "resume"), ("stopped", "resume"), ("authority_hold", "resume"),
    ("paused", "wake"), ("active", "wake"), ("paused_wait", "wake"),
])
def test_command_state_policy_refuses_before_refresh(fixture, runtime, state, command):
    record = create_operation(fixture, runtime)
    if state == "authority_hold":
        record = control(runtime, record, "revoke")
        record = control(runtime, record)
    elif state == "paused_wait":
        record = control(runtime, wait(fixture, runtime, record))
    elif state != "active":
        record = control(runtime, record, "stop" if state == "stopped" else "pause")
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, record, command=command))
    assert fixture[-1] == [] and current(runtime, record) == record


@pytest.mark.parametrize("change", ["legacy", "future", "control", "checkpoint",
                                     "scheduled", "delegation", "expired", "deadline"])
def test_unknown_or_ineligible_original_reference_cannot_resume(fixture, runtime, change):
    record = control(runtime, create_operation(fixture, runtime))
    operation = thaw(record.operation)
    if change == "legacy":
        operation["version"] = 1
        operation["authority"]["reference_kind"] = "session"
    elif change == "future":
        operation["version"] = 3
    elif change == "control":
        operation["control"] = {"version": 2}
    elif change == "checkpoint":
        runtime.execute("UPDATE persistent_assignment SET data=jsonb_set(data,'{checkpoint}',"
                        "'{\"schema_version\":2}'::jsonb) WHERE id=%s", (record.assignment_id,))
    elif change in {"scheduled", "delegation"}:
        operation["authority"].update(origin="scheduled" if change == "scheduled" else "interactive",
            reference_kind="offline_grant" if change == "scheduled" else "delegation")
    else:
        target = operation["authority"] if change == "expired" else operation
        target["expires_at" if change == "expired" else "deadline_at"] = "2000-01-01T00:00:00+00:00"
    if change != "checkpoint":
        set_operation(runtime, record, operation)
    stored = current(runtime, record)
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, stored))
    assert fixture[-1] == [] and current(runtime, record) == stored


@pytest.mark.parametrize("field,value", [
    ("context", {}), ("original", {}), ("command", "pause"), ("command", "stop"),
    ("command", []), ("sessions", object()), ("owner", "other-owner"),
    ("runtime", object()),
])
def test_private_inputs_and_runtime_are_not_request_authority(fixture, runtime, field, value):
    record = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        selected = await context(fixture, runtime)
        values = dict(context=selected, original=record, command="resume", sessions=fixture[0])
        if field == "owner":
            values["original"] = replace(record, owner_id=value)
        elif field == "runtime":
            values["context"] = replace(selected, plane_runtime=value)
        else:
            values[field] = value
        with pytest.raises(execution.SessionAuthorityUnavailable):
            await module().refresh_operation_control_authority(**values)
    asyncio.run(scenario())
    assert fixture[-1] == []


def test_current_request_and_newer_session_do_not_replace_original_issuance(fixture, runtime):
    store, owner, sid, _, seen = fixture
    paused = control(runtime, create_operation(fixture, runtime))
    other = uuid4().hex
    store.create(other, user_id=owner, access_token="synthetic-other-access",
                 refresh_token="synthetic-other-refresh", hard_max_seconds=3600)
    try:
        before = get_session_record(runtime, other)
        result = asyncio.run(resolve(fixture, runtime, paused))
        assert result.observation.credential.session_id == sid
        assert get_session_record(runtime, other) == before
        assert seen[0][0] == "synthetic-initial-refresh"
    finally:
        store.delete(other)


@pytest.mark.parametrize("cap", ["request", "refreshed_jwt", "operation", "deadline"])
def test_control_observation_keeps_every_original_expiry_cap(fixture, runtime, monkeypatch, cap):
    record = create_operation(fixture, runtime,
        expiry_seconds=8 if cap == "operation" else 300,
        deadline_seconds=8 if cap == "deadline" else None)
    paused = control(runtime, record)
    expires = time.time() + 8
    async def scenario():
        selected = await context(fixture, runtime, **({"exp": expires} if cap == "request" else {}))
        if cap == "refreshed_jwt":
            async def exchange(*args):
                return {"access_token": fixture[3](exp=expires)}
            monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
        return await resolve(fixture, runtime, paused, selected=selected)
    result = asyncio.run(scenario())
    expected = (datetime.fromisoformat(paused.operation["authority"]["expires_at"]).timestamp()
                if cap == "operation" else datetime.fromisoformat(paused.operation["deadline_at"]).timestamp()
                if cap == "deadline" else expires)
    assert result.observation.valid_until.timestamp() == pytest.approx(expected, abs=.000001)


@pytest.mark.parametrize("change", ["replace", "delete", "rotate"])
def test_session_replacement_between_incarnation_and_sid_reads_makes_zero_http_calls(
        fixture, runtime, monkeypatch, change):
    paused = control(runtime, create_operation(fixture, runtime))
    repo = fixture[0]._sessions.repository
    read = repo.get_execution_state
    after = []
    def changed(transaction, **kwargs):
        if change == "replace":
            replace_session_record(runtime, get_session_record(runtime, fixture[2]))
        elif change == "delete":
            fixture[0].delete(fixture[2])
        else:
            fixture[0].update_tokens(fixture[2], access_token="synthetic-later-access",
                                     refresh_token="synthetic-later-refresh")
        after.append(get_session_record(runtime, fixture[2]))
        return read(transaction, **kwargs)
    monkeypatch.setattr(repo, "get_execution_state", changed)
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, paused))
    assert fixture[-1] == [] and len(after) == 1
    assert get_session_record(runtime, fixture[2]) == after[0]


@pytest.mark.parametrize("change", ["session", "stop", "resume"])
def test_remote_response_cannot_adopt_replacement_or_changed_control(fixture, runtime, monkeypatch, change):
    paused = control(runtime, create_operation(fixture, runtime))
    changed = []
    async def exchange(*args):
        fixture[-1].append("attempt")
        if change == "session":
            changed.append(replace_session_record(runtime, get_session_record(runtime, fixture[2])))
        else:
            changed.append(control(runtime, paused, change))
        return {"access_token": fixture[3](), "refresh_token": "synthetic-late-refresh"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, paused))
    assert fixture[-1] == ["attempt"] and len(changed) == 1
    assert (get_session_record(runtime, fixture[2]) if change == "session"
            else current(runtime, paused)) == changed[0]


@pytest.mark.parametrize("failure", ["unknown", "malformed", "cancel"])
def test_unknown_refresh_or_cancellation_does_not_retry_or_return_control(fixture, runtime, monkeypatch, failure):
    paused = control(runtime, create_operation(fixture, runtime))
    async def exchange(*args):
        fixture[-1].append("attempt")
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "unknown":
            raise TimeoutError("PRIVATE-TRANSPORT")
        return {"access_token": "PRIVATE MALFORMED"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else execution.SessionAuthorityUnavailable):
        asyncio.run(resolve(fixture, runtime, paused))
    with pytest.raises(execution.SessionAuthorityUnavailable, match="^session_authority_unavailable$"):
        asyncio.run(resolve(fixture, runtime, paused))
    assert fixture[-1] == ["attempt"] and current(runtime, paused) == paused


@pytest.mark.parametrize("stage", ["original", "final"])
def test_operation_sql_reads_refuse_while_actual_table_blocker_stays_held(fixture, runtime, monkeypatch, signing_key, stage):
    paused = control(runtime, create_operation(fixture, runtime))
    with ExitStack() as stack:
        def block():
            tx = stack.enter_context(runtime.transaction())
            tx.execute("LOCK TABLE persistent_assignment IN ACCESS EXCLUSIVE MODE")
        if stage == "original":
            block()
        else:
            async def keys(*args, **kwargs):
                # Context IAM runs before refresh; block only the refreshed JWT.
                if fixture[-1]:
                    block()
                return signing_key[1]
            monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
        started = time.monotonic()
        with pytest.raises(execution.SessionAuthorityUnavailable):
            asyncio.run(resolve(fixture, runtime, paused))
        assert time.monotonic() - started < 3
        assert runtime._pool.snapshot.borrowed == 1
    assert len(fixture[-1]) == int(stage == "final")


def test_request_expiry_after_final_assignment_read_refuses_control(fixture, runtime, monkeypatch):
    paused = control(runtime, create_operation(fixture, runtime))
    original = runtime.repositories.assignments.get_operation
    reads = []
    expires = time.time() + 2
    def reread(tx, **kwargs):
        value = original(tx, **kwargs)
        reads.append(value)
        if len(reads) == 2:
            time.sleep(max(0, expires - time.time()) + .05)
        return value
    monkeypatch.setattr(runtime.repositories.assignments, "get_operation", reread)
    async def scenario():
        selected = await context(fixture, runtime, exp=expires)
        with pytest.raises(execution.SessionAuthorityUnavailable):
            await resolve(fixture, runtime, paused, selected=selected)
    asyncio.run(scenario())
    assert len(reads) == 2 and len(fixture[-1]) == 1


def test_expired_authenticated_request_refuses_without_session_refresh(fixture, runtime):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        selected = await context(fixture, runtime)
        expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        claims = selected.claims
        claims["exp"] = expiry.timestamp()
        selected = replace(selected, principal_expires_at=expiry, _claims_json=json.dumps(claims))
        with pytest.raises(execution.SessionAuthorityUnavailable):
            await resolve(fixture, runtime, paused, selected=selected)
    asyncio.run(scenario())
    assert fixture[-1] == []


@pytest.mark.parametrize("changes", [
    {"sub": "other-owner"}, {"iss": "https://wrong.invalid"},
    {"azp": "untrusted-client"}, {"realm_access": {"roles": []}},
    {"act": {"sub": "agent"}}, {"exp": True},
])
def test_refreshed_signed_token_must_pass_ordinary_owner_iam(fixture, runtime, monkeypatch, changes):
    paused = control(runtime, create_operation(fixture, runtime))
    async def exchange(*args):
        fixture[-1].append("attempt")
        return {"access_token": fixture[3](**changes)}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(execution.SessionAuthorityUnavailable, match="^session_authority_unavailable$"):
        asyncio.run(resolve(fixture, runtime, paused))
    assert fixture[-1] == ["attempt"] and current(runtime, paused) == paused


def test_bound_session_keeps_original_issuer_client_and_incarnation(fixture, runtime, monkeypatch):
    store, owner, sid, token, seen = fixture
    store.delete(sid)
    store.create(sid, user_id=owner, access_token=token(), refresh_token="synthetic-bound-refresh",
        hard_max_seconds=3600, issuing_issuer="https://request-authority.invalid/realm",
        issuing_client_id="astral-web")
    paused = control(runtime, create_operation(fixture, runtime))
    async def exchange(refresh, identity):
        seen.append((refresh, identity))
        return {"access_token": token(), "refresh_token": "synthetic-bound-rotated"}
    monkeypatch.setattr(web_auth, "_exchange_bound_session_refresh", exchange)
    result = asyncio.run(resolve(fixture, runtime, paused))
    credential = result.observation.credential
    assert credential.issuing_issuer == "https://request-authority.invalid/realm"
    assert credential.issuing_client_id == "astral-web"
    assert credential.incarnation_id == paused.operation["authority"]["reference_id"]
    assert len(seen) == 1 and seen[0][0] == "synthetic-bound-refresh"
    assert seen[0][1].owner_id == owner
    assert seen[0][1].issuer == credential.issuing_issuer
    assert seen[0][1].client_id == credential.issuing_client_id


def test_concurrent_control_observations_do_not_share_a_refresh_claim(fixture, runtime, monkeypatch):
    paused = control(runtime, create_operation(fixture, runtime))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        selected = await context(fixture, runtime)
        async def exchange(*args):
            fixture[-1].append("attempt")
            entered.set()
            await release.wait()
            return {"access_token": fixture[3]()}
        monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
        first = asyncio.create_task(resolve(fixture, runtime, paused, selected=selected))
        await asyncio.wait_for(entered.wait(), timeout=3)
        try:
            with pytest.raises(execution.SessionAuthorityUnavailable):
                await resolve(fixture, runtime, paused, selected=selected)
        finally:
            release.set()
        assert (await first).record == paused
    asyncio.run(scenario())
    assert fixture[-1] == ["attempt"] and current(runtime, paused) == paused
