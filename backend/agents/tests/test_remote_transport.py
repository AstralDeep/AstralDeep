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


def test_fake_gate_maps_resolution_failure_to_unreachable(monkeypatch):
    def _boom(host, port):
        raise rt.net_guard.HostResolutionError("no such host")

    monkeypatch.setattr(rt.net_guard, "assert_ssh_target_allowed", _boom)
    r = FakeTransport().probe(_target(), timeout=5)
    assert r.verdict is Verdict.UNREACHABLE
    assert "reachable" in r.next_action


def test_fake_consequential_timeout_surfaces_unconfirmed():
    # Fake mirrors production: a non-retryable deadline expiry has an UNKNOWN
    # outcome, so verb tests see ``unconfirmed`` rather than a re-attemptable
    # ``timeout`` (FR-036/SC-010).
    ft = FakeTransport(force_verdict=Verdict.TIMEOUT)
    assert ft.run(_target(), ["sbatch", "j.sh"], timeout=5).verdict is Verdict.UNCONFIRMED
    assert ft.run(_target(), ["squeue"], timeout=5, retryable=True).verdict is Verdict.TIMEOUT


def test_fake_stat_and_put_file_honour_the_gate():
    ft = FakeTransport()
    blocked = _target(address="169.254.169.254")
    assert ft.stat(blocked, "/data/x", timeout=5).verdict is Verdict.BLOCKED_ADDRESS
    put = ft.put_file(blocked, b"payload", "/data/x", timeout=5)
    assert put.verdict is Verdict.BLOCKED_ADDRESS
    assert ft.files == {}  # nothing written past a refused gate


def test_default_transport_constructs_without_paramiko():
    rt.set_transport(None)
    t = rt.get_transport()
    assert isinstance(t, rt.ParamikoTransport)
    rt.set_transport(None)


# --- ParamikoTransport internals (fake client injected at the paramiko seam) ---
#
# paramiko is imported lazily inside the methods, so these tests opt in via the
# ``paramiko_mod`` fixture and the module above still imports without it. The
# client object is faked; paramiko's real exception CLASSES are used so the
# exception->verdict mapping is exercised against production types.


@pytest.fixture()
def paramiko_mod():
    return pytest.importorskip("paramiko")


class _ExecChan:
    """Channel double for the run() path (adds stderr over _FakeChan)."""

    def __init__(self, chunks=(b"",), exit_status=0, stderr=b"", recv_error=None):
        self._chunks = list(chunks)
        self._stderr = bytearray(stderr)
        self.exit_status = exit_status
        self.recv_error = recv_error
        self.closed = False

    def recv(self, n):
        if self.recv_error is not None:
            raise self.recv_error
        return self._chunks.pop(0) if self._chunks else b""

    def recv_stderr_ready(self):
        return bool(self._stderr)

    def recv_stderr(self, n):
        chunk, self._stderr = bytes(self._stderr[:n]), self._stderr[n:]
        return chunk

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self.exit_status

    def close(self):
        self.closed = True


class _Stdout:
    def __init__(self, chan):
        self.channel = chan


class _FakeSFTP:
    def __init__(self, *, existing=(), stat_error=None, put_error=None):
        self.existing = set(existing)
        self.stat_error = stat_error
        self.put_error = put_error
        self.written = {}
        self.timeout = None
        self.closed = 0

    def get_channel(self):
        outer = self

        class _Chan:
            def settimeout(self, t):
                outer.timeout = t

        return _Chan()

    def stat(self, path):
        if self.stat_error is not None:
            raise self.stat_error
        if path not in self.existing:
            raise FileNotFoundError(path)
        return object()

    def putfo(self, fo, path):
        if self.put_error is not None:
            raise self.put_error
        self.written[path] = fo.read()

    def close(self):
        self.closed += 1


