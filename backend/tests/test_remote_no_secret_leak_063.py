"""Feature 063 T075 — FR-049 secret-leak sweep across register → probe → verb →
failure paths.

Registers a machine with a distinctive fake PEM, key passphrase, and password
(unique sentinel strings), then drives the REAL paths — the remote-machines
surface HANDLERS (chrome_machine_add / probe / delete), the remote-observe read
verbs, and the tracked-job poller (component refresh + finish notification) —
over the FakeTransport seam, with the real CredentialManager (real Fernet) and
the real ``build_target`` decrypt, so the sentinels genuinely flow through the
system (positive controls assert they reach the decrypted MachineTarget).

Every output channel is captured and swept: audit events emitted, every log
record (DEBUG up, message + args + tracebacks), every returned dict / notice /
rendered payload, notifications, and workspace upserts. No sentinel may appear
in any of them; the stored credential row itself must be Fernet ciphertext that
round-trips through the key. Hermetic: in-memory DB double, no postgres, no
network (same posture as test_remote_confirmation_063.py).
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from agents.remote_observe import mcp_tools as obs
from orchestrator import remote_jobs as rj
from orchestrator.credential_manager import CredentialManager
from orchestrator.remote_transport import FakeTransport, set_transport
from webrender.chrome.surfaces import remote_machines as surface

USER = "user-1"

# Distinctive, never-legitimate strings. The PEM body sentinel stands in for
# "private-key bytes"; any one of these appearing in an output channel is a leak.
SENTINEL_KEY = "ASTRAL063LEAKSENTINELPRIVATEKEYBYTES"
FAKE_PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
            f"{SENTINEL_KEY}\n"
            "-----END OPENSSH PRIVATE KEY-----")
SENTINEL_PASSPHRASE = "astral063.leak.sentinel.passphrase"
SENTINEL_PASSWORD = "astral063.leak.sentinel.password"
SENTINELS = (SENTINEL_KEY, SENTINEL_PASSPHRASE, SENTINEL_PASSWORD)

_SQUEUE_FAILED = ('{"jobs":[{"job_id":42,"job_state":"FAILED","name":"train",'
                  '"partition":"gpu","node_count":1,"state_reason":"None"}]}')


# ── in-memory DB double (matches the modules' exact queries) ──────────────────

class _Cur:
    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount


class _MemDB:
    def __init__(self):
        self.machines: dict = {}      # machine_id -> remote_machine row
        self.credentials: dict = {}   # machine_id -> machine_credential row
        self.jobs: dict = {}          # tracked_job_id -> tracked_job row

    def fetch_one(self, q, params=None):
        s = " ".join(q.split())
        if s.startswith("SELECT * FROM remote_machine WHERE machine_id"):
            mid, uid = params
            r = self.machines.get(mid)
            return dict(r) if r and r["owner_user_id"] == uid else None
        if s.startswith("SELECT * FROM remote_machine WHERE owner_user_id"):
            uid, label, address = params
            for r in sorted(self.machines.values(), key=lambda r: r["label"]):
                if r["owner_user_id"] == uid and (
                        r["label"].lower() == label.lower()
                        or r["address"].lower() == address.lower()):
                    return dict(r)
            return None
        if s.startswith("SELECT cred_type, encrypted_secret, encrypted_passphrase"):
            r = self.credentials.get(params[0])
            return dict(r) if r else None
        if s.startswith("SELECT * FROM tracked_job WHERE owner_user_id"):
            uid, jid = params
            for r in self.jobs.values():
                if r["owner_user_id"] == uid and str(r["scheduler_job_id"]) == str(jid):
                    return dict(r)
            return None
        raise AssertionError("unexpected fetch_one: " + s)

    def fetch_all(self, q, params=None):
        s = " ".join(q.split())
        if "FROM remote_machine WHERE owner_user_id" in s:
            uid = params[0]
            return [dict(r) for r in sorted(self.machines.values(), key=lambda r: r["label"])
                    if r["owner_user_id"] == uid]
        if "FROM tracked_job WHERE terminal = FALSE" in s:
            return [dict(r) for r in self.jobs.values() if not r.get("terminal")]
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
        if s.startswith("UPDATE tracked_job SET state="):
            if "notified=?" in s:
                state, exit_code, terminal, fail_count, now, finished, notified, tid = params
            else:
                state, exit_code, terminal, fail_count, now, finished, tid = params
                notified = None
            r = self.jobs.get(tid)
            if r:
                r.update(state=state, exit_code=exit_code, terminal=bool(terminal),
                         fail_count=int(fail_count), last_polled_at=now)
                if r.get("finished_at") is None:
                    r["finished_at"] = finished
                if notified is not None:
                    r["notified"] = bool(notified)
            return _Cur()
        if s.startswith("UPDATE tracked_job SET notified=TRUE"):
            r = self.jobs.get(params[0])
            if r:
                r["notified"] = True
            return _Cur()
        if s.startswith("UPDATE tracked_job SET fail_count="):
            fc, now, tid = params
            r = self.jobs.get(tid)
            if r:
                r.update(fail_count=int(fc), last_polled_at=now)
            return _Cur()
        if "SET state='orphaned'" in s:
            finished, tid = params
            r = self.jobs.get(tid)
            if r:
                r.update(state="orphaned", terminal=True)
                if r.get("finished_at") is None:
                    r["finished_at"] = finished
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


class _Async:
    """Recording async callable (workspace.aupsert / send_ui_upsert / notify_user)."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    async def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.result


