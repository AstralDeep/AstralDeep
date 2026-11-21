"""Typed, pre-SQL-validated records for the optional scheduled-job policy contract:
per-episode allowances, admission results, and Stop outcomes. Carry no instruction or
source content; consumed by repositories/scheduler.py.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

_LIMIT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

MAX_RUNS_LIMIT: Final = 1_000_000
MAX_OUTSTANDING_EPISODES_LIMIT: Final = 64
MAX_PER_EPISODE_LIMIT_ENTRIES: Final = 32
MAX_PER_EPISODE_LIMIT_VALUE: Final = 2_147_483_647
MAX_SPEND: Final = MAX_RUNS_LIMIT
_MAX_BIGINT_CAS: Final = 9_007_199_254_740_991

ADMISSION_REASONS: Final = frozenset(
    {
        "admitted",
        "replayed",
        "policy_missing",
        "terminal_stop",
        "episode_outstanding",
        "allowance_exhausted",
    }
)


def _bounded_int(name: str, value: object, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _flag(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _owner(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("owner_id must be a non-empty string of at most 512 characters")
    return value


def _uuid4(name: str, value: object) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a canonical UUIDv4") from exc
    if parsed.int == 0 or str(parsed) != value or parsed.version != 4:
        raise ValueError(f"{name} must be a canonical non-nil UUIDv4")
    return str(value)


def normalize_per_episode_limits(value: object) -> tuple[tuple[str, int], ...]:
    if isinstance(value, Mapping):
        items = tuple(value.items())
    elif isinstance(value, tuple):
        items = value
    else:
        raise ValueError("per_episode_limits must be a mapping or pair tuple")
    if len(items) > MAX_PER_EPISODE_LIMIT_ENTRIES:
        raise ValueError("per_episode_limits declares too many entries")
    normalized: dict[str, int] = {}
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("per_episode_limits entries must be name/limit pairs")
        name, limit = item
        if not isinstance(name, str) or _LIMIT_KEY.fullmatch(name) is None:
            raise ValueError("per_episode_limits names must be bounded snake_case")
        if name in normalized:
            raise ValueError("per_episode_limits names must be unique")
        normalized[name] = _bounded_int(
            f"per_episode_limits[{name}]", limit, 0, MAX_PER_EPISODE_LIMIT_VALUE
        )
    return tuple(sorted(normalized.items()))


# put_job_policy must never lower a charge or clear a stop
@dataclass(frozen=True, slots=True)
class ScheduledJobPolicy:
    job_id: str
    owner_id: str
    version: int
    max_runs: int | None
    admitted_runs: int
    per_episode_limits: tuple[tuple[str, int], ...]
    max_outstanding_episodes: int
    monitor_changes: bool
    definition_revision: int
    terminal_stop: bool
    last_assignment_id: str | None
    updated_at: int

    def __post_init__(self) -> None:
        _uuid4("job_id", self.job_id)
        _owner(self.owner_id)
        _bounded_int("version", self.version, 1, _MAX_BIGINT_CAS)
        if self.max_runs is not None:
            _bounded_int("max_runs", self.max_runs, 1, MAX_RUNS_LIMIT)
        ceiling = MAX_RUNS_LIMIT if self.max_runs is None else self.max_runs
        _bounded_int("admitted_runs", self.admitted_runs, 0, ceiling)
        object.__setattr__(
            self, "per_episode_limits", normalize_per_episode_limits(self.per_episode_limits)
        )
        _bounded_int(
            "max_outstanding_episodes",
            self.max_outstanding_episodes,
            1,
            MAX_OUTSTANDING_EPISODES_LIMIT,
        )
        _flag("monitor_changes", self.monitor_changes)
        _bounded_int("definition_revision", self.definition_revision, 1, _MAX_BIGINT_CAS)
        _flag("terminal_stop", self.terminal_stop)
        if self.last_assignment_id is not None:
            _uuid4("last_assignment_id", self.last_assignment_id)
        _bounded_int("updated_at", self.updated_at, 0, 2**63 - 1)

    @property
    def remaining_runs(self) -> int | None:
        return None if self.max_runs is None else self.max_runs - self.admitted_runs

    def limits(self) -> dict[str, int]:
        return dict(self.per_episode_limits)


@dataclass(frozen=True, slots=True)
class EpisodeAdmission:
    job_id: str
    owner_id: str
    occurrence_id: str
    assignment_id: str
    admitted: bool
    created: bool
    reason: str
    spend: int
    policy: ScheduledJobPolicy | None

    def __post_init__(self) -> None:
        _uuid4("job_id", self.job_id)
        _owner(self.owner_id)
        _uuid4("occurrence_id", self.occurrence_id)
        _uuid4("assignment_id", self.assignment_id)
        _flag("admitted", self.admitted)
        _flag("created", self.created)
        if self.reason not in ADMISSION_REASONS:
            raise ValueError("admission reason is not supported")
        _bounded_int("spend", self.spend, 0, MAX_SPEND)
        if self.admitted != (self.reason in {"admitted", "replayed"}):
            raise ValueError("admission flag and reason disagree")
        if self.created != (self.reason == "admitted"):
            raise ValueError("created flag and reason disagree")
        if self.policy is not None and not isinstance(self.policy, ScheduledJobPolicy):
            raise ValueError("admission policy must be a ScheduledJobPolicy")


@dataclass(frozen=True, slots=True)
class JobStopOutcome:
    job_id: str
    owner_id: str
    stopped: bool
    policy: ScheduledJobPolicy
    cancelled_occurrence_ids: tuple[str, ...]
    cancelled_operation_ids: tuple[str, ...]
    outstanding_assignment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _uuid4("job_id", self.job_id)
        _owner(self.owner_id)
        _flag("stopped", self.stopped)
        if not isinstance(self.policy, ScheduledJobPolicy) or not self.policy.terminal_stop:
            raise ValueError("stop outcome must carry a terminally stopped policy")
        for name in (
            "cancelled_occurrence_ids",
            "cancelled_operation_ids",
            "outstanding_assignment_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise ValueError(f"{name} must be a tuple of unique identifiers")
            for value in values:
                _uuid4(name, value)


__all__ = (
    "ADMISSION_REASONS",
    "MAX_OUTSTANDING_EPISODES_LIMIT",
    "MAX_PER_EPISODE_LIMIT_ENTRIES",
    "MAX_PER_EPISODE_LIMIT_VALUE",
    "MAX_RUNS_LIMIT",
    "MAX_SPEND",
    "EpisodeAdmission",
    "JobStopOutcome",
    "ScheduledJobPolicy",
    "normalize_per_episode_limits",
)
