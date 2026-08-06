"""Sandboxed execution for generated agent/parser code.

Model-written agent/parser code is launched as a child process (the draft-agent
subprocess in ``agent_lifecycle.start_draft_agent``). Today that child inherits
the orchestrator's full resources, environment and temp filesystem. This wraps
the launch in an OS-level sandbox so the static checks have a runtime backstop:

* **Resource limits** (POSIX ``resource.setrlimit`` via a ``preexec_fn`` applied
  in the child between fork and exec): address space, single-file write size,
  open files, and child processes — so generated code can't OOM the host, fill
  the disk, exhaust descriptors, or fork-bomb. CPU-seconds is OPT-IN (default
  off): a draft agent is a persistent server, and ``RLIMIT_CPU`` is cumulative,
  so a CPU cap would eventually kill a healthy long-lived agent.
* **Temp-scoped filesystem**: ``TMPDIR``/``TEMP``/``TMP`` point at a per-draft
  sandbox dir, so scratch writes land in a disposable location.
* **Secret-scrubbed environment**: high-value secrets the agent framework does
  not need (LLM keys, DB URL, HMAC/Fernet keys) are removed from the child env,
  so generated code can't read them out of ``os.environ``.

These compose with the EXISTING defenses (the static ``CodeSecurityAnalyzer``
blocklist, the egress-gated HTTP path, the self-test timeout). Socket creation is
constrained here only indirectly (the descriptor cap + the static socket block +
egress gating); a full seccomp syscall filter is a documented follow-on.

Pure config + a fork-time hook (``resource`` is POSIX-only). Flag
``FF_SANDBOX_CODEGEN`` (default ON) gates the wrap, which is additive +
fail-open: off, on a non-POSIX host, or on any setup error, the launch is
exactly today's. Limits are env-tunable with generous defaults so a normal
parser agent is unaffected.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("orchestrator.sandbox")

#: Secrets a draft/parser agent never needs — scrubbed from the child env so
#: generated code cannot read them from ``os.environ``. ``AGENT_API_KEY`` is KEPT
#: (the agent framework needs it to register with the orchestrator), and the
#: ``DB_*`` connection vars are KEPT because a parser agent resolves its
#: attachment through ``shared.database`` (``resolve_attachment_path``); scrubbing
#: them would break the very file read the sandbox exists to run. Names here must
#: be the exact env keys the code reads — an env key that is never set (a typo or
#: a renamed secret) protects nothing, so this list is verified against the
#: producers (``credential_manager``/``session_store``/``web_auth``/etc.).
_SECRET_ENV_DENYLIST = (
    # LLM / search provider keys (not needed for best-effort extraction).
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "SEARCH_API_KEY",
    # Fernet key over every user's LLM API key + remote-machine SSH credential.
    "CREDENTIAL_ENCRYPTION_KEY",
    # Session cookie Fernet + HMAC — forging a session is full impersonation.
    "WEB_SESSION_ENC_KEY", "WEB_SESSION_SECRET",
    # Offline-grant Fernet (also the session-store/device-login enc fallback).
    "OFFLINE_GRANT_ENC_KEY",
    # HMAC/signing keys (MEMORY_HMAC_KEY is also the txn/MAS/delegation fallback).
    "MEMORY_HMAC_KEY", "TXN_TOKEN_KEY", "MAS_MESSAGE_KEY",
    "DELEGATION_CHILD_SIGNING_KEY",
    # Audit hash-chain HMAC. Critical PRECISELY BECAUSE the DB_* vars stay
    # readable: this key is the only thing that makes writes to audit_events
    # tamper-EVIDENT. A child holding both could rewrite rows and recompute a
    # chain that verify_chain accepts. Read by audit/pii.py at call time.
    "AUDIT_HMAC_SECRET",
    # Optional GitHub PAT used by the desktop-codegen release lookup.
    "GITHUB_TOKEN",
    # OIDC confidential-client + token-exchange service-client secrets.
    "KEYCLOAK_CLIENT_SECRET", "AGENT_SERVICE_CLIENT_SECRET",
    # Voice/media-plane secrets.
    "VOICE_CONTROL_SECRET", "LIVEKIT_API_SECRET", "LIVEKIT_API_KEY",
    # Kept for completeness if ever set as a single URL (the DB_* parts stay).
    "DATABASE_URL",
)

#: Prefixes scrubbed wholesale. ``audit/pii.py`` resolves rotation keys as
#: ``AUDIT_HMAC_SECRET_<KEY_ID_UPPER>`` (e.g. ``AUDIT_HMAC_SECRET_K0``), which no
#: fixed-name list can enumerate — a retired-but-still-set key signs the same
#: chain, so it has to go too.
_SECRET_ENV_PREFIXES = ("AUDIT_HMAC_SECRET_",)


def sandbox_enabled() -> bool:
    """FF_SANDBOX_CODEGEN feature flag (default ON since the H4 remediation).

    The env scrub applies on every platform; the rlimit preexec applies on
    POSIX only. Set FF_SANDBOX_CODEGEN=false to restore the legacy
    secret-inheriting child environment.
    """
    return os.getenv("FF_SANDBOX_CODEGEN", "true").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SandboxLimits:
    address_space_mb: int = 2048   # RLIMIT_AS    — max virtual memory
    file_size_mb: int = 256        # RLIMIT_FSIZE — max single-file write
    open_files: int = 512          # RLIMIT_NOFILE — max file descriptors
    processes: int = 512           # RLIMIT_NPROC  — max child procs/threads (per-uid)
    cpu_seconds: int = 0           # RLIMIT_CPU    — 0 = disabled (server-unsafe)


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
        return v if v >= 0 else default
    except (ValueError, AttributeError):
        return default


def build_limits() -> SandboxLimits:
    """Resource limits from env (SANDBOX_MEM_MB / SANDBOX_FSIZE_MB /
    SANDBOX_NOFILE / SANDBOX_NPROC / SANDBOX_CPU_S), generous server-safe
    defaults that don't impede a normal parser agent."""
    return SandboxLimits(
        address_space_mb=_int_env("SANDBOX_MEM_MB", 2048) or 2048,
        file_size_mb=_int_env("SANDBOX_FSIZE_MB", 256) or 256,
        open_files=_int_env("SANDBOX_NOFILE", 512) or 512,
        processes=_int_env("SANDBOX_NPROC", 512) or 512,
        cpu_seconds=_int_env("SANDBOX_CPU_S", 0),
    )


