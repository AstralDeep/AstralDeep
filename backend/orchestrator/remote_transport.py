"""Injectable SSH transport boundary for the remote-compute agents (feature 063).

All remote access flows through the single ``RemoteTransport`` protocol so the whole
capability is exercisable in tests with no real machine, no SSH server, and no
network (spec FR-050, SC-015). Production wires ``ParamikoTransport``; tests wire
``FakeTransport`` via ``set_transport``.

Design invariants (see specs/063-remote-compute-agents/contracts/transport.md):
- Commands run as a **discrete argv vector** through a **login shell**
  (``bash -lc 'exec "$@"' _ …``) — proven live to resolve Slurm on a Bright cluster
  AND to neutralise shell metacharacters, reconciling the login-shell requirement
  with FR-022 (no shell-string assembly).
- A connection-time egress gate (``shared.net_guard``) runs before any socket opens,
  AND the peer address actually connected to is verified against the gate's resolved
  set (FR-019 anti-rebinding step).
- Host identity is pinned by SHA256 fingerprint: recorded at first registration,
  verified on every connection; a mismatch refuses (FR-020). No auto-accept of a
  *changed* identity on any path.
- Every remote operation is bounded by a wall-clock deadline; a hang surfaces the
  ``timeout`` verdict, never an indefinite wait (FR-021).
- ``paramiko`` is imported **lazily** (inside methods) so this module, the verdict
  vocabulary, the argv builder, the pure host-key/bounded-read helpers, and
  ``FakeTransport`` import and test without it.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import shlex
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Protocol, Tuple, runtime_checkable

from shared import net_guard

# Hard ceiling on bytes read from a single remote command; verbs bound their own
# typed fields far tighter (FR-040). This only prevents a runaway stream.
MAX_OUTPUT_BYTES = 1_048_576
_RECV_CHUNK = 65536


class Verdict(str, Enum):
    """The fixed result vocabulary (spec FR-034 / contracts/result-vocabulary.md)."""

    OK = "ok"
    PARTIAL = "partial"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    CREDENTIAL_NOT_CONFIGURED = "credential_not_configured"
    CREDENTIAL_UNDECRYPTABLE = "credential_undecryptable"
    BLOCKED_ADDRESS = "blocked_address"
    PERMISSION_DENIED_REMOTE = "permission_denied_remote"
    QUOTA_EXHAUSTED = "quota_exhausted"
    MFA_REQUIRED = "mfa_required"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENT = "invalid_argument"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    UNCONFIRMED = "unconfirmed"
    UNATTENDED_REFUSED = "unattended_refused"


class RemoteTransportError(Exception):
    """An unexpected transport-layer bug (not a remote condition).

    Known remote conditions are returned as a ``RemoteResult`` with a vocabulary
    verdict; only genuinely unexpected errors raise this, for the dispatch layer's
    generic handler to log — a bug must never masquerade as a remote verdict.
    """


class HostKeyMismatch(Exception):
    """Raised internally when a presented host key does not match the pinned one."""


@dataclass
class MachineTarget:
    """Everything the transport needs to reach one machine — built server-side from
    a ``remote_machine`` row + decrypted ``machine_credential``. The model never
    supplies any of these fields (FR-018)."""

    machine_id: str
    label: str
    address: str
    port: int
    username: str
    cred_type: str  # 'ssh_key' | 'password'
    secret: str = ""  # decrypted PEM or password (transient in memory — FR-014)
    passphrase: Optional[str] = None
    host_key_fingerprint: Optional[str] = None  # 'SHA256:...'; None => first registration


@dataclass
class RemoteResult:
    """A transport outcome. ``data`` carries typed fields for the verb layer; ``stdout``
    is internal (verbs parse it into typed fields and never pass it to the model)."""

    verdict: Verdict
    machine: str  # label or id, for user-facing messages (FR-035)
    next_action: str = ""
    data: Dict = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""  # bounded; surfaced by verbs to explain a non-zero exit (FR-035)
    exit_status: Optional[int] = None
    host_key: Optional[Dict] = None  # {'type','blob_b64','fingerprint'} captured on first probe
    retryable: bool = False  # consequential default; reads may override True

    @property
    def ok(self) -> bool:
        return self.verdict == Verdict.OK


def build_login_command(argv: List[str]) -> str:
    """Wrap a discrete argv vector for a login-shell, injection-safe remote exec.

    Produces ``bash -lc 'exec "$@"' _ <shlex-quoted argv...>``. The single sshd-exec
    parse is covered by ``shlex.quote`` per token; ``exec "$@"`` then re-vectorises so
    metacharacters in an argument are never interpreted (proven live). ``-l`` sources
    the login profile so cluster tools (Slurm via env-modules) resolve on PATH.
    """
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "bash -lc 'exec \"$@\"' _ " + quoted


def _sha256_fingerprint(key) -> str:
    """OpenSSH-style ``SHA256:<base64-nopad>`` fingerprint of a paramiko PKey."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def evaluate_host_key(expected_fp: Optional[str], presented_fp: str) -> str:
    """Pure host-key decision (unit-testable without paramiko).

    Returns 'record' (first registration — no pin yet), 'match' (pin verified), or
    'mismatch' (a changed/unknown key — MUST refuse). There is no path that returns
    an accept for a changed pin (FR-020).
    """
    if expected_fp is None:
        return "record"
    if presented_fp == expected_fp:
        return "match"
    return "mismatch"


