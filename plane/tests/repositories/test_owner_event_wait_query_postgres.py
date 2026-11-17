"""Tests owner-scoped event waiter filtering and cursor bounds over real Plane records.
The assignment fixtures exercise exact event keys, prefixes, and foreign-owner refusal.
"""

import pytest
from test_assignments_postgres import create_operation, uid
from test_assignments_postgres import database as database
from test_assignments_postgres import repo as repo
from test_assignments_postgres import tx as tx
from test_operation_control_postgres import control
from test_owner_event_wait_postgres import arguments

from astralplane.repositories import RepositoryValidationError


def parked(repo, tx, key):
    record = create_operation(repo, tx, caller_key=uid())
    return repo.set_owner_event_wait(tx, **arguments(record, event_key=key)).assignment


def test_exact_keys_and_literal_prefixes_filter_before_cursor_pagination(tx, repo):
    records = [
        parked(repo, tx, key) for key in ("operation:first", "operation:second", "remote:first")
    ]
    create_operation(repo, tx, caller_key=uid())
    expected = sorted(records[:2], key=lambda record: record.assignment_id)
    first = repo.list_owner_event_waits(
        tx, owner_id="owner", event_key_prefix="operation:", limit=1
    )
    second = repo.list_owner_event_waits(
        tx,
        owner_id="owner",
        event_key_prefix="operation:",
        limit=1,
        after_id=first[0].assignment.assignment_id,
    )
    assert [first[0].assignment, second[0].assignment] == expected
    assert repo.list_owner_event_waits(tx, owner_id="owner", event_key_prefix="operation:%") == ()
    selected = repo.list_owner_event_waits(tx, owner_id="owner", event_keys=("remote:first",))
    assert tuple(row.assignment for row in selected) == (records[2],)
    assert repo.list_owner_event_waits(tx, owner_id="foreign", event_keys=("remote:first",)) == ()


def test_terminal_filter_preserves_paused_reservations_and_default_history(tx, repo):
    records = [parked(repo, tx, "source:key") for _ in range(4)]
    control(repo, tx, records[1], "pause")
    control(repo, tx, records[2], "stop")
    tx.execute(
        "UPDATE persistent_assignment SET lifecycle='completed',"
        "data=jsonb_set(data,'{lifecycle}',to_jsonb('completed'::text)) WHERE id=%s",
        (records[3].assignment_id,),
    )
    historical = repo.list_owner_event_waits(tx, owner_id="owner", event_keys=("source:key",))
    reserved = repo.list_owner_event_waits(
        tx, owner_id="owner", event_keys=("source:key",), include_terminal=False
    )
    assert {row.assignment.assignment_id for row in historical} == {
        row.assignment_id for row in records
    }
    assert {row.assignment.assignment_id for row in reserved} == {
        row.assignment_id for row in records[:2]
    }
    assert {row.assignment.lifecycle for row in reserved} == {"active", "paused"}


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"event_keys": ()},
        {"event_keys": "key"},
        {"event_keys": ("",)},
        {"event_keys": ("x" * 129,)},
        {"event_keys": ("key",) * 65},
        {"event_key_prefix": ""},
        {"event_keys": ("key",), "event_key_prefix": "key"},
        {"event_keys": ("key",), "limit": 101},
        {"event_keys": ("key",), "limit": True},
        {"event_keys": ("key",), "after_id": "invalid"},
        {"event_keys": ("key",), "include_terminal": 1},
        {"event_keys": ("key",), "include_terminal": None},
        {"event_keys": ("key",), "include_terminal": "false"},
    ],
)
def test_query_filters_and_page_bounds_are_explicit(tx, repo, options):
    with pytest.raises(RepositoryValidationError):
        repo.list_owner_event_waits(tx, owner_id="owner", **options)