# ── capture / sweep helpers ───────────────────────────────────────────────────

def _dump(obj) -> str:
    return json.dumps(obj, default=repr, ensure_ascii=False)


def _log_text(caplog) -> str:
    parts = []
    for rec in caplog.records:
        parts.append(rec.getMessage())
        if rec.args:
            parts.append(repr(rec.args))
        if isinstance(rec.exc_info, tuple):
            parts.append("".join(traceback.format_exception(*rec.exc_info)))
        elif rec.exc_text:
            parts.append(rec.exc_text)
    return "\n".join(parts)


def _audit_text(recorder) -> str:
    parts = []
    for e in recorder.events:
        parts.append(repr(e))
        dump = getattr(e, "model_dump", None)
        if callable(dump):
            try:
                parts.append(_dump(dump()))
            except Exception:  # noqa: BLE001 — repr above already captured it
                pass
    return "\n".join(parts)


def _assert_clean(pieces):
    for where, text in pieces:
        for s in SENTINELS:
            assert s not in text, f"secret sentinel leaked into {where}"


def _channel_pieces(env):
    """The always-swept channels: every log record and every audit event."""
    return [("log records", _log_text(env.caplog)),
            ("audit events", _audit_text(env.audit))]


# ── environment ───────────────────────────────────────────────────────────────

@pytest.fixture()
def env(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(surface, "_enabled", lambda: True)
    rec = _AuditRecorder()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: rec)
    db = _MemDB()
    credmgr = CredentialManager(db=db)
    orch = SimpleNamespace(history=SimpleNamespace(db=db), credential_manager=credmgr)
    obs.register_deps(db, credmgr)
    yield SimpleNamespace(db=db, credmgr=credmgr, orch=orch, audit=rec, caplog=caplog)
    obs.register_deps(None, None)
    set_transport(None)


async def _register(env, *, cred_type: str = "ssh_key", label: str = "dgx"):
    """Drive chrome_machine_add exactly as a client submit does — every credential
    field present (legacy clients render them all), the handler reads by cred_type."""
    before = set(env.db.machines)
    fields = {"label": label, "address": "10.0.0.5", "port": "22", "username": "me",
              "os_family": "linux", "role": "cluster", "cred_type": cred_type,
              "private_key": FAKE_PEM, "passphrase": SENTINEL_PASSPHRASE,
              "password": SENTINEL_PASSWORD}
    ret = await surface._h_machine_add(env.orch, None, USER, ["user"], {"fields": fields})
    (mid,) = set(env.db.machines) - before
    return mid, ("chrome_machine_add return", _dump(ret))


