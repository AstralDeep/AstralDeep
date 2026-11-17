"""Builds and installs the classic BPF seccomp filter the Linux plan describes: it denies the listed syscalls, bounds fork to thread creation and lets socket create only the listed domain and type pairs.
inner.py installs it on the tool process after Landlock, the harness checks its behavior on aarch64 and in-process tests run the program for x86_64.
"""

from __future__ import annotations

import ctypes
import platform
import struct
from typing import Callable

AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_COMMON = {
    "setpgid": {"x86_64": 109, "aarch64": 154},
    "setsid": {"x86_64": 112, "aarch64": 157},
    "msgget": {"x86_64": 68, "aarch64": 186},
    "msgsnd": {"x86_64": 69, "aarch64": 189},
    "msgrcv": {"x86_64": 70, "aarch64": 188},
    "msgctl": {"x86_64": 71, "aarch64": 187},
    "semget": {"x86_64": 64, "aarch64": 190},
    "semctl": {"x86_64": 66, "aarch64": 191},
    "semtimedop": {"x86_64": 220, "aarch64": 192},
    "semop": {"x86_64": 65, "aarch64": 193},
    "shmget": {"x86_64": 29, "aarch64": 194},
    "shmctl": {"x86_64": 31, "aarch64": 195},
    "shmat": {"x86_64": 30, "aarch64": 196},
    "shmdt": {"x86_64": 67, "aarch64": 197},
    "mq_open": {"x86_64": 240, "aarch64": 180},
    "mq_unlink": {"x86_64": 241, "aarch64": 181},
    "mq_timedsend": {"x86_64": 242, "aarch64": 182},
    "mq_timedreceive": {"x86_64": 243, "aarch64": 183},
    "mq_notify": {"x86_64": 244, "aarch64": 184},
    "mq_getsetattr": {"x86_64": 245, "aarch64": 185},
    "io_uring_setup": {"x86_64": 425, "aarch64": 425},
    "io_uring_enter": {"x86_64": 426, "aarch64": 426},
    "io_uring_register": {"x86_64": 427, "aarch64": 427},
    "socket": {"x86_64": 41, "aarch64": 198},
    "socketpair": {"x86_64": 53, "aarch64": 199},
    "clone": {"x86_64": 56, "aarch64": 220},
    "clone3": {"x86_64": 435, "aarch64": 435},
    "fork": {"x86_64": 57},
    "vfork": {"x86_64": 58},
}

LD_ABS_W = 0x20
ALU_AND_K = 0x54
JEQ_K = 0x15
JGE_K = 0x35
JA = 0x05
RET_K = 0x06
ARCH_OFFSET = 4
NR_OFFSET = 0
ARG0_LOW = 16
ARG1_LOW = 24
X32_BIT = 0x40000000
DOMAINS = {"AF_INET": 2, "AF_INET6": 10}
SOCK_TYPES = {"SOCK_STREAM": 1}
SOCK_FLAGS = 0x800 | 0x80000
SOCK_TYPE_MASK = 0xFFFFFFFF & ~SOCK_FLAGS
CLONE_THREAD = 0x00010000
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000
RET_KILL_PROCESS = 0x80000000
EPERM, ENOSYS = 1, 38


def numbers(machine: str) -> dict[str, int]:
    if machine not in AUDIT_ARCH:
        raise OSError(f"no syscall filter for {machine}")
    return {name: table[machine] for name, table in _COMMON.items() if machine in table}


class _Assembler:
    def __init__(self) -> None:
        self._rows: list[tuple[int, int, object, object, int]] = []
        self._labels: dict[str, int] = {}

    def label(self, name: str) -> None:
        self._labels[name] = len(self._rows)

    def ld(self, offset: int) -> None:
        self._rows.append((LD_ABS_W, 0, 0, 0, offset))

    def andk(self, value: int) -> None:
        self._rows.append((ALU_AND_K, 0, 0, 0, value))

    def jeq(self, value: int, target: str) -> None:
        self._rows.append((JEQ_K, target, 0, 0, value))

    def jge(self, value: int, target: str) -> None:
        self._rows.append((JGE_K, target, 0, 0, value))

    def ja(self, target: str) -> None:
        self._rows.append((JA, None, None, target, 0))

    def ret(self, value: int) -> None:
        self._rows.append((RET_K, 0, 0, 0, value))

    def assemble(self) -> bytes:
        code = b""
        for index, (op, jt, jf, ja_target, k) in enumerate(self._rows):
            if op == JA:
                k = self._labels[ja_target] - (index + 1)
                jt_byte = jf_byte = 0
            else:
                jt_byte = self._offset(jt, index)
                jf_byte = self._offset(jf, index)
            code += struct.pack("HBBI", op, jt_byte, jf_byte, k)
        return code

    def _offset(self, target: object, index: int) -> int:
        if isinstance(target, str):
            distance = self._labels[target] - (index + 1)
        else:
            distance = int(target)
        if not 0 <= distance <= 255:
            raise OSError("seccomp jump out of range")
        return distance


