"""Unit tests for the SSH transport boundary (feature 063, orchestrator/remote_transport.py).

Covers the login-shell argv builder (FR-022 injection safety), the FR-034 verdict
vocabulary, host-key pinning logic (FR-020), the deadline-bounded read (FR-021), and
FakeTransport behaviour (FR-050 test seam). Stdlib-only; paramiko is imported lazily
by ParamikoTransport, so this module imports and runs without it. The paramiko-backed
connect/exec paths themselves are exercised by the in-container + live checklist.
"""
import base64
import hashlib
import socket

import pytest

from orchestrator import remote_transport as rt
from orchestrator.remote_transport import (
    FakeTransport,
    MachineTarget,
    Verdict,
    build_login_command,
    evaluate_host_key,
    _sha256_fingerprint,
    _peer_in_resolved,
)


def _target(**kw):
    d = dict(machine_id="m1", label="dgx", address="10.33.77.11", port=22,
             username="me", cred_type="password", secret="pw")
    d.update(kw)
    return MachineTarget(**d)


# --- argv builder (FR-022) ----------------------------------------------------

def test_build_login_command_basic():
    cmd = build_login_command(["squeue", "--me", "-o", "%i|%T"])
    assert cmd == "bash -lc 'exec \"$@\"' _ squeue --me -o '%i|%T'"


def test_build_login_command_quotes_injection_arg():
    nasty = "a; echo PWNED $(whoami) `id`"
    cmd = build_login_command(["/bin/echo", nasty])
    assert cmd.startswith("bash -lc 'exec \"$@\"' _ /bin/echo ")
    assert "'a; echo PWNED $(whoami) `id`'" in cmd


def test_build_login_command_rejects_empty():
    with pytest.raises(ValueError):
        build_login_command([])


def test_build_login_command_rejects_non_str():
    with pytest.raises(ValueError):
        build_login_command(["ls", 5])  # type: ignore[list-item]


# --- verdict vocabulary (FR-034) ---------------------------------------------

def test_verdict_vocabulary_is_exact():
    expected = {
        "ok", "partial", "unreachable", "auth_failed", "host_key_mismatch",
        "credential_not_configured", "credential_undecryptable", "blocked_address",
        "permission_denied_remote", "quota_exhausted", "mfa_required", "timeout",
        "not_found", "invalid_argument", "confirmation_required",
        "confirmation_expired", "unconfirmed", "unattended_refused",
    }
    assert {v.value for v in Verdict} == expected


# --- host-key pinning logic (FR-020) -----------------------------------------

def test_evaluate_host_key_branches():
    assert evaluate_host_key(None, "SHA256:abc") == "record"      # first registration
    assert evaluate_host_key("SHA256:abc", "SHA256:abc") == "match"
    assert evaluate_host_key("SHA256:abc", "SHA256:xyz") == "mismatch"  # changed => refuse


class _FakeKey:
    def __init__(self, blob: bytes):
        self._blob = blob

    def asbytes(self) -> bytes:
        return self._blob

    def get_name(self) -> str:
        return "ssh-ed25519"


def test_sha256_fingerprint_matches_openssh_format():
    fp = _sha256_fingerprint(_FakeKey(b"host-key-bytes"))
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(b"host-key-bytes").digest()).decode().rstrip("=")
    assert fp == expected
    assert fp.startswith("SHA256:") and "=" not in fp


# --- anti-rebinding peer check (FR-019) --------------------------------------

def test_peer_in_resolved_normalises():
    assert _peer_in_resolved("10.33.77.11", ["10.33.77.11"]) is True
    assert _peer_in_resolved("127.0.0.1", ["10.33.77.11"]) is False  # rebind to blocked addr
    assert _peer_in_resolved("::1", ["0:0:0:0:0:0:0:1"]) is True     # textual variance normalised
    assert _peer_in_resolved(None, ["10.0.0.1"]) is False            # unreadable peer => fail-closed
    assert _peer_in_resolved("garbage", ["10.0.0.1"]) is False


# --- deadline-bounded read + exit (FR-021, the HIGH finding) ------------------

class _FakeChan:
    """Minimal channel double for _read_bounded/_await_exit (no paramiko)."""

    def __init__(self, chunks, exit_status=0, raise_timeout=False, exit_ready=True):
        self._chunks = list(chunks)
        self.exit_status = exit_status
        self._raise_timeout = raise_timeout
        self._exit_ready = exit_ready
        self.closed = False

    def recv(self, n):
        if self._raise_timeout:
            raise socket.timeout()
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True

    def exit_status_ready(self):
        return self._exit_ready

    def recv_exit_status(self):
        return self.exit_status