def _apply_limits(limits: SandboxLimits, res: Any) -> None:
    """Apply the limits via the given ``resource`` module (injected for
    testability). Total — any individual failure is swallowed so the child still
    execs (fail-open); a limit is never raised above the inherited hard cap."""
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
    """A ``preexec_fn`` applying the resource limits in the forked child. Returns
    None on a non-POSIX host (preexec_fn unsupported) or when ``resource`` is
    unavailable, so the caller omits the kwarg and the launch is unchanged."""
    if os.name != "posix":
        return None
    try:
        import resource  # POSIX-only
    except ImportError:
        return None
    return lambda: _apply_limits(limits, resource)


def sandbox_env(base_env: Optional[Dict[str, str]], tmpdir: str) -> Dict[str, str]:
    """A child environment with secrets scrubbed and ``TMP*`` pointed at
    ``tmpdir`` (the per-draft scratch dir)."""
    env = dict(base_env if base_env is not None else os.environ)
    for key in _SECRET_ENV_DENYLIST:
        env.pop(key, None)
    # Iterate a snapshot — popping while iterating the live dict would raise.
    for key in [k for k in env if k.startswith(_SECRET_ENV_PREFIXES)]:
        env.pop(key, None)
    env["TMPDIR"] = tmpdir
    env["TEMP"] = tmpdir
    env["TMP"] = tmpdir
    return env
