"""Fixed values of the execution policy: base roots, Seatbelt profile names, limit ranges, codes, launcher names and user facing messages.
Every policy value mirrors the committed vectors, and the policy, selection, renderer and launcher modules read them from here.
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
DARWIN_EXEC_BASE = ("/usr", "/System", "/Library", "/private/var/db/dyld")
DARWIN_EXEC_ONLY = ("/bin", "/sbin")
DARWIN_METADATA_ONLY = ("/bin", "/sbin")
DARWIN_KEYCHAINS = ("/Library/Keychains", "/System/Library/Keychains")
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
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LAUNCH_MARKER = "ASTRAL_EXEC_MARKER"
REFUSED_EXIT_STATUS = 125
OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
TRAMPOLINE_READY = b"exec"
NAMESPACE_DENIED = "userns_denied"
LINUX_BASE = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
LINUX_DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")
LANDLOCK_ABI = 6
PROXY_PORT = 3128
EGRESS_SOCKET = "/run/astral/egress.sock"
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
BWRAP_PATHS = ("/usr/bin/bwrap", "/bin/bwrap")
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
AGENT_SERVER_REASONS = ("phi_node",)
PROC_CHILDREN_MISSING = "proc_children_missing"
HOST_MESSAGES = {
    PROC_CHILDREN_MISSING: "This Linux kernel has no /proc children files (CONFIG_PROC_CHILDREN), and the process limit cannot be enforced without them.",
}
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
    "root_node": "This node runs as root, and bubblewrap would map root to root inside the sandbox. Run the node as an unprivileged user.",
    "sandbox_exec_missing": "/usr/bin/sandbox-exec is missing, and untrusted tools need it on macOS.",
    "app_sandboxed": "The App Sandboxed build cannot confine child processes, so it cannot host untrusted tools. Use the direct-download build.",
    "platform_unsupported": "No confinement backend exists for {platform}.",
    "phi_node": "This node is in your PHI group, so agents you create cannot run on it. Exclude the node from the PHI group to run them here.",
}
