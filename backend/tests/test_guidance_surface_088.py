"""Closed private-note UI inputs; no caller, secret, file or provider access."""
from copy import deepcopy
from uuid import uuid4

import pytest

from persistent_agents.models import AssignmentError


def save(**fields):
    return {"note_id": str(uuid4()), "expected_revision": 0, "fields": {
        "category": "Context", "value": "Prefer short paragraphs.", "enabled": True,
        "expiry": "No expiry", "expiry_date": "", **fields}}


@pytest.mark.parametrize("action,payload", [
    ("chrome_open", {"surface": "guidance", "params": {"mode": "list", "search": "short"}}),
    ("chrome_open", {"surface": "guidance", "params": {"mode": "new"}}),
    ("chrome_note_search", {"fields": {"search": "short"}}),
    ("chrome_note_save", save()),
    ("chrome_note_toggle", {"note_id": str(uuid4()), "expected_revision": 1, "enabled": False}),
    ("chrome_note_forget", {"note_id": str(uuid4()), "expected_revision": 1}),
])
def test_closed_request_copies_all_inputs(action, payload):
    from orchestrator.projection_surfaces.guidance import _request
    expected = deepcopy(payload)
    frozen = _request(action, payload)
    payload.clear()
    assert frozen == expected


@pytest.mark.parametrize("change", [
    {"category": "context"}, {"category": []}, {"enabled": "true"}, {"enabled": 1},
    {"value": ""}, {"value": "x" * 4097}, {"value": "\ud800"}, {"value": "hidden\x00value"},
    {"expiry": "Keep current expiry"}, {"expiry": "Tomorrow"}, {"expiry": []},
    {"expiry_date": 0}, {"owner_id": "another-owner"},
])
def test_invalid_save_fields_are_data_free(change):
    from orchestrator.projection_surfaces.guidance import _request
    with pytest.raises(AssignmentError, match="explicit_note_request_invalid"):
        _request("chrome_note_save", save(**change))


@pytest.mark.parametrize("value", ["2026-12-31", "2026-12-31T23:59:00+01:00",
    "2026-12-31T23:59:00", "2026-12-31T23:59:00.1234Z", "2026-02-30T00:00:00Z",
    "2026-12-31T23:59:60Z", " 2026-12-31T23:59:00Z", "", "1970-01-01T00:00:00Z"])
def test_explicit_date_is_exact_bounded_utc(value):
    from orchestrator.projection_surfaces.guidance import _request
    with pytest.raises(AssignmentError):
        _request("chrome_note_save", save(expiry="Set a date", expiry_date=value))


def test_date_milliseconds_are_exact_not_float_or_local_timezone():
    from orchestrator.projection_surfaces.guidance import _date
    assert _date("2026-12-31T23:59:00.123Z") == 1798761540123
    assert _date("2026-12-31T23:59:00.1Z") == 1798761540100


@pytest.mark.parametrize("action,payload", [
    ("chrome_note_unknown", {}), ("chrome_open", {"surface": "personalization"}),
    ("chrome_open", {"surface": "guidance", "params": "{}"}),
    ("chrome_open", {"surface": "guidance", "params": {"mode": "history"}}),
    ("chrome_open", {"surface": "guidance", "params": {"mode": "edit", "note_id": str(uuid4()), "expected_revision": True}}),
    ("chrome_open", {"surface": "guidance", "params": {"mode": "new", "note_id": str(uuid4())}}),
    ("chrome_open", {"surface": "guidance", "params": {"after_id": "not-an-id"}}),
    ("chrome_note_search", {"fields": {"search": "x" * 257}}),
    ("chrome_note_search", {"fields": {"search": "", "value": "private"}}),
    ("chrome_note_toggle", {"note_id": str(uuid4()), "expected_revision": 0, "enabled": True}),
    ("chrome_note_toggle", {"note_id": str(uuid4()), "expected_revision": 1, "enabled": "false"}),
    ("chrome_note_forget", {"note_id": str(uuid4()), "expected_revision": 1, "value": "private"}),
    ("chrome_note_forget", {"note_id": str(uuid4()).upper(), "expected_revision": 1}),
    ("chrome_note_save", {**save(), "expected_revision": 2**53-1}),
])
def test_unknown_or_conflicting_input_never_becomes_an_action(action, payload):
    from orchestrator.projection_surfaces.guidance import _request
    with pytest.raises(AssignmentError):
        _request(action, payload)


def test_socket_policy_is_code_owned_and_malformed_surface_has_no_authority():
    from orchestrator.human_request_authority import _socket_method
    for action in ("chrome_note_save", "chrome_note_toggle", "chrome_note_forget"):
        assert _socket_method({"type": "ui_event", "action": action, "payload": {"read_only": True}}) == "WS_WRITE"
    for action, payload in (("chrome_note_search", {"write": True}),
                            ("chrome_open", {"surface": "guidance", "write": True})):
        assert _socket_method({"type": "ui_event", "action": action, "payload": payload}) == "WS_READ"
    assert _socket_method({"type": "ui_event", "action": "chrome_note_unknown"}) is None
    assert _socket_method({"type": "ui_event", "action": "chrome_open", "payload": {"surface": []}}) is None