class _FakeSSHClient:
    """Stand-in for ``paramiko.SSHClient`` covering only what the transport calls."""

    def __init__(self, *, peer="10.33.77.11", connect_error=None, peer_error=False,
                 present_key=None, chan=None, exec_error=None, sftp=None):
        self.peer = peer
        self.connect_error = connect_error
        self.peer_error = peer_error
        self.present_key = present_key
        self.chan = chan
        self.exec_error = exec_error
        self.sftp = sftp
        self.policy = None
        self.connect_kwargs = None
        self.exec_call = None
        self.closed = 0

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.present_key is not None:  # drives the pinning policy, as sshd does
            self.policy.missing_host_key(self, kwargs["hostname"], self.present_key)
        if self.connect_error is not None:
            raise self.connect_error

    def get_transport(self):
        if self.peer_error:
            raise OSError("transport is gone")

        peer = self.peer

        class _Sock:
            def getpeername(self):
                return (peer, 22)

        class _Transport:
            sock = _Sock()

        return _Transport()

    def exec_command(self, command, timeout=None):
        self.exec_call = (command, timeout)
        if self.exec_error is not None:
            raise self.exec_error
        return None, _Stdout(self.chan), None

    def open_sftp(self):
        if isinstance(self.sftp, Exception):
            raise self.sftp
        return self.sftp

    def close(self):
        self.closed += 1


def _wire(monkeypatch, paramiko_mod, client):
    monkeypatch.setattr(paramiko_mod, "SSHClient", lambda: client)
    return client


def _stub_first_key_class(monkeypatch, paramiko_mod, *, result=None, error=None):
    """Replace Ed25519Key — the first class ``_load_private_key`` tries."""
    seen = {}

    class _Stub:
        @staticmethod
        def from_private_key(fileobj, password=None):
            seen["pem"] = fileobj.read()
            seen["password"] = password
            if error is not None:
                raise error
            return result

    monkeypatch.setattr(paramiko_mod, "Ed25519Key", _Stub)
    return seen


# --- private-key loading ------------------------------------------------------

def test_load_private_key_returns_the_first_supported_class(monkeypatch, paramiko_mod):
    sentinel = object()
    seen = _stub_first_key_class(monkeypatch, paramiko_mod, result=sentinel)
    key = rt.ParamikoTransport()._load_private_key("PEM-BYTES", "hunter2")
    assert key is sentinel
    assert seen == {"pem": "PEM-BYTES", "password": "hunter2"}


def test_load_private_key_propagates_password_required(monkeypatch, paramiko_mod):
    # A missing passphrase is a CREDENTIAL problem: it must not be swallowed into
    # the "try the next key class" loop.
    _stub_first_key_class(monkeypatch, paramiko_mod,
                          error=paramiko_mod.PasswordRequiredException("encrypted"))
    with pytest.raises(paramiko_mod.PasswordRequiredException):
        rt.ParamikoTransport()._load_private_key("PEM", None)


def test_load_private_key_rejects_unusable_material(paramiko_mod):
    with pytest.raises(paramiko_mod.SSHException) as exc:
        rt.ParamikoTransport()._load_private_key("not-a-key-at-all", None)
    assert "unsupported or invalid private key" in str(exc.value)
    for cls in ("Ed25519Key", "ECDSAKey", "RSAKey"):  # every class was tried
        assert cls in str(exc.value)


# --- exception -> verdict mapping (FR-034) ------------------------------------

def _exc(paramiko_mod, name):
    class _K:
        def get_base64(self):
            return "AAAA"

    return {
        "blocked": rt.net_guard.BlockedTargetError("h", "127.0.0.1", "refused"),
        "resolution": rt.net_guard.HostResolutionError("no DNS"),
        "mismatch": rt.HostKeyMismatch("changed"),
        "bad_host_key": paramiko_mod.BadHostKeyException("h", _K(), _K()),
        "auth": paramiko_mod.AuthenticationException("bad creds"),
        "password_required": paramiko_mod.PasswordRequiredException("encrypted"),
        "sock_timeout": socket.timeout("timed out"),
        "timeout": TimeoutError("deadline"),
        "permission": PermissionError("EACCES"),
        "ssh": paramiko_mod.SSHException("negotiation failed"),
        "connection": ConnectionRefusedError("refused"),
        "os": OSError("network unreachable"),
    }[name]


