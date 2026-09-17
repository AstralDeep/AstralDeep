"""Real-Postgres framework-caller compatibility for Work (feature 088 T049).

Exercises ``orchestrator.work_operations.FrameworkWorkOperations`` end to end
against a real Plane schema: issue a credential (via
``orchestrator.framework_credentials.FrameworkCredentialService``), resolve
its bearer, then submit/read/cancel through the SAME repository the
interactive REST router uses. No HTTP layer, MCP transport or A2A executor is
exercised here (those are thin routers over this same facade); this proves
the facade's own admission, idempotency, scope, and human-only-refusal
behavior against real Postgres.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from audit.repository import AuditRepository
from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.session_store import WebSessionStore
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.models import AssignmentError
from persistent_agents.service import AssignmentService
from persistent_agents.store import AssignmentStore
from persistent_agents.tests.test_engine_postgres import plane as plane
from personalization.phi_gate import PHIGate
from tests.helpers.session_consent_088 import consent_from_store

runtime = plane

_ALL_SCOPES = ("operations.submit", "operations.read", "operations.control", "artifacts.read")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-work-framework-audit-key")


@pytest.fixture
def session(runtime):
    owner = str(uuid.uuid4())
    sid = uuid.uuid4().hex
    store = WebSessionStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store.create(sid, user_id=owner, access_token="synthetic-access",
                 refresh_token="synthetic-refresh", hard_max_seconds=3600)
    yield store, owner, sid


@pytest.fixture
def credentials(runtime):
    return FrameworkCredentialService(plane_runtime=runtime, audit=AuditRepository(plane_runtime=runtime))


@pytest.fixture
def assignments(runtime):
    orch = SimpleNamespace(history=SimpleNamespace(get_chat=lambda *args, **kwargs: None))
    return AssignmentService(orch, AssignmentStore(plane_runtime=runtime), enabled=True,
                             phi_gate=PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])))


@pytest.fixture
def ops(assignments, credentials):
    return FrameworkWorkOperations(assignments=assignments, credentials=credentials)


def _issue(credentials, store, owner, sid, **overrides):
    fields = {
        "owner_id": owner, "caller": consent_from_store(store, owner, sid).observation,
        "name": "SDK key", "scopes": _ALL_SCOPES, "expires_in_seconds": 3600, "max_admissions": 1000,
    }
    fields.update(overrides)
    return credentials.issue(**fields)


def _caller(credentials, token):
    caller = credentials.resolve_bearer(token)
    assert caller is not None
    return caller


# --- submit / get -------------------------------------------------------------

@pytest.mark.asyncio
async def test_framework_submit_creates_a_readable_one_shot_operation(ops, credentials, session, runtime):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    result = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="Draft a note",
                              instructions="Write one short paragraph.")
    assert result["created"] is True and result["title"] == "Draft a note"
    fetched = await ops.get(caller, result["id"])
    assert fetched["id"] == result["id"]
    with runtime.transaction() as tx:
        n = tx.fetch_one(
            "SELECT count(*) AS n FROM persistent_assignment WHERE owner_user_id=%s "
            "AND execution_profile='one_shot'", (owner,))["n"]
        receipts = tx.fetch_one(
            "SELECT count(*) AS n FROM assignment_operation_receipt WHERE owner_id=%s "
            "AND credential_id=%s", (owner, caller.credential_id))["n"]
    assert n == 1 and receipts == 1


@pytest.mark.asyncio
async def test_submit_requires_the_submit_scope(ops, credentials, session):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid, scopes=("operations.read",))
    caller = _caller(credentials, token)
    with pytest.raises(AssignmentError, match="framework_scope_required"):
        await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")


@pytest.mark.asyncio
async def test_read_commands_require_the_read_scope(ops, credentials, session):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid, scopes=("operations.submit",))
    caller = _caller(credentials, token)
    with pytest.raises(AssignmentError, match="framework_scope_required"):
        await ops.list(caller)
    with pytest.raises(AssignmentError, match="framework_scope_required"):
        await ops.get(caller, str(uuid.uuid4()))


# --- idempotent replay / conflict ---------------------------------------------

@pytest.mark.asyncio
async def test_same_key_replay_returns_the_original_with_no_new_row(ops, credentials, session, runtime):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    key = str(uuid.uuid4())
    first = await ops.submit(caller, idempotency_key=key, name="A", instructions="B")
    second = await ops.submit(caller, idempotency_key=key, name="A", instructions="B")
    assert first["id"] == second["id"]
    assert first["created"] is True and second["created"] is False
    with runtime.transaction() as tx:
        n = tx.fetch_one("SELECT count(*) AS n FROM persistent_assignment WHERE owner_user_id=%s",
                         (owner,))["n"]
    assert n == 1


@pytest.mark.asyncio
async def test_a_different_credential_on_the_same_key_conflicts(ops, credentials, session):
    store, owner, sid = session
    _v1, token1 = _issue(credentials, store, owner, sid)
    _v2, token2 = _issue(credentials, store, owner, sid)
    caller1, caller2 = _caller(credentials, token1), _caller(credentials, token2)
    key = str(uuid.uuid4())
    await ops.submit(caller1, idempotency_key=key, name="A", instructions="B")
    with pytest.raises(AssignmentError, match="assignment_idempotency_conflict"):
        await ops.submit(caller2, idempotency_key=key, name="A", instructions="B")


# --- allowance is consumed exactly once per genuine create --------------------

@pytest.mark.asyncio
async def test_admission_is_consumed_once_per_create_never_on_replay(ops, credentials, session):
    store, owner, sid = session
    view, token = _issue(credentials, store, owner, sid, max_admissions=2)
    caller = _caller(credentials, token)
    key = str(uuid.uuid4())
    await ops.submit(caller, idempotency_key=key, name="A", instructions="B")
    await ops.submit(caller, idempotency_key=key, name="A", instructions="B")  # replay
    rows = credentials.list(owner_id=owner)
    row = next(r for r in rows if r["credential_id"] == view["credential_id"])
    assert row["consumed_admissions"] == 1
    await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="C", instructions="D")
    rows = credentials.list(owner_id=owner)
    row = next(r for r in rows if r["credential_id"] == view["credential_id"])
    assert row["consumed_admissions"] == 2
    with pytest.raises(AssignmentError, match="credential_allowance_exhausted"):
        await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="E", instructions="F")


# --- revocation ends further use ----------------------------------------------

@pytest.mark.asyncio
async def test_revoked_credential_refuses_a_new_submission(ops, credentials, session):
    store, owner, sid = session
    view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    credentials.revoke(owner_id=owner, credential_id=view["credential_id"])
    with pytest.raises(AssignmentError, match="framework_credential_authority_unavailable"):
        await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")


@pytest.mark.asyncio
async def test_revoked_credential_ends_further_polling(ops, credentials, session):
    store, owner, sid = session
    view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
    credentials.revoke(owner_id=owner, credential_id=view["credential_id"])
    # Reads go through the SAME resolve_bearer path a real MCP/A2A caller
    # would take on its NEXT request; a revoked credential resolves to nothing.
    assert credentials.resolve_bearer(token) is None


# --- cancel / pause: applied once, idempotent, scope-gated --------------------

@pytest.mark.asyncio
async def test_cancel_requires_the_control_scope(ops, credentials, session):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid, scopes=("operations.submit", "operations.read"))
    caller = _caller(credentials, token)
    result = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
    with pytest.raises(AssignmentError, match="framework_scope_required"):
        await ops.cancel(caller, result["id"], submission_id=str(uuid.uuid4()),
                         expected_revision=result["revision"])


@pytest.mark.asyncio
async def test_cancel_applies_once_and_replay_is_a_safe_no_op(ops, credentials, session):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    result = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
    submission_id = str(uuid.uuid4())
    first = await ops.cancel(caller, result["id"], submission_id=submission_id,
                             expected_revision=result["revision"])
    assert first["applied"] is True
    assert first["operation"]["disposition"] == "cancelled"
    second = await ops.cancel(caller, result["id"], submission_id=submission_id,
                              expected_revision=result["revision"])
    assert second["applied"] is False
    assert second["operation"]["disposition"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_on_a_foreign_operation_is_not_found(ops, credentials, session):
    store, owner, sid = session
    _v1, token1 = _issue(credentials, store, owner, sid)
    caller1 = _caller(credentials, token1)
    result = await ops.submit(caller1, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")

    other_owner = str(uuid.uuid4())
    other_sid = uuid.uuid4().hex
    store.create(other_sid, user_id=other_owner, access_token="a", refresh_token="b",
                hard_max_seconds=3600)
    _v2, token2 = _issue(credentials, store, other_owner, other_sid)
    caller2 = _caller(credentials, token2)
    with pytest.raises(AssignmentError, match="work_not_found"):
        await ops.cancel(caller2, result["id"], submission_id=str(uuid.uuid4()), expected_revision=1)


# --- human-only commands refuse before any read or mutation -------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["decide", "reconcile", "delete"])
async def test_human_only_commands_refuse_before_any_mutation(ops, credentials, session, runtime, command):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    result = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
    with pytest.raises(AssignmentError, match="assignment_human_required"):
        await getattr(ops, command)(caller, result["id"])
    unchanged = await ops.get(caller, result["id"])
    assert unchanged["revision"] == result["revision"]
    assert unchanged["control_epoch"] == result["control_epoch"]
    assert unchanged["disposition"] == result["disposition"]


# --- not-yet-wired controls refuse honestly, never silently no-op ------------

@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["resume", "wait", "wake"])
async def test_resume_wait_wake_are_not_yet_wired_for_framework_callers(ops, credentials, session, command):
    store, owner, sid = session
    _view, token = _issue(credentials, store, owner, sid)
    caller = _caller(credentials, token)
    result = await ops.submit(caller, idempotency_key=str(uuid.uuid4()), name="A", instructions="B")
    with pytest.raises(AssignmentError, match="framework_control_unavailable"):
        await getattr(ops, command)(caller, result["id"])


# --- these are never advertised as MCP tools ----------------------------------

def test_not_yet_wired_names_are_absent_from_the_dispatchable_and_projected_sets():
    from orchestrator.mcp_projection import _WORK_TOOL_SPECS, project_tools
    from orchestrator.work_operations import DISPATCHABLE_TOOL_NAMES

    for absent in ("astral_resume_operation", "astral_wait_operation", "astral_wake_operation",
                  "astral_list_artifacts", "astral_list_agents"):
        assert absent not in DISPATCHABLE_TOOL_NAMES
        assert absent not in _WORK_TOOL_SPECS
    names = {t.name for t in project_tools(None, "u1", {"sub": "u1", "_framework_scopes": _ALL_SCOPES})}
    assert names == set(DISPATCHABLE_TOOL_NAMES)