def _peer_in_resolved(peer_ip: Optional[str], resolved: List[str]) -> bool:
    """True iff ``peer_ip`` normalises to one of the gate-vetted ``resolved`` addresses.

    Fail-closed: an unparseable peer is treated as not-in-set.
    """
    if not peer_ip:
        return False
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for addr in resolved:
        try:
            if ipaddress.ip_address(addr) == peer:
                return True
        except ValueError:
            continue
    return False


@runtime_checkable
class RemoteTransport(Protocol):
    """The single seam. Every method returns a ``RemoteResult`` with a vocabulary
    verdict for known remote conditions; unexpected bugs raise ``RemoteTransportError``.

    ``put_file`` writes unconditionally: the destructive ``if_exists`` decision for
    ``upload_file`` belongs to the confirmation dispatch gate (via ``stat``), not the
    transport (see contracts/confirmation.md — gate, not tool)."""

    def run(self, target: MachineTarget, argv: List[str], *, timeout: float,
            retryable: bool = False) -> RemoteResult: ...

    def put_file(self, target: MachineTarget, data: bytes, remote_path: str, *,
                 timeout: float) -> RemoteResult: ...

    def stat(self, target: MachineTarget, remote_path: str, *, timeout: float) -> RemoteResult: ...

    def probe(self, target: MachineTarget, *, timeout: float) -> RemoteResult: ...


