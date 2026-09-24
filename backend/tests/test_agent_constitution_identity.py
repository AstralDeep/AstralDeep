"""Confirms the baked backend/agent_constitution/agent_constitution.md matches the specs
source byte for byte, skipping when the specs copy is absent from the runtime image.
"""

from __future__ import annotations

import os

import pytest

_BACKEND_COPY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent_constitution", "agent_constitution.md",
)
_SPECS_COPY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "specs", "057-byo-client-agents", "agent-constitution.md",
)


def test_baked_copy_exists_and_loads():
    assert os.path.exists(_BACKEND_COPY), "baked agent constitution missing from backend/"
    from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION, load_checklist
    assert AGENT_CONSTITUTION_VERSION, "version must parse"
    assert [p.letter for p in load_checklist()] == list("ABCDEFGHIJKL")


def test_baked_copy_byte_identical_to_specs_source():
    if not os.path.exists(_SPECS_COPY):
        pytest.skip("specs/ source not present (runtime image) — enforced on host/CI checkout")
    with open(_BACKEND_COPY, "rb") as a, open(_SPECS_COPY, "rb") as b:
        assert a.read() == b.read(), (
            "backend/agent_constitution/agent_constitution.md has drifted from the "
            "specs/057-byo-client-agents/agent-constitution.md source — re-copy so they match")
