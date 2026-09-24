"""OS-level sandbox for launched agent/parser child processes: POSIX resource limits via
a preexec_fn plus a scrubbed, temp-scoped environment, backstopping the static checks
agent_lifecycle.py's start_draft_agent already runs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("orchestrator.sandbox")

_SECRET_ENV_DENYLIST = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "SEARCH_API_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    "WEB_SESSION_ENC_KEY", "WEB_SESSION_SECRET",
    "OFFLINE_GRANT_ENC_KEY",
    "MEMORY_HMAC_KEY", "TXN_TOKEN_KEY", "MAS_MESSAGE_KEY",
    "DELEGATION_CHILD_SIGNING_KEY",
    # Uniquely required to keep the audit chain tamper-evident
    "AUDIT_HMAC_SECRET",
    "GITHUB_TOKEN",
    "KEYCLOAK_CLIENT_SECRET", "AGENT_SERVICE_CLIENT_SECRET",
    "VOICE_CONTROL_SECRET", "LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    "DATABASE_URL",
    "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL",
)

_SECRET_ENV_PREFIXES = ("AUDIT_HMAC_SECRET_",)


def sandbox_enabled() -> bool:
    return os.getenv("FF_SANDBOX_CODEGEN", "true").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SandboxLimits:
    address_space_mb: int = 2048
    file_size_mb: int = 256
    open_files: int = 512
    processes: int = 512
    cpu_seconds: int = 0


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
        return v if v >= 0 else default
    except (ValueError, AttributeError):
        return default


def build_limits() -> SandboxLimits:
    return SandboxLimits(
        address_space_mb=_int_env("SANDBOX_MEM_MB", 2048) or 2048,
        file_size_mb=_int_env("SANDBOX_FSIZE_MB", 256) or 256,
        open_files=_int_env("SANDBOX_NOFILE", 512) or 512,
        processes=_int_env("SANDBOX_NPROC", 512) or 512,
        cpu_seconds=_int_env("SANDBOX_CPU_S", 0),
    )


def _apply_limits(limits: SandboxLimits, res: Any) -> None:
    def _set(attr: str, value: int) -> None:
        r = getattr(res, attr, None)
        if r is None:
            return
        try:
            _cur_soft, cur_hard = res.getrlimit(r)
            hard = value if cur_hard == res.RLIM_INFINITY else min(value, cur_hard)
            res.setrlimit(r, (min(value, hard), hard))
        except (ValueError, OSError):
            pass

    mb = 1024 * 1024
    _set("RLIMIT_AS", limits.address_space_mb * mb)
    _set("RLIMIT_FSIZE", limits.file_size_mb * mb)
    _set("RLIMIT_NOFILE", limits.open_files)
    _set("RLIMIT_NPROC", limits.processes)
    if limits.cpu_seconds > 0:
        _set("RLIMIT_CPU", limits.cpu_seconds)


def make_preexec(limits: SandboxLimits) -> Optional[Callable[[], None]]:
    if os.name != "posix":
        return None
    try:
        import resource
    except ImportError:
        return None
    return lambda: _apply_limits(limits, resource)


def sandbox_env(base_env: Optional[Dict[str, str]], tmpdir: str) -> Dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    for key in _SECRET_ENV_DENYLIST:
        env.pop(key, None)
    for key in [k for k in env if k.startswith(_SECRET_ENV_PREFIXES)]:
        env.pop(key, None)
    env["TMPDIR"] = tmpdir
    env["TEMP"] = tmpdir
    env["TMP"] = tmpdir
    return env
