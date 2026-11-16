"""Applies the Landlock part of the Linux plan: the ABI-6 filesystem, TCP and scope rule set that grants read and execute on the system and read roots, read and write on the four device files and every right except execute and socket creation on the workspace.
inner.py calls apply() before seccomp and exec, and the harness proves the live kernel honors the rules. The rule builders here are pure, so they verify on any host.
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SYS_CREATE_RULESET = 444
SYS_ADD_RULE = 445
SYS_RESTRICT_SELF = 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH = 1
RULE_NET_PORT = 2
PR_SET_NO_NEW_PRIVS = 38
REQUIRED_ABI = 6

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1, 2, 4, 8
TRUNCATE = 1 << 14
MAKE_SOCK = 1 << 9
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE
READ_RIGHTS = EXECUTE | READ_FILE | READ_DIR
DEVICE_RIGHTS = READ_FILE | WRITE_FILE
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1, 2
PROXY_PORT = 3128
_ABI_FS_BITS = {1: 13, 2: 14, 3: 15, 4: 15, 5: 16}


def fs_rights(abi: int) -> int:
    return (1 << _ABI_FS_BITS.get(abi, 16)) - 1


def workspace_rights(fs: int) -> int:
    return fs & ~(EXECUTE | MAKE_SOCK)


def ruleset_attr(fs: int) -> bytes:
    return struct.pack(
        "QQQ",
        fs,
        NET_BIND_TCP | NET_CONNECT_TCP,
        SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL,
    )


def grants(plan: dict[str, Any], fs: int) -> list[tuple[str, int]]:
    rows = [(path, READ_RIGHTS) for path in plan["exec_read"]]
    rows += [(path, DEVICE_RIGHTS) for path in plan["device_files"]]
    rows.append((plan["workspace"], workspace_rights(fs)))
    return rows


@dataclass(frozen=True)
class Kernel:
    syscall: Callable[..., int]
    prctl: Callable[..., int]
    get_errno: Callable[[], int]

    def abi(self) -> int:
        return max(self.syscall(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION), 0)

    def create_ruleset(self, attr: bytes) -> int:
        fd = self.syscall(SYS_CREATE_RULESET, attr, len(attr), 0)
        if fd < 0:
            raise OSError(self.get_errno(), "landlock_create_ruleset failed")
        return fd

    def add_rule(self, ruleset: int, kind: int, rule: bytes) -> None:
        if self.syscall(SYS_ADD_RULE, ruleset, kind, rule, 0) != 0:
            raise OSError(self.get_errno(), "landlock_add_rule failed")

    def restrict(self, ruleset: int) -> None:
        if self.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(self.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if self.syscall(SYS_RESTRICT_SELF, ruleset, 0) != 0:
            raise OSError(self.get_errno(), "landlock_restrict_self failed")


def _real_kernel() -> Kernel:
    libc = ctypes.CDLL(None, use_errno=True)
    return Kernel(libc.syscall, libc.prctl, ctypes.get_errno)


def apply(
    plan: dict[str, Any],
    hosted: bool,
    kernel: Kernel | None = None,
    open_path: Callable[[str], int] = lambda path: os.open(
        path, os.O_PATH | os.O_CLOEXEC
    ),
    isdir: Callable[[str], bool] = os.path.isdir,
    close: Callable[[int], None] = os.close,
) -> None:
    kernel = kernel or _real_kernel()
    abi = kernel.abi()
    if abi < REQUIRED_ABI:
        raise OSError(
            f"Landlock ABI {REQUIRED_ABI} or newer is required; this kernel offers {abi}"
        )
    fs = fs_rights(REQUIRED_ABI)
    ruleset = kernel.create_ruleset(ruleset_attr(fs))
    try:
        for path, rights in grants(plan, fs):
            try:
                fd = open_path(path)
            except OSError:
                continue
            try:
                allowed = rights if isdir(path) else rights & FILE_RIGHTS
                kernel.add_rule(
                    ruleset, RULE_PATH_BENEATH, struct.pack("=Qi", allowed & fs, fd)
                )
            finally:
                close(fd)
        if hosted:
            kernel.add_rule(
                ruleset,
                RULE_NET_PORT,
                struct.pack("=QQ", NET_CONNECT_TCP, PROXY_PORT),
            )
        kernel.restrict(ruleset)
    finally:
        close(ruleset)
