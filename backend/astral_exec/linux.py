"""The Linux backend on the host side: the plan the contract fixes for a policy, the bubblewrap command around the inner launcher and the checks that must pass before it starts.
launch.py dispatches here for the linux backend and supervises the call, and inner.py applies the plan's Landlock, seccomp and rlimit parts inside the sandbox.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    CORE_LIMIT_BYTES,
    DENIED_SYSCALLS,
    EGRESS_SOCKET,
    FORK_SYSCALLS,
    LANDLOCK_ABI,
    LINUX_BASE,
    LINUX_DEVICES,
    PROXY_PORT,
)
from .errors import ContextError
from .paths import path_problem
from .policy import Policy

NAMESPACE_FLAGS = (
    "--unshare-user",
    "--unshare-pid",
    "--unshare-net",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup",
    "--die-with-parent",
    "--new-session",
    "--clearenv",
)


def _document(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, Policy):
        raise ContextError("the Linux plan needs a Policy")
    if policy.loaded_for is not None and policy.loaded_for[0] != "linux":
        raise ContextError("the Linux plan needs a policy loaded for linux")
    return Policy.parse(policy.canonical_bytes(), "linux").document


def _socket_path(value: Any) -> str:
    if not isinstance(value, str) or path_problem(value):
        raise ContextError("a policy with hosts needs an absolute egress socket path")
    return value


def plan_document(document: dict[str, Any], egress_socket: Any) -> dict[str, Any]:
    hosted = bool(document["hosts"])
    workspace = document["workspace"]
    limits = document["limits"]
    bwrap = list(NAMESPACE_FLAGS)
    for root in LINUX_BASE:
        bwrap += ["--ro-bind-try", root, root]
    for root in document["reads"]:
        bwrap += ["--ro-bind", root, root]
    bwrap += ["--bind", workspace, workspace, "--proc", "/proc", "--dev", "/dev"]
    if hosted:
        bwrap += ["--bind", _socket_path(egress_socket), EGRESS_SOCKET]
    bwrap += ["--chdir", workspace]
    return {
        "bwrap": bwrap,
        "workspace_noexec": True,
        "landlock": {
            "abi": LANDLOCK_ABI,
            "exec_read": [*LINUX_BASE, *document["reads"]],
            "device_files": list(LINUX_DEVICES),
            "workspace": workspace,
            "workspace_rights": "all_except_execute_and_make_sock",
            "tcp_connect": [PROXY_PORT] if hosted else [],
            "tcp_bind": [],
            "scopes": ["abstract_unix_socket", "signal"],
        },
        "egress": {"listen": f"127.0.0.1:{PROXY_PORT}", "socket": EGRESS_SOCKET}
        if hosted
        else None,
        "seccomp": {
            "deny_syscalls": list(DENIED_SYSCALLS),
            "deny_fork": limits["processes"] == 1,
            "fork_syscalls": list(FORK_SYSCALLS),
            "inet_stream_sockets": hosted,
            "deny_socket_types": ["SOCK_DGRAM", "SOCK_RAW"],
            "deny_socket_domains": ["AF_UNIX"],
            "allow_socketpair": True,
        },
        "rlimits": {
            "RLIMIT_AS": limits["memory_bytes"],
            "RLIMIT_NOFILE": limits["open_files"],
            "RLIMIT_FSIZE": limits["file_bytes"],
            "RLIMIT_CORE": CORE_LIMIT_BYTES,
        },
        "watchdog": {
            "wall_ms": document["time"]["wall_ms"],
            "processes": limits["processes"],
        },
    }


def plan(policy: Policy, egress_socket: str | None) -> dict[str, Any]:
    return plan_document(_document(policy), egress_socket)
