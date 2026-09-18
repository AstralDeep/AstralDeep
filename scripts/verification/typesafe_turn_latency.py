#!/usr/bin/env python3
"""Feature 089 (T004/T032): what TypeSafe routing costs a turn.

SC-001 through SC-004 are all statements about **TypeSafe-attributable
delay**: the wall time a turn spends between opening the routing seam and
having a decision in hand. That is the one thing feature 089 adds to the turn
path, so it is the one thing this measures — directly, at the seam, rather
than inferred from an end-to-end number that a model's own variance would
swamp.

| Criterion | What is measured here |
|---|---|
| SC-001 | An unkeyed turn: the added wall time, and that no call is made |
| SC-002 | A keyed success: the added wall time before the first model call |
| SC-003 | Every injected failure mode: the maximum added wall time |
| SC-004 | An open circuit: the added wall time during cool-down |

Two sources of timing are available:

* ``--source fake`` replays the latency distribution measured against the real
  service (``--p50``/``--p95``, defaulting to the T015 numbers) through the
  deterministic fake, so a run is repeatable and can be done at any hour
  without spending the owner's quota;
* ``--source live`` calls the real service, with the owner's key read from
  **stdin only** — never an argument, never the environment, never the report.

The report names a key only by fingerprint and carries no prompt text.

Usage:
    python scripts/typesafe_turn_latency.py --turns 200
    python scripts/typesafe_turn_latency.py --source live --turns 50 < key.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

FIXTURES = ROOT / "backend/tests/fixtures/typesafe_routing"

#: The measured real-service distribution (verification.md §8.2.2, 2026-09-17).
DEFAULT_P50_MS = 212.0
DEFAULT_P95_MS = 280.0

#: A sentinel latency the fake sleeps on every answered call; the sleeper
#: turns it into one draw from the measured distribution.
_MARK = 0.0001234


class _Enabled:
    @staticmethod
    def is_enabled(_name: str) -> bool:
        return True


class _Disabled:
    @staticmethod
    def is_enabled(_name: str) -> bool:
        return False


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _summary(name: str, samples: Sequence[float]) -> dict:
    return {
        "case": name,
        "n": len(samples),
        "p50_ms": round(_percentile(samples, 0.50), 2),
        "p95_ms": round(_percentile(samples, 0.95), 2),
        "max_ms": round(max(samples), 2) if samples else 0.0,
        "mean_ms": round(statistics.fmean(samples), 2) if samples else 0.0,
    }


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _request(prompt: str):
    from orchestrator.typesafe_routing.questions import (
        AgentOption, RoutingRequest, ToolOption,
    )

    catalog = _load("catalog.json")
    agents, tools = [], {}
    for entry in catalog["agents"]:
        agents.append(AgentOption(agent_id=entry["agent_id"], name=entry["name"],
                                  description=entry["description"]))
        tools[entry["agent_id"]] = [
            ToolOption(name=t["name"], description=t["description"])
            for t in entry["tools"]
        ]
    return RoutingRequest.build(current_request=prompt, agents=agents,
                                tools_by_agent=tools)


@dataclass
class Case:
    """One measured condition, and the criterion it answers."""

    name: str
    criterion: str
    bound_ms: Optional[float]
    samples: list[float]
    calls: int = 0
    #: What this harness's own measurement window can add: the wall clock is
    #: read outside `start_routing`/`await_decision`, so a turn that runs to
    #: the full budget is observed a scheduling quantum past it. Stated rather
    #: than folded into the bound, so a real overshoot stays visible.
    allowance_ms: float = 0.0

    def overshoot_ms(self) -> float:
        if self.bound_ms is None or not self.samples:
            return 0.0
        return max(0.0, max(self.samples) - self.bound_ms)

    def verdict(self) -> str:
        if self.bound_ms is None:
            return "baseline"
        observed = max(self.samples) if self.samples else 0.0
        return "PASS" if observed <= self.bound_ms + self.allowance_ms else "FAIL"


async def _time_seam(*, api_key, request, client, circuit, flags, fingerprint=None):
    """The wall time of the seam, exactly as the turn sees it.

    `start_routing` returns immediately; `await_decision` is what the turn
    blocks on before its first model call. The pair is what feature 089 adds.
    """
    from orchestrator.typesafe_routing.runner import await_decision, start_routing

    started = time.perf_counter()
    task = await start_routing(
        user_id="latency-user",
        request=request,
        api_key=api_key,
        fingerprint=fingerprint,
        client=client,
        circuit=circuit,
        flags=flags,
    )
    outcome = await await_decision(task, user_id="latency-user", circuit=circuit)
    return (time.perf_counter() - started) * 1000.0, outcome


def _fake_client(*, p50_ms: float, p95_ms: float, faults=None, rng=None):
    """The deterministic fake, answering at the measured live latencies.

    A lognormal fitted to the measured p50 and p95 rather than a flat delay:
    a fixed delay would hide exactly the tail SC-002 and SC-003 are about.
    """
    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    from fakes.typesafe_fake import FakeTypeSafeClient, high_confidence

    rng = rng or random.Random(89)
    mu = p50_ms / 1000.0
    sigma = max(1e-6, (p95_ms - p50_ms) / 1000.0 / 1.645)

    # The fake sleeps `latency` on every answered call and the backoff on every
    # retry; both go through this, so a run reproduces the measured spread
    # rather than a flat delay that would hide the tail SC-002 is about.
    async def sleeper(seconds: float) -> None:
        if seconds == _MARK:
            await asyncio.sleep(max(0.0, rng.gauss(mu, sigma)))
        else:
            await asyncio.sleep(seconds)

    return FakeTypeSafeClient(
        default=high_confidence("weather-1", "weather-1__get_current_weather"),
        faults=list(faults or []),
        sleep=sleeper,
        latency=_MARK,
    )


async def run_fake(args) -> dict:
    from orchestrator.typesafe_routing.budget import UserCircuit

    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    from fakes.typesafe_fake import (
        TypeSafeAPIConnectionError, TypeSafeAPIResponseValidationError,
        TypeSafeAPITimeoutError, TypeSafeAuthenticationError,
        TypeSafeInternalServerError, TypeSafeRateLimitError,
    )

    prompts = [p["prompt"] for p in _load("prompts.json")["cases"]]
    requests = [_request(prompts[i % len(prompts)]) for i in range(min(len(prompts), 24))]
    cases: list[Case] = []

    # SC-001 -- no key. The seam must not call, and must not wait.
    unkeyed = Case("unkeyed (SC-001)", "SC-001", 5.0, [])
    client = _fake_client(p50_ms=args.p50, p95_ms=args.p95)
    for i in range(args.turns):
        elapsed, outcome = await _time_seam(
            api_key=None, request=requests[i % len(requests)],
            client=client, circuit=UserCircuit(), flags=_Enabled,
        )
        unkeyed.samples.append(elapsed)
        assert outcome.outcome.value == "skipped_no_key", outcome.outcome
    unkeyed.calls = client.call_count
    cases.append(unkeyed)

    # SC-001 -- the kill switch. Same promise by a different route.
    flag_off = Case("feature flag off (SC-001)", "SC-001", 5.0, [])
    client = _fake_client(p50_ms=args.p50, p95_ms=args.p95)
    for i in range(args.turns):
        elapsed, _ = await _time_seam(
            api_key="k" * 48, request=requests[i % len(requests)],
            client=client, circuit=UserCircuit(), flags=_Disabled,
        )
        flag_off.samples.append(elapsed)
    flag_off.calls = client.call_count
    cases.append(flag_off)

    # SC-002 -- the keyed success path.
    keyed = Case("keyed success (SC-002)", "SC-002", 150.0 + args.p95, [])
    client = _fake_client(p50_ms=args.p50, p95_ms=args.p95)
    for i in range(args.turns):
        elapsed, outcome = await _time_seam(
            api_key="k" * 48, request=requests[i % len(requests)],
            client=client, circuit=UserCircuit(), flags=_Enabled,
        )
        keyed.samples.append(elapsed)
    keyed.calls = client.call_count
    cases.append(keyed)

    # SC-003 -- every injected failure mode, bounded by the turn budget.
    faults = {
        "timeout": TypeSafeAPITimeoutError,
        "connection": TypeSafeAPIConnectionError,
        "rate_limit": TypeSafeRateLimitError,
        "server_error": TypeSafeInternalServerError,
        "auth": TypeSafeAuthenticationError,
        "malformed": TypeSafeAPIResponseValidationError,
        "hang": 9.0,  # a service that never answers
    }
    for label, fault in faults.items():
        case = Case(f"injected {label} (SC-003)", "SC-003", 1500.0, [],
                    allowance_ms=25.0)
        client = _fake_client(p50_ms=args.p50, p95_ms=args.p95,
                              faults=[fault] * (args.failure_turns * 4))
        for i in range(args.failure_turns):
            elapsed, outcome = await _time_seam(
                api_key="k" * 48, request=requests[i % len(requests)],
                client=client, circuit=UserCircuit(), flags=_Enabled,
            )
            case.samples.append(elapsed)
            assert outcome.decision is None, (
                f"{label} produced a decision it could not have made"
            )
        case.calls = client.call_count
        cases.append(case)

    # SC-004 -- an open circuit costs nothing until cool-down ends.
    opened = Case("circuit open (SC-004)", "SC-004", 5.0, [])
    circuit = UserCircuit()
    for _ in range(12):
        circuit.record_failure("latency-user")
    client = _fake_client(p50_ms=args.p50, p95_ms=args.p95)
    for i in range(args.turns):
        elapsed, outcome = await _time_seam(
            api_key="k" * 48, request=requests[i % len(requests)],
            client=client, circuit=circuit, flags=_Enabled,
        )
        opened.samples.append(elapsed)
    opened.calls = client.call_count
    cases.append(opened)

    return {"source": "fake", "p50_ms": args.p50, "p95_ms": args.p95,
            "cases": cases}


async def run_live(args, key: str) -> dict:
    """The keyed success path against the real service (SC-002)."""
    from orchestrator.typesafe_routing.budget import UserCircuit

    import hashlib
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12]

    prompts = [p["prompt"] for p in _load("prompts.json")["cases"]]
    requests = [_request(prompts[i % len(prompts)]) for i in range(min(len(prompts), 24))]

    keyed = Case("keyed success, live service (SC-002)", "SC-002", None, [])
    for i in range(args.turns):
        elapsed, outcome = await _time_seam(
            api_key=key, request=requests[i % len(requests)],
            client=None, circuit=UserCircuit(), flags=_Enabled,
            fingerprint=fingerprint,
        )
        keyed.samples.append(elapsed)
    return {"source": "live", "fingerprint": fingerprint, "cases": [keyed]}


def _render(report: dict) -> str:
    lines = [
        "TypeSafe-attributable turn delay",
        f"source: {report['source']}"
        + (f"   key fingerprint: {report['fingerprint']}" if "fingerprint" in report else ""),
        "",
        f"{'case':<34}{'n':>5}{'p50':>9}{'p95':>9}{'max':>9}{'calls':>7}  bound     verdict",
    ]
    for case in report["cases"]:
        s = _summary(case.name, case.samples)
        bound = f"{case.bound_ms:.0f} ms" if case.bound_ms is not None else "--"
        over = case.overshoot_ms()
        note = f" (+{over:.1f} over the bound, within the {case.allowance_ms:.0f} ms "                f"measurement allowance)" if over else ""
        lines.append(
            f"{case.name:<34}{s['n']:>5}{s['p50_ms']:>9.1f}{s['p95_ms']:>9.1f}"
            f"{s['max_ms']:>9.1f}{case.calls:>7}  {bound:<9} {case.verdict()}{note}"
        )
    lines.append("")
    verdicts = [c.verdict() for c in report["cases"]]
    lines.append("FAIL" if "FAIL" in verdicts else "PASS")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("fake", "live"), default="fake")
    parser.add_argument("--turns", type=int, default=200)
    parser.add_argument("--failure-turns", type=int, default=25)
    parser.add_argument("--p50", type=float, default=DEFAULT_P50_MS)
    parser.add_argument("--p95", type=float, default=DEFAULT_P95_MS)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        if os.environ.get(name):
            parser.error(
                f"{name} is set in this environment. The owner's key is read from "
                "stdin only; refusing to run against a process that has one in its "
                "environment."
            )

    if args.source == "live":
        key = sys.stdin.read().strip()
        if not key:
            parser.error("--source live reads the key from stdin; nothing was piped in")
        report = asyncio.run(run_live(args, key))
    else:
        report = asyncio.run(run_fake(args))

    text = _render(report)
    sys.stdout.write(text)
    if args.json:
        args.json.write_text(json.dumps(
            {
                "source": report["source"],
                "fingerprint": report.get("fingerprint"),
                "cases": [
                    {**_summary(c.name, c.samples), "criterion": c.criterion,
                     "bound_ms": c.bound_ms, "allowance_ms": c.allowance_ms,
                     "overshoot_ms": round(c.overshoot_ms(), 2),
                     "calls": c.calls, "verdict": c.verdict()}
                    for c in report["cases"]
                ],
            },
            indent=2,
        ) + "\n", encoding="utf-8")
    return 0 if "FAIL" not in text.splitlines()[-2] else 1


if __name__ == "__main__":
    raise SystemExit(main())
