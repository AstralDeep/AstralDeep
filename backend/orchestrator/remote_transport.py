"""Injectable SSH transport boundary for remote-compute agents: commands run as a
login-shell argv vector past an egress gate, yielding a fixed RemoteResult verdict.
Production wires ParamikoTransport; tests inject FakeTransport via set_transport.
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

MAX_OUTPUT_BYTES = 1_048_576
_RECV_CHUNK = 65536


class Verdict(str, Enum):
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
    pass


class HostKeyMismatch(Exception):
    pass


@dataclass
class MachineTarget:
    machine_id: str
    label: str
    address: str
    port: int
    username: str
    cred_type: str
    secret: str = ""
    passphrase: Optional[str] = None
    host_key_fingerprint: Optional[str] = None


@dataclass
class RemoteResult:
    verdict: Verdict
    machine: str
    next_action: str = ""
    data: Dict = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_status: Optional[int] = None
    host_key: Optional[Dict] = None
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.verdict == Verdict.OK


def build_login_command(argv: List[str]) -> str:
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    quoted = " ".join(shlex.quote(a) for a in argv)
    return "bash -lc 'exec \"$@\"' _ " + quoted


def _sha256_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def evaluate_host_key(expected_fp: Optional[str], presented_fp: str) -> str:
    if expected_fp is None:
        return "record"
    if presented_fp == expected_fp:
        return "match"
    return "mismatch"


def _peer_in_resolved(peer_ip: Optional[str], resolved: List[str]) -> bool:
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
    def run(self, target: MachineTarget, argv: List[str], *, timeout: float,
            retryable: bool = False) -> RemoteResult: ...

    def put_file(self, target: MachineTarget, data: bytes, remote_path: str, *,
                 timeout: float) -> RemoteResult: ...

    def stat(self, target: MachineTarget, remote_path: str, *, timeout: float) -> RemoteResult: ...

    def probe(self, target: MachineTarget, *, timeout: float) -> RemoteResult: ...


class ParamikoTransport:
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
                raise
            except paramiko.SSHException as e:
                errors.append(f"{cls.__name__}: {e}")
                continue
        raise paramiko.SSHException("unsupported or invalid private key (" + "; ".join(errors) + ")")

    def _connect(self, target: MachineTarget, timeout: float):
        import paramiko

        resolved = net_guard.assert_ssh_target_allowed(target.address, target.port)

        client = paramiko.SSHClient()
        policy = self._host_key_policy(target)
        client.set_missing_host_key_policy(policy)

        connect_kwargs = dict(
            hostname=target.address, port=target.port, username=target.username,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            allow_agent=False, look_for_keys=False,
        )
        if target.cred_type == "ssh_key":
            try:
                connect_kwargs["pkey"] = self._load_private_key(target.secret, target.passphrase)
            except paramiko.SSHException as e:
                raise paramiko.AuthenticationException(f"private key could not be loaded: {e}") from e
        else:
            connect_kwargs["password"] = target.secret

        client.connect(**connect_kwargs)

        # Re-checks peer post-connect: closes a DNS-rebinding gap
        peer_ip = None
        try:
            peer_ip = client.get_transport().sock.getpeername()[0]
        except Exception:  # noqa: BLE001
            peer_ip = None
        if not _peer_in_resolved(peer_ip, resolved):
            client.close()
            raise net_guard.BlockedTargetError(
                target.address, str(peer_ip),
                "connected peer not in the vetted address set (possible DNS rebinding)")
        return client, policy

    def _verdict_for_exception(self, exc: Exception) -> Optional[Verdict]:
        if isinstance(exc, net_guard.BlockedTargetError):
            return Verdict.BLOCKED_ADDRESS
        if isinstance(exc, net_guard.HostResolutionError):
            return Verdict.UNREACHABLE
        if isinstance(exc, HostKeyMismatch):
            return Verdict.HOST_KEY_MISMATCH
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return Verdict.TIMEOUT
        # Must precede OSError below: PermissionError is a subclass
        if isinstance(exc, PermissionError):
            return Verdict.PERMISSION_DENIED_REMOTE
        if isinstance(exc, (ConnectionError, OSError)):
            return Verdict.UNREACHABLE
        try:
            import paramiko
        except ModuleNotFoundError:
            return None
        if isinstance(exc, paramiko.BadHostKeyException):
            return Verdict.HOST_KEY_MISMATCH
        if isinstance(
            exc,
            (paramiko.AuthenticationException, paramiko.PasswordRequiredException),
        ):
            return Verdict.AUTH_FAILED
        if isinstance(exc, paramiko.SSHException):
            return Verdict.UNREACHABLE
        return None

    def _result_for_exception(self, target: MachineTarget, exc: Exception,
                              *, retryable: bool) -> RemoteResult:
        verdict = self._verdict_for_exception(exc)
        if verdict is None:
            raise RemoteTransportError(f"unexpected transport error for {target.label}: {exc}") from exc
        if verdict is Verdict.TIMEOUT and not retryable:
            verdict = Verdict.UNCONFIRMED
        return RemoteResult(verdict=verdict, machine=target.label,
                            next_action=_NEXT_ACTION.get(verdict, ""), retryable=retryable)

    def _read_bounded(self, chan, timeout: float) -> Tuple[bytes, bool]:
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
        chan.close()
        return bytes(buf[:MAX_OUTPUT_BYTES]), True

    def _drain_stderr(self, chan, cap: int = 16384) -> str:
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
        command = build_login_command(argv)
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
        except Exception as exc:  # noqa: BLE001
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
                sftp.get_channel().settimeout(timeout)
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
                sftp.get_channel().settimeout(timeout)
                from io import BytesIO
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
        self.calls: List[Dict] = []

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
            verdict = self.force_verdict
            if verdict is Verdict.TIMEOUT and not retryable:
                verdict = Verdict.UNCONFIRMED
            return RemoteResult(verdict=verdict, machine=target.label,
                                next_action=_NEXT_ACTION.get(verdict, ""), retryable=retryable)
        if not self.reachable:
            return RemoteResult(verdict=Verdict.UNREACHABLE, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.UNREACHABLE], retryable=retryable)
        if not self.authenticated:
            return RemoteResult(verdict=Verdict.AUTH_FAILED, machine=target.label,
                                next_action=_NEXT_ACTION[Verdict.AUTH_FAILED], retryable=retryable)
        return None

    def run(self, target: MachineTarget, argv: List[str], *, timeout: float,
            retryable: bool = False) -> RemoteResult:
        build_login_command(argv)
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


_transport: Optional[RemoteTransport] = None


def get_transport() -> RemoteTransport:
    global _transport
    if _transport is None:
        _transport = ParamikoTransport()
    return _transport


def set_transport(transport: Optional[RemoteTransport]) -> None:
    global _transport
    _transport = transport