# ── the sweep must itself be able to catch a leak (tripwires) ─────────────────

def test_sweep_helper_detects_a_planted_leak():
    with pytest.raises(AssertionError, match="leaked into planted"):
        _assert_clean([("planted", f"prefix {FAKE_PEM} suffix")])


async def test_sweep_catches_a_leak_planted_in_a_driven_path(env, monkeypatch):
    # End-to-end negative control: a log line emitted INSIDE a driven code path
    # must reach the sweep's capture — otherwise every "clean" result above
    # would be vacuous.
    set_transport(FakeTransport())
    real = CredentialManager.set_machine_credential

    def leaky(self, machine_id, owner_user_id, cred_type, secret, passphrase=None):
        logging.getLogger("CredentialManager").info("storing secret %s", secret)
        return real(self, machine_id, owner_user_id, cred_type, secret, passphrase)

    monkeypatch.setattr(CredentialManager, "set_machine_credential", leaky)
    await _register(env)
    with pytest.raises(AssertionError, match="leaked into log records"):
        _assert_clean(_channel_pieces(env))


# ── register → probe → render (happy path) ────────────────────────────────────

async def test_register_probe_and_rendered_surfaces_leak_nothing(env):
    set_transport(FakeTransport())
    pieces = []
    mid, add_piece = await _register(env)
    pieces.append(add_piece)

    # Positive control: the sentinels really are in play — stored, decrypted, and
    # flowing into the MachineTarget the transport sees (otherwise this whole
    # sweep would pass vacuously).
    cred = env.credmgr.get_machine_credential(mid)
    assert SENTINEL_KEY in cred["secret"] and cred["passphrase"] == SENTINEL_PASSPHRASE

    probe_ret = await surface._h_machine_probe(env.orch, None, USER, ["user"],
                                               {"machine_id": mid})
    pieces.append(("chrome_machine_probe return", _dump(probe_ret)))
    pieces.append(("web render()", await surface.render(env.orch, USER, ["user"], {})))
    pieces.append(("native components()",
                   _dump(await surface.components(env.orch, USER, ["user"], {}))))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


# ── read verbs over the real resolve/decrypt path ─────────────────────────────

async def test_read_verb_results_leak_nothing(env):
    set_transport(FakeTransport())
    pieces = []
    mid, add_piece = await _register(env)
    pieces.append(add_piece)

    t = FakeTransport(command_stdout="f\t512\t1700000001.5\tresults.txt\n")
    set_transport(t)
    for name, res in (
            ("list_machines", obs.list_machines(user_id=USER)),
            ("probe_machine", obs.probe_machine(user_id=USER, machine_id=mid)),
            ("list_directory", obs.list_directory(user_id=USER, machine_id=mid,
                                                  path="/home/me")),
    ):
        pieces.append((f"{name} result", _dump(res)))
    pieces.append(("transport call log", _dump(t.calls)))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


# ── induced failures: wrong credential / unreachable / undecryptable ──────────

async def test_wrong_credential_failure_leaks_nothing(env):
    set_transport(FakeTransport(authenticated=False))
    pieces = []
    mid, add_piece = await _register(env)  # add's immediate probe fails auth
    pieces.append(add_piece)
    assert "auth_failed" in add_piece[1]  # the failure path actually ran
    pieces.append(("verb auth_failed result",
                   _dump(obs.list_directory(user_id=USER, machine_id=mid, path="/x"))))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


async def test_unreachable_failure_leaks_nothing(env):
    set_transport(FakeTransport(reachable=False))
    pieces = []
    mid, add_piece = await _register(env)
    pieces.append(add_piece)
    assert "unreachable" in add_piece[1]
    pieces.append(("verb unreachable result",
                   _dump(obs.host_facts(user_id=USER, machine_id=mid))))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


