"""Hard input/output bounds for the pilot: snapshot size and bytes, query length,
batch and call counts, token estimates, and the optional wall-clock deadline; every
bound refuses work past its limit instead of truncating silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

CHARS_PER_TOKEN_ESTIMATE = 4


class BoundsExceeded(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text or "") / CHARS_PER_TOKEN_ESTIMATE))


@dataclass(frozen=True)
class Bounds:
    max_snapshot_rows: int = 256
    max_row_chars: int = 512
    max_query_chars: int = 512
    max_snapshot_bytes: int = 65536
    max_batch_rows: int = 32
    max_provider_calls: int = 64
    max_retries: int = 1
    max_total_tokens: int = 24000
    max_candidates: int = 32
    max_report_bytes: int = 1 << 20
    runtime_deadline_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        positive = {
            "max_snapshot_rows": self.max_snapshot_rows,
            "max_row_chars": self.max_row_chars,
            "max_query_chars": self.max_query_chars,
            "max_snapshot_bytes": self.max_snapshot_bytes,
            "max_batch_rows": self.max_batch_rows,
            "max_provider_calls": self.max_provider_calls,
            "max_total_tokens": self.max_total_tokens,
            "max_candidates": self.max_candidates,
            "max_report_bytes": self.max_report_bytes,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.max_batch_rows > self.max_snapshot_rows:
            raise ValueError("max_batch_rows must not exceed max_snapshot_rows")
        if self.max_candidates > self.max_snapshot_rows:
            raise ValueError("max_candidates must not exceed max_snapshot_rows")
        if self.runtime_deadline_seconds is not None and self.runtime_deadline_seconds <= 0:
            raise ValueError("runtime_deadline_seconds must be positive when set")

    def validate_query(self, query: str) -> None:
        if not isinstance(query, str) or not query.strip():
            raise BoundsExceeded("query is empty")
        if len(query) > self.max_query_chars:
            raise BoundsExceeded(
                f"query length {len(query)} exceeds max_query_chars {self.max_query_chars}")

    def validate_snapshot(self, rows: List[Dict[str, Any]]) -> int:
        if len(rows) > self.max_snapshot_rows:
            raise BoundsExceeded(
                f"snapshot has {len(rows)} rows, exceeding max_snapshot_rows "
                f"{self.max_snapshot_rows}")
        total_chars = 0
        for row in rows:
            value = row.get("value")
            if not isinstance(value, str):
                raise BoundsExceeded("snapshot row value is not a string")
            if len(value) > self.max_row_chars:
                raise BoundsExceeded(
                    f"row {row.get('id')!r} value length {len(value)} exceeds "
                    f"max_row_chars {self.max_row_chars}")
            total_chars += len(value)
        total_bytes = total_chars + len(rows)
        if total_bytes > self.max_snapshot_bytes:
            raise BoundsExceeded(
                f"snapshot bytes {total_bytes} exceed max_snapshot_bytes "
                f"{self.max_snapshot_bytes}")
        return total_bytes

    def validate_batch(self, texts: List[str]) -> None:
        if len(texts) > self.max_batch_rows:
            raise BoundsExceeded(
                f"batch of {len(texts)} texts exceeds max_batch_rows {self.max_batch_rows}")

    def validate_candidates(self, count: int) -> None:
        if count > self.max_candidates:
            raise BoundsExceeded(
                f"{count} candidates exceed max_candidates {self.max_candidates}")

    def validate_call_count(self, calls: int) -> None:
        if calls > self.max_provider_calls:
            raise BoundsExceeded(
                f"{calls} provider calls exceed max_provider_calls {self.max_provider_calls}")

    def validate_token_total(self, tokens: int) -> None:
        if tokens > self.max_total_tokens:
            raise BoundsExceeded(
                f"estimated tokens {tokens} exceed max_total_tokens {self.max_total_tokens}")

    def validate_report_bytes(self, size: int) -> None:
        if size > self.max_report_bytes:
            raise BoundsExceeded(
                f"report size {size} exceeds max_report_bytes {self.max_report_bytes}")
