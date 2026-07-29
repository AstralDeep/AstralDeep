"""Feature 063 (T020/T025) — remote-machines settings surface: web render() +
native components() parity, and a multi-line PEM surviving a round trip through
the surface into the per-machine credential store.

Hermetic: a minimal fake orch; the DB listing + the connect-probe are
monkeypatched so no Postgres / SSH is touched.
"""
import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.credential_manager import CredentialUndecryptable
from orchestrator.remote_transport import MachineTarget, RemoteResult, Verdict
from webrender.chrome.surfaces import get_surface
from webrender.chrome.surfaces import remote_machines as rm

# Captured at import — the _no_db fixture patches rm._enabled to a constant, so
# the flag-reading original is only reachable from a reference taken beforehand.
_REAL_ENABLED = rm._enabled

PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
       "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAA\n"
       "tzc2gtZWQyNTUxOQAAACD0line3line4padding==\n"
       "-----END OPENSSH PRIVATE KEY-----")


def run(coro):
    return asyncio.run(coro)


class FakeCM:
    """Records set_machine_credential so we can assert the stored secret."""

    def __init__(self):
        self.creds = {}

    def set_machine_credential(self, machine_id, user_id, cred_type, secret, passphrase):
        self.creds[machine_id] = dict(user_id=user_id, cred_type=cred_type,
                                      secret=secret, passphrase=passphrase)


def _orch(cm=None):
    return SimpleNamespace(history=SimpleNamespace(db=object()),
                           credential_manager=cm or FakeCM())


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # render()/components() list machines — stub to empty (no Postgres).
    monkeypatch.setattr(rm.remote_machines, "list_machines", lambda db, uid: [])
    # CI runs with FF_REMOTE_COMPUTE unset; these tests exercise the ENABLED
    # surface (the disabled posture is test_remote_flag_off_063.py's job).
    monkeypatch.setattr(rm, "_enabled", lambda: True)


def _picker(comps):
    ps = [c for c in comps if isinstance(c, dict) and c.get("type") == "param_picker"]
    assert len(ps) == 1, f"expected one form, got {ps!r}"
    return ps[0]


def _field(picker, name):
    for f in picker["fields"]:
        if f.get("name") == name:
            return f
    return None


# ── registry + handler contract ───────────────────────────────────────────────

def test_registry_resolves_surface():
    mod = get_surface("remote_machines")
    assert mod is rm
    assert mod.TITLE == "Remote machines"


def test_handlers_cover_the_machine_actions():
    # T026 extended the set with the machine-scoped credential + re-trust
    # actions (machine-namespaced: the flat chrome action map already gives
    # plain chrome_credential_delete to the agents surface).
    assert set(rm.HANDLERS) == {"chrome_machine_add", "chrome_machine_probe",
                                "chrome_machine_delete",
                                "chrome_machine_credential_set",
                                "chrome_machine_credential_delete",
                                "chrome_machine_retrust"}
    for fn in rm.HANDLERS.values():
        assert asyncio.iscoroutinefunction(fn)


# ── web render() ───────────────────────────────────────────────────────────────

def test_render_has_private_key_textarea_and_add_action():
    html = run(rm.render(_orch(), "u1", ["user"], {}))
    assert '<textarea name="private_key"' in html
    assert 'name="password"' in html and 'name="cred_type"' in html
    assert 'data-ui-action="chrome_machine_add"' in html


# ── native components() ─────────────────────────────────────────────────────────

def test_components_form_has_all_fields_and_submit_action():
    comps = run(rm.components(_orch(), "u1", ["user"], {}))
    picker = _picker(comps)
    names = {f["name"] for f in picker["fields"]}
    assert {"label", "address", "port", "username", "os_family", "role",
            "cred_type", "private_key", "passphrase", "password"} <= names
    # the private key is a textarea (multi-line PEM); the secrets are password kind
    assert _field(picker, "private_key")["kind"] == "textarea"
    assert _field(picker, "passphrase")["kind"] == "password"
    assert _field(picker, "password")["kind"] == "password"
    assert _field(picker, "cred_type")["kind"] == "select"
    # submit binds to the SAME handler the web form uses
    assert picker.get("submit_action") == "chrome_machine_add"


