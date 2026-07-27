"""Feature 063 (T020/T025) — remote-machines settings surface: web render() +
native components() parity, and a multi-line PEM surviving a round trip through
the surface into the per-machine credential store.

Hermetic: a minimal fake orch; the DB listing + the connect-probe are
monkeypatched so no Postgres / SSH is touched.
"""
import asyncio
from types import SimpleNamespace

import pytest

from webrender.chrome.surfaces import get_surface
from webrender.chrome.surfaces import remote_machines as rm

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
    assert set(rm.HANDLERS) == {"chrome_machine_add", "chrome_machine_probe",
                                "chrome_machine_delete"}
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
