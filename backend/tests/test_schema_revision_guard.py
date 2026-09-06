"""Independent Plane schema and Deep user-agent-policy revision guards.

AstralPlane owns schema migration source, revision, and digest qualification.
Deep retains only the deterministic product-policy revision guard; changing
that policy remains independent from Plane's schema revision.
"""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from astralplane import SCHEMA_REVISION

EXPECTED_SCHEMA_REVISION = "079.001"
EXPECTED_USER_AGENT_POLICY_REVISION = "constitution=0.1.0;analyze=2"
EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256 = (
    "1651a3a0e81a9012ed56e978dd3d4fef41d7ca5e266480525f3b2e340a6daa50"
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
    """Hash the exact baked constitution plus deterministic Analyze module."""
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
