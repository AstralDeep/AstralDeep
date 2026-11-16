"""Reference validator and renderers for the execution policy: the parse and validation order, the digest, the canonical macOS Seatbelt text, the Linux plan and the backend selection table.
It reuses the preserved 105 reference for LETS-CJ/1 bytes, and the vectors in vectors/exec-policy-v1.json pin its behavior for every other implementation.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[3]
        / "105-decentralized-personal-mesh/contracts/reference"
    ),
)
from astral_mesh_ref.cj import canonical, strict_loads
from astral_mesh_ref.errors import MeshError

FORMAT = "astral.exec-policy/v1"
MAX_READS = 64
MAX_HOSTS = 64
MAX_PATH_BYTES = 4096
HOST_PORTS = (80, 443)
LIMITS = {
    "memory_bytes": (16777216, 68719476736),
    "processes": (1, 4096),
    "open_files": (16, 65536),
    "file_bytes": (4096, 1099511627776),
}
WALL_MS = (1, 86400000)
TOP_FIELDS = ("v", "reads", "workspace", "hosts", "limits", "time")
HOST_FIELDS = ("host", "port")
TIME_FIELDS = ("wall_ms",)
DARWIN_BASE = (
    "/usr",
    "/System",
    "/Library",
    "/private/etc",
    "/private/var/db/dyld",
    "/dev",
)
DARWIN_KEYCHAINS = ("/Library/Keychains", "/System/Library/Keychains")
LINUX_BASE = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
LINUX_DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")
LANDLOCK_ABI = 6
PROXY_PORT = 3128
EGRESS_SOCKET = "/run/astral/egress.sock"
VALIDATION_CODES = (
    "policy.malformed",
    "policy.unknown_field",
    "policy.path_invalid",
    "policy.too_many_entries",
    "policy.host_invalid",
    "policy.limit_out_of_range",
    "policy.time_out_of_range",
)
BACKEND_REASONS = (
    "landlock_abi",
    "bwrap_missing",
    "userns_denied",
    "sandbox_exec_missing",
    "app_sandboxed",
    "platform_unsupported",
)
MESSAGES = {
    "exec.backend_unavailable": "No confinement backend can run untrusted tools on this machine.",
    "exec.agent_server_unsupported": "Agents you create cannot run on this node yet, because it has no way to confine them.",
    "exec.shadow_refused": "Shadow mode never runs untrusted code.",
    "exec.policy_overlaps_data": "A read root or the workspace contains the node data directory.",
    "exec.time_limit": "The tool ran past its time limit and was stopped.",
    "exec.memory_limit": "The tool used more memory than its limit allows and was stopped.",
    "exec.process_limit": "The tool started more processes than its limit allows and was stopped.",
    "exec.file_limit": "The tool went past its open file or file size limit and was stopped.",
    "exec.launch_failed": "The confinement backend could not start the tool.",
    "landlock_abi": "This Linux kernel offers Landlock ABI {observed} and untrusted tools need ABI {required} or newer.",
    "bwrap_missing": "bubblewrap (bwrap) is not installed, and untrusted tools need it.",
    "userns_denied": "Unprivileged user namespaces are disabled, and bubblewrap needs them to confine untrusted tools.",
    "sandbox_exec_missing": "/usr/bin/sandbox-exec is missing, and untrusted tools need it on macOS.",
    "app_sandboxed": "The App Sandboxed build cannot confine child processes, so it cannot host untrusted tools. Use the direct-download build.",
    "platform_unsupported": "No confinement backend exists for {platform}.",
}
DENIED_SYSCALLS = (
    "msgctl",
    "msgget",
    "msgrcv",
    "msgsnd",
    "mq_getsetattr",
    "mq_notify",
    "mq_open",
    "mq_timedreceive",
    "mq_timedsend",
    "mq_unlink",
    "semctl",
    "semget",
    "semop",
    "semtimedop",
    "setpgid",
    "setsid",
    "shmat",
    "shmctl",
    "shmdt",
    "shmget",
)
FORK_SYSCALLS = ("clone3", "fork", "vfork")
HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class PolicyError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ContextError(Exception):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _path_problem(path: str) -> str | None:
    try:
        encoded = path.encode("utf-8")
    except UnicodeEncodeError:
        return "not UTF-8"
    if len(encoded) > MAX_PATH_BYTES:
        return "too long"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return "control character"
    if not path.startswith("/") or path == "/":
        return "must be absolute and not the root"
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        return "not normalized"
    return None


def _contains(root: str, path: str) -> bool:
    return path == root or path.startswith(root + "/")


def _object(value: Any, fields: tuple[str, ...], where: str) -> None:
    if not isinstance(value, dict):
        raise PolicyError("policy.malformed", f"{where} must be an object")
    if set(value) - set(fields):
        raise PolicyError("policy.unknown_field", where)
    if set(fields) - set(value):
        raise PolicyError("policy.malformed", f"{where} is missing a field")


def _checked_path(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise PolicyError("policy.malformed", f"{where} must be a string")
    problem = _path_problem(value)
    if problem:
        raise PolicyError("policy.path_invalid", f"{where}: {problem}")
    return value


def _checked_host(value: Any, where: str) -> dict[str, Any]:
    _object(value, HOST_FIELDS, where)
    host, port = value["host"], value["port"]
    if not isinstance(host, str) or not _is_int(port):
        raise PolicyError("policy.malformed", f"{where} has a wrong type")
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(HOST_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise PolicyError("policy.host_invalid", f"{where}.host")
    if port not in HOST_PORTS:
        raise PolicyError("policy.host_invalid", f"{where}.port")
    return value


def parse(data: bytes | str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(data, (bytes, str)):
        try:
            policy = strict_loads(data)
        except (MeshError, UnicodeDecodeError) as error:
            raise PolicyError("policy.malformed", str(error)) from error
    else:
        policy = data
    _object(policy, TOP_FIELDS, "policy")
    if not _is_int(policy["v"]) or policy["v"] != 1:
        raise PolicyError("policy.malformed", "v must be 1")
    reads = policy["reads"]
    if not isinstance(reads, list):
        raise PolicyError("policy.malformed", "reads must be a list")
    if len(reads) > MAX_READS:
        raise PolicyError("policy.too_many_entries", "reads")
    previous = None
    for index, path in enumerate(reads):
        _checked_path(path, f"reads[{index}]")
        if previous is not None and path <= previous:
            raise PolicyError(
                "policy.path_invalid", f"reads[{index}] not sorted and unique"
            )
        previous = path
    workspace = _checked_path(policy["workspace"], "workspace")
    for path in reads:
        if _contains(workspace, path) or _contains(path, workspace):
            raise PolicyError(
                "policy.path_invalid", "a read root overlaps the workspace"
            )
    hosts = policy["hosts"]
    if not isinstance(hosts, list):
        raise PolicyError("policy.malformed", "hosts must be a list")
    if len(hosts) > MAX_HOSTS:
        raise PolicyError("policy.too_many_entries", "hosts")
    previous_key = None
    for index, entry in enumerate(hosts):
        _checked_host(entry, f"hosts[{index}]")
        key = (entry["host"], entry["port"])
        if previous_key is not None and key <= previous_key:
            raise PolicyError(
                "policy.host_invalid", f"hosts[{index}] not sorted and unique"
            )
        previous_key = key
    limits = policy["limits"]
    _object(limits, tuple(LIMITS), "limits")
    for name, (low, high) in LIMITS.items():
        if not _is_int(limits[name]):
            raise PolicyError("policy.malformed", f"limits.{name} must be an integer")
        if not low <= limits[name] <= high:
            raise PolicyError("policy.limit_out_of_range", name)
    wall = policy["time"]
    _object(wall, TIME_FIELDS, "time")
    if not _is_int(wall["wall_ms"]):
        raise PolicyError("policy.malformed", "time.wall_ms must be an integer")
    if not WALL_MS[0] <= wall["wall_ms"] <= WALL_MS[1]:
        raise PolicyError("policy.time_out_of_range", "wall_ms")
    return policy


def digest(policy: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(parse(policy))).hexdigest()


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _checked_context_path(value: Any, name: str) -> str:
    if not isinstance(value, str) or _path_problem(value):
        raise ContextError(f"{name} must be an absolute normalized path")
    return value


def seatbelt_profile(policy: dict[str, Any], home: str, proxy_port: int | None) -> str:
    policy = parse(policy)
    home = _checked_context_path(home, "home")
    if policy["hosts"] and not (_is_int(proxy_port) and 1 <= proxy_port <= 65535):
        raise ContextError("a policy with hosts needs a proxy port from 1 to 65535")
    workspace = policy["workspace"]
    lines = ["(version 1)", "(deny default)", '(import "system.sb")', ""]
    lines += ["(allow process-exec*)"]
    if policy["limits"]["processes"] > 1:
        lines.append("(allow process-fork)")
    lines += [
        "(allow process-info* (target self))",
        "(allow signal (target self))",
        "(allow mach-lookup)",
        "(allow sysctl-read)",
        "(allow file-ioctl)",
        "",
    ]
    lines += [
        f"(allow file-read* (subpath {quote(root)}))"
        for root in (*DARWIN_BASE, *policy["reads"])
    ]
    lines += [f"(allow file-read* file-write* (subpath {quote(workspace)}))", ""]
    lines.append(f"(deny process-exec* (subpath {quote(workspace)}))")
    lines += [
        f"(deny file-read* (subpath {quote(root)}))"
        for root in (home + "/Library/Keychains", *DARWIN_KEYCHAINS)
    ]
    if policy["hosts"]:
        lines += ["", f'(allow network-outbound (remote ip "localhost:{proxy_port}"))']
    return "\n".join(lines) + "\n"


def linux_plan(policy: dict[str, Any], egress_socket: str | None) -> dict[str, Any]:
    policy = parse(policy)
    hosted = bool(policy["hosts"])
    if hosted:
        egress_socket = _checked_context_path(egress_socket, "egress_socket")
    workspace = policy["workspace"]
    limits = policy["limits"]
    bwrap = [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    for root in LINUX_BASE:
        bwrap += ["--ro-bind-try", root, root]
    for root in policy["reads"]:
        bwrap += ["--ro-bind", root, root]
    bwrap += ["--bind", workspace, workspace, "--proc", "/proc", "--dev", "/dev"]
    if hosted:
        bwrap += ["--bind", egress_socket, EGRESS_SOCKET]
    bwrap += ["--chdir", workspace]
    return {
        "bwrap": bwrap,
        "landlock": {
            "abi": LANDLOCK_ABI,
            "exec_read": [*LINUX_BASE, *policy["reads"]],
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
        },
        "rlimits": {
            "RLIMIT_AS": limits["memory_bytes"],
            "RLIMIT_NOFILE": limits["open_files"],
            "RLIMIT_FSIZE": limits["file_bytes"],
        },
        "watchdog": {
            "wall_ms": policy["time"]["wall_ms"],
            "processes": limits["processes"],
        },
    }


def _facts(facts: Any) -> dict[str, Any]:
    if not isinstance(facts, dict):
        raise ContextError("facts must be an object")
    out = {"mode": "enforce", **facts}
    if not isinstance(out.get("platform"), str):
        raise ContextError("facts.platform must be a string")
    if out["mode"] not in ("enforce", "shadow"):
        raise ContextError("facts.mode must be enforce or shadow")
    if not _is_int(out.get("landlock_abi")) or out["landlock_abi"] < 0:
        raise ContextError("facts.landlock_abi must be a non-negative integer")
    for key in ("bwrap", "userns", "sandbox_exec", "app_sandboxed"):
        if not isinstance(out.get(key), bool):
            raise ContextError(f"facts.{key} must be a boolean")
    return out


def _refuse(code: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    template = MESSAGES[reason or code]
    return {
        "ok": False,
        "code": code,
        **({"reason": reason} if reason else {}),
        **extra,
        "message": template.format(**extra) if extra else template,
    }


def select(
    policy: dict[str, Any], facts: dict[str, Any], data_dir: str
) -> dict[str, Any]:
    policy = parse(policy)
    facts = _facts(facts)
    data_dir = _checked_context_path(data_dir, "data_dir")
    platform = facts["platform"]
    base = {"darwin": DARWIN_BASE, "linux": LINUX_BASE}.get(platform, ())
    roots = (*base, *policy["reads"], policy["workspace"])
    if any(_contains(root, data_dir) for root in roots):
        return _refuse("exec.policy_overlaps_data")
    if facts["mode"] == "shadow":
        return _refuse("exec.shadow_refused")
    if platform == "linux":
        if facts["landlock_abi"] < LANDLOCK_ABI:
            return _refuse(
                "exec.backend_unavailable",
                "landlock_abi",
                observed=facts["landlock_abi"],
                required=LANDLOCK_ABI,
            )
        if not facts["bwrap"]:
            return _refuse("exec.backend_unavailable", "bwrap_missing")
        if not facts["userns"]:
            return _refuse("exec.backend_unavailable", "userns_denied")
        return {"ok": True, "backend": "linux"}
    if platform == "darwin":
        if not facts["sandbox_exec"]:
            return _refuse("exec.backend_unavailable", "sandbox_exec_missing")
        if facts["app_sandboxed"]:
            return _refuse("exec.backend_unavailable", "app_sandboxed")
        return {"ok": True, "backend": "seatbelt"}
    return _refuse(
        "exec.backend_unavailable", "platform_unsupported", platform=platform
    )
