"""Tests for astralplane.repositories.preferences: chat notice-preference reads default
safely, merge under an owner lock without clobbering other keys, and require typed
boolean writes.
"""

import json
from types import SimpleNamespace

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.preferences import PreferencesRepository


class Transaction:
    def __init__(self, row, rowcount=1):
        self.row, self.rowcount, self.calls = row, rowcount, []

    def fetch_one(self, sql, params):
        self.calls.append((sql, params))
        return self.row

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return SimpleNamespace(rowcount=self.rowcount)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),
        ({}, True),
        ({"chat_phi_notice_enabled": True}, True),
        ({"chat_phi_notice_enabled": False}, False),
    ],
)
def test_read_defaults_and_boolean_choices(value, expected):
    tx = Transaction(None if value is None else {"preferences": json.dumps(value)})
    assert PreferencesRepository().get_chat_phi_notice_enabled(tx, owner_id="alice") is expected
    assert tx.calls[0][1] == ("alice",)


@pytest.mark.parametrize("value", ["[]", '{"chat_phi_notice_enabled":"false"}', '"invalid"'])
def test_corrupt_saved_settings_refuse(value):
    with pytest.raises(RepositoryDataError):
        PreferencesRepository().get_chat_phi_notice_enabled(
            Transaction({"preferences": value}), owner_id="alice"
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_update_preserves_other_keys_under_owner_lock(enabled):
    before = {"theme": {"preset": "midnight"}, "disabled_agents": ["a"], "other": True}
    tx = Transaction({"preferences": json.dumps(before)})
    PreferencesRepository().set_chat_phi_notice_enabled(tx, owner_id="alice", enabled=enabled)
    assert "FOR UPDATE" in tx.calls[1][0]
    assert tx.calls[0][1] == tx.calls[1][1] == ("alice",)
    data, owner = tx.calls[-1][1]
    assert owner == "alice"
    assert json.loads(data) == {**before, "chat_phi_notice_enabled": enabled}


@pytest.mark.parametrize("enabled", [None, "false", 0, 1, []])
def test_write_requires_boolean_before_any_mutation(enabled):
    tx = Transaction({"preferences": "{}"})
    with pytest.raises(RepositoryValidationError):
        PreferencesRepository().set_chat_phi_notice_enabled(tx, owner_id="alice", enabled=enabled)
    assert not tx.calls


@pytest.mark.parametrize(
    "row,rowcount,error",
    [
        (None, 1, RepositoryConflictError),
        ({"preferences": "[]"}, 1, RepositoryDataError),
        ({"preferences": "{}"}, 0, RepositoryConflictError),
    ],
)
def test_write_failures_are_not_success(row, rowcount, error):
    with pytest.raises(error):
        PreferencesRepository().set_chat_phi_notice_enabled(
            Transaction(row, rowcount), owner_id="alice", enabled=False
        )


@pytest.mark.parametrize("operation", ["get", "set"])
def test_owner_is_required(operation):
    with pytest.raises(RepositoryValidationError):
        repo = PreferencesRepository()
        if operation == "get":
            repo.get_chat_phi_notice_enabled(Transaction(None), owner_id="")
        else:
            repo.set_chat_phi_notice_enabled(Transaction(None), owner_id="", enabled=False)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, False),
        ({}, False),
        ({"phi_protection_enabled": True}, True),
        ({"phi_protection_enabled": False}, False),
        ({"chat_phi_notice_enabled": True}, False),
    ],
)
def test_phi_protection_defaults_off_until_explicitly_enabled(value, expected):
    row = None if value is None else {"preferences": json.dumps(value)}
    repository = PreferencesRepository()
    assert repository.get_phi_protection_enabled(Transaction(row), owner_id="u") is expected


def test_phi_protection_rejects_a_non_boolean_stored_value():
    row = {"preferences": json.dumps({"phi_protection_enabled": "yes"})}
    with pytest.raises(RepositoryDataError):
        PreferencesRepository().get_phi_protection_enabled(
            Transaction(row), owner_id="u"
        )


def test_phi_protection_write_merges_into_the_document_and_requires_a_bool():
    stored = {"theme": {"preset": "graphite"}, "chat_phi_notice_enabled": False}
    row = {"preferences": json.dumps(stored)}
    transaction = Transaction(row)
    repository = PreferencesRepository()
    repository.set_phi_protection_enabled(transaction, owner_id="u", enabled=True)
    update_sql, update_params = transaction.calls[-1]
    assert update_sql.startswith("UPDATE user_preferences SET preferences")
    assert json.loads(update_params[0]) == {**stored, "phi_protection_enabled": True}
    with pytest.raises(RepositoryValidationError):
        repository.set_phi_protection_enabled(Transaction(row), owner_id="u", enabled="on")
    with pytest.raises(RepositoryConflictError):
        repository.set_phi_protection_enabled(
            Transaction(row, rowcount=0), owner_id="u", enabled=True
        )
