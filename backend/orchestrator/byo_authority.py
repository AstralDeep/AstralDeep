"""Server-owned v3 authority binding for personal-agent executors."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Final

from shared.protocol import RuntimeFence


BYO_EXECUTOR_AUDIENCE_PREFIX: Final = "astraldeep.byo-executor/v1:"
_AUDIENCE_DOMAIN: Final = "astraldeep.byo_user"


class ByoRuntimeAuthorityError(RuntimeError):
    """A delivery cannot prove one exact active BYO authority binding."""


def _uuid4(value: object, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ByoRuntimeAuthorityError(f"{field}_invalid") from None
    if (
        not isinstance(value, str)
        or parsed.version != 4
        or parsed.variant != uuid.RFC_4122
        or str(parsed) != value
    ):
        raise ByoRuntimeAuthorityError(f"{field}_invalid")
    return value


def derive_byo_executor_audience(host_id: str, host_session_id: str) -> str:
    """Derive the final executor identity from authenticated server fences.

    The desktop recomputes the same value from its accepted stable host ID and
    server-issued session ID. No card, tool, delivery, or client-proposed
    audience is trusted.
    """

    host = _uuid4(host_id, "host_id")
    session = _uuid4(host_session_id, "host_session_id")
    material = f"{_AUDIENCE_DOMAIN}\0{host}\0{session}".encode("utf-8")
    return BYO_EXECUTOR_AUDIENCE_PREFIX + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ByoRuntimeAuthority:
    """Exact non-secret authority object carried beside a v3 delivery fence."""

    owner_id: str
    binding_id: str
    lease_id: str
    lineage_id: str
    population: str
    executor_audience: str
    agent_id: str
    runtime_instance_id: str
    lifecycle_generation: int

    def __post_init__(self) -> None:
        for value, code in (
            (self.owner_id, "owner_id_invalid"),
            (self.binding_id, "binding_id_invalid"),
            (self.lease_id, "lease_id_invalid"),
            (self.lineage_id, "lineage_id_invalid"),
            (self.agent_id, "agent_id_invalid"),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ByoRuntimeAuthorityError(code)
        if self.population != "byo_user":
            raise ByoRuntimeAuthorityError("population_invalid")
        audience_digest = (
            self.executor_audience.removeprefix(BYO_EXECUTOR_AUDIENCE_PREFIX)
            if isinstance(self.executor_audience, str)
            else ""
        )
        if (
            not isinstance(self.executor_audience, str)
            or not self.executor_audience.startswith(BYO_EXECUTOR_AUDIENCE_PREFIX)
            or len(audience_digest) != 64
            or any(character not in "0123456789abcdef" for character in audience_digest)
        ):
            raise ByoRuntimeAuthorityError("executor_audience_invalid")
        _uuid4(self.runtime_instance_id, "runtime_instance_id")
        if (
            type(self.lifecycle_generation) is not int
            or self.lifecycle_generation < 1
        ):
            raise ByoRuntimeAuthorityError("lifecycle_generation_invalid")

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    @classmethod
    def from_active_binding(
        cls,
        binding: Any,
        *,
        fence: RuntimeFence,
        owner_id: str,
    ) -> ByoRuntimeAuthority:
        """Bind one active Plane record to the selected host/runtime fence."""

        state = getattr(getattr(binding, "state", None), "value", None)
        population = getattr(getattr(binding, "population", None), "value", None)
        expected = (
            (getattr(binding, "owner_id", None), owner_id),
            (getattr(binding, "agent_id", None), fence.agent_id),
            (getattr(binding, "runtime_id", None), fence.runtime_instance_id),
            (
                getattr(binding, "runtime_generation", None),
                fence.lifecycle_generation,
            ),
        )
        if state != "active":
            raise ByoRuntimeAuthorityError("binding_not_active")
        if population != "byo_user":
            raise ByoRuntimeAuthorityError("binding_population_mismatch")
        if any(actual != wanted for actual, wanted in expected):
            raise ByoRuntimeAuthorityError("binding_fence_mismatch")
        return cls(
            owner_id=owner_id,
            binding_id=str(getattr(binding, "binding_id", "")),
            lease_id=str(getattr(binding, "lease_id", "")),
            lineage_id=str(getattr(binding, "lineage_id", "")),
            population="byo_user",
            executor_audience=derive_byo_executor_audience(
                fence.host_id,
                fence.host_session_id,
            ),
            agent_id=fence.agent_id,
            runtime_instance_id=fence.runtime_instance_id,
            lifecycle_generation=fence.lifecycle_generation,
        )


__all__ = (
    "BYO_EXECUTOR_AUDIENCE_PREFIX",
    "ByoRuntimeAuthority",
    "ByoRuntimeAuthorityError",
    "derive_byo_executor_audience",
)
