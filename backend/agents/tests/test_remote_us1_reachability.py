"""Integration tests for the register-then-probe reachability path: the real
chrome_machine_add flow and remote_observe.probe_machine over AstralPlane
repositories, transport faked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.remote_observe import mcp_tools as obs
from orchestrator import remote_machines
from orchestrator.remote_transport import FakeTransport, Verdict, set_transport
from orchestrator.projection_surfaces import remote_machines as surface
from tests.helpers.remote_plane_runtime import make_remote_plane_source

USER = "user-1"
ADDR = "10.33.77.11"

class MemCredMgr:
    def __init__(self):
        self.creds = {}

    def set_machine_credential(self, machine_id, owner_user_id, cred_type, secret,
                               passphrase=None):
        self.creds[machine_id] = {
            "owner_user_id": owner_user_id,
            "cred_type": cred_type,
            "secret": secret,
            "passphrase": passphrase,
        }

    def get_machine_credential(self, machine_id, owner_user_id):
        credential = self.creds.get(machine_id)
        if credential is None or credential["owner_user_id"] != owner_user_id:
            return None
        return credential

    def delete_machine_credential(self, machine_id, owner_user_id):
        credential = self.creds.get(machine_id)
        if credential is not None and credential["owner_user_id"] == owner_user_id:
            self.creds.pop(machine_id)


@pytest.fixture()
def db():
    return make_remote_plane_source(
        SimpleNamespace(machines={}, credentials={}, jobs={})
    )


@pytest.fixture()
def credmgr():
    return MemCredMgr()


@pytest.fixture()
def orch(db, credmgr):
    return SimpleNamespace(
        plane_repository_source=db,
        credential_manager=credmgr,
    )


@pytest.fixture(autouse=True)
def _wire(db, credmgr, monkeypatch):
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
    rows = remote_machines.list_machines(db, USER)
    return rows[0] if rows else None


async def test_register_reachable_reports_ok_and_pins_host_key(orch, db):
    set_transport(FakeTransport())
    key, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    assert key == surface.SURFACE_KEY
    assert "reachable and authenticated" in notice
    assert "SHA256:fake" in notice
    row = _machine_row(db)
    assert row["last_verdict"] == Verdict.OK.value
    assert row["host_key_fingerprint"] == "SHA256:fake"


async def test_register_wrong_credential_reports_auth_failed(orch, db):
    set_transport(FakeTransport(authenticated=False))
    _, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    assert Verdict.AUTH_FAILED.value in notice
    assert "re-check the username and credential" in notice
    row = _machine_row(db)
    assert row["last_verdict"] == Verdict.AUTH_FAILED.value
    assert row["host_key_fingerprint"] is None


async def test_register_unroutable_reports_unreachable(orch, db):
    set_transport(FakeTransport(reachable=False))
    _, _, notice = await surface._h_machine_add(orch, None, USER, ["user"], _add_payload())
    assert Verdict.UNREACHABLE.value in notice
    assert "check the machine is on" in notice
    assert _machine_row(db)["last_verdict"] == Verdict.UNREACHABLE.value


async def test_register_ssh_key_credential_round_trips_pem(orch, db, credmgr):
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
    res = obs.probe_machine(user_id=USER, machine_id="dgx")
    data = res["_data"]
    assert data["verdict"] == expected_verdict.value
    assert data["machine"] == "dgx"
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
    assert t.calls == []