async def test_undecryptable_credential_failure_leaks_nothing(env):
    set_transport(FakeTransport())
    pieces = []
    mid, add_piece = await _register(env)
    pieces.append(add_piece)
    # Simulate a rotated encryption key: ciphertext under a key we no longer hold.
    env.db.credentials[mid]["encrypted_secret"] = (
        Fernet(Fernet.generate_key()).encrypt(FAKE_PEM.encode()).decode())
    probe_ret = await surface._h_machine_probe(env.orch, None, USER, ["user"],
                                               {"machine_id": mid})
    pieces.append(("undecryptable probe notice", _dump(probe_ret)))
    verb = obs.list_directory(user_id=USER, machine_id=mid, path="/x")
    assert verb["_data"]["verdict"] == "credential_undecryptable"
    pieces.append(("verb undecryptable result", _dump(verb)))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


# ── password credential + delete ──────────────────────────────────────────────

async def test_password_credential_and_delete_leak_nothing(env):
    set_transport(FakeTransport())
    pieces = []
    mid, add_piece = await _register(env, cred_type="password")
    pieces.append(add_piece)
    assert env.credmgr.get_machine_credential(mid)["secret"] == SENTINEL_PASSWORD
    del_ret = await surface._h_machine_delete(env.orch, None, USER, ["user"],
                                              {"machine_id": mid})
    pieces.append(("chrome_machine_delete return", _dump(del_ret)))
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


# ── tracked-job poller: component refresh + finish notification (FR-045) ──────

async def test_job_poller_component_and_notification_leak_nothing(env):
    set_transport(FakeTransport())
    mid, add_piece = await _register(env)
    assert env.db.machines[mid]["host_key_fingerprint"]  # pinned → poller may run

    env.db.jobs["t1"] = {
        "tracked_job_id": "t1", "owner_user_id": USER, "machine_id": mid,
        "chat_id": "chat-1", "scheduler_job_id": "42", "submit_marker": None,
        "output_path": "/home/me/.astral_jobs/42.out", "component_id": "au_job42",
        "job_name": "train", "state": "RUNNING", "notify_on_finish": True,
        "notified": False, "terminal": False, "fail_count": 0, "created_at": 0,
        "finished_at": None, "exit_code": None,
    }
    set_transport(FakeTransport(command_stdout=_SQUEUE_FAILED))

    orch = env.orch
    orch.workspace = SimpleNamespace(
        aupsert=_Async(result=[{"op": "update", "component_id": "au_job42"}]))
    orch.send_ui_upsert = _Async()
    orch.notify_user = _Async()

    async def _run_detached(*, chat_id, user_id, mutation):
        return await mutation()
    orch.run_detached_conversation_mutation = _run_detached

    await rj.poll_once(orch)

    assert len(orch.notify_user.calls) == 1  # the notification path actually ran
    assert len(orch.workspace.aupsert.calls) == 1
    pieces = [
        add_piece,
        ("job notification payload", _dump(orch.notify_user.calls)),
        ("job canvas component", _dump(orch.workspace.aupsert.calls)),
        ("job ui_upsert ops", _dump(orch.send_ui_upsert.calls)),
        ("tracked_job row after poll", _dump(env.db.jobs["t1"])),
    ]
    pieces.extend(_channel_pieces(env))
    _assert_clean(pieces)


# ── at rest: the credential row is Fernet ciphertext, not the sentinel ────────

async def test_credential_row_is_fernet_ciphertext_at_rest(env):
    set_transport(FakeTransport())
    mid, _ = await _register(env)
    row = env.db.credentials[mid]
    _assert_clean([("stored machine_credential row", _dump(row))])
    assert row["encrypted_secret"].startswith("gAAAA")  # Fernet token, not encoding
    assert row["encrypted_passphrase"].startswith("gAAAA")
    f = Fernet(os.environ["CREDENTIAL_ENCRYPTION_KEY"].encode())
    assert f.decrypt(row["encrypted_secret"].encode()).decode() == FAKE_PEM + "\n"
    assert f.decrypt(row["encrypted_passphrase"].encode()).decode() == SENTINEL_PASSPHRASE
