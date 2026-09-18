#!/usr/bin/env python3
"""Measure real TypeSafe System One routing (feature 089, T015). Local only.

This is the measurement spike that turns the provisional constants in
``orchestrator/typesafe_routing/`` into values someone measured. It answers two
questions:

* **Latency.** How does one ``system_one`` call scale with the number of
  questions and the number of options per question? That curve is what sets
  ``ATTEMPT_TIMEOUT_MS``, ``MAX_ROUTING_AGENTS`` and ``MAX_TOOLS_PER_AGENT``:
  a catalog large enough to blow the 1.5 s turn budget has to be truncated,
  and the truncation bound should come from the curve rather than a guess.
* **Accuracy.** Against the labeled corpus in
  ``backend/tests/fixtures/typesafe_routing/``, how often does the model pick
  an accepted tool, and what tier do the current thresholds assign? That is
  the input to the T028 calibration.

Credential handling (FR-044, T003a). The key is read from **stdin only**:

    python scripts/typesafe_routing_bench.py --mode both < key.txt
    printf '%s' "$KEY" | python scripts/typesafe_routing_bench.py

It is never read from the environment, never accepted as a command-line
argument (arguments are visible in the process table and in shell history), and
never printed, logged or written to any output file. The report identifies the
key only by the 12-character fingerprint the credential store uses.

Nothing here authorizes a release. It makes real network calls to TypeSafe and
spends the owner's quota, so it is run deliberately, not from a test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

FIXTURES = BACKEND / "tests" / "fixtures" / "typesafe_routing"


def _read_key_from_stdin() -> str:
    """Read the key from stdin. Refuse anything else."""
    if sys.stdin is None or sys.stdin.isatty():
        raise SystemExit(
            "the TypeSafe key must be piped to stdin, for example:\n"
            "  printf '%s' \"$KEY\" | python scripts/typesafe_routing_bench.py"
        )
    key = sys.stdin.read().strip()
    if not key:
        raise SystemExit("stdin was empty; the TypeSafe key is required")
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        if os.environ.get(name):
            raise SystemExit(
                f"{name} is set in the environment. Feature 089 never reads TypeSafe "
                f"configuration from the environment; unset it and pipe the key instead."
            )
    return key


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _catalog_options(catalog: dict, which: str = "agents"):
    from orchestrator.typesafe_routing.questions import AgentOption, ToolOption

    source = catalog[which] if which == "agents" else catalog[which]["agents"]
    agents = [
        AgentOption(a["agent_id"], a["name"], a.get("description", "")) for a in source
    ]
    tools = {
        a["agent_id"]: [
            ToolOption(t["name"], t.get("description", "")) for t in a["tools"]
        ]
        for a in source
    }
    return agents, tools


def _synthetic(agents: int, tools_per_agent: int):
    from orchestrator.typesafe_routing.questions import AgentOption, ToolOption

    options = []
    tools = {}
    for index in range(agents):
        agent_id = f"synthetic-{index}"
        options.append(
            AgentOption(agent_id, f"Synthetic {index}", f"Synthetic agent {index}")
        )
        tools[agent_id] = [
            ToolOption(f"{agent_id}__tool_{n}", f"Synthetic tool {n} of agent {index}")
            for n in range(tools_per_agent)
        ]
    return options, tools


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _summary(samples: Sequence[float]) -> dict[str, Any]:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "min_ms": round(min(samples), 1),
        "p50_ms": round(_percentile(samples, 0.50), 1),
        "p95_ms": round(_percentile(samples, 0.95), 1),
        "p99_ms": round(_percentile(samples, 0.99), 1),
        "max_ms": round(max(samples), 1),
        "mean_ms": round(statistics.fmean(samples), 1),
    }


async def _one_call(client, key: str, request, timeout: float):
    """Issue one real call and return (elapsed_ms, response_or_error)."""
    from orchestrator.typesafe_routing.client import load_sdk
    from orchestrator.typesafe_routing.questions import build_questions

    question_set = build_questions(request, load_sdk())
    started = time.monotonic()
    try:
        response = await client.system_one(
            api_key=key,
            state=request.state(),
            questions=question_set.questions,
            timeout=timeout,
        )
    except Exception as error:  # noqa: BLE001 - the bench records failures too
        return (time.monotonic() - started) * 1000.0, type(error).__name__, None, question_set
    return (time.monotonic() - started) * 1000.0, None, response, question_set


async def run_latency(client, key: str, repeats: int, timeout: float) -> list[dict]:
    """Latency against catalog size, which is what sets the truncation bounds."""
    from orchestrator.typesafe_routing.questions import RoutingRequest

    catalog = _load("catalog.json")
    real_agents, real_tools = _catalog_options(catalog)

    shapes = [
        ("security_only", 0, 0),
        ("1x3", 1, 3),
        ("2x6", 2, 6),
        ("4x6", 4, 6),
        ("bundled", len(real_agents), max(len(v) for v in real_tools.values())),
        ("12x8", 12, 8),
        ("20x12", 20, 12),
        ("60x8_truncated", 60, 8),
    ]

    results = []
    for label, agents, tools_per_agent in shapes:
        if label == "bundled":
            options, tools = real_agents, real_tools
        elif agents == 0:
            options, tools = real_agents[:1], {real_agents[0].agent_id: real_tools[real_agents[0].agent_id][:1]}
        else:
            options, tools = _synthetic(agents, tools_per_agent)

        request = RoutingRequest.build(
            current_request="What's the weather in Lexington right now?",
            agents=options,
            tools_by_agent=tools,
            max_agents=max(agents, 1) if label != "60x8_truncated" else 20,
            max_tools_per_agent=max(tools_per_agent, 1) if label != "60x8_truncated" else 12,
        )
        question_count = 3 + 1 + len(request.agents) + 1
        option_count = sum(len(v) for v in request.tools_by_agent.values()) + len(request.agents)

        samples: list[float] = []
        failures: dict[str, int] = {}
        for _ in range(repeats):
            elapsed, error, _response, _qs = await _one_call(client, key, request, timeout)
            if error:
                failures[error] = failures.get(error, 0) + 1
            else:
                samples.append(elapsed)

        results.append(
            {
                "shape": label,
                "agents_sent": len(request.agents),
                "questions": question_count,
                "options": option_count,
                "failures": failures,
                **_summary(samples),
            }
        )
        print(
            f"  {label:<16} agents={len(request.agents):>3} questions={question_count:>3} "
            f"options={option_count:>4}  "
            f"p50={results[-1].get('p50_ms', 0):>7.1f}ms p95={results[-1].get('p95_ms', 0):>7.1f}ms"
            + (f"  failures={failures}" if failures else ""),
            flush=True,
        )
    return results


async def run_accuracy(client, key: str, timeout: float) -> dict[str, Any]:
    """Tier and tool accuracy against the labeled corpus."""
    from orchestrator.typesafe_routing.decision import Tier, parse_decision
    from orchestrator.typesafe_routing.questions import RoutingRequest

    catalog = _load("catalog.json")
    prompts = _load("prompts.json")

    rows = []
    for case in prompts["cases"]:
        which = case.get("catalog", "agents")
        agents, tools = _catalog_options(
            catalog, "agents" if which == "agents" else which
        )

        excluded_agents = set(case.get("excluded_agents", ()))
        excluded_tools = set(case.get("excluded_tools", ()))
        agents = [a for a in agents if a.agent_id not in excluded_agents]
        tools = {
            agent_id: [t for t in entries if t.name not in excluded_tools]
            for agent_id, entries in tools.items()
            if agent_id not in excluded_agents
        }

        synthetic = case.get("synthetic_catalog")
        if synthetic:
            pad_agents, pad_tools = _synthetic(
                synthetic["agents"], synthetic["tools_per_agent"]
            )
            agents = agents + pad_agents
            tools = {**tools, **pad_tools}

        request = RoutingRequest.build(
            current_request=case["prompt"],
            history=case.get("history", ()),
            agents=agents,
            tools_by_agent=tools,
        )
        elapsed, error, response, question_set = await _one_call(
            client, key, request, timeout
        )
        if error:
            rows.append({"id": case["id"], "error": error, "elapsed_ms": round(elapsed, 1)})
            print(f"  {case['id']:<32} ERROR {error}", flush=True)
            continue

        decision = parse_decision(response, question_set)
        accepted = set(case.get("accepted_tools", ()))
        accepted_agents = set(case.get("accepted_agents", ()))
        # A low-tier decision narrows nothing, so round one is exactly what an
        # unkeyed user would have got. When the label expects low, that is the
        # correct outcome and scoring it against a tool name would measure the
        # wrong thing.
        if decision.tier is Tier.LOW and case["expected_tier"] == "low":
            tool_ok = True
            agent_ok = True
        else:
            tool_ok = (
                (decision.tool_name in accepted)
                if accepted
                else (decision.tool_name is None)
            )
            agent_ok = (
                decision.agent_id in accepted_agents
                or (decision.no_tool_needed and "no_tool_needed" in accepted_agents)
            )
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expected_tier": case["expected_tier"],
                "observed_tier": decision.tier.value,
                "tier_match": decision.tier.value == case["expected_tier"],
                "agent_id": decision.agent_id,
                "agent_confidence": round(decision.agent_confidence, 3),
                "agent_ok": agent_ok,
                "tool_name": decision.tool_name,
                "tool_confidence": round(decision.tool_confidence, 3),
                "tool_margin": round(decision.tool_margin, 3),
                "tool_ok": tool_ok,
                "style": decision.style,
                "expected_style": case.get("expected_style"),
                "jailbreak": round(decision.security.jailbreak_probability, 3),
                "harm": round(decision.security.harm_score, 3),
                "threat": decision.security.threat_category,
                "elapsed_ms": round(elapsed, 1),
            }
        )
        print(
            f"  {case['id']:<32} tier={decision.tier.value:<6} "
            f"(want {case['expected_tier']:<6}) tool={'ok ' if tool_ok else 'MISS'} "
            f"agent={'ok ' if agent_ok else 'MISS'} {elapsed:>7.1f}ms",
            flush=True,
        )

    scored = [r for r in rows if "error" not in r]
    high = [r for r in scored if r["observed_tier"] == "high"]
    return {
        "cases": rows,
        "totals": {
            "scored": len(scored),
            "errors": len(rows) - len(scored),
            "agent_accuracy": round(
                sum(1 for r in scored if r["agent_ok"]) / len(scored), 3
            )
            if scored
            else 0.0,
            "tool_accuracy": round(
                sum(1 for r in scored if r["tool_ok"]) / len(scored), 3
            )
            if scored
            else 0.0,
            "tier_agreement": round(
                sum(1 for r in scored if r["tier_match"]) / len(scored), 3
            )
            if scored
            else 0.0,
            "high_tier_count": len(high),
            "high_tier_acceptance": round(
                sum(1 for r in high if r["tool_ok"]) / len(high), 3
            )
            if high
            else None,
            "latency": _summary([r["elapsed_ms"] for r in scored]),
        },
    }


async def run_benign(client, key: str, timeout: float) -> dict[str, Any]:
    """Security judgments over the benign corpus: the false-positive input."""
    from orchestrator.typesafe_routing.decision import parse_security
    from orchestrator.typesafe_routing.questions import RoutingRequest
    from orchestrator.typesafe_routing.security_policy import Verdict, verdict_for

    catalog = _load("catalog.json")
    agents, tools = _catalog_options(catalog)
    corpus = _load("benign_reference.json")

    rows = []
    for case in corpus["cases"]:
        request = RoutingRequest.build(
            current_request=case["prompt"], agents=agents, tools_by_agent=tools
        )
        elapsed, error, response, _qs = await _one_call(client, key, request, timeout)
        if error:
            rows.append({"id": case["id"], "error": error})
            continue
        judgment = parse_security(response)
        # Both tiers are reported: the refuse tier is disabled in code, and the
        # question T036 has to answer is what it *would* have done.
        rows.append(
            {
                "id": case["id"],
                "adjacent_to": case.get("adjacent_to"),
                "jailbreak": round(judgment.jailbreak_probability, 3),
                "harm": round(judgment.harm_score, 3),
                "threat": judgment.threat_category,
                "verdict_now": verdict_for(judgment, refuse_enabled=False).value,
                "verdict_if_refuse_enabled": verdict_for(
                    judgment, refuse_enabled=True
                ).value,
                "elapsed_ms": round(elapsed, 1),
            }
        )
        print(
            f"  {case['id']:<32} jb={rows[-1]['jailbreak']:<6} harm={rows[-1]['harm']:<5} "
            f"threat={rows[-1]['threat']:<22} -> {rows[-1]['verdict_if_refuse_enabled']}",
            flush=True,
        )

    scored = [r for r in rows if "error" not in r]
    refused = [r for r in scored if r["verdict_if_refuse_enabled"] == Verdict.REFUSE.value]
    confirmed = [
        r for r in scored if r["verdict_if_refuse_enabled"] == Verdict.CONFIRM_TOOLS.value
    ]
    return {
        "cases": rows,
        "totals": {
            "scored": len(scored),
            "errors": len(rows) - len(scored),
            "false_refuse_rate": round(len(refused) / len(scored), 3) if scored else 0.0,
            "confirm_rate": round(len(confirmed) / len(scored), 3) if scored else 0.0,
            "false_refusals": [r["id"] for r in refused],
            "confirmations": [r["id"] for r in confirmed],
        },
    }


async def run_adversarial(client, key: str, timeout: float) -> dict[str, Any]:
    """Security judgments over the benchmark's adversarial cases.

    Scope note, which matters for reading the number: the TypeSafe screen sits
    at turn ingress and sees the user's message. It therefore addresses
    **direct** prompt injection -- an instruction the user's own message
    carries. It does not see tool output, so an injection delivered through a
    retrieved document is outside what this screen can catch and is the gate
    stack's job, unchanged by 089. Cases are scored as "the screen would have
    raised friction" when the verdict is confirm_tools or refuse.
    """
    import importlib

    from orchestrator.typesafe_routing.decision import parse_security
    from orchestrator.typesafe_routing.questions import RoutingRequest
    from orchestrator.typesafe_routing.security_policy import Verdict, verdict_for

    catalog = _load("catalog.json")
    agents, tools = _catalog_options(catalog)

    cases = []
    for module_name in ("injecagent", "agentdojo", "asb", "chained"):
        module = importlib.import_module(
            "security_benchmark.adapters." + module_name
        )
        adapter_class = next(
            value
            for name, value in vars(module).items()
            if isinstance(value, type)
            and name.endswith("Adapter")
            and value.__module__ == module.__name__
        )
        for case in adapter_class().load_cases():
            prompt = " ".join(
                part for part in (case.user_prompt, case.injection) if part
            ).strip()
            cases.append(
                {
                    "case_id": case.case_id,
                    "suite": module_name,
                    "category": case.category,
                    "prompt": prompt,
                    # The placeholder suites carry bracketed markers instead of
                    # prose; they are reported separately rather than mixed in,
                    # because a judgment on "[dpi] adversarial instruction"
                    # measures nothing about the model.
                    "prose": "[" not in prompt.split(" ", 1)[0],
                }
            )

    rows = []
    for case in cases:
        request = RoutingRequest.build(
            current_request=case["prompt"], agents=agents, tools_by_agent=tools
        )
        elapsed, error, response, _qs = await _one_call(client, key, request, timeout)
        if error:
            rows.append({**case, "error": error})
            continue
        judgment = parse_security(response)
        row = {
            **case,
            "jailbreak": round(judgment.jailbreak_probability, 3),
            "harm": round(judgment.harm_score, 3),
            "threat": judgment.threat_category,
            "verdict_now": verdict_for(judgment, refuse_enabled=False).value,
            "verdict_if_refuse_enabled": verdict_for(
                judgment, refuse_enabled=True
            ).value,
            "elapsed_ms": round(elapsed, 1),
        }
        rows.append(row)
        print(
            f"  {case['case_id']:<18} {case['category']:<28} jb={row['jailbreak']:<6} "
            f"harm={row['harm']:<5} threat={row['threat']:<22} -> "
            f"{row['verdict_if_refuse_enabled']}",
            flush=True,
        )

    prose = [r for r in rows if r.get("prose") and "error" not in r]
    caught = [r for r in prose if r["verdict_now"] != Verdict.PASS.value]
    refused = [
        r for r in prose if r["verdict_if_refuse_enabled"] == Verdict.REFUSE.value
    ]
    return {
        "cases": rows,
        "totals": {
            "prose_cases": len(prose),
            "placeholder_cases": len([r for r in rows if not r.get("prose")]),
            "errors": len([r for r in rows if "error" in r]),
            "friction_rate": round(len(caught) / len(prose), 3) if prose else 0.0,
            "refuse_rate_if_enabled": round(len(refused) / len(prose), 3)
            if prose
            else 0.0,
            "missed": [r["case_id"] for r in prose if r["verdict_now"] == Verdict.PASS.value],
        },
    }


async def main_async(args: argparse.Namespace, key: str) -> int:
    from orchestrator.typesafe_routing.client import (
        TYPESAFE_API_BASE,
        TYPESAFE_MODEL,
        TypeSafeAdapterClient,
    )
    from llm_config.typesafe_store import key_fingerprint

    fingerprint = key_fingerprint(key)
    print(f"TypeSafe routing bench — key fingerprint {fingerprint}")
    print(f"  base {TYPESAFE_API_BASE}  model {TYPESAFE_MODEL}  timeout {args.timeout}s")
    print()

    client = TypeSafeAdapterClient()
    report: dict[str, Any] = {
        "key_fingerprint": fingerprint,
        "base_url": TYPESAFE_API_BASE,
        "model": TYPESAFE_MODEL,
        "timeout_s": args.timeout,
    }
    try:
        if args.mode in ("latency", "both"):
            print("Latency by catalog shape:")
            report["latency"] = await run_latency(client, key, args.repeats, args.timeout)
            print()
        if args.mode in ("accuracy", "both"):
            print("Routing accuracy over the labeled corpus:")
            report["accuracy"] = await run_accuracy(client, key, args.timeout)
            print(f"  totals: {report['accuracy']['totals']}")
            print()
        if args.mode in ("adversarial", "both"):
            print("Security judgments over the benchmark's adversarial cases:")
            report["adversarial"] = await run_adversarial(client, key, args.timeout)
            print(f"  totals: {report['adversarial']['totals']}")
            print()
        if args.mode in ("benign", "both"):
            print("Security judgments over the benign corpus:")
            report["benign"] = await run_benign(client, key, args.timeout)
            print(f"  totals: {report['benign']['totals']}")
            print()
    finally:
        await client.aclose()

    if args.json_out:
        destination = Path(args.json_out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(report, indent=2)
        assert key not in serialized, "refusing to write a report containing the key"
        destination.write_text(serialized, encoding="utf-8")
        print(f"report written to {destination}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure real TypeSafe routing latency and accuracy (local only)."
    )
    parser.add_argument(
        "--mode",
        choices=("latency", "accuracy", "benign", "adversarial", "both"),
        default="both",
        help="which measurements to take ('both' runs all three)",
    )
    parser.add_argument(
        "--repeats", type=int, default=10, help="calls per latency shape (default 10)"
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-call timeout in seconds"
    )
    parser.add_argument("--json-out", help="write the full report here (never the key)")
    args = parser.parse_args(argv)

    key = _read_key_from_stdin()
    try:
        return asyncio.run(main_async(args, key))
    finally:
        del key


if __name__ == "__main__":
    sys.exit(main())
