"""Checks Atlas link command, record, cursor, receipt, and batch bounds without PostgreSQL.
Real database behavior is covered by test_atlas_links_postgres.py in the same directory.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.atlas_links import (
    MAX_LINK_LIST,
    MAX_LINK_TARGETS,
    OUTBOUND,
    AtlasLinkBatch,
    AtlasLinkChange,
    AtlasLinkCommandReceipt,
    AtlasLinkCursor,
    AtlasLinkRecord,
    AtlasLinkRepository,
    AtlasLinkSetCommand,
    _link,
    _receipt,
)
from tests.repositories._support import ScriptedTransaction

PAGE = "11111111-1111-4111-8111-111111111111"
TARGET = "11111111-1111-4111-8111-111111111112"
OTHER = "11111111-1111-4111-8111-111111111113"
COMMAND = "22222222-2222-4222-8222-222222222222"


def link_command(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        expected_page_revision=1,
        expected_link_revision=0,
        targets=(TARGET,),
        updated_at=20,
    )
    values.update(changes)
    return AtlasLinkSetCommand(**values)


def link_row(**changes):
    values = dict(
        owner_id="atlas-owner",
        source_page_id=PAGE,
        target_page_id=TARGET,
        source_revision=1,
        target_revision=1,
        link_revision=1,
        linked_at=20,
    )
    values.update(changes)
    return values


def receipt_row(**changes):
    command = link_command()
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        request_digest=command.request_digest,
        expected_page_revision=1,
        expected_link_revision=0,
        result_link_revision=1,
        linked_at=20,
    )
    values.update(changes)
    return values


def test_link_command_digest_binds_fences_targets_and_time():
    original = link_command()
    assert original.targets == (TARGET,)
    assert original.request_digest == link_command().request_digest
    assert original.request_digest == link_command(targets=[TARGET]).request_digest
    assert original.request_digest != link_command(targets=()).request_digest
    assert original.request_digest != link_command(expected_link_revision=1).request_digest
    assert original.request_digest != link_command(updated_at=21).request_digest
    assert link_command(targets=(OTHER, TARGET)).targets == (TARGET, OTHER)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"owner_id": " padded"},
        {"owner_id": "bad\x00owner"},
        {"command_id": "not-a-uuid"},
        {"page_id": PAGE.replace("1111", "AAAA", 1)},
        {"expected_page_revision": 0},
        {"expected_page_revision": True},
        {"expected_link_revision": -1},
        {"expected_link_revision": 2**53 - 1},
        {"targets": "one"},
        {"targets": (TARGET, TARGET)},
        {"targets": (PAGE,)},
        {"targets": ("bad",)},
        {
            "targets": tuple(
                str(index).zfill(8) + "-1111-4111-8111-111111111111"
                for index in range(MAX_LINK_TARGETS + 1)
            )
        },
        {"updated_at": -1},
        {"updated_at": "now"},
    ],
)
def test_link_command_rejects_invalid_bounds_before_sql(changes):
    with pytest.raises(RepositoryValidationError):
        link_command(**changes)


def test_link_record_accepts_current_shapes_and_rejects_corruption():
    record = _link(link_row())
    assert record == AtlasLinkRecord("atlas-owner", PAGE, TARGET, 1, 1, 1, 20)
    for changes in (
        {"source_page_id": TARGET, "target_page_id": TARGET},
        {"source_revision": 0},
        {"target_revision": 0},
        {"link_revision": 0},
        {"linked_at": -1},
        {"linked_at": 2**53},
    ):
        with pytest.raises((RepositoryValidationError, RepositoryDataError)):
            _link(link_row(**changes))
    with pytest.raises(RepositoryDataError):
        _link({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _link(link_row(link_revision="one"))


def test_link_receipt_and_change_refuse_inconsistent_values():
    receipt = _receipt(receipt_row())
    assert receipt == AtlasLinkCommandReceipt(
        "atlas-owner", COMMAND, PAGE, link_command().request_digest, 1, 0, 1, 20
    )
    assert AtlasLinkChange(receipt, (_link(link_row()),), False).receipt == receipt
    assert AtlasLinkBatch((_link(link_row()),), None).records[0].target_page_id == TARGET
    with pytest.raises(RepositoryValidationError):
        AtlasLinkCommandReceipt(
            "atlas-owner", COMMAND, PAGE, "a" * 64, 1, 0, 2, 20
        )
    with pytest.raises(RepositoryValidationError):
        AtlasLinkCommandReceipt(
            "atlas-owner", COMMAND, PAGE, "not-a-digest", 1, 0, 1, 20
        )
    with pytest.raises(RepositoryDataError):
        _receipt({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _receipt(receipt_row(result_link_revision=2))
    with pytest.raises(RepositoryValidationError):
        AtlasLinkChange(object(), (), False)
    with pytest.raises(RepositoryValidationError):
        AtlasLinkChange(receipt, (object(),), False)
    with pytest.raises(RepositoryValidationError):
        AtlasLinkChange(receipt, (), "no")
    with pytest.raises(RepositoryValidationError):
        AtlasLinkChange(
            receipt,
            (
                AtlasLinkRecord(
                    "atlas-owner",
                    OTHER,
                    TARGET,
                    1,
                    1,
                    1,
                    20,
                ),
            ),
            False,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasLinkBatch([_link(link_row())], None)
    with pytest.raises(RepositoryValidationError):
        AtlasLinkBatch((_link(link_row()),), object())


def test_repository_preflights_commands_reads_and_cursors_before_sql():
    repository = AtlasLinkRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.set_links(query, command=object())
    with pytest.raises(RepositoryValidationError):
        repository.list_links(query, owner_id="", page_id=PAGE)
    with pytest.raises(RepositoryValidationError):
        repository.list_links(query, owner_id="atlas-owner", page_id="bad")
    with pytest.raises(RepositoryValidationError):
        repository.list_backlinks(query, owner_id="atlas-owner", page_id=PAGE, limit=0)
    with pytest.raises(RepositoryValidationError):
        repository.list_backlinks(
            query,
            owner_id="atlas-owner",
            page_id=PAGE,
            limit=MAX_LINK_LIST + 1,
        )
    cursor = AtlasLinkCursor("atlas-owner", PAGE, OUTBOUND, 20, PAGE, TARGET)
    with pytest.raises(RepositoryValidationError):
        repository.list_backlinks(
            query,
            owner_id="atlas-owner",
            page_id=PAGE,
            cursor=cursor,
        )
    with pytest.raises(RepositoryValidationError):
        repository.list_links(
            query,
            owner_id="atlas-owner",
            page_id=OTHER,
            cursor=cursor,
        )
    with pytest.raises(RepositoryValidationError):
        repository.list_links(
            query,
            owner_id="other-owner",
            page_id=PAGE,
            cursor=cursor,
        )
    assert query.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": "sideways"},
        {"linked_at": 2**53},
        {"source_page_id": "bad"},
        {"target_page_id": "bad"},
        {"owner_id": ""},
    ],
)
def test_cursor_rejects_invalid_scope_and_bounds(changes):
    values = dict(
        owner_id="atlas-owner",
        page_id=PAGE,
        direction=OUTBOUND,
        linked_at=20,
        source_page_id=PAGE,
        target_page_id=TARGET,
    )
    values.update(changes)
    with pytest.raises(RepositoryValidationError):
        AtlasLinkCursor(**values)


def test_scripted_set_success_covers_bounded_target_inserts():
    repository = AtlasLinkRepository()
    command = link_command()
    page = {
        "owner_id": "atlas-owner",
        "page_id": PAGE,
        "revision": 1,
        "updated_at": 10,
        "deleted_at": None,
    }
    advanced = {
        "owner_id": "atlas-owner",
        "page_id": PAGE,
        "link_revision": 1,
        "updated_at": 20,
    }
    transaction = ScriptedTransaction(
        one=[{"acquired": None}, None, page, None, advanced],
        all_rows=[({"page_id": TARGET, "revision": 1},)],
    )
    change = repository.set_links(transaction, command=command)
    assert not change.replayed
    assert change.receipt.result_link_revision == 1
    assert change.links == (AtlasLinkRecord("atlas-owner", PAGE, TARGET, 1, 1, 1, 20),)
    assert sum(call[0] == "execute" for call in transaction.calls) == 3


def test_scripted_set_failures_are_typed_before_writing_links():
    repository = AtlasLinkRepository()
    command = link_command()
    with pytest.raises(RepositoryConflictError, match="identity was reused"):
        repository.set_links(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(request_digest="a" * 64),
                ]
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="identity was reused"):
        repository.set_links(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(
                        page_id=OTHER,
                    ),
                ]
            ),
            command=command,
        )
    page = {
        "owner_id": "atlas-owner",
        "page_id": PAGE,
        "revision": 1,
        "updated_at": 10,
        "deleted_at": None,
    }
    with pytest.raises(RepositoryConflictError, match="revision changed"):
        repository.set_links(
            ScriptedTransaction(
                one=[{"acquired": None}, None, page | {"revision": 2}],
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="clock regressed"):
        repository.set_links(
            ScriptedTransaction(
                one=[{"acquired": None}, None, page | {"updated_at": 21}],
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="deleted"):
        repository.set_links(
            ScriptedTransaction(
                one=[{"acquired": None}, None, page | {"deleted_at": 10}],
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="link revision changed"):
        repository.set_links(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    None,
                    page,
                    {"link_revision": 1, "updated_at": 10},
                ]
            ),
            command=command,
        )
    with pytest.raises(RepositoryNotFoundError):
        repository.set_links(
            ScriptedTransaction(one=[{"acquired": None}, None, None]),
            command=command,
        )


def test_scripted_missing_or_duplicate_target_and_head_failure_are_typed():
    repository = AtlasLinkRepository()
    command = link_command()
    page = {
        "owner_id": "atlas-owner",
        "page_id": PAGE,
        "revision": 1,
        "updated_at": 10,
        "deleted_at": None,
    }
    with pytest.raises(RepositoryNotFoundError, match="endpoint"):
        repository.set_links(
            ScriptedTransaction(
                one=[{"acquired": None}, None, page, None],
                all_rows=[()],
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="link revision changed"):
        repository.set_links(
            ScriptedTransaction(
                one=[{"acquired": None}, None, page, None, None],
                all_rows=[({"page_id": TARGET, "revision": 1},)],
            ),
            command=command,
        )