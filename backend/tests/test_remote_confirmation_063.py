"""US3 destructive-operation confirmation gate — adversarial unit tests (SC-004).

Exercises ``orchestrator/remote_confirmation.py`` in isolation with a fake,
in-memory proposal store (the module touches exactly one table with simple,
stable queries) and the transport test seam. No postgres, no network — runs the
same in CI and in-container.

The security properties proved here (spec confirmation.md / FR-027..FR-033, FR-044):
- a destructive verb NEVER executes on first reach — it produces a proposal + refusal;
- an UNATTENDED turn is refused for EVERY mutating-registry verb — destructive or
  not — with NO proposal created and NO transport contact (T043/FR-033), while
  read verbs still pass this gate (FR-044's status-poll allowance);
- approval is single-use, owner-bound, TTL-bounded, and argument-fingerprint-bound;
- a re-labelled / re-argumented / replayed approval does not carry over;
- a non-destructive mutating verb proceeds on an ATTENDED turn (its explicit grant
  already gated it).

T036/T037 adversarial suite (bottom sections): the same properties proved through
the REAL gate stack (``_run_gate_stack`` via ``execute_single_tool`` /
``execute_parallel_tools`` / the chained-hop seam), per destructive verb, across
machine-turn classes, and across an orchestrator restart (SC-004/SC-005/SC-006) —
with zero destructive transport operations asserted via the FakeTransport call log.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import remote_confirmation as rc
from orchestrator.chain_authority import MACHINE_TURN_CLASSES
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport

USER = "user-1"
OTHER = "user-2"
def FUTURE():
    return int(time.time()) + 900


def PAST():
    return int(time.time()) - 10


# ── fake proposal store (matches remote_confirmation's exact queries) ──────────

class _FakeDB:
    def __init__(self):
        self.rows: dict = {}

    # sync (evaluate path)
    def fetch_one(self, q, params):
        qs = q.strip()
        if qs.startswith("SELECT"):
            r = self.rows.get(params[0])
            return dict(r) if r else None
        if qs.startswith("UPDATE") and "status='consumed'" in q:
            ts, pid = params
            r = self.rows.get(pid)
            if r and r["status"] == "approved":
                r["status"], r["consumed_at"] = "consumed", ts
                return {"proposal_id": pid}
            return None
        raise AssertionError("unexpected fetch_one: " + q)

    def execute(self, q, params):
        if q.strip().startswith("INSERT"):
            (pid, owner, chat, machine, agent, verb, args_json, fp, summary,
             created, expires) = params
            self.rows[pid] = {
                "proposal_id": pid, "owner_user_id": owner, "chat_id": chat,
                "machine_id": machine, "agent_id": agent, "verb": verb,
                "args_json": args_json, "args_fingerprint": fp, "summary": summary,
                "status": "pending", "created_at": created, "expires_at": expires,
                "consumed_at": None, "decided_at": None,
            }
            return
        raise AssertionError("unexpected execute: " + q)

    # async (handle_decision path)
    async def afetch_one(self, q, params):
        qs = q.strip()
        if qs.startswith("SELECT"):
            r = self.rows.get(params[0])
            return dict(r) if r else None
        if qs.startswith("UPDATE") and "status='approved'" in q and "RETURNING" in q:
            now, pid = params
            r = self.rows.get(pid)
            if r and r["status"] == "pending":
                r["status"], r["decided_at"] = "approved", now
                return {"proposal_id": pid}
            return None
        raise AssertionError("unexpected afetch_one: " + q)

    async def aexecute(self, q, params):
        if "status='expired'" in q:
            r = self.rows.get(params[0])
            if r and r["status"] == "pending":
                r["status"] = "expired"
            return
        if "status='declined'" in q:
            now, pid = params
            r = self.rows.get(pid)
            if r and r["status"] == "pending":
                r["status"], r["decided_at"] = "declined", now
            return
        raise AssertionError("unexpected aexecute: " + q)


class _Rec:
    def __init__(self):
        self.calls = []

    async def __call__(self, *a, **k):
        self.calls.append((a, k))


class _WS:
    """Hashable websocket stand-in (a real WS is hashable; SimpleNamespace is not
    because it defines __eq__ — production only ever reaches ui_sessions.get with a
    real socket or None, both hashable)."""

    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def _orch(db):
    return SimpleNamespace(
        history=SimpleNamespace(db=db),
        credential_manager=object(),
        ui_sessions={},
        send_ui_render=_Rec(),
        execute_single_tool=_Rec(),
    )


def _seed(db, pid, *, owner, verb, args, status="approved", expires_at=None,
          chat_id="chat-1", agent_id="remote-compute-1"):
    db.rows[pid] = {
        "proposal_id": pid, "owner_user_id": owner, "chat_id": chat_id,
        "machine_id": args.get("machine_id"), "agent_id": agent_id, "verb": verb,
        "args_json": json.dumps(args), "args_fingerprint": rc._fingerprint(args),
        "summary": "seeded", "status": status, "created_at": int(time.time()),
        "expires_at": expires_at or FUTURE(), "consumed_at": None, "decided_at": None,
    }


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)


# ── fingerprint / canonicalisation ─────────────────────────────────────────────

def test_canonical_args_excludes_underscore_keys_and_is_order_independent():
    a = {"machine_id": "m", "path": "/x", "_remote_op_proposal_id": "p"}
    b = {"path": "/x", "machine_id": "m"}
    assert rc._canonical_args(a) == rc._canonical_args(b)
    assert rc._fingerprint(a) == rc._fingerprint(b)


def test_fingerprint_changes_when_a_real_arg_changes():
    assert rc._fingerprint({"path": "/a"}) != rc._fingerprint({"path": "/b"})


# ── classification predicate ────────────────────────────────────────────────────

def test_classification_for_known_and_unknown():
    assert rc.classification_for("remove_path") == "always"
    assert rc.classification_for("not_a_verb") is None


def test_is_destructive_never_always_by_action():
    o = _orch(_FakeDB())
    assert rc._is_destructive(o, USER, "make_directory", {}, "never") is False
    assert rc._is_destructive(o, USER, "remove_path", {}, "always") is True
    by = {"by_action": ["stop", "disable", "restart"]}
    assert rc._is_destructive(o, USER, "control_service", {"action": "restart"}, by) is True
    assert rc._is_destructive(o, USER, "control_service", {"action": "start"}, by) is False


def test_is_destructive_if_exists_uses_a_readonly_stat(monkeypatch):
    tgt = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                        username="me", cred_type="password", secret="x")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: tgt)
    o = _orch(_FakeDB())
    set_transport(FakeTransport(files={"/there.txt": b"x"}))
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/there.txt"}, "if_exists") is True
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/absent.txt"}, "if_exists") is False


def test_is_destructive_if_exists_fails_closed_when_stat_errors(monkeypatch):
    tgt = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                        username="me", cred_type="password", secret="x")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: tgt)
    o = _orch(_FakeDB())
    set_transport(FakeTransport(force_verdict=Verdict.UNREACHABLE))
    # Cannot tell whether the file exists → treat as destructive (fail-closed).
    assert rc._is_destructive(o, USER, "upload_file", {"machine_id": "m1", "remote_path": "/x"}, "if_exists") is True


# ── no-live-human detection (FR-033) ────────────────────────────────────────────

def test_no_live_human_none_socket_is_unattended():
    assert rc._no_live_human(_orch(_FakeDB()), None) is True


def test_no_live_human_machine_class_is_unattended():
    o = _orch(_FakeDB())
    ws = _WS(machine_claims={"machine_class": "scheduler"})
    assert rc._no_live_human(o, ws) is True


def test_no_live_human_plain_socket_is_attended():
    assert rc._no_live_human(_orch(_FakeDB()), object()) is False


# ── evaluate: the gate hook ─────────────────────────────────────────────────────

def test_evaluate_read_verb_on_merged_agent_never_gates():
    # Merge safety: read verbs are not in the destructive map, so the gate fires
    # for NONE of them — the unified agent's reads run under the safe-seed baseline
    # untouched. Only the mutating agent's destructive verbs are gated.
    o = _orch(_FakeDB())
    for verb in ("list_queue", "job_status", "host_facts", "list_processes"):
        assert rc.evaluate(o, object(), "remote-compute-1", verb,
                           {"machine_id": "m"}, "chat", USER) is None


def test_evaluate_non_destructive_verb_proceeds():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, object(), "remote-compute-1", "make_directory",
                       {"machine_id": "m", "path": "/tmp/x"}, "chat", USER) is None


def test_evaluate_other_agent_is_ignored():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, object(), "general-1", "remove_path",
                       {"machine_id": "m", "path": "/x"}, "chat", USER) is None


def test_evaluate_destructive_first_reach_creates_proposal_and_refuses():
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                      {"machine_id": "m", "path": "/data", "recursive": True}, "chat", USER)
    assert out is not None
    msg, comps = out
    assert "confirmation_required" in msg
    assert comps and isinstance(comps, list)
    # exactly one pending proposal recorded, owned by the caller, not executed
    assert len(db.rows) == 1
    (row,) = db.rows.values()
    assert row["status"] == "pending" and row["owner_user_id"] == USER and row["verb"] == "remove_path"


def test_evaluate_unattended_refuses_and_creates_no_proposal():
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, None, "remote-compute-1", "remove_path",
                      {"machine_id": "m", "path": "/data"}, "chat", USER)
    assert out is not None
    msg, _ = out
    assert "unattended_refused" in msg
    assert db.rows == {}  # NOTHING persisted; the op cannot be approved later out-of-band


def test_evaluate_valid_marker_consumes_single_use_and_proceeds():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    passed = dict(args, **{rc._MARKER: "P1"})
    assert rc.evaluate(o, object(), "remote-compute-1", "cancel_job", passed, "chat", USER) is None
    assert rc._MARKER not in passed          # marker stripped, never reaches the agent
    assert db.rows["P1"]["status"] == "consumed"


def test_evaluate_consumed_marker_cannot_be_replayed():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    assert rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                       dict(args, **{rc._MARKER: "P1"}), "chat", USER) is None
    # second use of the same approval must be refused (single-use)
    out = rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and "no longer valid" in out[0]


def test_evaluate_marker_owned_by_another_user_is_refused():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=OTHER, verb="cancel_job", args=args, status="approved")
    out = rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and db.rows["P1"]["status"] == "approved"  # untouched


def test_evaluate_expired_marker_is_refused():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved", expires_at=PAST())
    out = rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None


def test_evaluate_marker_with_mutated_args_is_refused():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="remove_path",
          args={"machine_id": "m", "path": "/safe"}, status="approved")
    # Approval was for /safe; the model now asks to delete /etc with the same token.
    out = rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                      {"machine_id": "m", "path": "/etc", rc._MARKER: "P1"}, "chat", USER)
    assert out is not None and "no longer valid" in out[0]


# ── handle_decision: the approve/decline click ──────────────────────────────────

async def test_handle_decision_approve_redispatches_with_stored_args_and_marker():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="remove_path",
          args={"machine_id": "m", "path": "/data"}, status="pending")
    await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
    assert db.rows["P1"]["status"] == "approved"
    assert len(o.execute_single_tool.calls) == 1
    (args, kwargs) = o.execute_single_tool.calls[0]
    tc = args[1]
    assert tc.function.name == "remove_path"
    redis = json.loads(tc.function.arguments)
    assert redis["path"] == "/data" and redis[rc._MARKER] == "P1"


async def test_handle_decision_decline_does_not_dispatch():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="cancel_job",
          args={"machine_id": "m", "job_id": "9"}, status="pending")
    await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "decline"})
    assert db.rows["P1"]["status"] == "declined"
    assert o.execute_single_tool.calls == []


async def test_handle_decision_by_non_owner_is_refused():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="cancel_job",
          args={"machine_id": "m", "job_id": "9"}, status="pending")
    await rc.handle_decision(o, object(), OTHER, {"proposal_id": "P1", "decision": "approve"})
    assert db.rows["P1"]["status"] == "pending"  # untouched
    assert o.execute_single_tool.calls == []


async def test_handle_decision_expired_is_not_dispatched():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="cancel_job",
          args={"machine_id": "m", "job_id": "9"}, status="pending", expires_at=PAST())
    await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
    assert db.rows["P1"]["status"] == "expired"
    assert o.execute_single_tool.calls == []


async def test_handle_decision_double_approve_dispatches_once():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="remove_path",
          args={"machine_id": "m", "path": "/data"}, status="pending")
    await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
    await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
    assert len(o.execute_single_tool.calls) == 1  # second approve is a no-op (already handled)


# ── hash-chained audit lifecycle (FR-047/FR-048) ───────────────────────────────

class _FakeRecorder:
    """Captures the AuditEventCreate objects the gate emits. Construction of each
    validates event_class + outcome + required fields, so a bad shape would raise
    inside _audit_* (swallowed) and simply never reach here — the assertions below
    therefore also prove the events are schema-valid."""

    def __init__(self):
        self.events = []

    def record_blocking(self, ev):
        self.events.append(ev)
        return ev

    async def record(self, ev):
        self.events.append(ev)
        return ev


@pytest.fixture
def audit_rec(monkeypatch):
    rec = _FakeRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: rec)
    return rec


def _types(rec):
    return [e.action_type for e in rec.events]


def test_audit_proposed_event_is_valid_and_correlated(audit_rec):
    db = _FakeDB()
    o = _orch(db)
    rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                {"machine_id": "m9", "path": "/data"}, "chat", USER)
    assert _types(audit_rec) == ["remote_op.proposed"]
    ev = audit_rec.events[0]
    (pid,) = db.rows.keys()
    assert ev.outcome == "in_progress" and ev.correlation_id == pid
    assert ev.inputs_meta.get("machine_id") == "m9" and ev.inputs_meta.get("verb") == "remove_path"


def test_audit_unattended_refusal_is_recorded_as_failure(audit_rec):
    rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "remove_path",
                {"machine_id": "m", "path": "/data"}, "chat", USER)
    assert _types(audit_rec) == ["remote_op.refused_unattended"]
    assert audit_rec.events[0].outcome == "failure"


def test_audit_unattended_refusal_recorded_for_non_destructive_verb(audit_rec):
    # T043 widened the unattended refusal to non-destructive mutating verbs — the
    # audit trail must name those refusals identically.
    rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "submit_job",
                {"machine_id": "m", "script": "echo hi"}, "chat", USER)
    assert _types(audit_rec) == ["remote_op.refused_unattended"]
    assert audit_rec.events[0].outcome == "failure"
    assert audit_rec.events[0].inputs_meta.get("verb") == "submit_job"


def test_audit_consume_event_is_recorded(audit_rec):
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "5"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert _types(audit_rec) == ["remote_op.consumed"]
    assert audit_rec.events[0].outcome == "success" and audit_rec.events[0].correlation_id == "P1"


async def test_audit_approve_and_decline_events(audit_rec):
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "PA", owner=USER, verb="remove_path", args={"machine_id": "m", "path": "/a"}, status="pending")
    _seed(db, "PD", owner=USER, verb="cancel_job", args={"machine_id": "m", "job_id": "1"}, status="pending")
    await rc.handle_decision(o, object(), USER, {"proposal_id": "PA", "decision": "approve"})
    await rc.handle_decision(o, object(), USER, {"proposal_id": "PD", "decision": "decline"})
    by_type = {e.action_type: e for e in audit_rec.events}
    assert by_type["remote_op.approved"].outcome == "success"
    assert by_type["remote_op.declined"].outcome == "failure"
    assert by_type["remote_op.approved"].correlation_id == "PA"


# ═══════════════════════════════════════════════════════════════════════════════
# T036/T037 adversarial suite (SC-004/SC-005/SC-006)
#
# Zero destructive executions throughout: the FakeTransport call log must never
# contain a `run` or `put_file` op — the only transport call the GATE may make is
# the read-only `stat` that decides the `if_exists` classification, and only on an
# ATTENDED turn (an unattended turn is refused before ANY transport contact).
# ═══════════════════════════════════════════════════════════════════════════════

_TGT = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                     username="me", cred_type="password", secret="x")


def _fake(**kw):
    t = FakeTransport(**kw)
    set_transport(t)
    return t


def _destructive_ops(t):
    return [c for c in t.calls if c["op"] in ("run", "put_file")]


def _tc(name, args):
    return SimpleNamespace(id=f"tc-{name}", function=SimpleNamespace(
        name=name, arguments=json.dumps(args)))


#: One representative destructive invocation per gated verb. Completeness is
#: enforced against DESTRUCTIVE_CLASSIFICATION below, so a future verb addition
#: fails this suite until it gains a first-call case.
_FIRST_CALL_CASES = {
    "remove_path": {"machine_id": "m1", "path": "/data", "recursive": True},
    "cancel_job": {"machine_id": "m1", "job_id": "7"},
    "signal_process": {"machine_id": "m1", "pid": 4242, "signal": "TERM"},
    "control_service": {"machine_id": "m1", "service_name": "nginx", "action": "stop"},
    "manage_package": {"machine_id": "m1", "package_name": "vim", "action": "remove"},
    "upload_file": {"machine_id": "m1", "remote_path": "/exists.txt"},
}

#: One representative NON-destructive invocation per mutating shape: the "never"
#: verbs plus each by_action/if_exists verb's benign variant. Attended, all of
#: these proceed; on a machine turn every one is refused (T043/FR-033).
_NON_DESTRUCTIVE_CALL_CASES = {
    "make_directory": {"machine_id": "m1", "path": "/tmp/new"},
    "submit_job": {"machine_id": "m1", "script": "echo hi"},
    "run_job": {"machine_id": "m1", "script": "nvidia-smi"},
    "upload_file": {"machine_id": "m1", "remote_path": "/absent.txt"},
    "control_service": {"machine_id": "m1", "service_name": "nginx", "action": "start"},
    "manage_package": {"machine_id": "m1", "package_name": "vim", "action": "install"},
}

#: Both shapes per verb — the machine-turn sweep runs the union (12 cases).
_MUTATING_SWEEP = list(_FIRST_CALL_CASES.items()) + list(_NON_DESTRUCTIVE_CALL_CASES.items())


def test_first_call_matrix_covers_every_gated_verb():
    gated = {v for v, c in rc.DESTRUCTIVE_CLASSIFICATION.items() if c != "never"}
    assert set(_FIRST_CALL_CASES) == gated


def test_mutating_sweep_covers_every_registry_verb():
    # Completeness against the SINGLE SOURCE OF TRUTH: a verb added to the mutating
    # registry fails this suite until it gains a machine-turn case (T043).
    assert {v for v, _ in _MUTATING_SWEEP} == set(rc.DESTRUCTIVE_CLASSIFICATION)


# ── SC-005: each destructive verb's FIRST call proposes and has NO effect ──────

@pytest.mark.parametrize("verb", sorted(_FIRST_CALL_CASES))
def test_each_destructive_verb_first_call_proposes_no_effect(verb, monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})  # upload_file's if_exists probe finds it
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, object(), "remote-compute-1", verb,
                      dict(_FIRST_CALL_CASES[verb]), "chat", USER)
    assert out is not None and "confirmation_required" in out[0]
    (row,) = db.rows.values()
    assert row["status"] == "pending" and row["verb"] == verb
    assert _destructive_ops(t) == []
    assert o.execute_single_tool.calls == []


# ── verb binding: an approval for one verb cannot be consumed by another ───────

def test_approval_for_one_verb_cannot_be_consumed_by_another():
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "9"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    # IDENTICAL args (identical fingerprint) — only the verb name differs, so this
    # isolates the verb binding from the argument-fingerprint binding.
    out = rc.evaluate(o, object(), "remote-compute-1", "signal_process",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and "no longer valid" in out[0]
    assert db.rows["P1"]["status"] == "approved"  # NOT consumed by the wrong verb
    assert set(db.rows) == {"P1"}                 # and no bonus proposal appeared
    assert _destructive_ops(t) == []


# ── repeating the call does not auto-approve ───────────────────────────────────

def test_repeating_the_call_never_auto_approves():
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "path": "/data"}
    for _ in range(3):
        out = rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                          dict(args), "chat", USER)
        assert out is not None and "confirmation_required" in out[0]
    # each reach re-proposes; none ever advances past 'pending' without a human
    assert len(db.rows) == 3
    assert all(r["status"] == "pending" for r in db.rows.values())
    assert _destructive_ops(t) == []
    assert o.execute_single_tool.calls == []


# ── every stale-approval shape is refused with zero executions ─────────────────

@pytest.mark.parametrize("case", ["expired", "already_used", "other_user", "redirected_args"])
def test_invalid_approval_shapes_refused_store_intact(case):
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    approved_args = {"machine_id": "m", "path": "/safe"}
    call_args = dict(approved_args)
    owner, expires = USER, None
    if case == "expired":
        expires = PAST()
    elif case == "other_user":
        owner = OTHER
    elif case == "redirected_args":
        call_args = {"machine_id": "m", "path": "/etc"}  # approval was for /safe
    _seed(db, "P1", owner=owner, verb="remove_path", args=approved_args,
          status="approved", expires_at=expires)
    if case == "already_used":
        assert rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                           dict(approved_args, **{rc._MARKER: "P1"}), "chat", USER) is None
    out = rc.evaluate(o, object(), "remote-compute-1", "remove_path",
                      dict(call_args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None
    expected = "consumed" if case == "already_used" else "approved"
    assert db.rows["P1"]["status"] == expected  # refusal never mutates the row
    assert _destructive_ops(t) == []
    assert o.execute_single_tool.calls == []


# ── the REAL gate stack: parallel batch + chained hop still gate ───────────────

@pytest.fixture
def real_orch(monkeypatch):
    """A REAL Orchestrator whose gate stack runs the REAL confirmation gate over
    the fake proposal store, with the scope gate PASSING — so any refusal below is
    provably the confirmation gate's, not a missing grant's."""
    from orchestrator.orchestrator import Orchestrator
    o = Orchestrator()
    o.send_ui_render = AsyncMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.history = SimpleNamespace(db=_FakeDB())
    o._record_hop_audit = AsyncMock()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    return o


async def test_parallel_batch_with_destructive_verbs_still_gates(real_orch):
    t = _fake()
    results = await real_orch.execute_parallel_tools(
        _WS(), [_tc("remove_path", {"machine_id": "m", "path": "/data"}),
                _tc("cancel_job", {"machine_id": "m", "job_id": "7"})],
        {"remove_path": "remote-compute-1", "cancel_job": "remote-compute-1"},
        "chat-1", USER)
    assert len(results) == 2
    for r in results:
        assert r.error and "confirmation_required" in r.error["message"]
        assert any(c.get("type") == "card" for c in r.ui_components)  # proposal card
    db = real_orch.history.db
    assert len(db.rows) == 2
    assert all(row["status"] == "pending" for row in db.rows.values())
    assert _destructive_ops(t) == []


async def test_chained_hop_reaching_destructive_verb_still_gates(real_orch):
    t = _fake()
    # A mediated hop re-enters execute_single_tool with the initiator's parent
    # authority (056 US1) — the confirmation gate must fire identically there.
    parent = {"sub": USER, "scopes": ["tools:write"], "depth": 0,
              "act": {"sub": "agent:summarizer-1"}}
    resp = await real_orch.execute_single_tool(
        _WS(), _tc("remove_path", {"machine_id": "m", "path": "/data"}),
        {"remove_path": "remote-compute-1"}, "chat-1", user_id=USER,
        parent_token=parent, initiating_agent_id="summarizer-1")
    assert resp.error and "confirmation_required" in resp.error["message"]
    # the refused hop carries mint-failure audit evidence (056 SC-002 wrapper)
    real_orch._record_hop_audit.assert_awaited_once()
    kw = real_orch._record_hop_audit.await_args.kwargs
    assert kw["operation"] == "mint" and kw["outcome"] == "failure"
    (row,) = real_orch.history.db.rows.values()
    assert row["status"] == "pending" and row["verb"] == "remove_path"
    assert _destructive_ops(t) == []


# ── T043/FR-033: machine-initiated turns refused regardless of scope ───────────

@pytest.mark.parametrize("mclass", MACHINE_TURN_CLASSES)
def test_machine_turn_every_mutating_verb_refused_before_any_transport_contact(mclass, monkeypatch):
    # ANY mutating-registry verb — destructive shape OR benign variant, including
    # the if_exists upload whose classification probe is itself transport contact —
    # is refused on a machine turn with ZERO transport calls (FR-033). build_target
    # is patched to a live FakeTransport target precisely so a probe, had one run,
    # would be visible in the call log.
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": mclass})
    for verb, args in _MUTATING_SWEEP:
        out = rc.evaluate(o, ws, "remote-compute-1", verb, dict(args), "chat", USER)
        assert out is not None and "unattended_refused" in out[0], verb
    assert db.rows == {}   # nothing persisted a later actor could approve
    assert t.calls == []   # refused BEFORE any transport contact at all
    assert o.execute_single_tool.calls == []


def test_machine_turn_upload_refused_before_the_if_exists_probe(monkeypatch):
    # T043: the unattended check precedes the if_exists classification, so an
    # unattended upload — over an existing file OR to a new path — is refused with
    # no transport contact at all, not even the read-only stat (FR-033: a person
    # must be live for every remote-control verb).
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake(files={"/exists.txt": b"x"})
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": MACHINE_TURN_CLASSES[0]})
    for path in ("/exists.txt", "/absent.txt"):
        out = rc.evaluate(o, ws, "remote-compute-1", "upload_file",
                          {"machine_id": "m1", "remote_path": path}, "chat", USER)
        assert out is not None and "unattended_refused" in out[0]
    assert db.rows == {}
    assert t.calls == []                   # not even the read-only stat
    assert t.files["/exists.txt"] == b"x"  # untouched


def test_machine_turn_cannot_consume_an_approved_marker():
    # A human approval must be SPENT by a human turn: an unattended replay of a
    # valid marker is refused before marker consumption; the approval survives.
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "9"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    ws = _WS(machine_claims={"machine_class": MACHINE_TURN_CLASSES[0]})
    out = rc.evaluate(o, ws, "remote-compute-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and "unattended_refused" in out[0]
    assert db.rows["P1"]["status"] == "approved"  # NOT consumed unattended
    assert _destructive_ops(t) == []
    assert o.execute_single_tool.calls == []


def test_attended_non_destructive_mutating_verbs_still_proceed(monkeypatch):
    # T043 preserves the attended posture: every non-destructive mutating shape
    # (including the new-path upload, classified via a read-only stat) proceeds
    # with no proposal — the explicit grant already gated it.
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: _TGT)
    t = _fake()  # no files -> the upload probe finds nothing (non-destructive)
    db = _FakeDB()
    o = _orch(db)
    for verb, args in _NON_DESTRUCTIVE_CALL_CASES.items():
        assert rc.evaluate(o, object(), "remote-compute-1", verb,
                           dict(args), "chat", USER) is None, verb
    assert db.rows == {}                           # no proposal needed
    assert [c["op"] for c in t.calls] == ["stat"]  # the upload probe, nothing else
    assert _destructive_ops(t) == []


def test_machine_turn_read_verbs_still_pass_this_gate():
    # FR-044's status-poll allowance: read verbs are not in the mutating registry,
    # so the gate lets them through even on a machine turn — zero side effects.
    t = _fake()
    db = _FakeDB()
    o = _orch(db)
    ws = _WS(machine_claims={"machine_class": MACHINE_TURN_CLASSES[0]})
    for verb in ("list_queue", "job_status", "host_facts", "list_processes"):
        assert rc.evaluate(o, ws, "remote-compute-1", verb,
                           {"machine_id": "m"}, "chat", USER) is None
    assert db.rows == {} and t.calls == []


async def test_machine_turn_refused_even_with_full_scope_grant(real_orch):
    t = _fake()
    ws = _WS()
    real_orch.ui_sessions[ws] = {"machine_class": "scheduled_job"}
    resp = await real_orch.execute_single_tool(
        ws, _tc("remove_path", {"machine_id": "m", "path": "/data"}),
        {"remove_path": "remote-compute-1"}, "chat-1", user_id=USER)
    assert resp.error and "unattended_refused" in resp.error["message"]
    # the scope gate PASSED first — the refusal is unconditional on grants
    assert real_orch.tool_permissions.is_tool_allowed.called
    assert real_orch.history.db.rows == {}
    assert t.calls == []


async def test_machine_turn_submit_refused_even_with_full_scope_grant(real_orch):
    # FR-044: unattended SUBMISSION stays refused through the REAL gate stack even
    # when the scope gate passes — the refusal does not depend on a scheduler flag,
    # and a non-destructive classification ("never") does not exempt the verb.
    t = _fake()
    ws = _WS()
    real_orch.ui_sessions[ws] = {"machine_class": "scheduled_job"}
    resp = await real_orch.execute_single_tool(
        ws, _tc("submit_job", {"machine_id": "m", "script": "echo hi"}),
        {"submit_job": "remote-compute-1"}, "chat-1", user_id=USER)
    assert resp.error and "unattended_refused" in resp.error["message"]
    assert real_orch.tool_permissions.is_tool_allowed.called
    assert real_orch.history.db.rows == {}
    assert t.calls == []


# ── T037/SC-006: restart survival — pending survives, never auto-approves ──────

async def test_pending_proposal_survives_restart_and_never_auto_approves():
    t = _fake()
    db = _FakeDB()
    o1 = _orch(db)
    args = {"machine_id": "m", "path": "/data"}
    out = rc.evaluate(o1, object(), "remote-compute-1", "remove_path",
                      dict(args), "chat", USER)
    assert out is not None
    (pid,) = db.rows.keys()

    # "Restart": a fresh orchestrator over the SAME durable rows. The module keeps
    # no in-process approval state, so re-instantiation IS the restart surface.
    o2 = _orch(db)
    assert db.rows[pid]["status"] == "pending"  # survived; NOT auto-approved

    # the surviving pending proposal is still not an approval
    out = rc.evaluate(o2, object(), "remote-compute-1", "remove_path",
                      dict(args, **{rc._MARKER: pid}), "chat", USER)
    assert out is not None and "no longer valid" in out[0]
    assert db.rows[pid]["status"] == "pending"

    # re-issuing the verb post-restart re-proposes — it never silently proceeds
    out = rc.evaluate(o2, object(), "remote-compute-1", "remove_path",
                      dict(args), "chat", USER)
    assert out is not None and "confirmation_required" in out[0]
    assert len(db.rows) == 2

    # only an explicit human decision on the restarted instance dispatches
    await rc.handle_decision(o2, object(), USER, {"proposal_id": pid, "decision": "approve"})
    assert db.rows[pid]["status"] == "approved"
    assert len(o2.execute_single_tool.calls) == 1
    assert o1.execute_single_tool.calls == []
    assert _destructive_ops(t) == []


# ═══════════════════════════════════════════════════════════════════════════════
# Degraded-path behaviour: the gate's decisions must not depend on the audit sink,
# the machine-label lookup, or a resolvable machine — and the two "impossible"
# store shapes (unknown proposal id, lost single-use race) must still refuse.
# ═══════════════════════════════════════════════════════════════════════════════


class _BoomRecorder:
    """Audit sink that fails on every write (FR-047 is best-effort, never fatal)."""

    def record_blocking(self, ev):
        raise RuntimeError("audit sink down")

    async def record(self, ev):
        raise RuntimeError("audit sink down")


class TestAuditIsBestEffort:
    def test_sync_audit_swallows_a_recorder_failure(self, monkeypatch):
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: _BoomRecorder())
        rc._audit_sync(USER, "remote_op.proposed", "x", verb="remove_path",
                       machine_id="m1", proposal_id="P1")

    def test_sync_audit_is_a_noop_without_a_recorder(self, monkeypatch):
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
        rc._audit_sync(USER, "remote_op.proposed", "x")

    async def test_async_audit_swallows_a_recorder_failure(self, monkeypatch):
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: _BoomRecorder())
        await rc._audit_async(USER, "remote_op.approved", "x", proposal_id="P1")

    async def test_async_audit_is_a_noop_without_a_recorder(self, monkeypatch):
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
        await rc._audit_async(USER, "remote_op.approved", "x")

    def test_gate_still_refuses_when_the_audit_sink_is_down(self, monkeypatch):
        # The refusal is authoritative even with no usable audit sink — a broken
        # recorder must never turn a refusal into a pass.
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: _BoomRecorder())
        out = rc.evaluate(_orch(_FakeDB()), None, "remote-compute-1", "remove_path",
                          {"machine_id": "m", "path": "/data"}, "chat", USER)
        assert out is not None and "unattended_refused" in out[0]

    async def test_decision_still_dispatches_when_the_audit_sink_is_down(self, monkeypatch):
        monkeypatch.setattr("audit.recorder.get_recorder", lambda: _BoomRecorder())
        db = _FakeDB()
        o = _orch(db)
        _seed(db, "P1", owner=USER, verb="remove_path",
              args={"machine_id": "m", "path": "/data"}, status="pending")
        await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
        assert db.rows["P1"]["status"] == "approved"
        assert len(o.execute_single_tool.calls) == 1


class TestMachineLabelResolution:
    def test_missing_machine_id_renders_a_placeholder(self):
        assert rc._machine_label(_orch(_FakeDB()), USER, None) == "?"

    def test_friendly_label_is_used_when_the_machine_resolves(self, monkeypatch):
        monkeypatch.setattr("orchestrator.remote_machines.get_machine",
                            lambda db, uid, mid: {"machine_id": mid, "label": "dgx"})
        assert rc._machine_label(_orch(_FakeDB()), USER, "m1") == "dgx"

    def test_unlabelled_row_falls_back_to_the_raw_id(self, monkeypatch):
        monkeypatch.setattr("orchestrator.remote_machines.get_machine",
                            lambda db, uid, mid: {"machine_id": mid, "label": None})
        assert rc._machine_label(_orch(_FakeDB()), USER, "m1") == "m1"

    def test_lookup_failure_falls_back_to_the_raw_id(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr("orchestrator.remote_machines.get_machine", _boom)
        assert rc._machine_label(_orch(_FakeDB()), USER, "m1") == "m1"

    def test_summary_falls_back_to_the_verb_for_an_unmapped_tool(self, monkeypatch):
        monkeypatch.setattr("orchestrator.remote_machines.get_machine",
                            lambda db, uid, mid: {"label": "dgx"})
        assert rc._summary(_orch(_FakeDB()), USER, "make_directory",
                           {"machine_id": "m1"}) == "make_directory on dgx"

    def test_proposal_card_survives_an_unresolvable_machine(self, monkeypatch):
        # A proposal must still be offered (with the raw id in its summary) when the
        # inventory read fails — the human still sees exactly what would run.
        def _boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr("orchestrator.remote_machines.get_machine", _boom)
        db = _FakeDB()
        out = rc.evaluate(_orch(db), object(), "remote-compute-1", "remove_path",
                          {"machine_id": "m1", "path": "/data"}, "chat", USER)
        assert out is not None and "confirmation_required" in out[0]
        (row,) = db.rows.values()
        assert row["summary"] == "Delete /data on m1"


class TestDestructiveClassificationEdges:
    def test_if_exists_fails_closed_when_the_machine_cannot_be_resolved(self, monkeypatch):
        # build_target raising (credential gone / undecryptable) is indistinguishable
        # from "cannot tell whether the file exists" → destructive (fail-closed).
        def _boom(*a, **k):
            raise RuntimeError("credential undecryptable")
        monkeypatch.setattr("orchestrator.remote_machines.build_target", _boom)
        set_transport(FakeTransport())
        assert rc._is_destructive(_orch(_FakeDB()), USER, "upload_file",
                                  {"machine_id": "m1", "remote_path": "/x"}, "if_exists") is True

    def test_unknown_classification_fails_closed(self):
        # A future verb whose classification string nobody taught the gate must be
        # treated as destructive, never waved through.
        assert rc._is_destructive(_orch(_FakeDB()), USER, "weird_verb", {}, "sometimes") is True


class TestNoLiveHumanGuard:
    def test_virtual_websocket_is_unattended(self):
        from orchestrator.async_tasks import VirtualWebSocket
        assert rc._no_live_human(_orch(_FakeDB()), VirtualWebSocket(None)) is True

    def test_background_turn_is_refused_through_evaluate(self):
        from orchestrator.async_tasks import VirtualWebSocket
        db = _FakeDB()
        out = rc.evaluate(_orch(db), VirtualWebSocket(None), "remote-compute-1", "remove_path",
                          {"machine_id": "m", "path": "/data"}, "chat", USER)
        assert out is not None and "unattended_refused" in out[0]
        assert db.rows == {}

    def test_guard_survives_an_unusable_async_tasks_module(self, monkeypatch):
        # The isinstance probe is defensive: if async_tasks cannot be resolved the
        # guard must swallow it and fall through to the machine-claims check, not
        # explode inside the gate.
        import sys
        from types import ModuleType
        stub = ModuleType("orchestrator.async_tasks")
        stub.VirtualWebSocket = "not-a-class"  # isinstance() raises TypeError
        monkeypatch.setitem(sys.modules, "orchestrator.async_tasks", stub)
        assert rc._no_live_human(_orch(_FakeDB()), object()) is False


class TestProposalStoreEdges:
    def test_marker_for_an_unknown_proposal_is_refused(self):
        # A fabricated marker matches no row at all — refused, and it does not
        # conjure a proposal the model could then "approve".
        db = _FakeDB()
        o = _orch(db)
        out = rc.evaluate(o, object(), "remote-compute-1", "cancel_job",
                          {"machine_id": "m", "job_id": "9", rc._MARKER: "nope"}, "chat", USER)
        assert out is not None and "no longer valid" in out[0]
        assert db.rows == {}
        assert o.execute_single_tool.calls == []

    async def test_decision_without_a_proposal_id_is_refused(self):
        db = _FakeDB()
        o = _orch(db)
        await rc.handle_decision(o, object(), USER, {"decision": "approve"})
        assert o.execute_single_tool.calls == []
        assert len(o.send_ui_render.calls) == 1

    async def test_decision_for_an_unknown_proposal_is_refused(self):
        db = _FakeDB()
        o = _orch(db)
        await rc.handle_decision(o, object(), USER, {"proposal_id": "ghost", "decision": "approve"})
        assert o.execute_single_tool.calls == []
        assert len(o.send_ui_render.calls) == 1

    async def test_approve_losing_the_single_use_race_never_dispatches(self):
        # The SELECT still reads 'pending' but a concurrent tab wins the guarded
        # UPDATE, so the atomic approve returns no row — this click must report
        # "already handled" and must NOT re-enter the tool.
        class _RacyDB(_FakeDB):
            async def afetch_one(self, q, params):
                if q.strip().startswith("UPDATE"):
                    return None  # the other tab already flipped it
                return await super().afetch_one(q, params)

        db = _RacyDB()
        o = _orch(db)
        _seed(db, "P1", owner=USER, verb="remove_path",
              args={"machine_id": "m", "path": "/data"}, status="pending")
        await rc.handle_decision(o, object(), USER, {"proposal_id": "P1", "decision": "approve"})
        assert o.execute_single_tool.calls == []
        assert len(o.send_ui_render.calls) == 1
