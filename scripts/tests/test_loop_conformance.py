"""Runs shared loop fixtures against the node's production argument pipeline and repeat guard.
The Rust engine consumes the same hand-expected fixtures, including malformed inputs that must never bypass dispatch limits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from orchestrator.repeat_guard import RepeatGuard  # noqa: E402
from orchestrator.tool_arguments import prepare_arguments  # noqa: E402

FIXTURE = ROOT / "specs/105-decentralized-personal-mesh/contracts/conformance/loop.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES["arguments"], ids=lambda c: c["name"])
def test_arguments_match_the_shared_loop_contract(case: dict) -> None:
    result = prepare_arguments(case["raw"], case["schema"])
    assert {
        "args": result.args,
        "repaired": result.repaired,
        "code": result.error.code if result.error else None,
    } == case["expect"]


@pytest.mark.parametrize("case", CASES["repeats"], ids=lambda c: c["name"])
def test_repeats_match_the_shared_loop_contract(case: dict) -> None:
    guard = RepeatGuard()
    for step in case["steps"]:
        verdict = guard.observe(step["tool"], step["raw"], step["failed"])
        assert {"verdict": verdict, "blocked": guard.blocked(step["tool"], step["raw"])} == step["expect"]
