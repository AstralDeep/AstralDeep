"""Per-runtime LETS authority hand-off for server-hosted dynamic agents.

A draft/generated agent runs as a supervised child process of the
orchestrator (population ``server_dynamic``).  Under LETS enforce its
protected-executor seam (``generated_lets_executor.load_protected_executor``
and ``BaseA2AAgent._verify_and_claim_protected_request``) reads the admitted
authority binding, its executor audience and its replay-store roots from the
process environment.  This module derives exactly those values from the
Plane binding the lifecycle admitted BEFORE the process was spawned, so the
child can claim the receipts the orchestrator issues for it.

Mirrors the Windows BYO host (``win_agent/byo_host.py::_launch_v3``): the
binding is server-owned and injected explicitly, never inherited, and every
runtime gets its own executor audience, replay database root and authority
anchor root so two runtimes never share a replay store.

Nothing here runs when LETS is off or the admission produced no active
binding: the caller then spawns with the exact pre-existing environment.

Per-runtime root retention
--------------------------
The private roots are NOT removed when the runtime's lease closes.  The
replay database is the runtime's anti-replay state and its evidence of every
receipt it claimed; the lease can be closed while the child is still
draining (``stop_draft_agent`` quiesces BEFORE termination), and deleting a
SQLite database from under a live process is undefined.  Instead
``sweep_dynamic_runtime_roots`` runs at boot — when no server_dynamic child
of the previous orchestrator process can still be alive and every current
runtime gets a fresh UUID — and removes runtime-keyed directories untouched
for ``LETS_EXECUTOR_RUNTIME_RETENTION_DAYS`` (default 30).  The only
immediate removal is ``remove_dynamic_runtime_roots`` for a runtime that was
refused BEFORE spawn: no process ever held those roots, so there is no
evidence to retain.  The authority anchor and the replay database of one
runtime are always swept together; an anchor without its database is
worthless and a database without its anchor can never be re-opened.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from orchestrator.generated_lets_executor import (
    ProtectedExecutorError,
    _identifier_value,
)
from orchestrator.lets_config import LetsHostConfig

logger = logging.getLogger("AstralDeep.LETS.DynamicRuntime")

SERVER_DYNAMIC_POPULATION: Final = "server_dynamic"

#: Boot-time sweep knob: runtime-keyed executor roots untouched for this many
#: days are removed.  ``0`` removes every non-running runtime root at boot.
RETENTION_ENV: Final = "LETS_EXECUTOR_RUNTIME_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS: Final = 30
_MAX_RETENTION_DAYS: Final = 3650
_SECONDS_PER_DAY: Final = 86_400

#: ``LETS_EXECUTOR_INSTANCE_ID`` is re-parsed by the child's ``load_lets_config``
#: with this ceiling (``lets_config._identifier(maximum=128)``).
_MAX_AUDIENCE_LENGTH: Final = 128
#: Runtime ids are process UUIDs; the per-runtime directories are keyed by
#: them, so refuse anything that is not a plain UUID-shaped filename.
_RUNTIME_ID: Final = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")

_AUTHORITY_ENV_KEYS: Final = (
    "ASTRAL_AUTHORITY_OWNER_ID",
    "ASTRAL_AUTHORITY_BINDING_ID",
    "ASTRAL_AUTHORITY_LEASE_ID",
    "ASTRAL_AUTHORITY_LINEAGE_ID",
    "ASTRAL_RUNTIME_COHORT",
    "ASTRAL_RUNTIME_ID",
    "ASTRAL_RUNTIME_GENERATION",
    "LETS_EXECUTOR_INSTANCE_ID",
    "LETS_EXECUTOR_DB_ROOT",
    "LETS_EXECUTOR_AUTHORITY_ROOT",
    "LETS_WARDEN_ID",
)


class DynamicRuntimeAuthorityError(RuntimeError):
    """Stable, content-free refusal to hand authority to a child runtime."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identifier(value: object, code: str) -> str:
    """Apply the CHILD's identifier predicate so a bad id fails pre-spawn.

    ``generated_lets_executor._identifier_value`` is what the spawned runtime
    re-applies to every hand-off variable; reusing it (rather than a looser
    local copy) means internal whitespace or a Unicode category-C character
    is refused here with a named error instead of as an exit-78 child.
    """

    try:
        return _identifier_value(value, code)
    except ProtectedExecutorError as exc:
        raise DynamicRuntimeAuthorityError(exc.code) from None


def derive_dynamic_executor_audience(executor_instance_id: str, runtime_id: str) -> str:
    """Per-runtime receipt audience shared by the orchestrator and the child.

    The orchestrator stamps this on every receipt it requests for the runtime
    (``DispatchRuntime.executor_audience``) and the child verifies against it
    (``LETS_EXECUTOR_INSTANCE_ID``).  The separator is filesystem-neutral
    because both executor paths derive file names from the audience.
    """

    instance = _identifier(executor_instance_id, "executor_instance_id_invalid")
    runtime = _identifier(runtime_id, "runtime_id_invalid")
    if _RUNTIME_ID.fullmatch(runtime) is None:
        raise DynamicRuntimeAuthorityError("runtime_id_invalid")
    audience = f"{instance}--{runtime}"
    if len(audience) > _MAX_AUDIENCE_LENGTH:
        raise DynamicRuntimeAuthorityError("executor_audience_invalid")
    return audience


