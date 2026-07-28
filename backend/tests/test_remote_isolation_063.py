"""Feature 063 US1 — cross-user machine isolation (T021, SC-012/FR-010/FR-018).

User B can neither list, name, address, probe, nor act on user A's registered
machine through ANY verb path. The fake DB here is a thin sqlite3 shim that runs
``remote_machines``'s REAL owner-scoped SQL (its ``?`` placeholders are native
sqlite), so the ``owner_user_id`` clause in every query is exercised as written
rather than re-implemented by the fake. Transport is the FakeTransport seam —
the sweep asserts a foreign-machine attempt performs ZERO transport operations.
"""
from __future__ import annotations

import sqlite3

import pytest

from agents.remote_compute import mcp_tools as unified
from agents.remote_observe import mcp_tools as obs
from orchestrator import remote_machines
from orchestrator.remote_transport import FakeTransport, set_transport

USER_A = "user-a"
USER_B = "user-b"


class _SqliteDB:
    """Database stand-in executing the module's real SQL against in-memory
    sqlite, so a query that dropped its owner scoping would visibly leak."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE remote_machine (
                machine_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
                label TEXT, address TEXT, port INTEGER, username TEXT,
                os_family TEXT, role TEXT,
                last_verdict TEXT, last_checked_at INTEGER,
                host_key_type TEXT, host_key_fingerprint TEXT, host_key_blob TEXT,
                created_at INTEGER, updated_at INTEGER)""")

    def execute(self, q, params=()):
        self.conn.execute(q, params)
        self.conn.commit()

    def fetch_one(self, q, params=()):
        row = self.conn.execute(q, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, q, params=()):
        return [dict(r) for r in self.conn.execute(q, params).fetchall()]


class _CredMgr:
    def get_machine_credential(self, machine_id):
        return {"cred_type": "password", "secret": "x", "passphrase": None}


@pytest.fixture
def db():
    return _SqliteDB()


@pytest.fixture
def machines(db):
    """A owns 'dgx' + 'edge'; B owns their OWN machine also labelled 'dgx'
    (label collision must still resolve within the caller's inventory)."""
    a_dgx = remote_machines.create_machine(db, USER_A, "dgx", "10.0.0.5", 22, "alice", "linux", "cluster")
    a_edge = remote_machines.create_machine(db, USER_A, "edge", "10.0.0.6", 22, "alice", "linux", "host")
    b_dgx = remote_machines.create_machine(db, USER_B, "dgx", "10.0.9.9", 22, "bob", "linux", "cluster")
    return {"a_dgx": a_dgx, "a_edge": a_edge, "b_dgx": b_dgx}


@pytest.fixture
def transport(db):
    unified.register_deps(db, _CredMgr())
    t = FakeTransport(command_stdout='{"jobs":[]}', command_exit=0)
    set_transport(t)
    yield t
    set_transport(None)


def _verdict(res):
    return (res.get("_data") or {}).get("verdict")


# ── inventory-layer scoping (the SQL itself) ──────────────────────────────────

def test_list_machines_is_owner_scoped(db, machines):
    assert {r["machine_id"] for r in remote_machines.list_machines(db, USER_A)} \
        == {machines["a_dgx"], machines["a_edge"]}
    assert {r["machine_id"] for r in remote_machines.list_machines(db, USER_B)} \
        == {machines["b_dgx"]}


def test_resolve_machine_never_crosses_owners(db, machines):
    # B cannot resolve A's machine by id, label, or address …
    assert remote_machines.resolve_machine(db, USER_B, machines["a_edge"]) is None
    assert remote_machines.resolve_machine(db, USER_B, "edge") is None
    assert remote_machines.resolve_machine(db, USER_B, "10.0.0.5") is None
    # … a colliding label resolves to B's OWN row …
    assert remote_machines.resolve_machine(db, USER_B, "dgx")["machine_id"] == machines["b_dgx"]
    # … and A still resolves their own by all three forms.
    for ref in (machines["a_dgx"], "dgx", "10.0.0.5"):
        assert remote_machines.resolve_machine(db, USER_A, ref)["machine_id"] == machines["a_dgx"]


def test_build_target_refuses_a_foreign_machine(db, machines):
    with pytest.raises(remote_machines.MachineNotFound):
        remote_machines.build_target(db, _CredMgr(), USER_B, machines["a_dgx"])


def test_delete_and_probe_record_by_non_owner_are_noops(db, machines):
    assert remote_machines.delete_machine(db, USER_B, machines["a_dgx"]) is False
    remote_machines.record_probe(db, USER_B, machines["a_dgx"], "ok")
    row = remote_machines.get_machine(db, USER_A, machines["a_dgx"])
    assert row is not None and row["last_verdict"] is None  # untouched


# ── verb-layer sweep: every verb, every addressing form, zero connects ────────

# Valid-shaped extra args per verb so the ONLY failure is machine resolution.
_EXTRA_ARGS = {
    "probe_machine": {},
    "list_queue": {},
    "host_facts": {},
    "list_processes": {},
    "job_status": {"job_id": "123"},
    "job_history": {"days": 7},
    "list_directory": {"path": "/home/alice"},
    "read_job_output": {"output_path": "/home/alice/.astral_jobs/x.out"},
    "make_directory": {"path": "/tmp/x"},
    "remove_path": {"path": "/tmp/x", "recursive": True},
    "cancel_job": {"job_id": "123"},
    "control_service": {"service_name": "nginx", "action": "stop"},
    "manage_package": {"package_name": "vim", "action": "remove"},
    "signal_process": {"pid": "42", "signal": "KILL"},
    "submit_job": {"script_path": "/home/alice/job.sbatch"},
    "run_job": {"script": "echo hi"},
    "upload_file": {"attachment_id": "att-1", "remote_path": "/tmp/f"},
}


def test_every_verb_refuses_a_foreign_machine_and_never_connects(db, machines, transport):
    """SC-012: B addresses A's machine by id, label, and address through every
    registered verb — the not_found verdict every time, zero transport ops."""
    verbs = {v: e for v, e in unified.TOOL_REGISTRY.items() if v != "list_machines"}
    assert set(verbs) == set(_EXTRA_ARGS)  # a new verb must join this sweep
    attempts = 0
    for verb, entry in verbs.items():
        for ref in (machines["a_dgx"], "edge", "10.0.0.5"):
            res = entry["function"](user_id=USER_B, machine_id=ref, **_EXTRA_ARGS[verb])
            assert _verdict(res) == "not_found", f"{verb} addressed at {ref!r}"
            attempts += 1
    assert attempts >= 10  # SC-012's "at least 10 attempts"
    assert transport.calls == []  # never probed, ran, stat'ed, or uploaded


def test_list_machines_verb_shows_only_the_callers_rows(db, machines, transport):
    res = obs.list_machines(user_id=USER_B)
    listed = {m["machine_id"] for m in res["_data"]["machines"]}
    assert listed == {machines["b_dgx"]}
    assert machines["a_dgx"] not in listed and machines["a_edge"] not in listed


def test_owner_path_still_works(db, machines, transport):
    """Control case: the same wiring lets the OWNER reach their own machine —
    proving the sweep's refusals come from owner scoping, not a dead fixture."""
    res = obs.probe_machine(user_id=USER_A, machine_id="edge")
    assert res["_data"]["verdict"] == "ok" and res["_data"]["authenticated"] is True
    assert [c["op"] for c in transport.calls] == ["probe"]
    # First contact recorded the probe verdict on A's row, via A's own scope.
    row = remote_machines.get_machine(db, USER_A, machines["a_edge"])
    assert row["last_verdict"] == "ok"