def test_components_lists_existing_machines_with_probe_and_delete(monkeypatch):
    monkeypatch.setattr(rm.remote_machines, "list_machines", lambda db, uid: [
        {"machine_id": "m1", "label": "dgx", "address": "dgx.x", "port": 22,
         "os_family": "linux", "role": "cluster", "last_verdict": "ok"}])
    comps = run(rm.components(_orch(), "u1", ["user"], {}))
    flat = str(comps)
    assert "dgx" in flat
    actions = {c.get("action") for c in _iter_dicts(comps)}
    assert {"chrome_machine_probe", "chrome_machine_delete"} <= actions
    # each per-machine action carries the machine_id payload
    payloads = [c.get("payload") for c in _iter_dicts(comps)
                if c.get("action") in ("chrome_machine_probe", "chrome_machine_delete")]
    assert all(p.get("machine_id") == "m1" for p in payloads)


def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


# ── the round trip: a multi-line PEM survives into the credential store ────────

def test_multiline_pem_round_trips_through_add_handler(monkeypatch):
    cm = FakeCM()
    monkeypatch.setattr(rm.remote_machines, "create_machine", lambda *a, **k: "mid1")
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: "<probed>")
    orch = _orch(cm)
    payload = {"fields": {"label": "dgx", "address": "dgx.ai.uky.edu", "username": "me",
                          "port": "22", "cred_type": "ssh_key", "private_key": PEM}}
    surface, _params, notice = run(rm.HANDLERS["chrome_machine_add"](
        orch, None, "u1", ["user"], payload))
    assert surface == "remote_machines"
    stored = cm.creds["mid1"]
    assert stored["cred_type"] == "ssh_key"
    # newlines preserved: same line count as the input (handler appends a trailing \n)
    assert stored["secret"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert stored["secret"].rstrip("\n").endswith("-----END OPENSSH PRIVATE KEY-----")
    assert stored["secret"].count("\n") >= PEM.count("\n")
    assert "b3BlbnNzaC1rZXktdjEA" in stored["secret"]  # interior line intact


def test_add_missing_required_fields_is_rejected_without_credential(monkeypatch):
    cm = FakeCM()
    monkeypatch.setattr(rm.remote_machines, "create_machine",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create")))
    surface, _params, notice = run(rm.HANDLERS["chrome_machine_add"](
        _orch(cm), None, "u1", ["user"],
        {"fields": {"label": "", "address": "", "username": ""}}))
    assert surface == "remote_machines"
    assert cm.creds == {}
    assert "required" in notice.lower()


def test_add_password_cred_ignores_private_key(monkeypatch):
    cm = FakeCM()
    monkeypatch.setattr(rm.remote_machines, "create_machine", lambda *a, **k: "mid2")
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: "<probed>")
    run(rm.HANDLERS["chrome_machine_add"](
        _orch(cm), None, "u1", ["user"],
        {"fields": {"label": "h", "address": "h", "username": "me", "port": "22",
                    "cred_type": "password", "password": "s3cret", "private_key": PEM}}))
    stored = cm.creds["mid2"]
    assert stored["cred_type"] == "password" and stored["secret"] == "s3cret"
    assert stored["passphrase"] is None


# ── pure helpers: submitted-field parsing + credential validation ──────────────

def test_fields_drops_non_dict_payloads_and_structured_values():
    assert rm._fields(None) == {}
    assert rm._fields({"fields": "not-a-dict"}) == {}
    # scalars are stringified + trimmed; dict/list/None values and non-string keys
    # are dropped (a form post is flat text, so anything else is a crafted payload)
    assert rm._fields({"fields": {"label": "  dgx  ", "port": 22, "nested": {"a": 1},
                                  "listy": [1], "nothing": None}}) == {"label": "dgx",
                                                                       "port": "22"}


def test_credential_from_fields_requires_the_matching_secret():
    assert rm._credential_from_fields({"cred_type": "ssh_key"}) == (
        "Paste a private key for an SSH-key credential.")
    assert rm._credential_from_fields({"cred_type": "password"}) == (
        "Enter a password for a password credential.")


def test_credential_from_fields_defaults_to_ssh_key_and_terminates_the_pem():
    cred_type, secret, passphrase = rm._credential_from_fields(
        {"cred_type": "not-a-type", "private_key": PEM, "password": "ignored"})
    assert cred_type == "ssh_key" and passphrase is None
    assert secret == PEM + "\n"  # PEMs want a trailing newline


def test_credential_from_fields_password_ignores_the_key_fields():
    assert rm._credential_from_fields(
        {"cred_type": "password", "password": "s3cret",
         "private_key": PEM, "passphrase": "pp"}) == ("password", "s3cret", None)


# ── FF_REMOTE_COMPUTE re-check reads the real flag ────────────────────────────

@pytest.mark.parametrize("on", [True, False])
def test_enabled_delegates_to_the_remote_compute_flag(monkeypatch, on):
    # _REAL_ENABLED is captured at import, before _no_db patches _enabled to True.
    from shared import feature_flags
    monkeypatch.setattr(feature_flags.flags, "is_enabled",
                        lambda name: on if name == "remote_compute" else False)
    assert _REAL_ENABLED() is on


# ── _probe_notice: honest verdict, and failures that must not raise ────────────

class _ProbeTransport:
    """Only ``probe`` is reached from _probe_notice."""

    def __init__(self, result):
        self.result = result
        self.probes = 0

    def probe(self, target, *, timeout):
        self.probes += 1
        return self.result


def _target():
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x")


def _wire_probe(monkeypatch, result, *, build=None, record=None):
    monkeypatch.setattr(rm.remote_machines, "build_target",
                        build or (lambda db, cm, uid, mid: _target()))
    monkeypatch.setattr(rm.remote_machines, "record_probe", record or (lambda *a, **k: None))
    transport = _ProbeTransport(result)
    monkeypatch.setattr(rm, "get_transport", lambda: transport)
    return transport


def _no_probe(monkeypatch):
    monkeypatch.setattr(rm, "get_transport",
                        lambda: pytest.fail("must not open a connection"))


def test_probe_notice_success_records_the_verdict_and_names_the_host_key(monkeypatch):
    recorded = []
    _wire_probe(monkeypatch,
                RemoteResult(verdict=Verdict.OK, machine="dgx",
                             host_key={"fingerprint": "SHA256:abc"}),
                record=lambda *a, **k: recorded.append((a, k)))
    html = rm._probe_notice(_orch(), "u1", "m1", "dgx")
    assert "reachable and authenticated" in html and "SHA256:abc" in html
    assert recorded[0][0][3] == "ok"
    assert recorded[0][1]["host_key"] == {"fingerprint": "SHA256:abc"}


def test_probe_notice_success_without_a_host_key_omits_the_fingerprint(monkeypatch):
    _wire_probe(monkeypatch, RemoteResult(verdict=Verdict.OK, machine="dgx"))
    html = rm._probe_notice(_orch(), "u1", "m1", "dgx")
    assert "reachable and authenticated" in html and "Host key" not in html


def test_probe_notice_failure_shows_the_verdict_and_next_action(monkeypatch):
    _wire_probe(monkeypatch, RemoteResult(verdict=Verdict.AUTH_FAILED, machine="dgx",
                                          next_action="check the credential"))
    html = rm._probe_notice(_orch(), "u1", "m1", "dgx")
    assert "auth_failed" in html and "check the credential" in html


def test_probe_notice_survives_a_failed_verdict_write(monkeypatch):
    # record_probe is best-effort: a write failure must not lose the honest verdict.
    def _boom(*a, **k):
        raise RuntimeError("db down")
    _wire_probe(monkeypatch, RemoteResult(verdict=Verdict.OK, machine="dgx"), record=_boom)
    assert "reachable and authenticated" in rm._probe_notice(_orch(), "u1", "m1", "dgx")


def test_probe_notice_undecryptable_credential_is_named_not_raised(monkeypatch):
    def _boom(*a, **k):
        raise CredentialUndecryptable("key rotated")
    monkeypatch.setattr(rm.remote_machines, "build_target", _boom)
    _no_probe(monkeypatch)
    html = rm._probe_notice(_orch(), "u1", "m1", "dgx")
    assert "decrypted" in html and "re-enter it" in html


def test_probe_notice_unloadable_machine_is_reported_not_raised(monkeypatch):
    def _boom(*a, **k):
        raise rm.remote_machines.MachineNotFound("gone")
    monkeypatch.setattr(rm.remote_machines, "build_target", _boom)
    _no_probe(monkeypatch)
    assert "could not load machine or credential" in rm._probe_notice(
        _orch(), "u1", "m1", "dgx")


# ── handler refusals: bad payloads and machines you do not own ─────────────────

class TrackingCM(FakeCM):
    """FakeCM that also records (or fails) per-machine credential destroys."""

    def __init__(self, *, delete_raises: bool = False):
        super().__init__()
        self.deleted = []
        self._delete_raises = delete_raises

    def delete_machine_credential(self, machine_id):
        self.deleted.append(machine_id)
        if self._delete_raises:
            raise RuntimeError("vault unavailable")


def _tripwires(monkeypatch, *names):
    for name in names:
        monkeypatch.setattr(rm.remote_machines, name,
                            lambda *a, **k: pytest.fail("refused path must not touch state"))


_MACHINE_SCOPED = ["chrome_machine_probe", "chrome_machine_delete",
                   "chrome_machine_credential_set", "chrome_machine_credential_delete",
                   "chrome_machine_retrust"]


@pytest.mark.parametrize("action", _MACHINE_SCOPED)
def test_machine_scoped_handlers_require_a_machine_id(monkeypatch, action):
    _tripwires(monkeypatch, "get_machine", "delete_machine", "retrust_host_key",
               "audit_machine_event")
    cm = TrackingCM()
    key, _params, notice = run(rm.HANDLERS[action](_orch(cm), None, "u1", ["user"], {}))
    assert key == "remote_machines"
    assert "No machine specified." in notice
    assert cm.creds == {} and cm.deleted == []


@pytest.mark.parametrize("action", [a for a in _MACHINE_SCOPED if a != "chrome_machine_delete"])
def test_machine_scoped_handlers_refuse_a_machine_you_do_not_own(monkeypatch, action):
    # get_machine is owner-scoped: a foreign/unknown id reads as absent, and the
    # handler must stop there — never reaching the machine_id-keyed credential ops.
    monkeypatch.setattr(rm.remote_machines, "get_machine", lambda db, uid, mid: None)
    _tripwires(monkeypatch, "retrust_host_key", "audit_machine_event")
    _no_probe(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS[action](
        _orch(cm), None, "u1", ["user"],
        {"machine_id": "someone-elses", "fields": {"cred_type": "password", "password": "p"}}))
    assert "not in your inventory" in notice
    assert cm.creds == {} and cm.deleted == []


def test_delete_refused_for_a_foreign_machine_leaves_its_credential(monkeypatch):
    # delete_machine is itself owner-scoped; the credential destroy is keyed by
    # machine_id ALONE, so a refused delete must not reach it.
    monkeypatch.setattr(rm.remote_machines, "get_machine", lambda db, uid, mid: None)
    monkeypatch.setattr(rm.remote_machines, "delete_machine", lambda db, uid, mid: False)
    _tripwires(monkeypatch, "audit_machine_event")
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_delete"](
        _orch(cm), None, "u1", ["user"], {"machine_id": "someone-elses"}))
    assert "not in your inventory" in notice
    assert cm.deleted == []


