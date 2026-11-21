"""Launch options for model-written agent servers and parser children: POSIX resource limits through a preexec_fn, a scrubbed temp-scoped environment and a loopback bind address.
agent_lifecycle.py's start_draft_agent and start.py's generated agent launch both build their child through agent_launch.
Only a development node may turn the limits and the scrub off with FF_SANDBOX_CODEGEN.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from orchestrator.boot_posture import is_dev_mode

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
    "AGENT_SERVICE_CLIENT_SECRET",
    "VOICE_CONTROL_SECRET", "LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    "DATABASE_URL",
    "TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL",
)

_SECRET_ENV_PREFIXES = ("AUDIT_HMAC_SECRET_",)
_SECRET_ENV_SUFFIXES = ("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY", "_PRIVATE_KEY")
# The agent presents it in RegisterAgent and the orchestrator refuses the registration without it
_SECRET_ENV_KEEP = frozenset({"AGENT_API_KEY"})
LOOPBACK_HOST = "127.0.0.1"


def sandbox_enabled() -> bool:
    if not is_dev_mode():
        return True
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
    for key in [k for k in env if k.startswith(_SECRET_ENV_PREFIXES) or (k.endswith(_SECRET_ENV_SUFFIXES) and k not in _SECRET_ENV_KEEP)]:
        env.pop(key, None)
    env["TMPDIR"] = tmpdir
    env["TEMP"] = tmpdir
    env["TMP"] = tmpdir
    return env


@dataclass(frozen=True)
class AgentLaunch:
    popen: Dict[str, Any]
    limits_applied: bool


def agent_launch(agent_dir: str, base_env: Optional[Mapping[str, str]] = None) -> AgentLaunch:
    env = dict(os.environ if base_env is None else base_env)
    popen: Dict[str, Any] = {}
    limits_applied = False
    if sandbox_enabled():
        tmpdir = os.path.join(agent_dir, "_sandbox_tmp")
        os.makedirs(tmpdir, exist_ok=True)
        limits = build_limits()
        preexec = make_preexec(limits)
        if preexec is not None:
            popen["preexec_fn"] = preexec
            limits_applied = True
        env = sandbox_env(env, tmpdir)
        logger.info("agent launch limits for %s: %s", agent_dir, limits)
    env["HOST"] = LOOPBACK_HOST
    popen["env"] = env
    return AgentLaunch(popen, limits_applied)


def draft_confinement(limits_applied: bool) -> Dict[str, Any]:
    limits = "with process resource limits only" if limits_applied else "with no resource limits"
    return {
        "confined": False,
        "resource_limits": limits_applied,
        "notice": f"This agent runs unconfined {limits}. It can read and write files and reach the network as you can.",
    }
