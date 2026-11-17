"""Tests for astralplane.repositories.preferences chat organization: pinned and archived
conversation identities read safely from the owner's preferences document, change only for an
owned conversation under the owner lock, stay bounded, and never clobber other keys.
"""

import json

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.preferences import (
    CHAT_ORGANIZATION_LIMIT,
    ChatOrganizationRecord,
    PreferencesRepository,
)
from tests.repositories._support import Result, ScriptedTransaction


def _prefs(value):
    return {"preferences": json.dumps(value)}


@pytest.mark.parametrize(
    "row,pinned,archived",
    [
        (None, (), ()),
        (_prefs({}), (), ()),
        (
            _prefs({"chat_organization": {"pinned": ["a"], "archived": ["b", "c"]}}),
            ("a",),
            ("b", "c"),
        ),
        (_prefs({"chat_organization": {"pinned": ["a"]}}), ("a",), ()),
    ],
)
def test_read_defaults_to_nothing_pinned_or_archived(row, pinned, archived):
    tx = ScriptedTransaction(one=[row])
    record = PreferencesRepository().get_chat_organization(tx, owner_id="alice")
    assert record == ChatOrganizationRecord("alice", pinned, archived)
    assert tx.calls[0][2] == ("alice",)


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '{"chat_organization": []}',
        '{"chat_organization": {"pinned": "a"}}',
        '{"chat_organization": {"archived": [1]}}',
        '{"chat_organization": {"pinned": ["a", "a"]}}',
    ],
)
def test_corrupt_organization_refuses(value):
    with pytest.raises(RepositoryDataError):
        PreferencesRepository().get_chat_organization(
            ScriptedTransaction(one=[{"preferences": value}]), owner_id="alice"
        )


def _set(before, *, flag="pinned", value=True, present=("c1", "c2"), conversation="c1", rowcount=1):
    tx = ScriptedTransaction(
        one=[{"present": 1}, _prefs(before)],
        all_rows=[tuple({"id": item} for item in present)],
        execute=[Result(), Result(rowcount=rowcount)],
    )
    record = PreferencesRepository().set_chat_flag(
        tx, owner_id="alice", conversation_id=conversation, flag=flag, value=value
    )
    return tx, record


def test_pin_adds_first_and_preserves_other_keys_under_the_owner_lock():
    before = {
        "theme": {"preset": "midnight"},
        "chat_organization": {"pinned": ["c2"], "archived": []},
    }
    tx, record = _set(before)
    assert record == ChatOrganizationRecord("alice", ("c1", "c2"), ())
    assert tx.calls[0][2] == ("c1", "alice") and "user_id = %s" in tx.calls[0][1]
    assert "FOR UPDATE" in tx.calls[2][1] and tx.calls[2][2] == ("alice",)
    assert tx.calls[3][2] == ("alice", ["c2"])
    data, owner = tx.calls[-1][2]
    assert owner == "alice" and json.loads(data) == {
        **before,
        "chat_organization": {"pinned": ["c1", "c2"], "archived": []},
    }


def test_unpin_and_unarchive_remove_and_repeat_is_idempotent():
    _tx, record = _set(
        {"chat_organization": {"pinned": ["c1", "c2"], "archived": ["c1"]}}, value=False
    )
    assert record.pinned == ("c2",) and record.archived == ("c1",)
    _tx, record = _set(
        {"chat_organization": {"pinned": ["c1"], "archived": ["c1"]}}, flag="archived", value=True
    )
    assert record.archived == ("c1",)


def test_identities_of_deleted_conversations_are_pruned_on_write():
    _tx, record = _set(
        {"chat_organization": {"pinned": ["gone", "c2"], "archived": ["gone"]}},
        flag="archived",
        present=("c1", "c2"),
    )
    assert record == ChatOrganizationRecord("alice", ("c2",), ("c1",))


def test_a_foreign_or_missing_conversation_changes_nothing():
    tx = ScriptedTransaction(one=[None])
    with pytest.raises(RepositoryNotFoundError):
        PreferencesRepository().set_chat_flag(
            tx, owner_id="alice", conversation_id="theirs", flag="pinned", value=True
        )
    assert [call[0] for call in tx.calls] == ["one"]


def test_the_lists_stay_bounded():
    full = [f"c{index}" for index in range(CHAT_ORGANIZATION_LIMIT)]
    with pytest.raises(RepositoryValidationError):
        _set({"chat_organization": {"pinned": full}}, conversation="new", present=(*full, "new"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"owner_id": ""},
        {"conversation_id": ""},
        {"flag": "starred"},
        {"value": 1},
        {"value": None},
    ],
)
def test_invalid_requests_refuse_before_any_statement(kwargs):
    tx = ScriptedTransaction()
    arguments = {
        "owner_id": "alice",
        "conversation_id": "c1",
        "flag": "pinned",
        "value": True,
        **kwargs,
    }
    with pytest.raises(RepositoryValidationError):
        PreferencesRepository().set_chat_flag(tx, **arguments)
    assert tx.calls == []


@pytest.mark.parametrize(
    "locked,rowcount,error",
    [
        (None, 1, RepositoryConflictError),
        ({"preferences": "[]"}, 1, RepositoryDataError),
        (_prefs({}), 0, RepositoryConflictError),
    ],
)
def test_write_failures_are_not_success(locked, rowcount, error):
    tx = ScriptedTransaction(
        one=[{"present": 1}, locked], all_rows=[()], execute=[Result(), Result(rowcount=rowcount)]
    )
    with pytest.raises(error):
        PreferencesRepository().set_chat_flag(
            tx, owner_id="alice", conversation_id="c1", flag="pinned", value=True
        )
