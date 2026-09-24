"""Pure, dependency-free plan-vs-execution deviation detection plus the OWASP
ASI01-ASI10 agentic-security coverage matrix mapping each risk to the capabilities
that mitigate it; read by turn_hooks.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def asi_coverage_enabled() -> bool:
    return os.getenv("FF_ASI_COVERAGE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def plan_record(
    planned_tools: list,
    *,
    request: str = "",
    correlation_id: str = "",
) -> dict:
    return {
        "kind": "intended_plan",
        "request": request[:500],
        "planned_tools": [str(t) for t in planned_tools],
        "correlation_id": correlation_id,
        "step_count": len(planned_tools),
    }


@dataclass(frozen=True)
class Deviation:
    extra_calls: tuple
    skipped_steps: tuple
    out_of_order: bool


def detect_deviation(planned_tools: list, actual_tools: list) -> Deviation:
    planned = [str(t) for t in planned_tools]
    actual = [str(t) for t in actual_tools]

    planned_set = set(planned)
    actual_set = set(actual)

    extra_calls: list = []
    seen_extra: set = set()
    for tool in actual:
        if tool not in planned_set and tool not in seen_extra:
            extra_calls.append(tool)
            seen_extra.add(tool)

    skipped_steps: list = []
    seen_skipped: set = set()
    for tool in planned:
        if tool not in actual_set and tool not in seen_skipped:
            skipped_steps.append(tool)
            seen_skipped.add(tool)

    plan_first_index: dict = {}
    for idx, tool in enumerate(planned):
        if tool not in plan_first_index:
            plan_first_index[tool] = idx

    out_of_order = False
    last_index = -1
    for tool in actual:
        if tool not in plan_first_index:
            continue
        idx = plan_first_index[tool]
        if idx < last_index:
            out_of_order = True
            break
        last_index = idx

    return Deviation(
        extra_calls=tuple(extra_calls),
        skipped_steps=tuple(skipped_steps),
        out_of_order=out_of_order,
    )


def has_deviation(d: Deviation) -> bool:
    return bool(d.extra_calls) or bool(d.skipped_steps) or bool(d.out_of_order)


ASI_RISKS: tuple = (
    ("ASI01", "Agent Authorization & Control Hijacking"),
    ("ASI02", "Tool Misuse & Exploitation"),
    ("ASI03", "Memory & Context Poisoning"),
    ("ASI04", "Insecure Agent Orchestration"),
    ("ASI05", "Excessive Agency & Privilege"),
    ("ASI06", "Untrusted Input & Prompt Injection"),
    ("ASI07", "Sensitive Information Disclosure"),
    ("ASI08", "Supply Chain & Generated-Code Risk"),
    ("ASI09", "Identity & Impersonation"),
    ("ASI10", "Insufficient Monitoring & Auditability"),
)

COVERAGE: dict = {
    "ASI01": ["C-S3", "C-S8"],
    "ASI02": ["C-S2", "C-S11"],
    "ASI03": ["C-S9", "C-M6"],
    "ASI04": ["C-S14", "C-N7"],
    "ASI05": ["C-S1", "C-S5"],
    "ASI06": ["C-S4", "C-S5"],
    "ASI07": ["C-S2", "C-S7"],
    "ASI08": ["C-S6", "C-S7"],
    "ASI09": ["C-S8", "C-S14"],
    "ASI10": ["C-S12", "C-S3"],
}


def coverage_report() -> list:
    report: list = []
    for code, title in ASI_RISKS:
        caps = list(COVERAGE.get(code, []))
        report.append(
            {
                "code": code,
                "title": title,
                "capabilities": caps,
                "covered": bool(caps),
            }
        )
    return report


def uncovered_risks() -> list:
    return [code for code, _title in ASI_RISKS if not COVERAGE.get(code)]


def coverage_ratio() -> float:
    total = len(ASI_RISKS)
    if total == 0:
        return 0.0
    covered = sum(1 for _code, _title in ASI_RISKS if COVERAGE.get(_code))
    return covered / total
