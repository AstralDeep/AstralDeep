"""Reference validator and renderers for the execution policy: the parse and validation order, the digest, the canonical macOS Seatbelt text, the Linux plan and the backend selection table.
It reuses the preserved mesh reference for LETS-CJ/1 bytes, and the vectors in vectors/exec-policy-v1.json pin its behavior for every other implementation.
"""

from __future__ import annotations

import bisect
import hashlib
import json
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
CORE_LIMIT_BYTES = 0
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
    "root_node",
    "sandbox_exec_missing",
    "app_sandboxed",
    "platform_unsupported",
)
AGENT_SERVER_REASONS = ("phi_node", "not_excluded")
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
    "root_node": "This node runs as root, and bubblewrap would map root to root inside the sandbox. Run the node as an unprivileged user.",
    "sandbox_exec_missing": "/usr/bin/sandbox-exec is missing, and untrusted tools need it on macOS.",
    "app_sandboxed": "The App Sandboxed build cannot confine child processes, so it cannot host untrusted tools. Use the direct-download build.",
    "phi_node": "This node is in your PHI group, so agents you create cannot run on it. Exclude the node from the PHI group to run them here.",
    "not_excluded": "Agents you create run unconfined, so they start only on a node you excluded from your PHI group. To run them here, add this node to the group and then exclude it.",
    "platform_unsupported": "No confinement backend exists for {platform}.",
}
DENIED_SYSCALLS = (
    "io_uring_enter",
    "io_uring_register",
    "io_uring_setup",
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
NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
DARWIN_EXEC_ONLY = ("/bin", "/sbin")
DARWIN_EXEC_BASE = ("/usr", "/System", "/Library", "/private/var/db/dyld")
DARWIN_METADATA_ONLY = ("/bin", "/sbin")
DENIED_SERVICES = (
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.system.notification_center",
    "com.apple.analyticsd",
    "com.apple.analyticsd.messagetracer",
    "com.apple.diagnosticd",
    "com.apple.osanalytics.osanalyticshelper",
)
DENIED_LOG_SERVICES = (
    "com.apple.logd",
    "com.apple.logd.events",
    "com.apple.system.logger",
)
SYSLOG_SOCKET = "/private/var/run/syslog"
DNSSD_SERVICE = "com.apple.dnssd.service"
EXEC_SUBDIRECTORY = "exec"
FACT_KEYS = (
    "platform",
    "mode",
    "landlock_abi",
    "bwrap",
    "userns",
    "euid",
    "sandbox_exec",
    "app_sandboxed",
)


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
    if not path.startswith("/"):
        return "must be absolute"
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        return "not normalized"
    return None


_TABLE = json.loads((Path(__file__).resolve().parent / "path_fold.json").read_text())
_DECOMPOSE = {chr(int(key, 16)): value for key, value in _TABLE["decompose"].items()}
_FOLD = {chr(int(key, 16)): value for key, value in _TABLE["fold"].items()}
_CCC = {chr(int(key, 16)): value for key, value in _TABLE["ccc"].items()}
_ASSIGNED_STARTS = [low for low, _ in _TABLE["assigned"]]
HANGUL_BASE, HANGUL_COUNT = 0xAC00, 11172
JAMO_L, JAMO_V, JAMO_T = 0x1100, 0x1161, 0x11A7
JAMO_V_COUNT, JAMO_T_COUNT = 21, 28


def _assigned(char: str) -> bool:
    cp = ord(char)
    at = bisect.bisect_right(_ASSIGNED_STARTS, cp) - 1
    return at >= 0 and cp <= _TABLE["assigned"][at][1]


def _decomposed_hangul(char: str) -> str:
    index = ord(char) - HANGUL_BASE
    lead, rest = divmod(index, JAMO_V_COUNT * JAMO_T_COUNT)
    vowel, tail = divmod(rest, JAMO_T_COUNT)
    out = chr(JAMO_L + lead) + chr(JAMO_V + vowel)
    return out + (chr(JAMO_T + tail) if tail else "")


def _ordered(text: str) -> str:
    out: list[str] = []
    run: list[str] = []
    for ch in text:
        if _CCC.get(ch):
            run.append(ch)
            continue
        out += sorted(run, key=lambda mark: _CCC[mark]) + [ch]
        run = []
    return "".join(out + sorted(run, key=lambda mark: _CCC[mark]))


def _nfd(text: str) -> str:
    pieces = [
        _decomposed_hangul(ch)
        if 0 <= ord(ch) - HANGUL_BASE < HANGUL_COUNT
        else _DECOMPOSE.get(ch, ch)
        for ch in text
    ]
    return _ordered("".join(pieces))


def _fold(path: str, platform: str | None) -> str:
    if platform != "darwin":
        return path
    return _nfd("".join(_FOLD.get(ch, ch) for ch in _nfd(path)))


def _contains(root: str, path: str, platform: str | None = None) -> bool:
    root, path = _fold(root, platform), _fold(path, platform)
    return path == root or path.startswith(root + "/")


def _object(value: Any, fields: tuple[str, ...], where: str) -> None:
    if not isinstance(value, dict):
        raise PolicyError("policy.malformed", f"{where} must be an object")
    if set(value) - set(fields):
        raise PolicyError("policy.unknown_field", where)
    if set(fields) - set(value):
        raise PolicyError("policy.malformed", f"{where} is missing a field")


def _checked_path(value: Any, where: str, platform: str | None) -> str:
    if not isinstance(value, str):
        raise PolicyError("policy.malformed", f"{where} must be a string")
    problem = _path_problem(value)
    if problem is None and platform == "darwin" and not all(map(_assigned, value)):
        problem = "unassigned code point"
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
        or NUMERIC_LABEL.fullmatch(labels[-1])
    ):
        raise PolicyError("policy.host_invalid", f"{where}.host")
    if port not in HOST_PORTS:
        raise PolicyError("policy.host_invalid", f"{where}.port")
    return value


