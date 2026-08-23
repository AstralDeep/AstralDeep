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
"""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from orchestrator.lets_config import LetsHostConfig

SERVER_DYNAMIC_POPULATION: Final = "server_dynamic"

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
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise DynamicRuntimeAuthorityError(code)
    return value


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


__all__ = (
    "DynamicRuntimeAuthority",
    "DynamicRuntimeAuthorityError",
    "SERVER_DYNAMIC_POPULATION",
    "derive_dynamic_executor_audience",
    "dynamic_runtime_environment",
    "prepare_dynamic_runtime_roots",
)