# ── add: port + credential validation refuse before anything is created ────────

def _no_create(monkeypatch):
    monkeypatch.setattr(rm.remote_machines, "create_machine",
                        lambda *a, **k: pytest.fail("must not create a machine"))


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1", "22.5"])
def test_add_rejects_a_non_numeric_or_out_of_range_port(monkeypatch, port):
    _no_create(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_add"](
        _orch(cm), None, "u1", ["user"],
        {"fields": {"label": "l", "address": "a", "username": "u", "port": port,
                    "cred_type": "password", "password": "p"}}))
    assert "Port must be a number between 1 and 65535." in notice
    assert cm.creds == {}


def test_add_without_a_secret_creates_nothing(monkeypatch):
    _no_create(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_add"](
        _orch(cm), None, "u1", ["user"],
        {"fields": {"label": "l", "address": "a", "username": "u",
                    "cred_type": "ssh_key", "private_key": ""}}))
    assert "Paste a private key" in notice
    assert cm.creds == {}


def test_add_defaults_unknown_os_role_and_port(monkeypatch):
    seen = {}

    def _create(db, uid, label, address, port, username, os_family, role):
        seen.update(port=port, os_family=os_family, role=role)
        return "mid9"
    monkeypatch.setattr(rm.remote_machines, "create_machine", _create)
    events = []
    monkeypatch.setattr(rm.remote_machines, "audit_machine_event",
                        lambda uid, action, desc, **k: events.append((action, k.get("cred_type"))))
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: "<probed>")
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_add"](
        _orch(), None, "u1", ["user"],
        {"fields": {"label": "l", "address": "a", "username": "u",
                    "os_family": "plan9", "role": "bogus",
                    "cred_type": "password", "password": "p"}}))
    assert seen == {"port": 22, "os_family": "linux", "role": "plain"}
    assert events == [("remote_machine.registered", "password")]
    assert notice == "<probed>"