class ParamikoTransport:
    """Production transport. Imports paramiko lazily so the module loads without it."""

    def _host_key_policy(self, target: MachineTarget):
        import paramiko

        expected = target.host_key_fingerprint

        class _Policy(paramiko.MissingHostKeyPolicy):
            captured: Optional[Dict] = None

            def missing_host_key(self, client, hostname, key):
                fp = _sha256_fingerprint(key)
                self.captured = {
                    "type": key.get_name(),
                    "blob_b64": base64.b64encode(key.asbytes()).decode("ascii"),
                    "fingerprint": fp,
                }
                decision = evaluate_host_key(expected, fp)
                if decision == "mismatch":
                    raise HostKeyMismatch(f"{hostname}: {fp} != pinned {expected}")
                # 'record' (first registration — the user's deliberate act, spec R6)
                # or 'match' => accept. There is NO accept path for a changed pin.
                return

        return _Policy()

    def _load_private_key(self, pem: str, passphrase: Optional[str]):
        import paramiko
        from io import StringIO

        errors = []
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return cls.from_private_key(StringIO(pem), password=passphrase)
            except paramiko.PasswordRequiredException:
                raise  # missing passphrase — a credential problem (mapped to AUTH_FAILED)
            except paramiko.SSHException as e:
                errors.append(f"{cls.__name__}: {e}")
                continue
        raise paramiko.SSHException("unsupported or invalid private key (" + "; ".join(errors) + ")")

    def _connect(self, target: MachineTarget, timeout: float):
        """Open an authenticated SSH client. Runs the egress gate first, verifies the
        connected peer is in the vetted set (anti-rebinding), and pins the host key.
        Returns (client, policy) — ``policy.captured`` holds a first-probe key."""
        import paramiko

        # Egress gate BEFORE any socket (FR-019). Re-resolves at connect time.
        resolved = net_guard.assert_ssh_target_allowed(target.address, target.port)

        client = paramiko.SSHClient()
        policy = self._host_key_policy(target)
        client.set_missing_host_key_policy(policy)  # never load system known_hosts

        connect_kwargs = dict(
            hostname=target.address, port=target.port, username=target.username,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            allow_agent=False, look_for_keys=False,
        )
        if target.cred_type == "ssh_key":
            try:
                connect_kwargs["pkey"] = self._load_private_key(target.secret, target.passphrase)
            except paramiko.SSHException as e:
                # Bad key material or a wrong/missing passphrase is a CREDENTIAL
                # problem, not a network one — surface it as auth_failed, not
                # unreachable (FR-034). AuthenticationException maps to AUTH_FAILED.
                raise paramiko.AuthenticationException(f"private key could not be loaded: {e}") from e
        else:
            connect_kwargs["password"] = target.secret

        client.connect(**connect_kwargs)

        # FR-019 step 4: verify the address we actually connected to was one the gate
        # vetted — closes the DNS-rebinding TOCTOU where paramiko re-resolves a name to
        # a blocked address after the gate approved a safe one.
        peer_ip = None
        try:
            peer_ip = client.get_transport().sock.getpeername()[0]
        except Exception:  # noqa: BLE001 — treat an unreadable peer as unverifiable
            peer_ip = None
        if not _peer_in_resolved(peer_ip, resolved):
            client.close()
            raise net_guard.BlockedTargetError(
                target.address, str(peer_ip),
                "connected peer not in the vetted address set (possible DNS rebinding)")
        return client, policy

    def _verdict_for_exception(self, exc: Exception) -> Optional[Verdict]:
        """Map a KNOWN exception to a vocabulary verdict, else None (=> unexpected)."""
        import paramiko

        if isinstance(exc, net_guard.BlockedTargetError):
            return Verdict.BLOCKED_ADDRESS
        if isinstance(exc, net_guard.HostResolutionError):
            return Verdict.UNREACHABLE
        if isinstance(exc, HostKeyMismatch) or isinstance(exc, paramiko.BadHostKeyException):
            return Verdict.HOST_KEY_MISMATCH
        if isinstance(exc, (paramiko.AuthenticationException, paramiko.PasswordRequiredException)):
            return Verdict.AUTH_FAILED
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return Verdict.TIMEOUT
        # A remote permission failure (SFTP EACCES/EPERM => PermissionError) must be
        # distinguished from a network failure — check it BEFORE the OSError catch-all.
        if isinstance(exc, PermissionError):
            return Verdict.PERMISSION_DENIED_REMOTE
        if isinstance(exc, paramiko.SSHException):
            return Verdict.UNREACHABLE  # negotiation/protocol failure
        if isinstance(exc, (ConnectionError, OSError)):
            return Verdict.UNREACHABLE
        return None

    def _result_for_exception(self, target: MachineTarget, exc: Exception,
                              *, retryable: bool) -> RemoteResult:
        verdict = self._verdict_for_exception(exc)
        if verdict is None:
            raise RemoteTransportError(f"unexpected transport error for {target.label}: {exc}") from exc
        return RemoteResult(verdict=verdict, machine=target.label,
                            next_action=_NEXT_ACTION.get(verdict, ""), retryable=retryable)

    def _read_bounded(self, chan, timeout: float) -> Tuple[bytes, bool]:
        """Read stdout with a wall-clock deadline and a size cap.

        Returns (bytes, truncated). Raises ``TimeoutError`` if the command produces
        no output / never completes within ``timeout`` (paramiko-free; testable with a
        fake channel). Closing the channel on over-cap unblocks a remote stuck on
        write() so it cannot wedge the worker (the HIGH finding)."""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) <= MAX_OUTPUT_BYTES:
            try:
                chunk = chan.recv(_RECV_CHUNK)
            except socket.timeout as e:
                chan.close()
                raise TimeoutError("timed out reading command output") from e
            if not chunk:
                return bytes(buf), False
            buf += chunk
            if time.monotonic() > deadline:
                chan.close()
                raise TimeoutError("command exceeded its time bound")
        chan.close()  # over cap: stop consuming, unblock the remote
        return bytes(buf[:MAX_OUTPUT_BYTES]), True

    def _drain_stderr(self, chan, cap: int = 16384) -> str:
        """Best-effort bounded read of buffered stderr AFTER the command has exited
        (stdout already read to EOF, so stderr is complete). Never raises; used only
        to explain a non-zero exit — the transport still returns typed verdicts."""
        buf = bytearray()
        try:
            while len(buf) < cap and chan.recv_stderr_ready():
                chunk = chan.recv_stderr(min(_RECV_CHUNK, cap - len(buf)))
                if not chunk:
                    break
                buf += chunk
        except Exception:  # noqa: BLE001
            pass
        return bytes(buf).decode("utf-8", "replace")

    def _await_exit(self, chan, timeout: float, truncated: bool) -> Optional[int]:
        """Wait for the exit status within the deadline (never unbounded). Returns None
        when output was truncated (channel already closed)."""
        if truncated:
            return None
        deadline = time.monotonic() + timeout
        while not chan.exit_status_ready():
            if time.monotonic() > deadline:
                chan.close()
                raise TimeoutError("timed out awaiting command exit status")
            time.sleep(0.02)
        return chan.recv_exit_status()

    def run(self, target: MachineTarget, argv: List[str], *, timeout: float,
            retryable: bool = False) -> RemoteResult:
        command = build_login_command(argv)  # validate + build BEFORE connecting (fail fast; Fake parity)
        client = None
        try:
            client, _ = self._connect(target, timeout)
            _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
            chan = stdout.channel
            out, truncated = self._read_bounded(chan, timeout)
            exit_status = self._await_exit(chan, timeout, truncated)
            stderr_text = "" if truncated else self._drain_stderr(chan)
            return RemoteResult(verdict=Verdict.OK, machine=target.label,
                                stdout=out.decode("utf-8", "replace"), stderr=stderr_text,
                                exit_status=exit_status, retryable=retryable,
                                data={"truncated": truncated})
        except Exception as exc:  # noqa: BLE001 — mapped to vocabulary or re-raised
            return self._result_for_exception(target, exc, retryable=retryable)
        finally:
            if client is not None:
                client.close()

    def stat(self, target: MachineTarget, remote_path: str, *, timeout: float) -> RemoteResult:
        client = None
        try:
            client, _ = self._connect(target, timeout)
            sftp = client.open_sftp()
            try:
                sftp.get_channel().settimeout(timeout)  # bound the SFTP op (FR-021)
                try:
                    sftp.stat(remote_path)
                    exists = True
                except FileNotFoundError:
                    exists = False
            finally:
                sftp.close()
            return RemoteResult(verdict=Verdict.OK, machine=target.label,
                                data={"exists": exists, "path": remote_path}, retryable=True)
        except Exception as exc:  # noqa: BLE001
            return self._result_for_exception(target, exc, retryable=True)
        finally:
            if client is not None:
                client.close()

    def put_file(self, target: MachineTarget, data: bytes, remote_path: str, *,
                 timeout: float) -> RemoteResult:
        client = None
        try:
            client, _ = self._connect(target, timeout)
            sftp = client.open_sftp()
            try:
                sftp.get_channel().settimeout(timeout)  # bound the SFTP op (FR-021)
                from io import BytesIO
                # Writes unconditionally: the destructive if_exists decision is the
                # confirmation gate's job (via stat), not the transport's.
                sftp.putfo(BytesIO(data), remote_path)
            finally:
                sftp.close()
            return RemoteResult(verdict=Verdict.OK, machine=target.label,
                                data={"path": remote_path, "bytes": len(data)}, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._result_for_exception(target, exc, retryable=False)
        finally:
            if client is not None:
                client.close()

    def probe(self, target: MachineTarget, *, timeout: float) -> RemoteResult:
        client = None
        try:
            client, policy = self._connect(target, timeout)
            captured = getattr(policy, "captured", None)
            return RemoteResult(verdict=Verdict.OK, machine=target.label,
                                data={"authenticated": True}, host_key=captured, retryable=True)
        except Exception as exc:  # noqa: BLE001
            return self._result_for_exception(target, exc, retryable=True)
        finally:
            if client is not None:
                client.close()


_NEXT_ACTION = {
    Verdict.UNREACHABLE: "check the machine is on and reachable from the deployment; verify address/port",
    Verdict.AUTH_FAILED: "re-check the username and credential for this machine",
    Verdict.HOST_KEY_MISMATCH: "if the machine was legitimately rebuilt, re-trust it deliberately; otherwise do not proceed",
    Verdict.CREDENTIAL_NOT_CONFIGURED: "add a credential for this machine",
    Verdict.CREDENTIAL_UNDECRYPTABLE: "re-enter the credential for this machine",
    Verdict.BLOCKED_ADDRESS: "this target address is not permitted; register a routable machine address",
    Verdict.PERMISSION_DENIED_REMOTE: "use an account with sufficient rights on the machine",
    Verdict.QUOTA_EXHAUSTED: "check your cluster allocation / free space, then retry",
    Verdict.MFA_REQUIRED: "this machine requires MFA, which is not supported; use a key/password-only account",
    Verdict.TIMEOUT: "retry later; if it recurs the command may be hanging on the machine",
    Verdict.UNCONFIRMED: "check the queue / machine to see whether it took effect before re-issuing",
}


class FakeTransport:
    """In-memory transport double for tests — no paramiko, no socket.

    Still runs the egress gate on the target address so gate behaviour is exercised
    end-to-end, and validates argv shape and ordering exactly as ParamikoTransport
    does (both validate argv before the gate). Configure outcomes via the constructor.
    """

    def __init__(self, *, reachable: bool = True, authenticated: bool = True,
                 host_key: Optional[Dict] = None, files: Optional[Dict[str, bytes]] = None,
                 command_stdout: str = "", command_exit: int = 0, command_stderr: str = "",
                 force_verdict: Optional[Verdict] = None):
        self.reachable = reachable
        self.authenticated = authenticated
        self.host_key = host_key or {"type": "ssh-ed25519", "blob_b64": "AAAA", "fingerprint": "SHA256:fake"}
        self.files = dict(files or {})
        self.command_stdout = command_stdout
        self.command_exit = command_exit
        self.command_stderr = command_stderr
        self.force_verdict = force_verdict
        self.calls: List[Dict] = []  # recorded for assertions

    def _gate(self, target: MachineTarget) -> Optional[RemoteResult]:
        try:
            net_guard.assert_ssh_target_allowed(target.address, target.port)
        except net_guard.BlockedTargetError:
            return RemoteResult(verdict=Verdict.BLOCKED_ADDRESS, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.BLOCKED_ADDRESS])
        except net_guard.HostResolutionError:
            return RemoteResult(verdict=Verdict.UNREACHABLE, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.UNREACHABLE])
        return None

    def _precheck(self, target: MachineTarget, *, retryable: bool) -> Optional[RemoteResult]:
        blocked = self._gate(target)
        if blocked is not None:
            blocked.retryable = retryable
            return blocked
        if self.force_verdict is not None:
            return RemoteResult(verdict=self.force_verdict, machine=target.label,
                                next_action=_NEXT_ACTION.get(self.force_verdict, ""), retryable=retryable)
        if not self.reachable:
            return RemoteResult(verdict=Verdict.UNREACHABLE, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.UNREACHABLE], retryable=retryable)
        if not self.authenticated:
            return RemoteResult(verdict=Verdict.AUTH_FAILED, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.AUTH_FAILED], retryable=retryable)
        return None

    def run(self, target: MachineTarget, argv: List[str], *, timeout: float,
            retryable: bool = False) -> RemoteResult:
        build_login_command(argv)  # validate argv shape before the gate, as production does
        self.calls.append({"op": "run", "argv": list(argv)})
        pre = self._precheck(target, retryable=retryable)
        if pre is not None:
            return pre
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            stdout=self.command_stdout, stderr=self.command_stderr,
                            exit_status=self.command_exit,
                            retryable=retryable, data={"truncated": False})

    def stat(self, target: MachineTarget, remote_path: str, *, timeout: float) -> RemoteResult:
        self.calls.append({"op": "stat", "path": remote_path})
        pre = self._precheck(target, retryable=True)
        if pre is not None:
            return pre
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            data={"exists": remote_path in self.files, "path": remote_path}, retryable=True)

    def put_file(self, target: MachineTarget, data: bytes, remote_path: str, *,
                 timeout: float) -> RemoteResult:
        self.calls.append({"op": "put_file", "path": remote_path})
        pre = self._precheck(target, retryable=False)
        if pre is not None:
            return pre
        # Writes unconditionally (the if_exists decision is the confirmation gate's job).
        self.files[remote_path] = data
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            data={"path": remote_path, "bytes": len(data)})

    def probe(self, target: MachineTarget, *, timeout: float) -> RemoteResult:
        self.calls.append({"op": "probe"})
        pre = self._precheck(target, retryable=True)
        if pre is not None:
            return pre
        return RemoteResult(verdict=Verdict.OK, machine=target.label,
                            data={"authenticated": True}, host_key=self.host_key, retryable=True)


# --- Injection seam (mirrors llm_config.client_factory) ------------------------

_transport: Optional[RemoteTransport] = None


def get_transport() -> RemoteTransport:
    """Return the process transport (lazily defaulting to ParamikoTransport)."""
    global _transport
    if _transport is None:
        _transport = ParamikoTransport()
    return _transport


def set_transport(transport: Optional[RemoteTransport]) -> None:
    """Override the transport (tests wire a FakeTransport; None resets to default)."""
    global _transport
    _transport = transport
