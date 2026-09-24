"""Pure planning/gather logic for fanning a multi-item task across isolated per-item
sub-runs and verifying the reassembled count, avoiding the model's tendency to
fabricate entries in a single large context; used by turn_hooks.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

# Above this, models start fabricating entries to hit count
FABRICATION_THRESHOLD = 8


def fanout_enabled() -> bool:
    return os.getenv("FF_ASYNC_FANOUT", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def should_fan_out(item_count: int, *, threshold: int = 8) -> bool:
    return item_count > threshold


def decompose(items: List[Any], *, max_parallel: int = 8) -> List[List[Any]]:
    if max_parallel <= 0:
        max_parallel = 1
    return [items[i : i + max_parallel] for i in range(0, len(items), max_parallel)]


@dataclass(frozen=True)
class GatherResult:
    items: List[Any]
    expected: int
    complete: bool
    missing: int
    duplicates: int


def _flatten(results: List[Any]) -> List[Any]:
    flat: List[Any] = []
    for entry in results:
        if isinstance(entry, (list, tuple)):
            flat.extend(entry)
        else:
            flat.append(entry)
    return flat


def gather(
    results: List[Any],
    *,
    expected: int,
    key: Optional[Callable[[Any], Any]] = None,
) -> GatherResult:
    key_fn: Callable[[Any], Any] = key if key is not None else (lambda x: str(x))

    flat = _flatten(results)
    seen: set = set()
    unique: List[Any] = []
    for item in flat:
        k = key_fn(item)
        if k in seen:
            continue
        seen.add(k)
        unique.append(item)

    unique_count = len(unique)
    duplicates = len(flat) - unique_count
    missing = max(0, expected - unique_count)
    return GatherResult(
        items=unique,
        expected=expected,
        complete=(missing == 0),
        missing=missing,
        duplicates=duplicates,
    )


def verify_count(expected: int, produced: List[Any]) -> bool:
    return len(produced) >= expected
