"""Bounds the polling interval for the Plane assignment recovery worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bounded(name: str, default: int, lower: int, upper: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a bounded integer") from exc
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    tick_seconds: int = 15

    @classmethod
    def from_environment(cls) -> RunnerConfig:
        return cls(tick_seconds=_bounded("PERSISTENT_AGENTS_TICK_SECONDS", 15, 1, 30))
