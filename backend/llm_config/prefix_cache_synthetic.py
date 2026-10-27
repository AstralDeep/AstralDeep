"""Deterministic offline simulation of provider-side ephemeral prefix caching over a
bundled synthetic traffic fixture: reports hits, token cost, and modeled latency with
exact identities and denominators, and states an a priori effectiveness threshold so an
ineffective result is reported as such. This is a simulation, not a live measurement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

SYNTHETIC_REPORT_VERSION = "prefix-cache-synthetic-v1"
SIMULATED_CACHE_TTL_SECONDS = 300
CACHE_WRITE_COST_MULTIPLIER = 1.25
CACHE_READ_COST_MULTIPLIER = 0.10
MODEL_INPUT_COST_MULTIPLIER = 1.0
MODEL_OUTPUT_COST_MULTIPLIER = 1.0
UNCACHED_INPUT_LATENCY_MS_PER_TOKEN = 0.20
CACHED_READ_LATENCY_MS_PER_TOKEN = 0.05
EFFECTIVENESS_THRESHOLD_PERCENT = 10.0


@dataclass(frozen=True, slots=True)
class SyntheticRequest:
    at_seconds: int
    owner_id: str
    prefix_id: str
    prefix_tokens: int
    dynamic_tokens: int
    output_tokens: int
    cached: bool


@dataclass(slots=True)
class _StoreEntry:
    expires_at: int


@dataclass(slots=True)
class SyntheticReport:
    report_version: str
    fixture_id: str
    profile: str
    policy_revision: str
    ttl_seconds: int
    requests: int
    eligible_requests: int
    hits: int
    misses: int
    writes: int
    expired: int
    cached_total_cost_tokens: float
    uncached_total_cost_tokens: float
    savings_percent: Optional[float]
    cached_modeled_latency_ms: float
    uncached_modeled_latency_ms: float
    verdict: str
    assumptions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_version": self.report_version,
            "fixture_id": self.fixture_id,
            "profile": self.profile,
            "policy_revision": self.policy_revision,
            "ttl_seconds": self.ttl_seconds,
            "denominators": {
                "requests": self.requests,
                "eligible_requests": self.eligible_requests,
                "hits": self.hits,
                "misses": self.misses,
                "writes": self.writes,
                "expired": self.expired,
            },
            "cached_total_cost_tokens": self.cached_total_cost_tokens,
            "uncached_total_cost_tokens": self.uncached_total_cost_tokens,
            "savings_percent": self.savings_percent,
            "cached_modeled_latency_ms": self.cached_modeled_latency_ms,
            "uncached_modeled_latency_ms": self.uncached_modeled_latency_ms,
            "verdict": self.verdict,
            "assumptions": self.assumptions,
            "basis": "offline simulation over a bundled synthetic fixture; not a live provider measurement",
        }


def _prefix_digest(prefix_id: str) -> str:
    return hashlib.sha256(prefix_id.encode("utf-8")).hexdigest()


def _cost(
    *,
    prefix_tokens: int,
    dynamic_tokens: int,
    output_tokens: int,
    read_tokens: int,
    written_tokens: int,
) -> float:
    uncached_input = max(0, prefix_tokens - read_tokens - written_tokens)
    return (
        CACHE_READ_COST_MULTIPLIER * read_tokens
        + CACHE_WRITE_COST_MULTIPLIER * written_tokens
        + MODEL_INPUT_COST_MULTIPLIER * uncached_input
        + MODEL_INPUT_COST_MULTIPLIER * dynamic_tokens
        + MODEL_OUTPUT_COST_MULTIPLIER * output_tokens
    )


def _latency_ms(*, read_tokens: int, processed_tokens: int, dynamic_tokens: int, output_tokens: int) -> float:
    processed = max(0, processed_tokens - read_tokens)
    return (
        CACHED_READ_LATENCY_MS_PER_TOKEN * read_tokens
        + UNCACHED_INPUT_LATENCY_MS_PER_TOKEN * processed
        + UNCACHED_INPUT_LATENCY_MS_PER_TOKEN * dynamic_tokens
        + UNCACHED_INPUT_LATENCY_MS_PER_TOKEN * output_tokens
    )


def simulate(
    fixture: list[SyntheticRequest],
    *,
    fixture_id: str,
    policy_revision: str = "synthetic",
    profile: str = "native_messages",
) -> SyntheticReport:
    store: dict[str, _StoreEntry] = {}
    hits = misses = writes = expired = eligible = 0
    cached_cost = uncached_cost = 0.0
    cached_latency = uncached_latency = 0.0

    for request in sorted(fixture, key=lambda item: item.at_seconds):
        uncached_cost += MODEL_INPUT_COST_MULTIPLIER * (request.prefix_tokens + request.dynamic_tokens)
        uncached_cost += MODEL_OUTPUT_COST_MULTIPLIER * request.output_tokens
        uncached_latency += _latency_ms(
            read_tokens=0,
            processed_tokens=request.prefix_tokens,
            dynamic_tokens=request.dynamic_tokens,
            output_tokens=request.output_tokens,
        )
        if not request.cached:
            cached_cost += MODEL_INPUT_COST_MULTIPLIER * (request.prefix_tokens + request.dynamic_tokens)
            cached_cost += MODEL_OUTPUT_COST_MULTIPLIER * request.output_tokens
            cached_latency += _latency_ms(
                read_tokens=0,
                processed_tokens=request.prefix_tokens,
                dynamic_tokens=request.dynamic_tokens,
                output_tokens=request.output_tokens,
            )
            continue
        eligible += 1
        digest = _prefix_digest(request.prefix_id)
        entry = store.get(digest)
        if entry is not None and entry.expires_at <= request.at_seconds:
            expired += 1
            entry = None
        if entry is not None:
            hits += 1
            read = request.prefix_tokens
            written = 0
        else:
            misses += 1
            writes += 1
            read = 0
            written = request.prefix_tokens
            store[digest] = _StoreEntry(expires_at=request.at_seconds + SIMULATED_CACHE_TTL_SECONDS)
        cached_cost += _cost(
            prefix_tokens=request.prefix_tokens,
            dynamic_tokens=request.dynamic_tokens,
            output_tokens=request.output_tokens,
            read_tokens=read,
            written_tokens=written,
        )
        cached_latency += _latency_ms(
            read_tokens=read,
            processed_tokens=request.prefix_tokens,
            dynamic_tokens=request.dynamic_tokens,
            output_tokens=request.output_tokens,
        )

    savings = (
        (uncached_cost - cached_cost) / uncached_cost * 100.0
        if uncached_cost > 0
        else None
    )
    if savings is None:
        verdict = "indeterminate"
    elif savings >= EFFECTIVENESS_THRESHOLD_PERCENT:
        verdict = "effective"
    else:
        verdict = "ineffective"
    return SyntheticReport(
        report_version=SYNTHETIC_REPORT_VERSION,
        fixture_id=fixture_id,
        profile=profile,
        policy_revision=policy_revision,
        ttl_seconds=SIMULATED_CACHE_TTL_SECONDS,
        requests=len(fixture),
        eligible_requests=eligible,
        hits=hits,
        misses=misses,
        writes=writes,
        expired=expired,
        cached_total_cost_tokens=round(cached_cost, 3),
        uncached_total_cost_tokens=round(uncached_cost, 3),
        savings_percent=round(savings, 3) if savings is not None else None,
        cached_modeled_latency_ms=round(cached_latency, 3),
        uncached_modeled_latency_ms=round(uncached_latency, 3),
        verdict=verdict,
        assumptions={
            "cache_write_cost_multiplier": CACHE_WRITE_COST_MULTIPLIER,
            "cache_read_cost_multiplier": CACHE_READ_COST_MULTIPLIER,
            "model_input_cost_multiplier": MODEL_INPUT_COST_MULTIPLIER,
            "model_output_cost_multiplier": MODEL_OUTPUT_COST_MULTIPLIER,
            "uncached_input_latency_ms_per_token": UNCACHED_INPUT_LATENCY_MS_PER_TOKEN,
            "cached_read_latency_ms_per_token": CACHED_READ_LATENCY_MS_PER_TOKEN,
            "effectiveness_threshold_percent": EFFECTIVENESS_THRESHOLD_PERCENT,
        },
    )


def bundled_fixture() -> tuple[str, list[SyntheticRequest]]:
    fixture_id = "synthetic-stable-prefix-two-owners-v1"
    fixture: list[SyntheticRequest] = []
    at = 0
    for turn in range(12):
        for owner in ("owner-a", "owner-b"):
            fixture.append(SyntheticRequest(
                at_seconds=at,
                owner_id=owner,
                prefix_id="approved-core-v1",
                prefix_tokens=2400,
                dynamic_tokens=180 + 40 * turn,
                output_tokens=220,
                cached=True,
            ))
            at += 45
    fixture.append(SyntheticRequest(
        at_seconds=at + 400,
        owner_id="owner-a",
        prefix_id="approved-core-v1",
        prefix_tokens=2400,
        dynamic_tokens=200,
        output_tokens=220,
        cached=False,
    ))
    return fixture_id, fixture


def ineffective_fixture() -> tuple[str, list[SyntheticRequest]]:
    fixture_id = "synthetic-single-shot-uncached-v1"
    fixture = [
        SyntheticRequest(
            at_seconds=60 * index,
            owner_id=f"owner-{index}",
            prefix_id=f"unique-{index}",
            prefix_tokens=1500,
            dynamic_tokens=900,
            output_tokens=220,
            cached=True,
        )
        for index in range(6)
    ]
    return fixture_id, fixture


def render_report(report: SyntheticReport) -> str:
    return json.dumps(report.to_dict(), sort_keys=True, indent=2)


def main() -> int:
    fixture_id, fixture = bundled_fixture()
    print(render_report(simulate(fixture, fixture_id=fixture_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
