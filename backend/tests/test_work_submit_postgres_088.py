"""Unregistered acceptance with real JWT, Plane, policy and atomic audit.

Only external JWKS/refresh replies and PHI model output are synthetic. Each test
uses a task-owned PostgreSQL schema; no endpoint, runner or physical tool runs.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.repository import AuditRepository
from orchestrator import auth, web_auth, work_submit_authority as authority
from orchestrator.tool_permissions import ToolPermissionManager
from orchestrator.work_submit import WorkSubmitService
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from persistent_agents.runtime_values import thaw
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_request_session_authority_088 import fixture as fixture
from tests.test_request_session_authority_088 import request, signing_key as signing_key

runtime = plane


def command(**changes):
    values = {"version": 1, "caller_key": str(uuid4()), "kind": "research", "name": "Read release",
        "instructions": "Read one public release.", "source": {"url": "https://93.184.216.34/releases"},
        "limits": {"model_calls": 1, "tool_calls": 3, "tokens": 1, "elapsed_ms": 90000, "max_retries": 1},
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        "conversation_id": None}
    values.update(changes)
    return json.dumps(values, allow_nan=False).encode()


@pytest.fixture
def service(runtime, fixture, monkeypatch):
    sessions, owner, _, _, _ = fixture
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-acceptance-audit-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.invalid")
    permissions = ToolPermissionManager(plane_runtime=runtime)
    permissions.register_tool_scopes("web-research-1", {"fetch_page": "tools:read"})
    permissions.set_agent_scopes(owner, "web-research-1", {"tools:read": True})
    orch = SimpleNamespace(agents={"web-research-1": object()}, local_agents={},
        agent_cards={"web-research-1": SimpleNamespace(skills=[SimpleNamespace(id="fetch_page")])},
        _is_draft_agent=lambda _: False, security_flags={}, tool_permissions=permissions,
        history=SimpleNamespace(get_chat=lambda *_args, **_kwargs: None))
    assignments = AssignmentService(orch, AssignmentStore(plane_runtime=runtime), enabled=True,
        phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))
    return WorkSubmitService(assignments, AuditRepository(plane_runtime=runtime), sessions)


async def context(fixture, runtime, *, cookie=True, bearer=False, headers=(), changes=None, **kwargs):
    sid = fixture[2] if cookie else None
    fields = [(b"content-type", b"application/json"), (b"origin", b"https://app.invalid"), *headers]
    if bearer:
        fields.append((b"authorization", ("Bearer " + fixture[3](**(changes or {}))).encode()))
    req = request(sid, headers=fields, **kwargs)
    return await authority.authenticate_work_submission_request(req, sessions=fixture[0], plane_runtime=runtime)


def totals(runtime, owner):
    with runtime.transaction() as tx:
        return tuple(tx.fetch_one(sql, (owner,))["n"] for sql in [
            "SELECT count(*) AS n FROM persistent_assignment WHERE owner_user_id=%s",
            "SELECT count(*) AS n FROM assignment_operation_receipt WHERE owner_id=%s",
            "SELECT count(*) AS n FROM audit_events WHERE actor_user_id=%s AND action_type='work.accept'"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bearer", [False, True])
async def test_current_request_creates_one_task_receipt_and_normal_chain(service, fixture, runtime, bearer):
    selected = await context(fixture, runtime, bearer=bearer)
    result = await service.submit(selected, command())
    record = result.record
    assert result.created and totals(runtime, fixture[1]) == (1, 1, 1)
    assert record.operation["version"] == 2 and record.execution_profile == "one_shot"
    assert record.operation["authority"]["reference_id"] == get_session_record(runtime, fixture[2]).incarnation_id
    assert record.definition.offline_grant_id is None and record.definition.allowed_tools == ("web-research-1:fetch_page",)
    assert record.usage["spent"] == {} and record.usage["outstanding"] == {}
    rows, _ = service.audit.list_for_user(fixture[1])
    assert len(rows) == 1 and rows[0].action_type == "work.accept"
    assert service.audit.verify_chain(fixture[1]) is None
    assert len(fixture[-1]) == 1
    assert "synthetic" not in repr(selected) and "Read one" not in repr(result)


@pytest.mark.asyncio
async def test_accepted_retry_needs_no_cookie_refresh_source_or_policy(service, fixture, runtime, monkeypatch):
    body = command()
    accepted = await service.submit(await context(fixture, runtime), body)
    fixture[0].delete(fixture[2])
    service.assignments.orch.agent_cards.clear()
    def forbidden(*args, **kwargs):
        raise AssertionError("accepted retry expanded new intent")
    monkeypatch.setattr(service, "_definition", forbidden)
    monkeypatch.setattr(authority, "refresh_work_submission_authority", forbidden)
    from orchestrator import work_submit
    monkeypatch.setattr(work_submit, "refresh_work_submission_authority", forbidden)
    replay = await service.submit(await context(fixture, runtime, cookie=False, bearer=True), body)
    assert not replay.created and replay.record == accepted.record
    assert totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_bare_bearer_cannot_admit_or_borrow_owner_latest(service, fixture, runtime):
    selected = await context(fixture, runtime, cookie=False, bearer=True)
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"sub": "foreign-owner"}, {"realm_access": {"roles": []}},
    {"iss": "https://wrong.invalid"}, {"azp": "foreign-client"}, {"act": {"sub": "agent"}},
    {"exp": 1}, {"exp": None}, {"exp": "9999999999"}, {"aud": "astral-mcp"}])
async def test_normal_jwt_policy_and_owner_selection_refuse(service, fixture, runtime, changes):
    with pytest.raises(AssignmentError):
        selected = await context(fixture, runtime, bearer=True, changes=changes)
        await service.submit(selected, command())
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query", "origin", "duplicate-origin", "type", "options", "mock"])
async def test_write_gates_unchanged(service, fixture, runtime, monkeypatch, kind):
    values = {"query": b"token=secret"} if kind == "query" else {}
    if kind == "options":
        values["method"] = "OPTIONS"
    if kind == "mock":
        monkeypatch.setenv("USE_MOCK_AUTH", "true")
    headers = [(b"content-type", b"text/plain" if kind == "type" else b"application/json"),
               (b"origin", b"https://app.invalid:0" if kind == "origin" else b"https://app.invalid")]
    if kind == "duplicate-origin":
        headers.append((b"origin", b"https://app.invalid"))
    with pytest.raises(AssignmentError):
        await authority.authenticate_work_submission_request(request(fixture[2], headers=headers, **values),
            sessions=fixture[0], plane_runtime=runtime)
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_request_scope_mutation_during_verification_cannot_switch_selection(service, fixture, runtime, monkeypatch):
    original = auth.verify_production_token
    req = request(fixture[2], headers=[(b"content-type", b"application/json"),
        (b"authorization", ("Bearer " + fixture[3]()).encode())])
    async def verify(token):
        req.scope["headers"][:] = [(b"cookie", b"astral_session=forged"), (b"content-type", b"text/plain")]
        req.scope["query_string"] = b"token=changed"
        req.scope["method"] = "DELETE"
        return await original(token)
    monkeypatch.setattr(auth, "verify_production_token", verify)
    selected = await authority.authenticate_work_submission_request(req, sessions=fixture[0], plane_runtime=runtime)
    assert selected.session_id == fixture[2]
    result = await service.submit(selected, command())
    assert result.created


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["duplicate-cookie", "forged-cookie", "no-cookie"])
async def test_bad_cookie_is_new_admission_refusal(service, fixture, runtime, change):
    fields = [(b"cookie", b"astral_session=forged")] if change != "no-cookie" else []
    selected = await context(fixture, runtime, cookie=change == "duplicate-cookie", bearer=True, headers=fields)
    with pytest.raises(AssignmentError):
        await service.submit(selected, command())
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("kind", "chat"), ("source", {}), ("source", {"url": "https://127.0.0.1"}),
    ("name", ""), ("instructions", {}), ("conversation_id", "unowned"), ("limits", {}),
    ("limits", {"model_calls": 1, "tool_calls": True, "tokens": 1, "elapsed_ms": 90000, "max_retries": 1}),
    ("deadline_at", "2000-01-01T00:00:00Z")])
async def test_new_body_policy_refuses_without_partial_acceptance(service, fixture, runtime, field, value):
    with pytest.raises(AssignmentError):
        await service.submit(await context(fixture, runtime), command(**{field: value}))
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"{}", b"null", b"{", b"\xff", b"x" * 16385,
    command(version=True), command(deadline_at="2030-01-01T00:00:00+00:00"),
    command().replace(b'"version": 1', b'"version": 1, "version": 1'),
    command().replace(b'"model_calls": 1', b'"model_calls": NaN')])
async def test_bounded_shape_precedes_any_refresh(service, fixture, runtime, raw):
    with pytest.raises(AssignmentError, match="work_submit_invalid"):
        await service.submit(await context(fixture, runtime), raw)
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_mismatched_original_key_conflicts_without_refresh(service, fixture, runtime):
    body = command()
    await service.submit(await context(fixture, runtime), body)
    changed = json.loads(body)
    changed["instructions"] = "A different request."
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await service.submit(await context(fixture, runtime), json.dumps(changed).encode())
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_required_audit_failure_rolls_back_without_retry_or_compensation(service, fixture, runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit unavailable")
    monkeypatch.setattr(service.audit, "insert_in_transaction", fail)
    with pytest.raises(AssignmentError, match="assignment_transaction_unavailable"):
        await service.submit(await context(fixture, runtime), command())
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_commit_ack_loss_resolves_exact_receipt_once(service, fixture, runtime, monkeypatch):
    original = service.store.transaction
    lost = []
    async def transaction(callback, **kwargs):
        value = await original(callback, **kwargs)
        if getattr(value, "created", False) and not lost:
            lost.append(True)
            raise AssignmentError("assignment_transaction_unavailable", 503)
        return value
    monkeypatch.setattr(service.store, "transaction", transaction)
    result = await service.submit(await context(fixture, runtime), command())
    assert not result.created and lost == [True]
    assert totals(runtime, fixture[1]) == (1, 1, 1) and len(fixture[-1]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 3])
async def test_accepted_old_version_is_never_upgraded_or_reauthorized(service, fixture, runtime, version):
    body = command()
    initial = await service.submit(await context(fixture, runtime), body)
    with runtime.transaction() as tx:
        row = tx.fetch_one("SELECT data FROM persistent_assignment WHERE id=%s", (initial.record.assignment_id,))
        value = thaw(row["data"])
        value["operation"]["version"] = version
        if version == 1:
            value["operation"]["authority"].update(reference_kind="session", reference_id=fixture[2])
        tx.execute("UPDATE persistent_assignment SET data=%s::jsonb WHERE id=%s",
                   (json.dumps(value), initial.record.assignment_id))
    replay = await service.submit(await context(fixture, runtime, cookie=False, bearer=True), body)
    assert not replay.created and replay.record.operation["version"] == version
    assert replay.record.assignment_id == initial.record.assignment_id
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_original_principal_expires_during_accepted_receipt_read(service, fixture, runtime, monkeypatch):
    body = command()
    await service.submit(await context(fixture, runtime), body)
    expiry = time.time() + 1.6
    selected = await context(fixture, runtime, cookie=False, bearer=True, changes={"exp": expiry})
    original = service.store.transaction
    async def delayed(callback, **kwargs):
        result = await original(callback, **kwargs)
        await asyncio.sleep(max(0, expiry - time.time()) + .03)
        return result
    monkeypatch.setattr(service.store, "transaction", delayed)
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, body)
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["replacement", "logout", "owner"])
async def test_loss_after_refresh_never_commits_new_admission(service, fixture, runtime, monkeypatch, loss):
    original = service._definition
    async def definition(*args):
        result = await original(*args)
        def retire():
            if loss == "replacement":
                replace_session_record(runtime, get_session_record(runtime, fixture[2]))
            elif loss == "logout":
                fixture[0].delete(fixture[2])
            else:
                with runtime.transaction() as tx:
                    runtime.repositories.assignments.retire_operations_for_owner(tx, owner_id=fixture[1])
        await asyncio.to_thread(retire)
        return result
    monkeypatch.setattr(service, "_definition", definition)
    with pytest.raises(AssignmentError):
        await service.submit(await context(fixture, runtime), command())
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_two_current_sessions_race_one_original_key_atomically(service, fixture, runtime, monkeypatch):
    second_sid = uuid4().hex
    fixture[0].create(second_sid, user_id=fixture[1], access_token=fixture[3](),
                      refresh_token="synthetic-second-refresh", hard_max_seconds=3600)
    try:
        first = await context(fixture, runtime, bearer=True)
        alternate = (fixture[0], fixture[1], second_sid, fixture[3], fixture[4])
        second = await context(alternate, runtime, bearer=True)
        barrier, arrived = asyncio.Event(), []
        original = service._definition
        async def definition(*args):
            value = await original(*args)
            arrived.append(True)
            if len(arrived) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 5)
            return value
        monkeypatch.setattr(service, "_definition", definition)
        body = command()
        values = await asyncio.gather(service.submit(first, body), service.submit(second, body))
        assert sum(value.created for value in values) == 1
        assert values[0].record.assignment_id == values[1].record.assignment_id
        assert totals(runtime, fixture[1]) == (1, 1, 1)
    finally:
        fixture[0].delete(second_sid)


@pytest.mark.asyncio
async def test_audit_lock_wait_past_principal_expiry_rolls_back_whole_acceptance(service, fixture, runtime, monkeypatch):
    locked, release, attempted = threading.Event(), threading.Event(), threading.Event()
    def block():
        with runtime.transaction() as tx:
            tx.fetch_one("SELECT pg_advisory_xact_lock(hashtext(%s))", ("audit_events:" + fixture[1],))
            locked.set()
            assert release.wait(5)
    blocker = asyncio.create_task(asyncio.to_thread(block))
    assert await asyncio.to_thread(locked.wait, 3)
    expiry = time.time() + 1.7
    selected = await context(fixture, runtime, bearer=True, changes={"exp": expiry})
    original = service.audit.insert_in_transaction
    def insert(*args, **kwargs):
        attempted.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(service.audit, "insert_in_transaction", insert)
    pending = asyncio.create_task(service.submit(selected, command()))
    try:
        assert await asyncio.to_thread(attempted.wait, 3)
        await asyncio.sleep(max(0, expiry - time.time()) + .04)
    finally:
        release.set()
        await blocker
    with pytest.raises(AssignmentError):
        await pending
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_cancelled_remote_refresh_keeps_claim_and_never_admits(service, fixture, runtime, monkeypatch):
    selected = await context(fixture, runtime, bearer=True)
    before = get_session_record(runtime, fixture[2])
    entered = asyncio.Event()
    async def exchange(*args):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    task = asyncio.create_task(service.submit(selected, command()))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    after = get_session_record(runtime, fixture[2])
    assert after.incarnation_id == before.incarnation_id
    assert after.refresh_token_ciphertext != before.refresh_token_ciphertext
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", ["disabled", "blocked", "permission", "phi", "foreign-runtime"])
async def test_existing_owner_and_source_policy_denials(service, fixture, runtime, posture, monkeypatch):
    selected = await context(fixture, runtime, bearer=True)
    if posture == "disabled":
        service.assignments.enabled = False
    elif posture == "blocked":
        service.assignments.orch.security_flags = {"web-research-1": {"fetch_page": {"blocked": True}}}
    elif posture == "permission":
        service.assignments.orch.tool_permissions.set_agent_scopes(fixture[1], "web-research-1", {"tools:read": False})
    elif posture == "phi":
        monkeypatch.setattr(service.assignments.phi_gate, "contains_phi", lambda _: True)
    else:
        service.store.plane_runtime = object()
    with pytest.raises(AssignmentError):
        await service.submit(selected, command())
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["receipt", "create", "audit", "awaitable-receipt", "awaitable-audit", "empty-audit", "empty-create"])
async def test_missing_or_malformed_repository_contract_cannot_report_acceptance(service, fixture, runtime, monkeypatch, boundary):
    if boundary in {"audit", "awaitable-audit", "empty-audit"}:
        target, name = service.audit, "insert_in_transaction"
    else:
        target = service.store.repository
        name = "get_operation_receipt" if "receipt" in boundary else "create_operation"
    if boundary.startswith("awaitable"):
        async def invalid(*args, **kwargs):
            raise AssertionError("async repository call must not execute")
        replacement = invalid
    elif boundary.startswith("empty"):
        def replacement(*args, **kwargs):
            return None
    else:
        replacement = None
    monkeypatch.setattr(target, name, replacement)
    with pytest.raises(AssignmentError, match="work_repository_unavailable"):
        await service.submit(await context(fixture, runtime), command())
    assert totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong-owner", "wrong-role", "malformed", "timeout", "replaced-during-refresh"])
async def test_new_forced_refresh_is_one_attempt_with_normal_final_iam(service, fixture, runtime, monkeypatch, change):
    selected = await context(fixture, runtime, bearer=True)
    calls = []
    async def exchange(*args):
        calls.append(True)
        if change == "timeout":
            raise TimeoutError("synthetic unknown refresh outcome")
        if change == "malformed":
            return {}
        if change == "replaced-during-refresh":
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        token = fixture[3](**({"sub": "other-owner"} if change == "wrong-owner" else
                             {"realm_access": {"roles": []}} if change == "wrong-role" else {}))
        return {"access_token": token, "refresh_token": "synthetic-new-refresh"}
    monkeypatch.setattr(web_auth, "_exchange_session_refresh", exchange)
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert calls == [True] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_tombstoned_key_never_creates_another_task(service, fixture, runtime):
    body = command()
    initial = await service.submit(await context(fixture, runtime), body)
    record = initial.record
    with runtime.transaction() as tx:
        repo = runtime.repositories.assignments
        stopped = repo.apply_control(tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
            expected_instruction_revision=record.instruction_revision, expected_control_epoch=record.control_epoch,
            expected_state_version=record.state_version, submission_id=str(uuid4()),
            submission_digest="a" * 64, control="stop").assignment
        repo.delete_for_owner(tx, owner_id=record.owner_id, assignment_id=record.assignment_id,
                              expected_control_epoch=stopped.control_epoch,
                              expected_state_version=stopped.state_version)
    with pytest.raises(AssignmentError, match="assignment_operation_deleted"):
        await service.submit(await context(fixture, runtime, bearer=True), body)
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (0, 1, 1)


@pytest.mark.asyncio
async def test_original_database_fifteen_second_window_cannot_be_renewed_by_policy(service, fixture, runtime, monkeypatch):
    original = service._definition
    window = []
    async def delayed(context, authority, body):
        result = await original(context, authority, body)
        observation = authority.observation
        window.append(observation.valid_until - observation.started_at)
        await asyncio.sleep(max(0, observation.valid_until.timestamp() - time.time()) + .04)
        return result
    monkeypatch.setattr(service, "_definition", delayed)
    with pytest.raises(AssignmentError):
        await service.submit(await context(fixture, runtime, bearer=True), command())
    assert window == [timedelta(seconds=15)]
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["jwt", "receipt"])
async def test_cookie_iam_original_incarnation_cannot_be_replaced_before_new_capture(
    service, fixture, runtime, monkeypatch, boundary,
):
    before = get_session_record(runtime, fixture[2])
    replacements = []
    async def replace_once():
        if not replacements:
            replacements.append(await asyncio.to_thread(replace_session_record, runtime, before))
    if boundary == "jwt":
        original = auth.verify_production_token
        async def verify(token):
            await replace_once()
            return await original(token)
        monkeypatch.setattr(auth, "verify_production_token", verify)
    else:
        original = service.store.transaction
        async def read(callback, **kwargs):
            result = await original(callback, **kwargs)
            await replace_once()
            return result
        monkeypatch.setattr(service.store, "transaction", read)
    selected = await context(fixture, runtime)
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert replacements[0].incarnation_id != before.incarnation_id
    assert get_session_record(runtime, fixture[2]) == replacements[0]
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["jwt", "receipt"])
async def test_accepted_cookie_retry_survives_original_session_retirement(
    service, fixture, runtime, monkeypatch, boundary,
):
    body = command()
    accepted = await service.submit(await context(fixture, runtime), body)
    retired = []
    async def retire_once():
        if not retired:
            await asyncio.to_thread(fixture[0].delete, fixture[2])
            retired.append(True)
    if boundary == "jwt":
        original = auth.verify_production_token
        async def verify(token):
            await retire_once()
            return await original(token)
        monkeypatch.setattr(auth, "verify_production_token", verify)
    else:
        original = service.store.transaction
        async def read(callback, **kwargs):
            result = await original(callback, **kwargs)
            await retire_once()
            return result
        monkeypatch.setattr(service.store, "transaction", read)
    replay = await service.submit(await context(fixture, runtime), body)
    assert not replay.created and replay.record == accepted.record
    assert retired == [True] and len(fixture[-1]) == 1
    assert totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_bearer_cookie_first_incarnation_selection_remains_after_receipt(
    service, fixture, runtime, monkeypatch,
):
    selected = await context(fixture, runtime, bearer=True)
    before = get_session_record(runtime, fixture[2])
    replacements = []
    original = service.store.transaction
    async def read(callback, **kwargs):
        result = await original(callback, **kwargs)
        if not replacements:
            replacements.append(await asyncio.to_thread(replace_session_record, runtime, before))
        return result
    monkeypatch.setattr(service.store, "transaction", read)
    accepted = await service.submit(selected, command())
    assert accepted.created
    assert accepted.record.operation["authority"]["reference_id"] == replacements[0].incarnation_id
    assert replacements[0].incarnation_id != before.incarnation_id
    assert len(fixture[-1]) == 1 and totals(runtime, fixture[1]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_cookie_without_durable_incarnation_cannot_borrow_current_row(
    service, fixture, runtime, monkeypatch,
):
    original = web_auth.ensure_session
    async def process_only(request):
        value = dict(await original(request))
        value.pop("incarnation_id")
        return value
    monkeypatch.setattr(web_auth, "ensure_session", process_only)
    selected = await context(fixture, runtime)
    with pytest.raises(AssignmentError, match="work_authority_unavailable"):
        await service.submit(selected, command())
    assert not fixture[-1] and totals(runtime, fixture[1]) == (0, 0, 0)