def _number(nr: dict[str, int], name: object) -> int:
    if not isinstance(name, str) or name not in nr:
        raise OSError(f"no syscall number for {name!r} on this machine")
    return nr[name]


def _known(nr: dict[str, int], name: object) -> int | None:
    if not isinstance(name, str) or name not in _COMMON:
        raise OSError(f"no syscall number for {name!r}")
    return nr.get(name)


def _socket_pairs(rules: object) -> list[tuple[int, int]]:
    if not isinstance(rules, list):
        raise OSError("socket_allow must be a list")
    pairs = []
    for rule in rules:
        domain = rule.get("domain") if isinstance(rule, dict) else None
        kind = rule.get("type") if isinstance(rule, dict) else None
        if domain not in DOMAINS or kind not in SOCK_TYPES:
            raise OSError(f"the filter cannot express the socket rule {rule!r}")
        pairs.append((DOMAINS[domain], SOCK_TYPES[kind]))
    return pairs


def build(machine: str, plan: dict[str, object]) -> bytes:
    nr = numbers(machine)
    denied = [_number(nr, name) for name in plan["deny_syscalls"]]
    forks = [(name, _known(nr, name)) for name in plan["fork_syscalls"]]
    pairs = _socket_pairs(plan["socket_allow"])
    deny_fork = bool(plan["deny_fork"])
    asm = _Assembler()
    asm.ld(ARCH_OFFSET)
    asm.jeq(AUDIT_ARCH[machine], "arch_ok")
    asm.ja("kill")
    asm.label("arch_ok")
    asm.ld(NR_OFFSET)
    if machine == "x86_64":
        asm.jge(X32_BIT, "kill")
    for number in denied:
        asm.jeq(number, "eperm")
    if not plan["allow_socketpair"]:
        asm.jeq(nr["socketpair"], "eperm")
    if deny_fork:
        for name, number in forks:
            if number is not None:
                asm.jeq(number, "enosys" if name == "clone3" else "eperm")
        asm.jeq(nr["clone"], "clone_check")
    asm.jeq(nr["socket"], "socket_check")
    asm.ja("allow")
    if deny_fork:
        asm.label("clone_check")
        asm.ld(ARG0_LOW)
        asm.andk(CLONE_THREAD)
        asm.jeq(0, "eperm")
        asm.ja("allow")
    asm.label("socket_check")
    for index, (domain, kind) in enumerate(pairs):
        asm.label(f"pair_{index}")
        asm.ld(ARG0_LOW)
        asm.jeq(domain, f"pair_{index}_type")
        asm.ja(f"pair_{index + 1}")
        asm.label(f"pair_{index}_type")
        asm.ld(ARG1_LOW)
        asm.andk(SOCK_TYPE_MASK)
        asm.jeq(kind, "allow")
        asm.ja(f"pair_{index + 1}")
    asm.label(f"pair_{len(pairs)}")
    asm.ja("eperm")
    asm.label("allow")
    asm.ret(RET_ALLOW)
    asm.label("eperm")
    asm.ret(RET_ERRNO | EPERM)
    asm.label("enosys")
    asm.ret(RET_ERRNO | ENOSYS)
    asm.label("kill")
    asm.ret(RET_KILL_PROCESS)
    return asm.assemble()


def _real_prctl(option: int, arg2: int, arg3: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    result = libc.prctl(option, arg2, arg3, 0, 0)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"prctl({option}) failed")
    return result


def install(
    plan: dict[str, object],
    machine: str | None = None,
    prctl: Callable[[int, int, int], int] = _real_prctl,
) -> None:
    machine = machine or platform.machine()
    program = build(machine, plan)
    prctl(PR_SET_NO_NEW_PRIVS, 1, 0)
    buffer = ctypes.create_string_buffer(program, len(program))
    fprog = ctypes.create_string_buffer(
        struct.pack("HxxxxxxP", len(program) // 8, ctypes.addressof(buffer))
    )
    prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.addressof(fprog))
