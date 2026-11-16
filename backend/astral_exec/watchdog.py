"""Watches the processes of one confined call on macOS through libproc and KERN_PROCARGS2: it counts them, sums their memory footprint and kills them all at the end.
launch.py samples it between waits and ends it at exit, and tests drive the same logic through an injected process table.
"""

from __future__ import annotations

import ctypes
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

LIBSYSTEM = "/usr/lib/libSystem.B.dylib"
PROC_PGRP_ONLY = 2
PROC_UID_ONLY = 4
PROC_PPID_ONLY = 6
RUSAGE_INFO_V0 = 0
CTL_KERN = 1
KERN_ARGMAX = 8
KERN_PROCARGS2 = 49
SWEEP_ROUNDS = 64
SWEEP_PAUSE_SECONDS = 0.005


@dataclass(frozen=True)
class Usage:
    start: int
    exited: bool
    footprint: int


class System(Protocol):
    def pids(self, kind: int, info: int) -> list[int]: ...

    def usage(self, pid: int) -> Usage | None: ...

    def strings(self, pid: int) -> list[bytes] | None: ...


class _RusageInfoV0(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
    ]


def procargs_strings(raw: bytes) -> list[bytes]:
    return [part for part in raw[4:].split(b"\0") if part]


class Darwin:
    def __init__(self, lib: Any = None) -> None:
        lib = lib or ctypes.CDLL(LIBSYSTEM, use_errno=True)
        lib.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_listpids.restype = ctypes.c_int
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pid_rusage.restype = ctypes.c_int
        lib.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        lib.sysctl.restype = ctypes.c_int
        self._lib = lib
        argmax = ctypes.c_int(0)
        self._sysctl((CTL_KERN, KERN_ARGMAX), argmax, ctypes.sizeof(argmax))
        self._args = ctypes.create_string_buffer(argmax.value)

    def _sysctl(self, name: tuple[int, ...], out: Any, size: int) -> int:
        mib = (ctypes.c_int * len(name))(*name)
        length = ctypes.c_size_t(size)
        if self._lib.sysctl(
            mib, len(name), ctypes.byref(out), ctypes.byref(length), None, 0
        ):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return length.value

    def pids(self, kind: int, info: int) -> list[int]:
        size = self._lib.proc_listpids(kind, info, None, 0)
        while size >= 0:
            buffer = (ctypes.c_int * (size // 4 + 256))()
            filled = self._lib.proc_listpids(kind, info, buffer, ctypes.sizeof(buffer))
            if filled < 0:
                break
            if filled < ctypes.sizeof(buffer):
                return [pid for pid in buffer[: filled // 4] if pid > 0]
            size = filled * 2
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))

    def usage(self, pid: int) -> Usage | None:
        info = _RusageInfoV0()
        if self._lib.proc_pid_rusage(pid, RUSAGE_INFO_V0, ctypes.byref(info)):
            return None
        return Usage(
            info.ri_proc_start_abstime,
            info.ri_proc_exit_abstime != 0,
            info.ri_phys_footprint,
        )

    def strings(self, pid: int) -> list[bytes] | None:
        try:
            size = self._sysctl(
                (CTL_KERN, KERN_PROCARGS2, pid), self._args, len(self._args)
            )
        except OSError:
            return None
        return procargs_strings(ctypes.string_at(self._args, size))


class Watch:
    def __init__(
        self,
        root: int,
        marker: bytes | None,
        system: System | None = None,
        kill: Callable[[int, int], None] = os.kill,
        killpg: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self._system = system or Darwin()
        self._root = root
        self._marker = marker
        self._kill = kill
        self._killpg = killpg
        self._self = os.getpid()
        first = self._system.usage(root)
        self._root_start = None if first is None else first.start
        self._known: dict[int, int] = {} if first is None else {root: first.start}
        self._scanned: set[int] = set()

    def _group_is_ours(self) -> bool:
        current = self._system.usage(self._root)
        return current is None or current.start == self._root_start

    def _marked(self, every: bool) -> list[int]:
        listed = self._system.pids(PROC_UID_ONLY, os.geteuid())
        self._scanned &= set(listed)
        found = []
        for pid in listed:
            if pid == self._self or (pid in self._scanned and not every):
                continue
            self._scanned.add(pid)
            strings = self._system.strings(pid)
            if strings and self._marker in strings:
                found.append(pid)
        return found

    def _discover(self, every: bool) -> dict[int, Usage]:
        fresh = (
            self._system.pids(PROC_PGRP_ONLY, self._root)
            if self._group_is_ours()
            else []
        )
        if self._marker is not None:
            fresh += self._marked(every)
        pending = [(pid, False) for pid in self._known] + [(pid, True) for pid in fresh]
        found: dict[int, Usage] = {}
        while pending:
            pid, is_fresh = pending.pop()
            if pid in found or pid == self._self:
                continue
            usage = self._system.usage(pid)
            if usage is None or not (is_fresh or self._known[pid] == usage.start):
                continue
            found[pid] = usage
            pending += [
                (child, True) for child in self._system.pids(PROC_PPID_ONLY, pid)
            ]
        self._known = {pid: usage.start for pid, usage in found.items()}
        return found

    def sample(self) -> tuple[int, int]:
        found = self._discover(every=False)
        memory = sum(usage.footprint for usage in found.values() if not usage.exited)
        return len(found), memory

    def _signal_group(self) -> None:
        if self._group_is_ours():
            try:
                self._killpg(self._root, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _live(self) -> set[int]:
        self._signal_group()
        found = self._discover(every=True)
        return {pid for pid, usage in found.items() if not usage.exited}

    def end(self) -> set[int]:
        live = self._live()
        for _ in range(SWEEP_ROUNDS):
            if not live:
                break
            for pid in live:
                try:
                    self._kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(SWEEP_PAUSE_SECONDS)
            live = self._live()
        return live