@dataclass(frozen=True, slots=True)
class DynamicRuntimeAuthority:
    """Exact non-secret authority the child must hold to claim its receipts."""

    owner_id: str
    binding_id: str
    lease_id: str
    lineage_id: str
    agent_id: str
    runtime_id: str
    runtime_generation: int
    executor_audience: str
    population: str = SERVER_DYNAMIC_POPULATION

    @classmethod
    def from_binding(
        cls,
        binding: Any,
        *,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        executor_instance_id: str,
    ) -> "DynamicRuntimeAuthority":
        """Bind one ACTIVE Plane record to the runtime the caller is spawning.

        Every identity is taken from the binding only after it matches the
        caller's own fence, so a stale or foreign binding can never be handed
        to a child.
        """

        state = getattr(getattr(binding, "state", None), "value", None)
        population = getattr(getattr(binding, "population", None), "value", None)
        if state != "active":
            raise DynamicRuntimeAuthorityError("binding_not_active")
        if population != SERVER_DYNAMIC_POPULATION:
            raise DynamicRuntimeAuthorityError("binding_population_mismatch")
        generation = getattr(binding, "runtime_generation", None)
        fence = (
            (getattr(binding, "owner_id", None), owner_id),
            (getattr(binding, "agent_id", None), agent_id),
            (getattr(binding, "runtime_id", None), runtime_id),
        )
        if any(actual != wanted for actual, wanted in fence):
            raise DynamicRuntimeAuthorityError("binding_fence_mismatch")
        if type(generation) is not int or generation < 1:
            raise DynamicRuntimeAuthorityError("binding_generation_invalid")
        return cls(
            owner_id=_identifier(owner_id, "owner_id_invalid"),
            binding_id=_identifier(getattr(binding, "binding_id", None), "binding_id_invalid"),
            lease_id=_identifier(getattr(binding, "lease_id", None), "lease_id_invalid"),
            lineage_id=_identifier(getattr(binding, "lineage_id", None), "lineage_id_invalid"),
            agent_id=_identifier(agent_id, "agent_id_invalid"),
            runtime_id=runtime_id,
            runtime_generation=generation,
            executor_audience=derive_dynamic_executor_audience(
                executor_instance_id, runtime_id
            ),
        )


def _private_subdirectory(root: Path, runtime_id: str, code: str) -> Path:
    path = root / runtime_id
    try:
        if path.is_symlink():
            raise OSError
        path.mkdir(mode=0o700, exist_ok=True)
        # ``mkdir`` honours the umask; pin the mode so a permissive parent
        # umask never widens another runtime's replay state.
        os.chmod(path, stat.S_IRWXU)
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or resolved.parent != root.resolve():
            raise OSError
    except OSError:
        raise DynamicRuntimeAuthorityError(code) from None
    return resolved


def prepare_dynamic_runtime_roots(
    config: LetsHostConfig,
    runtime_id: str,
) -> tuple[Path, Path | None]:
    """Create the runtime's private (0700) executor DB and authority roots.

    They live directly under the orchestrator's own configured roots, keyed
    by the runtime id, so the child's ``load_lets_config`` accepts them and
    no two runtimes share a replay database or an authority anchor.
    """

    if _RUNTIME_ID.fullmatch(runtime_id or "") is None:
        raise DynamicRuntimeAuthorityError("runtime_id_invalid")
    if config.executor_db_root is None:
        raise DynamicRuntimeAuthorityError("executor_db_root_unavailable")
    database_root = _private_subdirectory(
        config.executor_db_root, runtime_id, "executor_db_root_unavailable"
    )
    authority_root: Path | None = None
    if config.executor_authority_root is not None:
        authority_root = _private_subdirectory(
            config.executor_authority_root,
            runtime_id,
            "executor_authority_root_unavailable",
        )
    return database_root, authority_root


def dynamic_runtime_environment(
    base_env: Mapping[str, str] | None,
    authority: DynamicRuntimeAuthority,
    *,
    warden_id: str,
    database_root: Path,
    authority_root: Path | None,
) -> dict[str, str]:
    """Return ``base_env`` (or the process environment) plus the LETS hand-off.

    Variable names are exactly those read by
    ``generated_lets_executor.load_protected_executor`` and
    ``BaseA2AAgent._verify_and_claim_protected_request``.
    """

    environment = dict(os.environ if base_env is None else base_env)
    for key in _AUTHORITY_ENV_KEYS:
        environment.pop(key, None)
    environment.update(
        {
            "ASTRAL_AUTHORITY_OWNER_ID": authority.owner_id,
            "ASTRAL_AUTHORITY_BINDING_ID": authority.binding_id,
            "ASTRAL_AUTHORITY_LEASE_ID": authority.lease_id,
            "ASTRAL_AUTHORITY_LINEAGE_ID": authority.lineage_id,
            "ASTRAL_RUNTIME_COHORT": authority.population,
            "ASTRAL_RUNTIME_ID": authority.runtime_id,
            "ASTRAL_RUNTIME_GENERATION": str(authority.runtime_generation),
            "LETS_EXECUTOR_INSTANCE_ID": authority.executor_audience,
            "LETS_EXECUTOR_DB_ROOT": str(database_root),
            "LETS_WARDEN_ID": _identifier(warden_id, "warden_id_invalid"),
        }
    )
    if authority_root is not None:
        environment["LETS_EXECUTOR_AUTHORITY_ROOT"] = str(authority_root)
    return environment


