"""Tests for astralplane.repositories.agent_models and agents: declarative-agent command
shape, bounded definition depth, canonical UTF-8 digesting, and clone/receipt
validation, independent of IAM policy.
"""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from astralplane.repositories import RepositoryDataError, RepositoryValidationError
from astralplane.repositories.agent_models import definition_snapshot
from astralplane.repositories.agents import DeclarativeAgentCommand, _declarative_receipt

UUID = "10000000-0000-4000-8000-000000000001"
REVISION = "20000000-0000-4000-8000-000000000002"


def valid(**changes):
    values = dict(
        owner_id="owner",
        agent_id="agent",
        command_id=UUID,
        command="create",
        revision_id=REVISION,
        display_name="Read",
        definition={"version": 1},
    )
    values.update(changes)
    return DeclarativeAgentCommand(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", ""),
        ("owner_id", 1),
        ("owner_id", "x" * 513),
        ("agent_id", "x" * 256),
        ("display_name", "x" * 1025),
        ("display_name", "bad\x00text"),
        ("display_name", "\ud800"),
        ("version", True),
        ("version", 2),
        ("version", 1.0),
        ("command", "execute"),
        ("command", []),
        ("command_id", "wrong"),
        ("command_id", 1),
        ("command_id", "10000000-0000-1000-8000-000000000001"),
        ("command_id", "A0000000-0000-4000-8000-000000000001"),
        ("revision_id", None),
        ("revision_id", "wrong"),
        ("expected_revision", 0),
        ("source_agent_id", "foreign"),
        ("parent_revision_id", UUID),
        ("definition", None),
    ],
)
def test_command_rejects_malformed_or_extraneous_fields(field, value):
    with pytest.raises(RepositoryValidationError):
        valid(**{field: value})


@pytest.mark.parametrize(
    "definition",
    [
        [],
        {"version": True},
        {"version": 1.0},
        {"version": 2},
        {"version": 1, 2: "bad"},
        {"version": 1, "value": object()},
        {"version": 1, "value": float("nan")},
        {"version": 1, "value": "\ud800"},
        {"version": 1, "value": "bad\x00text"},
        {"version": 1, "bad\x00key": "text"},
        {"version": 1, "value": "a" * 65536},
        {"version": 1, "value": list(range(4096))},
    ],
)
def test_definition_refuses_unbounded_or_non_json_shapes(definition):
    with pytest.raises(RepositoryValidationError):
        definition_snapshot(definition)


def test_nested_definition_depth_is_bounded():
    value = {"version": 1}
    for _ in range(18):
        value = {"version": 1, "child": value}
    with pytest.raises(RepositoryValidationError, match="structural"):
        definition_snapshot(value)


def test_utf8_canonical_digest_and_detachment_never_truncate_or_expose_definition():
    raw = {"version": 1, "purpose": "private café 漢字", "capabilities": [{"name": "read"}]}
    snapshot, digest = definition_snapshot(raw)
    assert (
        digest
        == sha256(
            json.dumps(
                raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
    )
    value = valid(definition=raw)
    before = value.request_digest
    raw["capabilities"][0]["name"] = "mutated"
    assert snapshot["capabilities"][0]["name"] == "read"
    assert value.request_digest == before
    assert "private" not in repr(value)
    with pytest.raises(TypeError):
        value.definition["capabilities"][0]["name"] = "mutated"
    with pytest.raises(FrozenInstanceError):
        value.command = "delete"


@pytest.mark.parametrize("counter", [True, 0.5, -1, 2**63])
def test_lifecycle_expected_revision_is_an_exact_bounded_integer(counter):
    with pytest.raises(RepositoryValidationError):
        DeclarativeAgentCommand(
            owner_id="owner",
            agent_id="agent",
            command_id=UUID,
            command="archive",
            expected_revision=counter,
        )


def test_clone_requires_distinct_source_and_new_revision_and_binds_all_original_fields():
    value = DeclarativeAgentCommand(
        owner_id="owner",
        agent_id="clone",
        command_id=UUID,
        command="clone",
        revision_id=REVISION,
        source_agent_id="source",
        source_revision_id=UUID,
        display_name="New draft",
    )
    for changed in (
        replace(value, display_name="Another"),
        replace(value, source_agent_id="other"),
        replace(value, revision_id=UUID),
        replace(value, source_revision_id=REVISION),
    ):
        assert changed.request_digest != value.request_digest
    with pytest.raises(RepositoryValidationError):
        replace(value, source_agent_id="clone")
    with pytest.raises(RepositoryValidationError):
        replace(value, revision_id=None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", "unknown"),
        ("command_version", True),
        ("agent_kind", "executable"),
        ("request_digest", "bad"),
        ("result_state_revision", True),
        ("created_at", 1),
    ],
)
def test_persisted_receipt_does_not_coerce_unsupported_metadata(field, value):
    row = dict(
        owner_user_id="owner",
        agent_id="agent",
        command_id=UUID,
        command="create",
        command_version=1,
        agent_kind="declarative",
        request_digest="a" * 64,
        result_state_revision=0,
        result_definition_revision_id=REVISION,
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    row[field] = value
    with pytest.raises(RepositoryDataError):
        _declarative_receipt(row)


def test_receipt_missing_required_metadata_is_a_data_refusal():
    with pytest.raises(RepositoryDataError):
        _declarative_receipt({})
