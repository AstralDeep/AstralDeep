"""Tests for astralplane.repositories.revocations: bounded, ordered, typed
revocation-queue pages, cursor validation, and a cycle that cannot rewind, relabel,
or silently end on a database failure.
"""

from dataclasses import FrozenInstanceError

import pytest

from astralplane.repositories import RepositoryDataError, RepositoryValidationError
from astralplane.repositories.revocations import (
    RevocationQueueCursor,
    RevocationQueueRepository,
)
from tests.repositories._support import ScriptedTransaction
from tests.repositories.test_revocations import _row


def result(*rows, ceiling=10):
    return tuple({**row, "cycle_ceiling": ceiling} for row in rows) or (
        {"id": None, "cycle_ceiling": ceiling},
    )


def test_page_is_bounded_ordered_typed_and_ciphertext_is_not_represented():
    query = ScriptedTransaction(all_rows=[result(_row(id=1), _row(id=2), _row(id=3))])
    page = RevocationQueueRepository().page_for_administration(query, limit=2)
    assert [row.queue_id for row in page.records] == [1, 2]
    assert page.ceiling == 10
    assert page.next_cursor == RevocationQueueCursor(42, 2)
    assert "opaque-refresh-ciphertext" not in repr(page)
    assert len(query.calls) == 1
    assert query.calls[0][2] == (None, None, None, None, 3)
    with pytest.raises(FrozenInstanceError):
        page.ceiling = 99


def test_continuation_preserves_ceiling_and_reports_cycle_end():
    query = ScriptedTransaction(all_rows=[result(_row(id=3), ceiling=4)])
    page = RevocationQueueRepository().page_for_administration(
        query, limit=2, after=RevocationQueueCursor(42, 2), ceiling=4
    )
    assert len(page.records) == 1 and page.next_cursor is None and page.ceiling == 4
    assert query.calls[0][2] == (4, 42, 42, 2, 3)


@pytest.mark.parametrize("ceiling", [None, 10])
def test_empty_queue_or_deleted_remainder_ends_cycle(ceiling):
    query = ScriptedTransaction(all_rows=[result(ceiling=ceiling)])
    page = RevocationQueueRepository().page_for_administration(query, ceiling=ceiling)
    assert page.records == () and page.next_cursor is None and page.ceiling == ceiling


@pytest.mark.parametrize("value", [True, False, -1, 2**63, 1.0, "1", [], None])
@pytest.mark.parametrize("field", ["enqueued_at", "queue_id"])
def test_cursor_rejects_noncanonical_numbers(value, field):
    fields = {"enqueued_at": 0, "queue_id": 1, field: value}
    with pytest.raises(RepositoryValidationError):
        RevocationQueueCursor(**fields)


def test_cursor_accepts_exact_endpoints_but_not_zero_id():
    assert RevocationQueueCursor(0, 1).queue_id == 1
    assert RevocationQueueCursor(2**63 - 1, 2**63 - 1).enqueued_at == 2**63 - 1
    with pytest.raises(RepositoryValidationError):
        RevocationQueueCursor(0, 0)


@pytest.mark.parametrize(
    "arguments",
    [
        *({"limit": value} for value in [True, 0, 201, 1.0, "20", None]),
        *({"ceiling": value} for value in [True, 0, -1, 2**63, "2", 2.0]),
        {"after": (1, 2), "ceiling": 3},
        {"after": {"enqueued_at": 1, "queue_id": 2}, "ceiling": 3},
        {"after": RevocationQueueCursor(1, 2)},
        {"after": RevocationQueueCursor(1, 3), "ceiling": 2},
    ],
)
def test_invalid_page_arguments_refuse_before_query(arguments):
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        RevocationQueueRepository().page_for_administration(query, **arguments)
    assert query.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        (),
        result(_row(id=1), ceiling=None),
        result(_row(id=1), ceiling=True),
        result(_row(id=True)),
        result(_row(enqueued_at=1.5)),
        result(_row(id=11)),
        result(_row(id=2), _row(id=1)),
        result(_row(id=1), _row(id=1)),
        result(_row(id=1), _row(id=2), _row(id=3), _row(id=4)),
        ({"id": None, "cycle_ceiling": 10}, {**_row(), "cycle_ceiling": 10}),
        ({**_row(id=1), "cycle_ceiling": 10}, {**_row(id=2), "cycle_ceiling": 11}),
        ({**_row(id=1)},),
    ],
)
def test_malformed_page_data_cannot_create_a_cursor(rows):
    with pytest.raises(RepositoryDataError):
        RevocationQueueRepository().page_for_administration(
            ScriptedTransaction(all_rows=[rows]), limit=2
        )


def test_returned_cursor_and_ceiling_cannot_rewind_or_relabel_cycle():
    for rows in (result(_row(id=2), ceiling=10), result(_row(id=3), ceiling=11)):
        with pytest.raises(RepositoryDataError):
            RevocationQueueRepository().page_for_administration(
                ScriptedTransaction(all_rows=[rows]),
                after=RevocationQueueCursor(42, 2),
                ceiling=10,
            )


def test_database_failure_is_not_silently_treated_as_end_of_cycle():
    with pytest.raises(RuntimeError, match="database unavailable"):
        RevocationQueueRepository().page_for_administration(
            ScriptedTransaction(all_rows=[RuntimeError("database unavailable")])
        )
