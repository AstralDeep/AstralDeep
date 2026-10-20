"""Registers the answer-behavior drivers: frozen (deterministic CI provider double) and
live (opt-in, bounded, non-gating real-model execution through the extraction path).
"""

from __future__ import annotations

from typing import Any

from answer_behavior.drivers.base import Driver
from answer_behavior.drivers.frozen import FrozenDriver
from answer_behavior.drivers.live import LIVE_ENV_FLAG, LiveDriver

DRIVER_MODES = ("frozen", "live")


def get_driver(mode: str, **kwargs: Any) -> Driver:
    if mode == "frozen":
        return FrozenDriver(**kwargs)
    if mode == "live":
        return LiveDriver(**kwargs)
    raise ValueError(f"unknown driver mode: {mode!r} (expected one of {DRIVER_MODES})")


__all__ = ["DRIVER_MODES", "LIVE_ENV_FLAG", "Driver", "FrozenDriver", "LiveDriver", "get_driver"]
