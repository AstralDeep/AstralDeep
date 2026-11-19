"""Runs the shared argument fixtures against the node's production argument pipeline, which every dispatched tool call still passes through.
The Rust engine consumes the same hand-expected fixtures and is the only consumer of the repeat cases, since it is the only turn engine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

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
