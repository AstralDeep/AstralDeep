"""Closed selected-guidance wire; identifiers never convey current authority."""

import json
from uuid import uuid4

import pytest

from orchestrator.work_submit import _parse
from persistent_agents.models import AssignmentError
from tests.test_work_submit_postgres_088 import command


def selection(**changes):
    return {
        "version": 1,
        "agent": None,
        "skills": [{"skill_id": str(uuid4()), "revision": 1}],
        "notes": [],
    } | changes


@pytest.mark.parametrize("retention", [None, "operation", "none"])
def test_closed_selected_input_is_part_of_original_canonical_receipt(retention):
    selected = selection(notes=[{"note_id": str(uuid4()), "revision": 2}])
    raw = command(selection=selected)
    body = json.loads(raw)
    if retention is not None:
        body["source_retention"] = retention
    raw = json.dumps(body).encode()
    parsed, _, receipt = _parse(raw)
    assert parsed == body and parsed["selection"] == selected
    assert _parse(json.dumps(body, indent=2).encode())[2] == receipt
    body["selection"]["notes"][0]["revision"] += 1
    assert _parse(json.dumps(body).encode())[2] != receipt


def test_command_array_order_is_not_rewritten_into_envelope_order():
    body = json.loads(command(selection=selection(
        skills=[{"skill_id": str(uuid4()), "revision": 1} for _ in range(2)])))
    original = _parse(json.dumps(body).encode())[2]
    body["selection"]["skills"].reverse()
    assert _parse(json.dumps(body).encode())[2] != original


@pytest.mark.parametrize("field,bad", [
    ("version", True), ("version", 2), ("owner_id", "another-owner"),
    ("binding_key_id", "key"), ("combined_binding", "a" * 64),
    ("agent", {}), ("agent", {"agent_id": "custom", "revision_id": "latest"}),
    ("skills", None), ("skills", {}),
    ("skills", [{"skill_id": str(uuid4()), "revision": True}]),
    ("skills", [{"skill_id": str(uuid4()), "revision": 0}]),
    ("skills", [{"skill_id": str(uuid4()), "revision": 2**53}]),
    ("skills", [{"skill_id": str(uuid4()), "revision": 1, "instructions": "injected"}]),
    ("skills", [{"skill_id": "not-uuid4", "revision": 1}]),
    ("notes", [{"note_id": str(uuid4()), "revision": 1, "value": "private"}]),
    ("notes", [{"note_id": str(uuid4()), "revision": 1} for _ in range(9)]),
    ("skills", [{"skill_id": str(uuid4()), "revision": 1} for _ in range(21)]),
])
def test_selection_rejects_unknown_fields_wrong_types_and_bounds(field, bad):
    with pytest.raises(AssignmentError) as caught:
        _parse(command(selection=selection(**{field: bad})))
    assert (caught.value.code, caught.value.status_code) == ("work_submit_invalid", 422)


@pytest.mark.parametrize("value", [None, [], {}, {"version": 1},
    {"version": 1, "agent": None, "skills": [], "notes": []}])
def test_absent_selection_is_omission_only(value):
    assert "selection" not in _parse(command())[0]
    with pytest.raises(AssignmentError):
        _parse(command(selection=value))


@pytest.mark.parametrize("kind,field", [("skills", "skill_id"), ("notes", "note_id")])
def test_duplicate_selection_ids_refuse_even_if_revisions_differ(kind, field):
    identity = str(uuid4())
    with pytest.raises(AssignmentError):
        _parse(command(selection=selection(**{kind: [
            {field: identity, "revision": 1}, {field: identity, "revision": 2}]})))


def test_selected_json_duplicate_keys_are_never_normalized_away():
    raw = command(selection=selection())
    malformed = raw.replace(b'"revision": 1', b'"revision": 1, "revision": 2')
    with pytest.raises(AssignmentError):
        _parse(malformed)


@pytest.mark.parametrize("identity", [True, None, str(uuid4()).upper(), "00000000-0000-1000-8000-000000000001"])
def test_revision_identity_requires_exact_canonical_uuid4(identity):
    with pytest.raises(AssignmentError):
        _parse(command(selection=selection(agent={"agent_id": "selected-agent", "revision_id": identity})))


@pytest.mark.parametrize("identity", ["agent\tname", "agent\x7fname", "agent\u0085name"])
def test_selected_agent_control_characters_refuse_at_wire_boundary(identity):
    with pytest.raises(AssignmentError) as error:
        _parse(command(selection=selection(agent={"agent_id": identity, "revision_id": str(uuid4())})))
    assert (error.value.code, error.value.status_code) == ("work_submit_invalid", 422)
