#!/usr/bin/env python3
"""Measures TypeSafe-attributable turn latency at the orchestrator/typesafe_routing/
seam, replaying a measured distribution (--source fake) or the real service (--source
live) across unkeyed, keyed, failure, and open-circuit cases.
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

DEFAULT_P50_MS = 212.0
DEFAULT_P95_MS = 280.0

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
    name: str
    criterion: str
    bound_ms: Optional[float]
    samples: list[float]
    calls: int = 0
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
    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    from fakes.typesafe_fake import FakeTypeSafeClient, high_confidence

    rng = rng or random.Random(89)
    mu = p50_ms / 1000.0
    sigma = max(1e-6, (p95_ms - p50_ms) / 1000.0 / 1.645)

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

    faults = {
        "timeout": TypeSafeAPITimeoutError,
        "connection": TypeSafeAPIConnectionError,
        "rate_limit": TypeSafeRateLimitError,
        "server_error": TypeSafeInternalServerError,
        "auth": TypeSafeAuthenticationError,
        "malformed": TypeSafeAPIResponseValidationError,
        "hang": 9.0,
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