def _plain_text(data: bytes | str) -> str:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    if text.startswith("\ufeff"):
        raise MeshError("canonical.bom", "byte order mark")
    return text


def parse(data: bytes | str | dict[str, Any], platform: str | None) -> dict[str, Any]:
    if isinstance(data, (bytes, str)):
        try:
            policy = strict_loads(_plain_text(data))
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
    seen: set[str] = set()
    for index, path in enumerate(reads):
        _checked_path(path, f"reads[{index}]", platform)
        if previous is not None and path <= previous:
            raise PolicyError(
                "policy.path_invalid", f"reads[{index}] not sorted and unique"
            )
        folded = _fold(path, platform)
        if folded in seen:
            raise PolicyError(
                "policy.path_invalid", f"reads[{index}] repeats a read root"
            )
        seen.add(folded)
        previous = path
    workspace = _checked_path(policy["workspace"], "workspace", platform)
    for path in reads:
        if _contains(workspace, path, platform) or _contains(path, workspace, platform):
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
    return "sha256:" + hashlib.sha256(canonical(parse(policy, None))).hexdigest()


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _checked_context_path(value: Any, name: str, platform: str | None = None) -> str:
    if not isinstance(value, str) or _path_problem(value):
        raise ContextError(f"{name} must be an absolute normalized path")
    if platform == "darwin" and not all(map(_assigned, value)):
        raise ContextError(f"{name} holds an unassigned code point")
    return value


