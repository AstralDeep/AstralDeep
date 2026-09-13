"""Strict ephemeral note commands never become authority or value history."""
from dataclasses import replace
from uuid import uuid4

import pytest

from personalization.explicit_note_service import ExplicitNoteCommand, ExplicitNoteService
from persistent_agents.models import AssignmentError


def command(**changes):
    return replace(ExplicitNoteCommand(command="save", note_id=str(uuid4()),
        expected_revision=0, category="preference", value="  Cafe\u0301\r\nbrief replies  ",
        enabled=True), **changes)


def test_detached_normalized_command_and_hidden_value():
    original = command()
    frozen = ExplicitNoteService.snapshot(original)
    assert frozen is not original and frozen.value == "Café\nbrief replies"
    object.__setattr__(original, "value", "changed later")
    assert frozen.value == "Café\nbrief replies"
    assert "brief replies" not in repr(frozen)


@pytest.mark.parametrize("change", [
    {"version": True}, {"version": 2}, {"command": "execute"},
    {"note_id": "ABC"}, {"expected_revision": True}, {"expected_revision": -1},
    {"expected_revision": 2**53-1}, {"category": "secret"}, {"category": []},
    {"enabled": 1}, {"value": ""}, {"value": "x"*4097}, {"value": "a\x00b"},
    {"expires_at": True}, {"expires_at": -1}, {"expires_at": 2**53},
    {"command": "forget"}, {"command": "set_enabled"},
])
def test_malformed_or_cross_command_fields_refuse(change):
    with pytest.raises(AssignmentError) as caught:
        ExplicitNoteService.snapshot(command(**change))
    assert caught.value.code == "explicit_note_command_invalid"


@pytest.mark.parametrize("kind", ["forget", "set_enabled"])
def test_control_requires_existing_revision_and_no_value(kind):
    value = ExplicitNoteCommand(command=kind, note_id=str(uuid4()), expected_revision=1,
                               enabled=False if kind == "set_enabled" else None)
    assert ExplicitNoteService.snapshot(value) == value
    with pytest.raises(AssignmentError):
        ExplicitNoteService.snapshot(replace(value, expected_revision=0))


def test_untyped_command_or_uncomposed_application_refuses():
    with pytest.raises(AssignmentError):
        ExplicitNoteService.snapshot({"command": "forget"})
    with pytest.raises(AssignmentError):
        ExplicitNoteService(object())
