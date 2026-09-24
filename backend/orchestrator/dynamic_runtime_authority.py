"""Derives a server-hosted generated agent's LETS executor audience and private
replay/authority directories from the Plane binding admitted before the process
spawns; mirrors win_agent/byo_host.py's explicit hand-off.
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

RETENTION_ENV: Final = "LETS_EXECUTOR_RUNTIME_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS: Final = 30
_MAX_RETENTION_DAYS: Final = 3650
_SECONDS_PER_DAY: Final = 86_400

_MAX_AUDIENCE_LENGTH: Final = 128
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
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _identifier(value: object, code: str) -> str:
    try:
        return _identifier_value(value, code)
    except ProtectedExecutorError as exc:
        raise DynamicRuntimeAuthorityError(exc.code) from None


def derive_dynamic_executor_audience(executor_instance_id: str, runtime_id: str) -> str:
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
        # mkdir honors umask; chmod stops it over-widening this dir
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
    for root in (
        getattr(config, "executor_db_root", None),
        getattr(config, "executor_authority_root", None),
    ):
        resolved = _runtime_root_under(root, runtime_id)
        if resolved is None:
            continue
        shutil.rmtree(resolved, ignore_errors=True)


def retention_days(environ: Mapping[str, str] | None = None) -> int:
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
