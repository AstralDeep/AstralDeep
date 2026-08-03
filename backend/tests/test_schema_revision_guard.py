"""Independent schema and user-agent-policy revision guards (060/T006).

Editing _init_db, _apply_full_schema, or any _migrate_*/_cleanup_* helper
without bumping SCHEMA_REVISION would leave already-marked databases on
the schema fast path with the new migration silently never applied. This
test pins a sha256 of that source region beside the expected revision, so
any schema-code change fails CI until SCHEMA_REVISION is bumped and both
constants below are updated in the same commit. No database required.
"""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

try:
    from shared.database import Database, SCHEMA_REVISION
except Exception:  # pragma: no cover - import guard
    Database = None  # type: ignore
    SCHEMA_REVISION = None  # type: ignore

EXPECTED_SCHEMA_REVISION = "065.001"
EXPECTED_SOURCE_SHA256 = (
    "e96c58028d462df28e7c131b755663ad65ec27078ecbb2801057e8de7c766c22"
)
EXPECTED_USER_AGENT_POLICY_REVISION = "constitution=0.1.0;analyze=1"
EXPECTED_USER_AGENT_POLICY_SOURCE_SHA256 = (
    "c16c8c6709bbc72af7b69657a1105028b416fc982f53eb338ada9e34afc1374b"
)

_BUMP_INSTRUCTIONS = (
    "You changed the schema-initialization source in backend/shared/database.py "
    "(_init_db, _apply_full_schema, or a _migrate_*/_cleanup_* helper). "
    "Bump SCHEMA_REVISION in backend/shared/database.py so deployed databases "
    "re-run the full migration set, then update EXPECTED_SCHEMA_REVISION and "
    "EXPECTED_SOURCE_SHA256 in backend/tests/test_schema_revision_guard.py "
    "to the new values (print the hash via "
    "tests.test_schema_revision_guard.schema_source_sha256())."
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


def _guarded_method_names() -> list:
    """Names of the schema-shaping methods covered by the hash."""
    names = ["_init_db", "_apply_full_schema"]
    names.extend(
        sorted(
            name
            for name in dir(Database)
            if name.startswith(("_migrate_", "_cleanup_"))
        )
    )
    return names


def schema_source_sha256() -> str:
    """sha256 over the concatenated source of every guarded method."""
    source = "\n".join(
        inspect.getsource(getattr(Database, name))
        for name in _guarded_method_names()
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


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


def _skip_unless_importable():
    if Database is None:
        pytest.skip("shared.database unavailable")


def test_schema_revision_matches_expected():
    _skip_unless_importable()
    assert SCHEMA_REVISION == EXPECTED_SCHEMA_REVISION, (
        f"SCHEMA_REVISION is {SCHEMA_REVISION!r} but this guard expects "
        f"{EXPECTED_SCHEMA_REVISION!r}. {_BUMP_INSTRUCTIONS}"
    )


def test_init_db_source_hash_requires_revision_bump():
    _skip_unless_importable()
    actual = schema_source_sha256()
    assert actual == EXPECTED_SOURCE_SHA256, (
        f"schema source hash changed: {actual} != {EXPECTED_SOURCE_SHA256}. "
        f"{_BUMP_INSTRUCTIONS}"
    )


def test_guard_covers_all_migration_helpers():
    _skip_unless_importable()
    names = _guarded_method_names()
    assert "_init_db" in names
    assert "_apply_full_schema" in names
    assert "_migrate_backfill_tool_kinds_052" in names
    assert "_migrate_runtime_reliability_060" in names
    assert "_migrate_conversational_voice_065" in names
    assert any(name.startswith("_cleanup_") for name in names)


def test_user_agent_policy_revision_is_exact_and_independent():
    from orchestrator import agent_analyze, agent_constitution

    assert agent_analyze.ANALYZE_POLICY_REVISION == "1"
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