def seatbelt_profile(policy: dict[str, Any], home: str, proxy_port: int | None) -> str:
    policy = parse(policy, "darwin")
    home = _checked_context_path(home, "home", "darwin")
    if policy["hosts"] and not (_is_int(proxy_port) and 1 <= proxy_port <= 65535):
        raise ContextError("a policy with hosts needs a proxy port from 1 to 65535")
    workspace = policy["workspace"]
    lines = ["(version 1)", "(deny default)", '(import "system.sb")', ""]
    lines += [
        f"(allow process-exec* (subpath {quote(root)}))"
        for root in (*DARWIN_EXEC_BASE, *policy["reads"], *DARWIN_EXEC_ONLY)
    ]
    if policy["limits"]["processes"] > 1:
        lines.append("(allow process-fork)")
    lines += [
        "(allow process-info* (target self))",
        "(allow signal (target self))",
        "(allow signal (target children))",
        "(allow sysctl-read)",
        "(allow file-ioctl)",
        "",
    ]
    lines += [f"(allow file-read* (subpath {quote(root)}))" for root in DARWIN_BASE]
    metadata = " ".join(f"(subpath {quote(root)})" for root in DARWIN_METADATA_ONLY)
    lines.append(f"(allow file-read-metadata {metadata})")
    lines += [f"(allow file-read* (subpath {quote(root)}))" for root in policy["reads"]]
    lines += [f"(allow file-read* file-write* (subpath {quote(workspace)}))", ""]
    lines += [
        f"(deny process-exec* (subpath {quote(workspace)}))",
        f"(deny file-map-executable (subpath {quote(workspace)}))",
        f'(deny mach-lookup (global-name "{DNSSD_SERVICE}"))',
        "(deny mach-lookup "
        + " ".join(f'(global-name "{name}")' for name in DENIED_SERVICES)
        + ' (xpc-service-name-prefix ""))',
        "(deny mach-lookup "
        + " ".join(f'(global-name "{name}")' for name in DENIED_LOG_SERVICES)
        + ")",
        f"(deny network-outbound (literal {quote(SYSLOG_SOCKET)}))",
    ]
    lines += [
        f"(deny file-read* (subpath {quote(root)}))"
        for root in (home + "/Library/Keychains", *DARWIN_KEYCHAINS)
    ]
    if policy["hosts"]:
        lines += ["", f'(allow network-outbound (remote ip "localhost:{proxy_port}"))']
    return "\n".join(lines) + "\n"


def linux_plan(policy: dict[str, Any], egress_socket: str | None) -> dict[str, Any]:
    policy = parse(policy, "linux")
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
        "workspace_noexec": True,
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
            "socket_allow": [
                {"domain": "AF_INET", "type": "SOCK_STREAM"},
                {"domain": "AF_INET6", "type": "SOCK_STREAM"},
            ]
            if hosted
            else [],
            "allow_socketpair": True,
        },
        "rlimits": {
            "RLIMIT_AS": limits["memory_bytes"],
            "RLIMIT_NOFILE": limits["open_files"],
            "RLIMIT_FSIZE": limits["file_bytes"],
            "RLIMIT_CORE": CORE_LIMIT_BYTES,
        },
        "watchdog": {
            "wall_ms": policy["time"]["wall_ms"],
            "processes": limits["processes"],
        },
    }


def _facts(facts: Any) -> dict[str, Any]:
    if not isinstance(facts, dict):
        raise ContextError("facts must be an object")
    unknown = set(facts) - set(FACT_KEYS)
    if unknown:
        raise ContextError(f"unknown facts: {sorted(unknown)}")
    out = {"mode": "enforce", **facts}
    if not isinstance(out.get("platform"), str):
        raise ContextError("facts.platform must be a string")
    if out["mode"] not in ("enforce", "shadow"):
        raise ContextError("facts.mode must be enforce or shadow")
    if not _is_int(out.get("landlock_abi")) or out["landlock_abi"] < 0:
        raise ContextError("facts.landlock_abi must be a non-negative integer")
    if not _is_int(out.get("euid")) or out["euid"] < 0:
        raise ContextError("facts.euid must be a non-negative integer")
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
    facts = _facts(facts)
    platform = facts["platform"]
    policy = parse(policy, platform)
    data_dir = _checked_context_path(data_dir, "data_dir", platform)
    base = {"darwin": DARWIN_BASE, "linux": LINUX_BASE}.get(platform, ())
    workspace = policy["workspace"]
    overlaps = any(
        _contains(root, data_dir, platform) or _contains(data_dir, root, platform)
        for root in (*base, *policy["reads"])
    )
    overlaps = overlaps or _contains(workspace, data_dir, platform)
    if _contains(data_dir, workspace, platform):
        exec_root = _fold(f"{data_dir}/{EXEC_SUBDIRECTORY}/", platform)
        overlaps = overlaps or not _fold(workspace, platform).startswith(exec_root)
    if overlaps:
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
        if facts["euid"] == 0:
            return _refuse("exec.backend_unavailable", "root_node")
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