# ── per-machine credential replace / remove / re-trust (owner-scoped) ──────────

def _owned(monkeypatch, label="dgx"):
    monkeypatch.setattr(rm.remote_machines, "get_machine",
                        lambda db, uid, mid: {"machine_id": mid, "label": label})


def _audit_sink(monkeypatch):
    events = []
    monkeypatch.setattr(rm.remote_machines, "audit_machine_event",
                        lambda uid, action, desc, **k: events.append((action, k.get("cred_type"))))
    return events


def test_credential_set_stores_the_secret_audits_and_reprobes(monkeypatch):
    _owned(monkeypatch)
    events = _audit_sink(monkeypatch)
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: "<reprobed>")
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_credential_set"](
        _orch(cm), None, "u1", ["user"],
        {"machine_id": "m1", "fields": {"cred_type": "ssh_key", "private_key": PEM,
                                        "passphrase": "pp"}}))
    stored = cm.creds["m1"]
    assert stored["cred_type"] == "ssh_key" and stored["passphrase"] == "pp"
    assert stored["secret"].endswith("-----END OPENSSH PRIVATE KEY-----\n")
    assert events == [("remote_machine.credential_set", "ssh_key")]
    assert notice == "<reprobed>"


def test_credential_set_without_a_secret_stores_nothing(monkeypatch):
    _owned(monkeypatch)
    _tripwires(monkeypatch, "audit_machine_event")
    _no_probe(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_credential_set"](
        _orch(cm), None, "u1", ["user"],
        {"machine_id": "m1", "fields": {"cred_type": "password", "password": ""}}))
    assert "Enter a password" in notice
    assert cm.creds == {}


