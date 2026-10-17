"""Binds terminal PTYs to operator-approved isolated SSH runtimes and current owner credentials.
The terminal manager uses this boundary instead of launching processes on the application host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import socket
import stat
import time
from dataclasses import dataclass
from typing import Callable

from orchestrator import remote_machines
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.remote_transport import MachineTarget, ParamikoTransport
from orchestrator.terminal_security import TerminalError

PROFILE_MAX_BYTES = 65536
CONNECT_TIMEOUT = 10.0
IO_TIMEOUT = 5.0
_PROFILE = "operator-isolated-ssh-pty/v1"
_FIELDS = frozenset({"owner_id", "machine_id", "address", "port", "username", "host_key_fingerprint"})


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= maximum
            and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value))


@dataclass(frozen=True)
class SSHBinding:
    profile_digest: str
    owner_id: str
    machine_id: str
    address: str
    port: int
    username: str
    host_key_fingerprint: str
    credential_digest: str


def _profile(owner_id: str):
    path = os.environ.get("TERMINAL_SSH_PROFILE_FILE")
    if not path:
        raise TerminalError("terminal_isolation_unavailable", 503)
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_mode & 0o227 or not 0 < before.st_size <= PROFILE_MAX_BYTES):
            raise ValueError()
        raw = os.read(descriptor, PROFILE_MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or len(raw) > PROFILE_MAX_BYTES
                or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode, before.st_uid)
                != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode, after.st_uid)):
            raise ValueError()
        document = json.loads(raw, object_pairs_hook=_pairs)
        if (type(document) is not dict or set(document) != {"profile", "targets"}
                or document["profile"] != _PROFILE or type(document["targets"]) is not list
                or not 1 <= len(document["targets"]) <= 64):
            raise ValueError()
        owners = set()
        accounts = set()
        selected = None
        for target in document["targets"]:
            if (type(target) is not dict or set(target) != _FIELDS
                    or not _text(target["owner_id"], 512)
                    or target["owner_id"] in owners
                    or not isinstance(target["machine_id"], str)
                    or not re.fullmatch(r"[a-f0-9]{32}", target["machine_id"])
                    or not _text(target["address"], 253)
                    or not isinstance(target["username"], str)
                    or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}", target["username"])
                    or type(target["port"]) is not int or not 1 <= target["port"] <= 65535
                    or not isinstance(target["host_key_fingerprint"], str)
                    or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", target["host_key_fingerprint"])):
                raise ValueError()
            account = (target["host_key_fingerprint"], target["username"])
            if account in accounts:
                raise ValueError()
            accounts.add(account)
            owners.add(target["owner_id"])
            if target["owner_id"] == owner_id:
                selected = target
        if selected is None:
            raise TerminalError("terminal_isolation_unavailable", 503)
        return selected, hashlib.sha256(raw).hexdigest()
    except TerminalError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        raise TerminalError("terminal_profile_invalid", 503) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def resolve(orchestrator, owner_id: str) -> tuple[SSHBinding, MachineTarget]:
    profile, digest = _profile(owner_id)
    try:
        target = remote_machines.build_target(
            plane_source_from_orchestrator(orchestrator), orchestrator.credential_manager,
            owner_id, profile["machine_id"],
        )
        if any(getattr(target, key) != profile[key] for key in (
            "machine_id", "address", "port", "username", "host_key_fingerprint",
        )):
            raise ValueError()
        if (target.cred_type not in {"ssh_key", "password"} or not isinstance(target.secret, str)
                or not target.secret or (target.passphrase is not None and not isinstance(target.passphrase, str))):
            raise ValueError()
        credential = hashlib.sha256(json.dumps(
            [target.cred_type, target.secret, target.passphrase], ensure_ascii=True,
        ).encode()).hexdigest()
        return SSHBinding(digest, owner_id, target.machine_id, target.address, target.port,
                          target.username, target.host_key_fingerprint, credential), target
    except Exception:
        raise TerminalError("terminal_target_unavailable", 503) from None


class SSHPTY:
    def __init__(self, client, channel) -> None:
        self.client = client
        self.channel = channel

    @classmethod
    def open(cls, target: MachineTarget, cols: int, rows: int, command: str | None,
             check: Callable[[], None]):
        client = channel = None
        try:
            check()
            client, _ = ParamikoTransport()._connect(target, CONNECT_TIMEOUT)
            check()
            channel = client.get_transport().open_session(timeout=CONNECT_TIMEOUT)
            channel.settimeout(IO_TIMEOUT)
            check()
            channel.get_pty(term="xterm", width=cols, height=rows)
            check()
            if command is None:
                channel.invoke_shell()
            else:
                channel.exec_command(command)
            check()
            return cls(client, channel)
        except BaseException:
            try:
                if channel is not None:
                    channel.close()
            finally:
                if client is not None:
                    client.close()
            raise

    def receive(self, size: int) -> bytes | None:
        if self.channel.recv_ready():
            return self.channel.recv(size)
        if self.channel.closed or self.channel.exit_status_ready():
            return b""
        select.select([self.channel], [], [], 0.1)
        return None

    def write(self, data: bytes, check: Callable[[], None]) -> None:
        remaining = memoryview(data)
        deadline = time.monotonic() + IO_TIMEOUT
        while remaining:
            check()
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError()
            self.channel.settimeout(timeout)
            count = self.channel.send(remaining)
            if not count:
                raise OSError()
            remaining = remaining[count:]

    def resize(self, cols: int, rows: int) -> None:
        self.channel.resize_pty(width=cols, height=rows)

    def close(self) -> None:
        try:
            self.channel.close()
        finally:
            self.client.close()


TRANSPORT_ERRORS = (OSError, EOFError, socket.timeout)
