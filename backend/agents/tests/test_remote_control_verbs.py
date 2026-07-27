"""Behaviour tests for the 8 mutating verbs of remote-control-1 (feature 063).

Uses the transport test seam (FakeTransport) + monkeypatched machine resolution
so no DB / SSH / network is touched. Asserts the exact argv each verb builds
(injection-safe discrete vectors — FR-022), exit-status interpretation (a non-zero
remote exit is a real failure the user sees), and the argument-shape guards
(FR-022/US5-3). The confirmation gate is NOT in play here — these tests call the
verb functions directly, i.e. the state AFTER an approval has been consumed.
"""
from __future__ import annotations

import pytest

from agents.remote_control import mcp_tools as ctl
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport

USER = "user-1"


def _target():
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    ctl.register_deps(object(), object())
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda db, cm, uid, mid: _target())
    yield
    set_transport(None)


def _fake(**kw):
    t = FakeTransport(**kw)
    set_transport(t)
    return t


def _argvs(t):
    return [c["argv"] for c in t.calls if c["op"] == "run"]


def _verdict(res):
    return (res.get("_data") or {}).get("verdict")


# ── argv construction ───────────────────────────────────────────────────────────

def test_make_directory_builds_mkdir_p():
    t = _fake(command_exit=0)
    res = ctl.make_directory(user_id=USER, machine_id="dgx", path="/tmp/x")
    assert _argvs(t) == [["mkdir", "-p", "/tmp/x"]]
    assert res["_data"]["exit_status"] == 0


def test_remove_path_recursive_and_nonrecursive_argv():
    t = _fake(command_exit=0)
    ctl.remove_path(user_id=USER, machine_id="dgx", path="/data", recursive=True)
    ctl.remove_path(user_id=USER, machine_id="dgx", path="/f", recursive=False)
    assert _argvs(t) == [["rm", "-r", "-f", "/data"], ["rm", "-f", "/f"]]


def test_cancel_job_builds_scancel():
    t = _fake(command_exit=0)
    ctl.cancel_job(user_id=USER, machine_id="dgx", job_id="12345")
    assert _argvs(t) == [["scancel", "12345"]]


def test_control_service_builds_systemctl():
    t = _fake(command_exit=0)
    ctl.control_service(user_id=USER, machine_id="dgx", service_name="nginx", action="restart")
    assert _argvs(t) == [["systemctl", "restart", "nginx"]]


def test_signal_process_builds_kill():
    t = _fake(command_exit=0)
    ctl.signal_process(user_id=USER, machine_id="dgx", pid="999", signal="KILL")
    assert _argvs(t) == [["kill", "-KILL", "999"]]


def test_manage_package_detects_manager_then_acts():
    # FakeTransport returns the same stdout for every run; the `which` probe finds
    # apt-get, then the install runs.
    t = _fake(command_stdout="/usr/bin/apt-get", command_exit=0)
    ctl.manage_package(user_id=USER, machine_id="dgx", package_name="vim", action="install")
    assert _argvs(t) == [["which", "apt-get", "dnf", "yum", "zypper"],
                         ["apt-get", "install", "-y", "vim"]]


def test_manage_package_no_manager_is_invalid_argument():
    t = _fake(command_stdout="", command_exit=0)
    res = ctl.manage_package(user_id=USER, machine_id="dgx", package_name="vim", action="remove")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value
    assert _argvs(t) == [["which", "apt-get", "dnf", "yum", "zypper"]]  # never ran the remove


def test_submit_job_parsable_and_flags():
    t = _fake(command_stdout="98765", command_exit=0)
    res = ctl.submit_job(user_id=USER, machine_id="dgx", script_path="/home/me/run.sh",
                         partition="gpu", nodes=2, gpus=4, time_limit="01:00:00")
    (argv,) = _argvs(t)
    assert argv[0:2] == ["sbatch", "--parsable"] and argv[-1] == "/home/me/run.sh"
    assert "--partition=gpu" in argv and "--nodes=2" in argv and "--gpus=4" in argv
    assert res["_data"]["job_id"] == "98765"


# ── exit-status interpretation ───────────────────────────────────────────────────

def test_nonzero_exit_is_a_failure_not_a_success():
    _fake(command_exit=1)
    res = ctl.remove_path(user_id=USER, machine_id="dgx", path="/data")
    assert _verdict(res) == Verdict.PARTIAL.value  # ran, but the remote rejected it


def test_transport_unreachable_surfaces_the_verdict():
    _fake(reachable=False)
    res = ctl.cancel_job(user_id=USER, machine_id="dgx", job_id="1")
    assert _verdict(res) == Verdict.UNREACHABLE.value


# ── argument-shape guards (no transport call on a bad arg) ───────────────────────

def test_make_directory_rejects_relative_path():
    t = _fake(command_exit=0)
    res = ctl.make_directory(user_id=USER, machine_id="dgx", path="relative/dir")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


def test_cancel_job_rejects_non_numeric_job_id():
    t = _fake(command_exit=0)
    res = ctl.cancel_job(user_id=USER, machine_id="dgx", job_id="abc; rm -rf /")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


def test_control_service_rejects_unknown_action():
    t = _fake(command_exit=0)
    res = ctl.control_service(user_id=USER, machine_id="dgx", service_name="nginx", action="nuke")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


def test_service_name_with_metacharacters_is_refused():
    t = _fake(command_exit=0)
    res = ctl.control_service(user_id=USER, machine_id="dgx", service_name="a;b", action="stop")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


def test_signal_process_rejects_unknown_signal():
    t = _fake(command_exit=0)
    res = ctl.signal_process(user_id=USER, machine_id="dgx", pid="1", signal="HUP")
    assert _verdict(res) == Verdict.INVALID_ARGUMENT.value and _argvs(t) == []


# ── upload_file (attachment → bytes → SFTP) ──────────────────────────────────────

def test_upload_file_resolves_attachment_and_puts(monkeypatch, tmp_path):
    from types import SimpleNamespace
    blob = tmp_path / "f.txt"
    blob.write_bytes(b"hello")
    monkeypatch.setattr("orchestrator.attachments.repository.AttachmentRepository.get_by_id",
                        lambda self, aid, uid: SimpleNamespace(filename="f.txt", size_bytes=5))
    monkeypatch.setattr("orchestrator.attachments.store.read_path",
                        lambda uid, aid, fn: blob)
    t = _fake()
    res = ctl.upload_file(user_id=USER, machine_id="dgx", attachment_id="a1", remote_path="/dest/f.txt")
    assert res["_data"]["bytes"] == 5
    assert any(c["op"] == "put_file" and c["path"] == "/dest/f.txt" for c in t.calls)


def test_upload_file_missing_attachment_is_not_found(monkeypatch):
    monkeypatch.setattr("orchestrator.attachments.repository.AttachmentRepository.get_by_id",
                        lambda self, aid, uid: None)
    _fake()
    res = ctl.upload_file(user_id=USER, machine_id="dgx", attachment_id="nope", remote_path="/d/f")
    assert _verdict(res) == Verdict.NOT_FOUND.value
