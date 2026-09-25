"""Tests that AstralPlane's schema revision and Deep's user-agent-policy revision
(orchestrator/agent_constitution.py, astralplane/__init__.py) stay pinned and
independently guarded against silent drift.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from astralplane import SCHEMA_REVISION

EXPECTED_SCHEMA_REVISION = "089.001"
EXPECTED_USER_AGENT_POLICY_REVISION = "constitution=0.1.0;analyze=2"
EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256 = (
    "45f1091922ac968d22f528446b99e776b89a0361b14244ae278b0c9f5aa50055"
)

_POLICY_BUMP_INSTRUCTIONS = (
    "The baked user-agent constitution or deterministic Analyze policy changed. "
    "Bump its owning AGENT_CONSTITUTION_VERSION or ANALYZE_POLICY_REVISION, "
    "confirm USER_AGENT_POLICY_REVISION has canonical "
    "'constitution=<semver>;analyze=<positive-integer>' form, then update "
    "EXPECTED_USER_AGENT_POLICY_REVISION and "
    "EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256 together. A policy-only change "
    "must not require a SCHEMA_REVISION bump."
)


def user_agent_policy_source_sha256() -> str:
    from orchestrator import agent_analyze, agent_constitution

    constitution_path = Path(agent_constitution.__file__).resolve().parents[1] / (
        "agent_constitution/agent_constitution.md"
    )
    source = constitution_path.read_bytes() + b"\0" + inspect.getsource(
        agent_analyze
    ).encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def test_schema_revision_matches_expected():
    assert SCHEMA_REVISION == EXPECTED_SCHEMA_REVISION


def test_user_agent_policy_revision_is_exact_and_independent():
    from orchestrator import agent_analyze, agent_constitution

    assert agent_analyze.ANALYZE_POLICY_REVISION == "2"
    assert (
        agent_constitution.USER_AGENT_POLICY_REVISION
        == EXPECTED_USER_AGENT_POLICY_REVISION
    )
    assert SCHEMA_REVISION == EXPECTED_SCHEMA_REVISION


def test_user_agent_policy_source_hash_requires_policy_revision_bump():
    actual = user_agent_policy_source_sha256()
    assert actual == EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256, (
        f"user-agent policy source hash changed: {actual} != "
        f"{EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256}. "
        f"{_POLICY_BUMP_INSTRUCTIONS}"
    )