@pytest.mark.parametrize("name,expected", [
    ("blocked", Verdict.BLOCKED_ADDRESS),
    ("resolution", Verdict.UNREACHABLE),
    ("mismatch", Verdict.HOST_KEY_MISMATCH),
    ("bad_host_key", Verdict.HOST_KEY_MISMATCH),
    ("auth", Verdict.AUTH_FAILED),
    ("password_required", Verdict.AUTH_FAILED),
    ("sock_timeout", Verdict.TIMEOUT),
    ("timeout", Verdict.TIMEOUT),
    ("permission", Verdict.PERMISSION_DENIED_REMOTE),
    ("ssh", Verdict.UNREACHABLE),
    ("connection", Verdict.UNREACHABLE),
    ("os", Verdict.UNREACHABLE),
])
def test_verdict_for_exception_maps_every_known_condition(paramiko_mod, name, expected):
    assert rt.ParamikoTransport()._verdict_for_exception(_exc(paramiko_mod, name)) is expected


def test_verdict_for_exception_returns_none_for_an_unknown_bug(paramiko_mod):
    # An unmapped exception is a transport BUG, never a remote verdict.
    assert rt.ParamikoTransport()._verdict_for_exception(ValueError("bug")) is None


def test_result_for_exception_raises_on_an_unmapped_error(paramiko_mod):
    with pytest.raises(rt.RemoteTransportError) as exc:
        rt.ParamikoTransport()._result_for_exception(_target(), ValueError("bug"), retryable=True)
    assert "dgx" in str(exc.value)


def test_consequential_timeout_becomes_unconfirmed(paramiko_mod):
    tr = rt.ParamikoTransport()
    consequential = tr._result_for_exception(_target(), TimeoutError("x"), retryable=False)
    assert consequential.verdict is Verdict.UNCONFIRMED
    assert "before re-issuing" in consequential.next_action
    read = tr._result_for_exception(_target(), TimeoutError("x"), retryable=True)
    assert read.verdict is Verdict.TIMEOUT and read.retryable is True


# --- _connect: gate, credentials, anti-rebinding ------------------------------

def test_connect_password_path_never_uses_agent_or_known_hosts(monkeypatch, paramiko_mod):
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient())
    got, policy = rt.ParamikoTransport()._connect(_target(), timeout=7)
    assert got is client and policy is client.policy
    kw = client.connect_kwargs
    assert kw["hostname"] == "10.33.77.11" and kw["port"] == 22 and kw["username"] == "me"
    assert kw["password"] == "pw" and "pkey" not in kw
    assert kw["allow_agent"] is False and kw["look_for_keys"] is False
    assert kw["timeout"] == kw["banner_timeout"] == kw["auth_timeout"] == 7


def test_connect_ssh_key_path_loads_the_pkey(monkeypatch, paramiko_mod):
    sentinel = object()
    _stub_first_key_class(monkeypatch, paramiko_mod, result=sentinel)
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient())
    rt.ParamikoTransport()._connect(
        _target(cred_type="ssh_key", secret="PEM", passphrase="pp"), timeout=5)
    assert client.connect_kwargs["pkey"] is sentinel
    assert "password" not in client.connect_kwargs


def test_connect_unloadable_key_is_auth_failed_not_unreachable(monkeypatch, paramiko_mod):
    # Bad key material is a CREDENTIAL problem; surfacing it as ``unreachable``
    # would send the user chasing the network instead of the credential.
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient())
    res = rt.ParamikoTransport().run(
        _target(cred_type="ssh_key", secret="not-a-key"), ["true"], timeout=5)
    assert res.verdict is Verdict.AUTH_FAILED
    assert "credential" in res.next_action


def test_connect_refuses_a_peer_outside_the_vetted_set(monkeypatch, paramiko_mod):
    # The FR-019 anti-rebinding step: paramiko re-resolved the name to an address
    # the gate never vetted.
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient(peer="203.0.113.9"))
    with pytest.raises(rt.net_guard.BlockedTargetError) as exc:
        rt.ParamikoTransport()._connect(_target(), timeout=5)
    assert "rebinding" in str(exc.value)
    assert client.closed == 1  # the socket is closed, not left dangling


