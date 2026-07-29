"""US1 register→probe reachability integration (feature 063, SC-001).

Drives the REAL register path (`chrome_machine_add` → create + credential store +
immediate probe) and the REAL chat-tier `probe_machine` verb over the real
``remote_machines`` SQL against an in-memory sqlite double of the Database facade;
only the transport is faked via the FR-050 seam. Asserts the exact enumerated
verdicts from contracts/result-vocabulary.md reach the caller for the three US1
outcomes: reachable, wrong credential, unroutable.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from agents.remote_observe import mcp_tools as obs
from orchestrator import remote_machines
from orchestrator.remote_transport import FakeTransport, Verdict, set_transport
from webrender.chrome.surfaces import remote_machines as surface

USER = "user-1"
ADDR = "10.33.77.11"  # RFC1918 literal: gate-permitted, resolves to itself (no DNS)

# Mirrors shared/database.py's remote_machine DDL (sqlite accepts the same shape).
_SCHEMA = """
CREATE TABLE remote_machine (
    machine_id            TEXT PRIMARY KEY,
    owner_user_id         TEXT NOT NULL,
    label                 TEXT NOT NULL,
    address               TEXT NOT NULL,
    port                  INTEGER NOT NULL DEFAULT 22,
    username              TEXT NOT NULL,
    os_family             TEXT NOT NULL,
    role                  TEXT NOT NULL,
    host_key_type         TEXT,
    host_key_fingerprint  TEXT,
    host_key_blob         TEXT,
    last_verdict          TEXT,
    last_checked_at       BIGINT,
    created_at            BIGINT NOT NULL,
    updated_at            BIGINT NOT NULL
)
"""


class MemDB:
    """sqlite double for the Database facade — same '?' placeholder dialect, so the
    real owner-scoped queries in orchestrator/remote_machines.py run unmodified."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)

    def execute(self, sql, params=()):
        self._conn.execute(sql, params)
        self._conn.commit()

    def fetch_one(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


class MemCredMgr:
    """In-memory stand-in for the machine-credential slice of CredentialManager."""

    def __init__(self):
        self.creds = {}

    def set_machine_credential(self, machine_id, owner_user_id, cred_type, secret,
                               passphrase=None):
        self.creds[machine_id] = {"cred_type": cred_type, "secret": secret,
                                  "passphrase": passphrase}

    def get_machine_credential(self, machine_id):
        return self.creds.get(machine_id)

    def delete_machine_credential(self, machine_id):
        self.creds.pop(machine_id, None)


@pytest.fixture()
def db():
    return MemDB()


@pytest.fixture()
def credmgr():
    return MemCredMgr()


@pytest.fixture()
def orch(db, credmgr):
    return SimpleNamespace(history=SimpleNamespace(db=db), credential_manager=credmgr)


@pytest.fixture(autouse=True)
def _wire(db, credmgr, monkeypatch):
    # The surfaces registry re-checks FF_REMOTE_COMPUTE per entry point (T064);
    # force it on so the test is hermetic w.r.t. the environment's flag state.
    monkeypatch.setattr(surface, "_enabled", lambda: True)
    obs.register_deps(db, credmgr)
    yield
    set_transport(None)


def _add_payload(**over):
    fields = {"label": "dgx", "address": ADDR, "port": "22", "username": "me",
              "os_family": "linux", "role": "cluster",
              "cred_type": "password", "password": "pw"}
    fields.update(over)
    return {"fields": fields}


def _machine_row(db):
    return db.fetch_one("SELECT * FROM remote_machine WHERE owner_user_id = ?", (USER,))


# ── register → probe via the surface (the SC-001 on-screen form path) ─────────

async def test_register_reachable_reports_ok_and_pins_host_key(orch, db):
    set_transport(FakeTransport())
    key, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    assert key == surface.SURFACE_KEY
    assert "reachable and authenticated" in notice
    assert "SHA256:fake" in notice  # the captured first-contact host key is shown
    row = _machine_row(db)
    assert row["last_verdict"] == Verdict.OK.value
    assert row["host_key_fingerprint"] == "SHA256:fake"  # pinned on first contact


async def test_register_wrong_credential_reports_auth_failed(orch, db):
    set_transport(FakeTransport(authenticated=False))
    _, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    # The exact enumerated verdict AND its vocabulary next-action reach the user.
    assert Verdict.AUTH_FAILED.value in notice
    assert "re-check the username and credential" in notice
    row = _machine_row(db)
    assert row["last_verdict"] == Verdict.AUTH_FAILED.value
    assert row["host_key_fingerprint"] is None  # no pin without an authenticated probe


async def test_register_unroutable_reports_unreachable(orch, db):
    set_transport(FakeTransport(reachable=False))
    _, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    assert Verdict.UNREACHABLE.value in notice
    assert "check the machine is on" in notice
    assert _machine_row(db)["last_verdict"] == Verdict.UNREACHABLE.value


async def test_register_ssh_key_credential_round_trips_pem(orch, db, credmgr):
    # SC-001's form pastes a multi-line PEM; the handler must store it intact
    # (plus the trailing newline PEM parsers require) and still probe.
    pem = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
           "b3BlbnNzaC1rZXktdjEAAAAA\n"
           "-----END OPENSSH PRIVATE KEY-----")
    set_transport(FakeTransport())
    _, _, notice = await surface._h_machine_add(
        orch, None, USER, ["user"],
        _add_payload(cred_type="ssh_key", private_key=pem, password=""))
    assert "reachable and authenticated" in notice
    stored = credmgr.creds[_machine_row(db)["machine_id"]]
    assert stored["cred_type"] == "ssh_key"
    assert stored["secret"] == pem + "\n"