def _runtime_root_under(root: Path | None, runtime_id: str) -> Path | None:
    """Resolve ``root/runtime_id`` only when it is a real private subdirectory."""

    if root is None or _RUNTIME_ID.fullmatch(runtime_id or "") is None:
        return None
    path = root / runtime_id
    try:
        if path.is_symlink() or not path.is_dir():
            return None
        resolved = path.resolve(strict=True)
        if resolved.parent != root.resolve(strict=True):
            return None
    except OSError:
        return None
    return resolved


def remove_dynamic_runtime_roots(config: Any, runtime_id: str) -> None:
    """Best-effort removal of a runtime's private roots that NO process used.

    Only for a hand-off refused before spawn.  Never call this for a runtime
    that ran: its replay database is retained for the boot-time sweep.
    """

    for root in (
        getattr(config, "executor_db_root", None),
        getattr(config, "executor_authority_root", None),
    ):
        resolved = _runtime_root_under(root, runtime_id)
        if resolved is None:
            continue
        shutil.rmtree(resolved, ignore_errors=True)


def retention_days(environ: Mapping[str, str] | None = None) -> int:
    """Parse ``LETS_EXECUTOR_RUNTIME_RETENTION_DAYS`` (default 30).

    An unusable value falls back to the default rather than to ``0``: a typo
    must never turn the sweep into "delete everything".
    """

    values = os.environ if environ is None else environ
    raw = values.get(RETENTION_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_RETENTION_DAYS
    raw = raw.strip()
    if not raw.isdigit() or int(raw) > _MAX_RETENTION_DAYS:
        logger.warning(
            "LETS: ignoring invalid %s; using default of %d days",
            RETENTION_ENV,
            DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return int(raw)


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for child in path.iterdir():
        try:
            newest = max(newest, child.lstat().st_mtime)
        except OSError:
            continue
    return newest


def sweep_dynamic_runtime_roots(
    config: Any,
    *,
    keep: frozenset[str] | set[str] = frozenset(),
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> int:
    """Remove runtime-keyed executor roots older than the retention window.

    Run at boot.  ``keep`` names runtime ids that are currently running and
    must never be touched.  Only UUID-shaped non-symlink subdirectories of
    the configured roots are candidates; anything else under those roots is
    the operator's and is left alone.  A runtime's replay-database root and
    authority root are judged TOGETHER (newest mtime across both) and
    removed together, so a sweep can never orphan one half.  Returns the
    number of directories removed.  Never raises: a sweep failure is logged,
    not fatal to boot.
    """

    days = retention_days(environ)
    cutoff = (time.time() if now is None else now) - days * _SECONDS_PER_DAY
    roots = [
        Path(root)
        for root in (
            getattr(config, "executor_db_root", None),
            getattr(config, "executor_authority_root", None),
        )
        if root is not None
    ]
    candidates: dict[str, list[Path]] = {}
    for root in roots:
        try:
            names = [entry.name for entry in root.iterdir()]
        except OSError:
            logger.warning("LETS: runtime root sweep could not list a root", exc_info=True)
            continue
        for name in names:
            if name in keep:
                continue
            resolved = _runtime_root_under(root, name)
            if resolved is not None:
                candidates.setdefault(name, []).append(resolved)
    removed = 0
    for paths in candidates.values():
        try:
            if max(_newest_mtime(path) for path in paths) > cutoff:
                continue
        except OSError:
            continue
        for path in paths:
            try:
                shutil.rmtree(path)
            except OSError:
                logger.warning("LETS: runtime root sweep skipped one directory", exc_info=True)
                continue
            removed += 1
    if removed:
        logger.info(
            "LETS: swept %d per-runtime executor root(s) older than %d day(s)",
            removed,
            days,
        )
    return removed


__all__ = (
    "DEFAULT_RETENTION_DAYS",
    "DynamicRuntimeAuthority",
    "DynamicRuntimeAuthorityError",
    "RETENTION_ENV",
    "SERVER_DYNAMIC_POPULATION",
    "derive_dynamic_executor_audience",
    "dynamic_runtime_environment",
    "prepare_dynamic_runtime_roots",
    "remove_dynamic_runtime_roots",
    "retention_days",
    "sweep_dynamic_runtime_roots",
)