def test_connect_treats_an_unreadable_peer_as_unverifiable(monkeypatch, paramiko_mod):
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(peer_error=True))
    with pytest.raises(rt.net_guard.BlockedTargetError):
        rt.ParamikoTransport()._connect(_target(), timeout=5)


def test_connect_runs_the_egress_gate_before_any_socket(monkeypatch, paramiko_mod):
    def _never(*_a, **_kw):
        raise AssertionError("client constructed after a refused gate")

    monkeypatch.setattr(paramiko_mod, "SSHClient", _never)
    with pytest.raises(rt.net_guard.BlockedTargetError):
        rt.ParamikoTransport()._connect(_target(address="169.254.169.254"), timeout=5)


# --- run() --------------------------------------------------------------------

def test_run_assembles_stdout_stderr_and_exit_status(monkeypatch, paramiko_mod):
    chan = _ExecChan(chunks=[b"JOBID|STATE\n", b""], exit_status=2, stderr=b"boom\n")
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient(chan=chan))
    res = rt.ParamikoTransport().run(_target(), ["squeue", "--me"], timeout=9, retryable=True)
    assert res.verdict is Verdict.OK
    assert res.stdout == "JOBID|STATE\n" and res.stderr == "boom\n"
    assert res.exit_status == 2 and res.data == {"truncated": False}
    assert res.retryable is True
    assert client.exec_call == (rt.build_login_command(["squeue", "--me"]), 9)
    assert client.closed == 1  # closed in finally


def test_run_truncated_output_drops_exit_status_and_stderr(monkeypatch, paramiko_mod):
    chan = _ExecChan(chunks=[b"x" * (rt.MAX_OUTPUT_BYTES + 5)], stderr=b"ignored")
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(chan=chan))
    res = rt.ParamikoTransport().run(_target(), ["cat", "big"], timeout=5)
    assert res.data["truncated"] is True
    assert res.exit_status is None and res.stderr == ""
    assert len(res.stdout) == rt.MAX_OUTPUT_BYTES


def test_run_consequential_timeout_is_unconfirmed_and_still_closes(monkeypatch, paramiko_mod):
    chan = _ExecChan(recv_error=socket.timeout("stalled"))
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient(chan=chan))
    res = rt.ParamikoTransport().run(_target(), ["sbatch", "job.sh"], timeout=1)
    assert res.verdict is Verdict.UNCONFIRMED
    assert client.closed == 1


def test_run_host_key_mismatch_propagates_from_the_policy(monkeypatch, paramiko_mod):
    client = _FakeSSHClient(present_key=_FakeKey(b"new-key"), chan=_ExecChan())
    _wire(monkeypatch, paramiko_mod, client)
    res = rt.ParamikoTransport().run(
        _target(host_key_fingerprint="SHA256:pinned"), ["true"], timeout=5)
    assert res.verdict is Verdict.HOST_KEY_MISMATCH
    assert "re-trust it deliberately" in res.next_action


def test_run_reraises_an_unexpected_transport_bug(monkeypatch, paramiko_mod):
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(connect_error=ValueError("bug")))
    with pytest.raises(rt.RemoteTransportError):
        rt.ParamikoTransport().run(_target(), ["true"], timeout=5)


def test_run_validates_argv_before_connecting(monkeypatch, paramiko_mod):
    def _never(*_a, **_kw):
        raise AssertionError("connected despite invalid argv")

    monkeypatch.setattr(paramiko_mod, "SSHClient", _never)
    with pytest.raises(ValueError):
        rt.ParamikoTransport().run(_target(), [], timeout=5)


# --- stat() / put_file() / probe() --------------------------------------------

def test_stat_reports_existence_and_bounds_the_channel(monkeypatch, paramiko_mod):
    sftp = _FakeSFTP(existing={"/data/x"})
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(sftp=sftp))
    res = rt.ParamikoTransport().stat(_target(), "/data/x", timeout=4)
    assert res.verdict is Verdict.OK and res.retryable is True
    assert res.data == {"exists": True, "path": "/data/x"}
    assert sftp.timeout == 4 and sftp.closed == 1


