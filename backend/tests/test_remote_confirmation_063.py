"""US3 destructive-operation confirmation gate — adversarial unit tests (SC-004).

Exercises ``orchestrator/remote_confirmation.py`` in isolation with a fake,
in-memory proposal store (the module touches exactly one table with simple,
stable queries) and the transport test seam. No postgres, no network — runs the
same in CI and in-container.

The security properties proved here (spec confirmation.md / FR-027..FR-033):
- a destructive verb NEVER executes on first reach — it produces a proposal + refusal;
- an UNATTENDED turn (no live human) is refused with NO proposal created;
- approval is single-use, owner-bound, TTL-bounded, and argument-fingerprint-bound;
- a re-labelled / re-argumented / replayed approval does not carry over;
- a non-destructive mutating verb proceeds (its explicit grant already gated it).
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from orchestrator import remote_confirmation as rc
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport

USER = "user-1"
OTHER = "user-2"
FUTURE = lambda: int(time.time()) + 900
PAST = lambda: int(time.time()) - 10


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
          chat_id="chat-1", agent_id="remote-control-1"):
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

def test_evaluate_non_destructive_verb_proceeds():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, object(), "remote-control-1", "make_directory",
                       {"machine_id": "m", "path": "/tmp/x"}, "chat", USER) is None


def test_evaluate_other_agent_is_ignored():
    o = _orch(_FakeDB())
    assert rc.evaluate(o, object(), "general-1", "remove_path",
                       {"machine_id": "m", "path": "/x"}, "chat", USER) is None


def test_evaluate_destructive_first_reach_creates_proposal_and_refuses():
    db = _FakeDB()
    o = _orch(db)
    out = rc.evaluate(o, object(), "remote-control-1", "remove_path",
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
    out = rc.evaluate(o, None, "remote-control-1", "remove_path",
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
    assert rc.evaluate(o, object(), "remote-control-1", "cancel_job", passed, "chat", USER) is None
    assert rc._MARKER not in passed          # marker stripped, never reaches the agent
    assert db.rows["P1"]["status"] == "consumed"


def test_evaluate_consumed_marker_cannot_be_replayed():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved")
    assert rc.evaluate(o, object(), "remote-control-1", "cancel_job",
                       dict(args, **{rc._MARKER: "P1"}), "chat", USER) is None
    # second use of the same approval must be refused (single-use)
    out = rc.evaluate(o, object(), "remote-control-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and "no longer valid" in out[0]


def test_evaluate_marker_owned_by_another_user_is_refused():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=OTHER, verb="cancel_job", args=args, status="approved")
    out = rc.evaluate(o, object(), "remote-control-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None and db.rows["P1"]["status"] == "approved"  # untouched


def test_evaluate_expired_marker_is_refused():
    db = _FakeDB()
    o = _orch(db)
    args = {"machine_id": "m", "job_id": "123"}
    _seed(db, "P1", owner=USER, verb="cancel_job", args=args, status="approved", expires_at=PAST())
    out = rc.evaluate(o, object(), "remote-control-1", "cancel_job",
                      dict(args, **{rc._MARKER: "P1"}), "chat", USER)
    assert out is not None


def test_evaluate_marker_with_mutated_args_is_refused():
    db = _FakeDB()
    o = _orch(db)
    _seed(db, "P1", owner=USER, verb="remove_path",
          args={"machine_id": "m", "path": "/safe"}, status="approved")
    # Approval was for /safe; the model now asks to delete /etc with the same token.
    out = rc.evaluate(o, object(), "remote-control-1", "remove_path",
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
