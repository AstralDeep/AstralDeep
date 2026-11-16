"""The inner launcher that runs inside bubblewrap: it enters a nested user and mount namespace to remount the workspace noexec, starts the egress forwarder when the policy allows hosts, then applies Landlock, seccomp and the resource limits before it reports ready and execs the tool.
launch.py runs it as the bwrap command through inherited descriptors, and it reuses the package's seccomp, landlock and forwarder builders rather than duplicating them.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import sys
from typing import Any

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
MOUNT_ATTR_NOEXEC = 0x08
AT_FDCWD = -100
MOUNT_SETATTR = 442
READY = b"exec"
DEVNULL = "/dev/null"
_RLIMITS = (
    ("RLIMIT_NOFILE", "RLIMIT_NOFILE"),
    ("RLIMIT_FSIZE", "RLIMIT_FSIZE"),
    ("RLIMIT_CORE", "RLIMIT_CORE"),
    ("RLIMIT_AS", "RLIMIT_AS"),
)


class _MountAttr(ctypes.Structure):
    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


def _fail(status_fd: int, reason: str) -> None:
    try:
        os.write(status_fd, b"fail " + reason.encode()[:200])
    except OSError:
        pass
    os._exit(126)


def _read_json(fd: int) -> Any:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    return json.loads(b"".join(chunks))


def _enter_namespaces(libc: ctypes.CDLL) -> None:
    uid, gid = os.getuid(), os.getgid()
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNS) != 0:
        raise OSError(ctypes.get_errno(), "unshare failed")
    with open("/proc/self/setgroups", "w") as handle:
        handle.write("deny")
    with open("/proc/self/uid_map", "w") as handle:
        handle.write(f"{uid} {uid} 1")
    with open("/proc/self/gid_map", "w") as handle:
        handle.write(f"{gid} {gid} 1")


def _remount_noexec(libc: ctypes.CDLL, workspace: str) -> None:
    attr = _MountAttr(MOUNT_ATTR_NOEXEC, 0, 0, 0)
    result = libc.syscall(
        MOUNT_SETATTR,
        AT_FDCWD,
        workspace.encode(),
        0,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
    )
    if result != 0:
        raise OSError(ctypes.get_errno(), "mount_setattr noexec failed")
    if not os.statvfs(workspace).f_flag & os.ST_NOEXEC:
        raise OSError("the workspace did not become noexec")


def _start_forwarder(forwarder: Any, listen: str, socket_path: str, keep: set[int]) -> None:
    host, _colon, port = listen.rpartition(":")
    listener = forwarder.build_listener(host, int(port))
    if os.fork() == 0:
        null = os.open(DEVNULL, os.O_RDWR)
        for standard in (0, 1, 2):
            os.dup2(null, standard)
        keep_open = {listener.fileno(), 0, 1, 2}
        for fd in list(keep) | {null}:
            if fd not in keep_open:
                try:
                    os.close(fd)
                except OSError:
                    pass
        os.closerange(max(keep_open) + 1, 65536)
        forwarder.serve(listener, socket_path)
        os._exit(0)
    listener.close()


def _apply_limits(plan: dict[str, Any]) -> None:
    for key, name in _RLIMITS:
        value = plan[key]
        resource.setrlimit(getattr(resource, name), (value, value))


def _run(options: Any, argv: list[str]) -> None:
    sys.path.insert(0, f"/proc/self/fd/{options.package_fd}")
    from astral_exec import forwarder, landlock, seccomp

    plan = _read_json(options.plan_fd)
    env = _read_json(options.env_fd)
    if not isinstance(env, dict):
        raise ValueError("the tool environment must be an object")
    libc = ctypes.CDLL(None, use_errno=True)
    _enter_namespaces(libc)
    workspace = plan["landlock"]["workspace"]
    _remount_noexec(libc, workspace)
    egress = plan["egress"]
    hosted = egress is not None
    keep = {options.package_fd, options.status_fd}
    if hosted:
        _start_forwarder(forwarder, egress["listen"], egress["socket"], keep)
    landlock.apply(plan["landlock"], hosted)
    seccomp.install(plan["seccomp"])
    _apply_limits(plan["rlimits"])
    os.write(options.status_fd, READY)
    os.closerange(3, 65536)
    os.execve(argv[0], argv, env)


def main(raw: list[str]) -> int:
    import argparse

    split = raw.index("--")
    parser = argparse.ArgumentParser(prog="astral_exec.inner")
    for name in ("--package-fd", "--plan-fd", "--env-fd", "--status-fd"):
        parser.add_argument(name, type=int, required=True)
    options = parser.parse_args(raw[:split])
    argv = raw[split + 1 :]
    if not argv:
        _fail(options.status_fd, "no tool argv")
    try:
        _run(options, argv)
    except BaseException as error:  # noqa: BLE001
        _fail(options.status_fd, type(error).__name__)
    return 126


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