# ── the same verdicts reach the chat caller (probe_machine verb) ──────────────

def _register_direct(db, credmgr, with_credential=True):
    mid = remote_machines.create_machine(db, USER, "dgx", ADDR, 22, "me", "linux", "cluster")
    if with_credential:
        credmgr.set_machine_credential(mid, USER, "password", "pw")
    return mid


@pytest.mark.parametrize("fake_kwargs,expected_verdict,expected_next", [
    ({"authenticated": False}, Verdict.AUTH_FAILED, "re-check the username and credential"),
    ({"reachable": False}, Verdict.UNREACHABLE, "check the machine is on"),
])
def test_probe_verb_surfaces_enumerated_failure(db, credmgr, fake_kwargs,
                                                expected_verdict, expected_next):
    _register_direct(db, credmgr)
    set_transport(FakeTransport(**fake_kwargs))
    res = obs.probe_machine(user_id=USER, machine_id="dgx")  # resolve by label
    data = res["_data"]
    assert data["verdict"] == expected_verdict.value
    assert data["machine"] == "dgx"  # every failure names the machine (FR-035)
    assert expected_next in data["next_action"]


def test_probe_verb_ok_persists_verdict_and_host_key(db, credmgr):
    mid = _register_direct(db, credmgr)
    set_transport(FakeTransport())
    res = obs.probe_machine(user_id=USER, machine_id="dgx")
    assert res["_data"] == {"verdict": "ok", "authenticated": True}
    row = remote_machines.get_machine(db, USER, mid)
    assert row["last_verdict"] == Verdict.OK.value
    assert row["host_key_fingerprint"] == "SHA256:fake"


def test_probe_verb_without_credential_is_credential_not_configured(db, credmgr):
    _register_direct(db, credmgr, with_credential=False)
    t = FakeTransport()
    set_transport(t)
    res = obs.probe_machine(user_id=USER, machine_id="dgx")
    assert res["_data"]["verdict"] == Verdict.CREDENTIAL_NOT_CONFIGURED.value
    assert "add a credential" in res["_data"]["next_action"]
    assert t.calls == []  # refused before any connection attempt
