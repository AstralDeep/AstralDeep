"""Feature 063 T029 — FR-047/FR-049 audit coverage for US1.

Machine registration + removal, credential set + delete, re-trust, and every
connection attempt with its verdict must land in the audit log naming the actor,
the machine label, and the outcome. Connection attempts audit at the single
``orchestrator.remote_machines.record_probe`` seam (the surface's probe notice
and the remote-observe verbs both already report verdicts through it), so
agents/** never audits. SECRETS NEVER appear in a row (FR-049) — the sweep here
drives every operation with distinctive sentinel credential strings and asserts
no row carries them.

Hermetic: in-memory DB double, real CredentialManager (real Fernet), transport
via ``set_transport(FakeTransport(...))``, recorder captured by patching
``audit.recorder.get_recorder`` (same conventions as
test_remote_no_secret_leak_063.py).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from audit.schemas import AuditEventCreate
from orchestrator import remote_machines
from orchestrator.credential_manager import CredentialManager
from orchestrator.remote_transport import FakeTransport, Verdict, set_transport
from webrender.chrome.surfaces import remote_machines as surface

USER = "user-1"
OTHER = "user-2"

# Never-legitimate strings: any one of these appearing in an audit row is a leak.
SENTINEL_KEY = "ASTRAL063AUDITSENTINELPRIVATEKEYBYTES"
FAKE_PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
            f"{SENTINEL_KEY}\n"
            "-----END OPENSSH PRIVATE KEY-----")
SENTINEL_PASSPHRASE = "astral063.audit.sentinel.passphrase"
SENTINEL_PASSWORD = "astral063.audit.sentinel.password"
SENTINELS = (SENTINEL_KEY, SENTINEL_PASSPHRASE, SENTINEL_PASSWORD)


# ── in-memory DB double (matches the modules' exact queries) ──────────────────

class _Cur:
    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount


class _MemDB:
    def __init__(self):
        self.machines: dict = {}      # machine_id -> remote_machine row
        self.credentials: dict = {}   # machine_id -> machine_credential row

    def fetch_one(self, q, params=None):
        s = " ".join(q.split())
        if s.startswith("SELECT * FROM remote_machine WHERE machine_id"):
            mid, uid = params
            r = self.machines.get(mid)
            return dict(r) if r and r["owner_user_id"] == uid else None
        if s.startswith("SELECT cred_type, encrypted_secret, encrypted_passphrase"):
            r = self.credentials.get(params[0])
            return dict(r) if r else None
        raise AssertionError("unexpected fetch_one: " + s)

    def fetch_all(self, q, params=None):
        s = " ".join(q.split())
        if "FROM remote_machine WHERE owner_user_id" in s:
            uid = params[0]
            return [dict(r) for r in sorted(self.machines.values(), key=lambda r: r["label"])
                    if r["owner_user_id"] == uid]
        raise AssertionError("unexpected fetch_all: " + s)

    def execute(self, q, params=None):
        s = " ".join(q.split())
        if s.startswith("INSERT INTO remote_machine"):
            (mid, uid, label, address, port, username, osf, role, created, updated) = params
            self.machines[mid] = {
                "machine_id": mid, "owner_user_id": uid, "label": label,
                "address": address, "port": port, "username": username,
                "os_family": osf, "role": role, "last_verdict": None,
                "last_checked_at": None, "host_key_type": None,
                "host_key_fingerprint": None, "host_key_blob": None,
                "created_at": created, "updated_at": updated,
            }
            return _Cur()
        if s.startswith("INSERT INTO machine_credential"):
            (mid, uid, ct, enc_secret, enc_pass, created, updated) = params
            self.credentials[mid] = {
                "machine_id": mid, "owner_user_id": uid, "cred_type": ct,
                "encrypted_secret": enc_secret, "encrypted_passphrase": enc_pass,
            }
            return _Cur()
        if s.startswith("UPDATE remote_machine SET last_verdict") and "host_key_type" in s:
            verdict, now, ktype, kfp, kblob, now2, mid, uid = params
            r = self.machines.get(mid)
            if r and r["owner_user_id"] == uid:
                r.update(last_verdict=verdict, last_checked_at=now, host_key_type=ktype,
                         host_key_fingerprint=kfp, host_key_blob=kblob, updated_at=now2)
            return _Cur()
        if s.startswith("UPDATE remote_machine SET last_verdict"):
            verdict, now, now2, mid, uid = params
            r = self.machines.get(mid)
            if r and r["owner_user_id"] == uid:
                r.update(last_verdict=verdict, last_checked_at=now, updated_at=now2)
            return _Cur()
        if s.startswith("UPDATE remote_machine SET host_key_type = NULL"):
            now, mid, uid = params
            r = self.machines.get(mid)
            if r and r["owner_user_id"] == uid:
                r.update(host_key_type=None, host_key_fingerprint=None,
                         host_key_blob=None, updated_at=now)
            return _Cur()
        if s.startswith("DELETE FROM machine_credential WHERE machine_id"):
            self.credentials.pop(params[0], None)
            return _Cur()
        if s.startswith("DELETE FROM remote_machine WHERE machine_id"):
            mid, uid = params
            r = self.machines.get(mid)
            if r and r["owner_user_id"] == uid:
                del self.machines[mid]
                self.credentials.pop(mid, None)  # FK cascade
            return _Cur()
        raise AssertionError("unexpected execute: " + s)


class _AuditRecorder:
    def __init__(self):
        self.events = []

    def record_blocking(self, ev):
        self.events.append(ev)
        return ev

    async def record(self, ev):
        self.events.append(ev)
        return ev


# ── environment ───────────────────────────────────────────────────────────────

@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(surface, "_enabled", lambda: True)
    rec = _AuditRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: rec)
    db = _MemDB()
    credmgr = CredentialManager(db=db)
    orch = SimpleNamespace(history=SimpleNamespace(db=db), credential_manager=credmgr)
    yield SimpleNamespace(db=db, credmgr=credmgr, orch=orch, audit=rec)
    set_transport(None)


async def _register(env, *, cred_type: str = "ssh_key", label: str = "dgx"):
    """Drive chrome_machine_add exactly as a client submit does — every credential
    field present (legacy clients render them all), the handler reads by cred_type."""
    before = set(env.db.machines)
    fields = {"label": label, "address": "10.0.0.5", "port": "22", "username": "me",
              "os_family": "linux", "role": "cluster", "cred_type": cred_type,
              "private_key": FAKE_PEM, "passphrase": SENTINEL_PASSPHRASE,
              "password": SENTINEL_PASSWORD}
    await surface._h_machine_add(env.orch, None, USER, ["user"], {"fields": fields})
    (mid,) = set(env.db.machines) - before
    return mid


def _rows(env, action_type):
    return [e for e in env.audit.events if e.action_type == action_type]


def _assert_actor_label_outcome(ev, *, label="dgx", outcome="success"):
    assert isinstance(ev, AuditEventCreate)  # construction itself validated the row
    assert ev.actor_user_id == USER
    assert ev.inputs_meta.get("machine_label") == label
    assert label in ev.description
    assert ev.outcome == outcome


# ── register / probe ──────────────────────────────────────────────────────────

async def test_register_emits_registered_row_and_connection_verdict(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    (reg,) = _rows(env, "remote_machine.registered")
    _assert_actor_label_outcome(reg)
    assert reg.inputs_meta.get("machine_id") == mid
    assert reg.inputs_meta.get("cred_type") == "ssh_key"
    assert reg.correlation_id == mid  # one machine's lifecycle correlates
    # add's immediate probe is a connection attempt — it must audit its verdict
    (conn,) = _rows(env, "remote_machine.connection")
    _assert_actor_label_outcome(conn)
    assert conn.inputs_meta.get("verdict") == "ok"
    assert conn.correlation_id == mid


async def test_probe_emits_connection_attempt_with_verdict(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    await surface._h_machine_probe(env.orch, None, USER, ["user"], {"machine_id": mid})
    (conn,) = _rows(env, "remote_machine.connection")
    _assert_actor_label_outcome(conn)
    assert conn.inputs_meta.get("verdict") == "ok"


async def test_wrong_credential_probe_emits_attempt_and_failure_verdict(env):
    set_transport(FakeTransport(authenticated=False))
    mid = await _register(env)  # add's immediate probe fails auth
    conns = _rows(env, "remote_machine.connection")
    assert len(conns) == 1
    _assert_actor_label_outcome(conns[0], outcome="failure")
    assert conns[0].inputs_meta.get("verdict") == "auth_failed"
    env.audit.events.clear()
    await surface._h_machine_probe(env.orch, None, USER, ["user"], {"machine_id": mid})
    (conn,) = _rows(env, "remote_machine.connection")
    assert conn.inputs_meta.get("verdict") == "auth_failed"
    assert conn.outcome == "failure"


async def test_verb_level_connections_audit_through_the_record_probe_seam(env):
    # The remote-observe verbs report every verdict through record_probe — the
    # seam itself must audit, so a verb connection never bypasses FR-047.
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    remote_machines.record_probe(env.db, USER, mid, Verdict.TIMEOUT.value)
    (conn,) = _rows(env, "remote_machine.connection")
    _assert_actor_label_outcome(conn, outcome="failure")
    assert conn.inputs_meta.get("verdict") == "timeout"


# ── credential set / delete ───────────────────────────────────────────────────

async def test_credential_set_emits_row_and_reprobes(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    ret = await surface._h_credential_set(
        env.orch, None, USER, ["user"],
        {"machine_id": mid, "fields": {"cred_type": "password",
                                       "password": SENTINEL_PASSWORD}})
    assert "reachable" in ret[2]
    (ev,) = _rows(env, "remote_machine.credential_set")
    _assert_actor_label_outcome(ev)
    assert ev.inputs_meta.get("cred_type") == "password"
    # the replace really landed, and the immediate probe audited its verdict
    assert env.credmgr.get_machine_credential(mid)["secret"] == SENTINEL_PASSWORD
    (conn,) = _rows(env, "remote_machine.connection")
    assert conn.inputs_meta.get("verdict") == "ok"


async def test_credential_delete_emits_row(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    ret = await surface._h_credential_delete(env.orch, None, USER, ["user"],
                                             {"machine_id": mid})
    assert "Removed the credential" in ret[2]
    (ev,) = _rows(env, "remote_machine.credential_deleted")
    _assert_actor_label_outcome(ev)
    assert mid not in env.db.credentials  # the delete really happened


# ── delete / re-trust ─────────────────────────────────────────────────────────

async def test_machine_delete_emits_removed_row(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    await surface._h_machine_delete(env.orch, None, USER, ["user"], {"machine_id": mid})
    (ev,) = _rows(env, "remote_machine.removed")
    _assert_actor_label_outcome(ev)
    assert mid not in env.db.machines


async def test_retrust_emits_row_and_repins_via_audited_probe(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    old_fp = env.db.machines[mid]["host_key_fingerprint"]
    assert old_fp  # first contact pinned a key
    env.db.machines[mid]["last_verdict"] = "host_key_mismatch"
    env.audit.events.clear()
    set_transport(FakeTransport(host_key={"type": "ssh-ed25519", "blob_b64": "BBBB",
                                          "fingerprint": "SHA256:new"}))
    await surface._h_machine_retrust(env.orch, None, USER, ["user"], {"machine_id": mid})
    (ev,) = _rows(env, "remote_machine.retrusted")
    _assert_actor_label_outcome(ev)
    # the re-probe re-recorded the NEW key and audited its own connection row
    assert env.db.machines[mid]["host_key_fingerprint"] == "SHA256:new"
    (conn,) = _rows(env, "remote_machine.connection")
    assert conn.inputs_meta.get("verdict") == "ok"


# ── owner scoping: a foreign user's attempt neither acts nor audits success ───

async def test_foreign_user_ops_refused_and_emit_no_rows(env):
    set_transport(FakeTransport())
    mid = await _register(env)
    env.audit.events.clear()
    for handler, payload in (
            (surface._h_credential_set, {"machine_id": mid,
                                         "fields": {"cred_type": "password", "password": "x"}}),
            (surface._h_credential_delete, {"machine_id": mid}),
            (surface._h_machine_retrust, {"machine_id": mid}),
            (surface._h_machine_delete, {"machine_id": mid}),
    ):
        ret = await handler(env.orch, None, OTHER, ["user"], payload)
        assert "not in your inventory" in ret[2]
    assert env.audit.events == []  # nothing happened, nothing recorded
    assert mid in env.db.machines and mid in env.db.credentials
    # record_probe for a non-owner is a no-op (owner-scoped lookup) — no row
    remote_machines.record_probe(env.db, OTHER, mid, "ok")
    assert env.audit.events == []


# ── FR-049: no sentinel secret bytes in any audit row ─────────────────────────

async def test_no_sentinel_secret_bytes_in_any_audit_row(env):
    set_transport(FakeTransport())
    mid = await _register(env)  # ssh_key + passphrase sentinels in play
    await surface._h_machine_probe(env.orch, None, USER, ["user"], {"machine_id": mid})
    await surface._h_credential_set(
        env.orch, None, USER, ["user"],
        {"machine_id": mid, "fields": {"cred_type": "password",
                                       "password": SENTINEL_PASSWORD}})
    await surface._h_machine_retrust(env.orch, None, USER, ["user"], {"machine_id": mid})
    await surface._h_credential_delete(env.orch, None, USER, ["user"], {"machine_id": mid})
    await surface._h_machine_delete(env.orch, None, USER, ["user"], {"machine_id": mid})
    # positive control: the audited operations really ran under the sentinels
    assert _rows(env, "remote_machine.registered") and _rows(env, "remote_machine.connection")
    for ev in env.audit.events:
        dump = ev.model_dump_json()
        for s in SENTINELS:
            assert s not in dump, f"secret sentinel leaked into audit row {ev.action_type}"
