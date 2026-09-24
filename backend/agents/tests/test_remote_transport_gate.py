"""Tests for the connection-time SSH gate: shared/net_guard.py's denylist and
anti-DNS-rebind checks, and orchestrator/remote_transport.py's host-key pinning
policy.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import remote_machines
from orchestrator import remote_transport as rt
from orchestrator.remote_transport import (
    FakeTransport,
    MachineTarget,
    Verdict,
    _peer_in_resolved,
    evaluate_host_key,
    set_transport,
)
from shared import net_guard
from tests.helpers.remote_plane_runtime import make_remote_plane_source

USER = "user-1"
RFC1918 = "10.33.77.11"


def _target(addr=RFC1918):
    return MachineTarget(machine_id="m1", label="dgx", address=addr, port=22,
                         username="me", cred_type="password", secret="pw")


@pytest.fixture(autouse=True)
def _reset_transport():
    yield
    set_transport(None)


@pytest.mark.parametrize("addr", [
    "127.0.0.1",
    "127.8.9.10",
    "::1",
    "169.254.169.254",
    "169.254.0.7",
    "fe80::1",
    "0.0.0.0",
    "224.0.0.1",
    "240.0.0.2",
    "not-an-ip",
])
def test_denylist_blocks(addr):
    assert net_guard.is_blocked_ssh_address(addr) is True


@pytest.mark.parametrize("addr", [
    "10.0.0.5", "172.16.3.4", "192.168.1.20",
    "8.8.8.8", "2607:f8b0::1",
])
def test_denylist_permits(addr):
    assert net_guard.is_blocked_ssh_address(addr) is False


@pytest.mark.parametrize("addr", [
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "2002:7f00:1::",
    "64:ff9b::7f00:1",
])
def test_ipv4_in_ipv6_encodings_cannot_bypass(addr):
    assert net_guard.is_blocked_ssh_address(addr) is True


def test_ipv4_mapped_rfc1918_stays_permitted():
    assert net_guard.is_blocked_ssh_address("::ffff:10.0.0.5") is False


def test_gate_refuses_metadata_and_loopback_targets():
    for addr in ("169.254.169.254", "127.0.0.1"):
        with pytest.raises(net_guard.BlockedTargetError):
            net_guard.assert_ssh_target_allowed(addr, 22)


def test_gate_permits_rfc1918_and_returns_vetted_set():
    assert net_guard.assert_ssh_target_allowed(RFC1918, 22) == [RFC1918]


@pytest.mark.parametrize("port", [0, -1, 65536, "22"])
def test_gate_refuses_invalid_port(port):
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_ssh_target_allowed(RFC1918, port)


def test_name_resolving_to_any_blocked_record_is_refused_whole(monkeypatch):
    monkeypatch.setattr(net_guard, "resolve_host_addresses",
                        lambda host: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_ssh_target_allowed("rebind.example", 22)


def test_connected_peer_must_be_in_the_vetted_set():
    vetted = net_guard.assert_ssh_target_allowed(RFC1918, 22)
    assert _peer_in_resolved(RFC1918, vetted) is True
    assert _peer_in_resolved("127.0.0.1", vetted) is False
    assert _peer_in_resolved(None, vetted) is False


@pytest.mark.parametrize("addr", ["127.0.0.1", "169.254.169.254"])
def test_transport_refuses_blocked_target_with_verdict(addr):
    res = FakeTransport().probe(_target(addr), timeout=5)
    assert res.verdict is Verdict.BLOCKED_ADDRESS
    assert "not permitted" in res.next_action


def test_transport_gate_runs_before_command_execution():
    ft = FakeTransport(command_stdout="should-never-run")
    res = ft.run(_target("127.0.0.1"), ["true"], timeout=5)
    assert res.verdict is Verdict.BLOCKED_ADDRESS
    assert res.stdout == ""


def test_transport_permits_rfc1918():
    assert FakeTransport().probe(_target("192.168.7.7"), timeout=5).verdict is Verdict.OK


def test_paramiko_maps_gate_and_hostkey_exceptions_to_verdicts():
    pytest.importorskip("paramiko")
    tr = rt.ParamikoTransport()
    exc = net_guard.BlockedTargetError("h", "127.0.0.1", "refused")
    assert tr._verdict_for_exception(exc) is Verdict.BLOCKED_ADDRESS
    assert tr._verdict_for_exception(rt.HostKeyMismatch("changed")) is Verdict.HOST_KEY_MISMATCH


class _PresentedKey:
    def __init__(self, blob: bytes = b"host-key-bytes", name: str = "ssh-ed25519"):
        self._blob = blob
        self._name = name

    def asbytes(self) -> bytes:
        return self._blob

    def get_name(self) -> str:
        return self._name


def _policy_for(pin):
    pytest.importorskip("paramiko")
    target = _target()
    target.host_key_fingerprint = pin
    return rt.ParamikoTransport()._host_key_policy(target)


def test_policy_records_the_key_on_first_registration():
    key = _PresentedKey()
    policy = _policy_for(None)
    assert policy.missing_host_key(None, "dgx", key) is None
    assert policy.captured["fingerprint"] == rt._sha256_fingerprint(key)
    assert policy.captured["type"] == "ssh-ed25519"
    assert policy.captured["blob_b64"] == "aG9zdC1rZXktYnl0ZXM="


def test_policy_accepts_a_key_matching_the_pin():
    key = _PresentedKey()
    policy = _policy_for(rt._sha256_fingerprint(key))
    assert policy.missing_host_key(None, "dgx", key) is None


def test_policy_refuses_a_changed_key_and_still_reports_what_it_saw():
    key = _PresentedKey(b"different-host-key")
    policy = _policy_for("SHA256:pinned-at-registration")
    with pytest.raises(rt.HostKeyMismatch) as exc:
        policy.missing_host_key(None, "dgx", key)
    assert "SHA256:pinned-at-registration" in str(exc.value)
    assert policy.captured["fingerprint"] == rt._sha256_fingerprint(key)


def test_policy_is_per_target_so_one_pin_never_leaks_into_another():
    key = _PresentedKey()
    matching = _policy_for(rt._sha256_fingerprint(key))
    other = _policy_for("SHA256:someone-elses-machine")
    assert matching.missing_host_key(None, "dgx", key) is None
    with pytest.raises(rt.HostKeyMismatch):
        other.missing_host_key(None, "dgx", key)


KEY_A = {"type": "ssh-ed25519", "blob_b64": "AAAA", "fingerprint": "SHA256:aaa"}
KEY_B = {"type": "ssh-ed25519", "blob_b64": "BBBB", "fingerprint": "SHA256:bbb"}


@pytest.fixture()
def db():
    return make_remote_plane_source(
        SimpleNamespace(machines={}, credentials={}, jobs={})
    )


def _register(db):
    return remote_machines.create_machine(db, USER, "dgx", RFC1918, 22, "me",
                                          "linux", "cluster")


def test_first_contact_pins_and_a_changed_key_never_overwrites(db):
    mid = _register(db)
    remote_machines.record_probe(db, USER, mid, "ok", host_key=KEY_A)
    assert remote_machines.get_machine(db, USER, mid)["host_key_fingerprint"] == "SHA256:aaa"
    remote_machines.record_probe(db, USER, mid, "host_key_mismatch", host_key=KEY_B)
    row = remote_machines.get_machine(db, USER, mid)
    assert row["host_key_fingerprint"] == "SHA256:aaa"
    assert row["last_verdict"] == "host_key_mismatch"


def test_mismatch_decision_has_no_accept_branch():
    assert evaluate_host_key("SHA256:aaa", "SHA256:aaa") == "match"
    assert evaluate_host_key("SHA256:aaa", "SHA256:bbb") == "mismatch"
    assert evaluate_host_key(None, "SHA256:bbb") == "record"


def test_mismatch_verdict_requires_the_explicit_retrust_action(monkeypatch):
    from agents.remote_observe import mcp_tools as obs

    obs.register_deps(object(), object())
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda db, cm, uid, mid: _target())
    set_transport(FakeTransport(force_verdict=Verdict.HOST_KEY_MISMATCH))
    res = obs.probe_machine(user_id=USER, machine_id="dgx")
    assert res["_data"]["verdict"] == Verdict.HOST_KEY_MISMATCH.value
    assert "re-trust it deliberately" in res["_data"]["next_action"]


def test_retrust_clears_the_pin_so_the_next_probe_rerecords(db):
    mid = _register(db)
    remote_machines.record_probe(db, USER, mid, "ok", host_key=KEY_A)
    remote_machines.retrust_host_key(db, USER, mid)
    row = remote_machines.get_machine(db, USER, mid)
    assert row["host_key_fingerprint"] is None
    assert row["host_key_type"] is None and row["host_key_blob"] is None
    assert evaluate_host_key(row["host_key_fingerprint"], KEY_B["fingerprint"]) == "record"
    remote_machines.record_probe(db, USER, mid, "ok", host_key=KEY_B)
    assert remote_machines.get_machine(db, USER, mid)["host_key_fingerprint"] == "SHA256:bbb"


def test_retrust_is_owner_scoped(db):
    mid = _register(db)
    remote_machines.record_probe(db, USER, mid, "ok", host_key=KEY_A)
    remote_machines.retrust_host_key(db, "someone-else", mid)
    assert remote_machines.get_machine(db, USER, mid)["host_key_fingerprint"] == "SHA256:aaa"