def test_read_bounded_normal():
    out, trunc = rt.ParamikoTransport()._read_bounded(_FakeChan([b"JOBID|STATE\n", b""]), timeout=5)
    assert out == b"JOBID|STATE\n"
    assert trunc is False


def test_read_bounded_truncates_over_cap_and_closes():
    big = b"x" * (rt.MAX_OUTPUT_BYTES + 10)
    chan = _FakeChan([big])
    out, trunc = rt.ParamikoTransport()._read_bounded(chan, timeout=5)
    assert trunc is True
    assert len(out) == rt.MAX_OUTPUT_BYTES
    assert chan.closed is True  # closed to unblock a remote stuck on write()


def test_read_bounded_times_out_on_stall():
    chan = _FakeChan([], raise_timeout=True)
    with pytest.raises(TimeoutError):
        rt.ParamikoTransport()._read_bounded(chan, timeout=1)
    assert chan.closed is True


def test_await_exit_returns_status():
    assert rt.ParamikoTransport()._await_exit(_FakeChan([], exit_status=3), timeout=5, truncated=False) == 3


def test_await_exit_none_when_truncated():
    assert rt.ParamikoTransport()._await_exit(_FakeChan([]), timeout=5, truncated=True) is None


def test_await_exit_times_out():
    chan = _FakeChan([], exit_ready=False)
    with pytest.raises(TimeoutError):
        rt.ParamikoTransport()._await_exit(chan, timeout=0.2, truncated=False)
    assert chan.closed is True


# --- FakeTransport behaviour (FR-050) ----------------------------------------

def test_fake_run_ok_is_non_retryable_by_default():
    ft = FakeTransport(command_stdout="JOBID|STATE\n", command_exit=0)
    r = ft.run(_target(), ["squeue", "--me"], timeout=5)
    assert r.verdict is Verdict.OK
    assert r.stdout.startswith("JOBID")
    assert r.retryable is False


def test_fake_run_validates_argv_before_gate():
    # argv validation happens first in BOTH Fake and Paramiko (fail fast, parity).
    ft = FakeTransport()
    with pytest.raises(ValueError):
        ft.run(_target(address="127.0.0.1"), [], timeout=5)  # bad argv beats the blocked addr


def test_fake_blocked_address():
    r = FakeTransport().run(_target(address="127.0.0.1"), ["true"], timeout=5)
    assert r.verdict is Verdict.BLOCKED_ADDRESS


def test_fake_unreachable_and_auth_failed():
    assert FakeTransport(reachable=False).probe(_target(), timeout=5).verdict is Verdict.UNREACHABLE
    assert FakeTransport(authenticated=False).probe(_target(), timeout=5).verdict is Verdict.AUTH_FAILED


def test_fake_stat_and_put_file_writes_unconditionally():
    # The destructive if_exists decision is the confirmation gate's job (via stat),
    # not the transport's — put_file always writes.
    ft = FakeTransport(files={"/data/x": b"old"})
    assert ft.stat(_target(), "/data/x", timeout=5).data["exists"] is True
    assert ft.stat(_target(), "/data/y", timeout=5).data["exists"] is False
    fresh = ft.put_file(_target(), b"new", "/data/y", timeout=5)
    assert fresh.verdict is Verdict.OK and ft.files["/data/y"] == b"new"
    clobber = ft.put_file(_target(), b"newer", "/data/x", timeout=5)
    assert clobber.verdict is Verdict.OK and ft.files["/data/x"] == b"newer"


def test_fake_probe_captures_host_key():
    r = FakeTransport().probe(_target(), timeout=5)
    assert r.verdict is Verdict.OK
    assert r.host_key and r.host_key["fingerprint"].startswith("SHA256:")


def test_force_verdict():
    ft = FakeTransport(force_verdict=Verdict.QUOTA_EXHAUSTED)
    assert ft.run(_target(), ["sbatch", "job.sh"], timeout=5).verdict is Verdict.QUOTA_EXHAUSTED


def test_default_transport_constructs_without_paramiko():
    rt.set_transport(None)
    t = rt.get_transport()
    assert isinstance(t, rt.ParamikoTransport)
    rt.set_transport(None)