def test_credential_delete_removes_and_audits(monkeypatch):
    _owned(monkeypatch)
    events = _audit_sink(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_credential_delete"](
        _orch(cm), None, "u1", ["user"], {"machine_id": "m1"}))
    assert cm.deleted == ["m1"]
    assert events == [("remote_machine.credential_deleted", None)]
    assert "add a new one to reconnect" in notice


def test_credential_delete_failure_is_honest_and_not_audited(monkeypatch):
    _owned(monkeypatch)
    _tripwires(monkeypatch, "audit_machine_event")
    cm = TrackingCM(delete_raises=True)
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_credential_delete"](
        _orch(cm), None, "u1", ["user"], {"machine_id": "m1"}))
    assert "Could not remove the credential." in notice
    assert cm.deleted == ["m1"]


def test_delete_destroys_the_credential_and_audits_the_removal(monkeypatch):
    _owned(monkeypatch)
    monkeypatch.setattr(rm.remote_machines, "delete_machine", lambda db, uid, mid: True)
    events = _audit_sink(monkeypatch)
    cm = TrackingCM()
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_delete"](
        _orch(cm), None, "u1", ["user"], {"machine_id": "m1"}))
    assert cm.deleted == ["m1"]
    assert events == [("remote_machine.removed", None)]
    assert "Removed dgx" in notice


def test_delete_completes_when_the_credential_destroy_fails(monkeypatch):
    # The FK cascade already removes the row; a vault hiccup must not leave the
    # machine half-deleted or raise out of the handler.
    _owned(monkeypatch)
    monkeypatch.setattr(rm.remote_machines, "delete_machine", lambda db, uid, mid: True)
    events = _audit_sink(monkeypatch)
    cm = TrackingCM(delete_raises=True)
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_delete"](
        _orch(cm), None, "u1", ["user"], {"machine_id": "m1"}))
    assert events == [("remote_machine.removed", None)]
    assert "Removed dgx" in notice


def test_retrust_clears_the_pin_audits_and_reprobes(monkeypatch):
    _owned(monkeypatch, label="edge")
    cleared = []
    monkeypatch.setattr(rm.remote_machines, "retrust_host_key",
                        lambda db, uid, mid: cleared.append((uid, mid)))
    events = _audit_sink(monkeypatch)
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: f"<reprobed {label}>")
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_retrust"](
        _orch(), None, "u1", ["user"], {"machine_id": "m2"}))
    assert cleared == [("u1", "m2")]
    assert events == [("remote_machine.retrusted", None)]
    assert notice == "<reprobed edge>"


def test_probe_handler_probes_an_owned_machine(monkeypatch):
    _owned(monkeypatch)
    monkeypatch.setattr(rm, "_probe_notice", lambda orch, uid, mid, label: f"<probed {label}>")
    _key, _params, notice = run(rm.HANDLERS["chrome_machine_probe"](
        _orch(), None, "u1", ["user"], {"machine_id": "m1"}))
    assert notice == "<probed dgx>"
