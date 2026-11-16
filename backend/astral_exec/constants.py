"""Fixed values of the execution policy: base roots, limit ranges, codes, ports and user facing messages.
Every value mirrors the committed policy vectors, and policy.py, select.py and errors.py read them from here.
"""

from __future__ import annotations

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
LINUX_BASE = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
LANDLOCK_ABI = 6
EXEC_SUBDIRECTORY = "exec"
FACT_KEYS = (
    "platform",
    "mode",
    "landlock_abi",
    "bwrap",
    "userns",
    "sandbox_exec",
    "app_sandboxed",
)
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
EXEC_CODES = (
    "exec.backend_unavailable",
    "exec.agent_server_unsupported",
    "exec.shadow_refused",
    "exec.policy_overlaps_data",
    "exec.time_limit",
    "exec.memory_limit",
    "exec.process_limit",
    "exec.file_limit",
    "exec.launch_failed",
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