def test_stat_missing_path_is_ok_with_exists_false(monkeypatch, paramiko_mod):
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(sftp=_FakeSFTP()))
    res = rt.ParamikoTransport().stat(_target(), "/data/nope", timeout=4)
    assert res.verdict is Verdict.OK and res.data["exists"] is False


def test_stat_maps_remote_permission_denied(monkeypatch, paramiko_mod):
    sftp = _FakeSFTP(stat_error=PermissionError("EACCES"))
    client = _wire(monkeypatch, paramiko_mod, _FakeSSHClient(sftp=sftp))
    res = rt.ParamikoTransport().stat(_target(), "/root/secret", timeout=4)
    assert res.verdict is Verdict.PERMISSION_DENIED_REMOTE and res.retryable is True
    assert sftp.closed == 1 and client.closed == 1  # sftp + client both released


def test_put_file_writes_unconditionally(monkeypatch, paramiko_mod):
    sftp = _FakeSFTP(existing={"/data/x"})
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(sftp=sftp))
    res = rt.ParamikoTransport().put_file(_target(), b"payload", "/data/x", timeout=6)
    assert res.verdict is Verdict.OK and res.retryable is False
    assert res.data == {"path": "/data/x", "bytes": 7}
    assert sftp.written["/data/x"] == b"payload" and sftp.timeout == 6


def test_put_file_maps_an_io_failure_to_unreachable(monkeypatch, paramiko_mod):
    sftp = _FakeSFTP(put_error=OSError("no space left on device"))
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(sftp=sftp))
    res = rt.ParamikoTransport().put_file(_target(), b"payload", "/data/x", timeout=6)
    assert res.verdict is Verdict.UNREACHABLE and res.retryable is False
    assert sftp.closed == 1


def test_probe_returns_the_captured_host_key(monkeypatch, paramiko_mod):
    key = _FakeKey(b"first-contact-key")
    _wire(monkeypatch, paramiko_mod, _FakeSSHClient(present_key=key))
    res = rt.ParamikoTransport().probe(_target(), timeout=5)  # no pin yet => record
    assert res.verdict is Verdict.OK and res.data == {"authenticated": True}
    assert res.host_key["fingerprint"] == _sha256_fingerprint(key)
    assert res.host_key["type"] == "ssh-ed25519"


def test_probe_reports_auth_failure(monkeypatch, paramiko_mod):
    _wire(monkeypatch, paramiko_mod,
          _FakeSSHClient(connect_error=paramiko_mod.AuthenticationException("no")))
    res = rt.ParamikoTransport().probe(_target(), timeout=5)
    assert res.verdict is Verdict.AUTH_FAILED and res.retryable is True
    assert res.host_key is None


# --- bounded read / stderr drain edge cases (FR-021, FR-035) ------------------

def test_read_bounded_raises_when_the_deadline_passes_mid_stream(monkeypatch):
    ticks = [0.0, 99.0]
    monkeypatch.setattr(rt.time, "monotonic", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[0])
    chan = _ExecChan(chunks=[b"partial output"])
    with pytest.raises(TimeoutError):
        rt.ParamikoTransport()._read_bounded(chan, timeout=1)
    assert chan.closed is True


def test_drain_stderr_is_bounded_by_the_cap():
    chan = _ExecChan(stderr=b"E" * 100)
    assert rt.ParamikoTransport()._drain_stderr(chan, cap=8) == "EEEEEEEE"


def test_drain_stderr_stops_on_an_empty_chunk():
    class _EmptyStderr(_ExecChan):
        def recv_stderr_ready(self):
            return True

        def recv_stderr(self, n):
            return b""

    assert rt.ParamikoTransport()._drain_stderr(_EmptyStderr()) == ""


def test_drain_stderr_never_raises():
    class _Broken:
        def recv_stderr_ready(self):
            raise OSError("channel closed")

    assert rt.ParamikoTransport()._drain_stderr(_Broken()) == ""


def test_peer_in_resolved_skips_unparseable_vetted_entries():
    assert _peer_in_resolved("10.0.0.1", ["not-an-ip", "10.0.0.1"]) is True
    assert _peer_in_resolved("10.0.0.1", ["not-an-ip"]) is False
