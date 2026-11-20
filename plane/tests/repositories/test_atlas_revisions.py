"""Checks Atlas revision command, record, cursor, change, and replay contracts without
PostgreSQL. Real edit fences, history, concurrency, and rollback are covered by
test_atlas_revisions_postgres.py in the same directory.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.atlas_pages import (
    MAX_CONTENT_BYTES,
    MAX_REVISION,
    MAX_REVISION_LIST,
    AtlasPageCommandReceipt,
    AtlasPageEditChange,
    AtlasPageEditCommand,
    AtlasPageRepository,
    AtlasPageRevisionBatch,
    AtlasPageRevisionCursor,
    AtlasPageRevisionRecord,
    _page,
    _receipt,
    _revision,
)
from tests.repositories._support import ScriptedTransaction

PAGE = "11111111-1111-4111-8111-111111111111"
COMMAND = "22222222-2222-4222-8222-222222222222"


def edit_command(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        expected_revision=1,
        sources=("https://example.invalid/source",),
        content=b"synthetic-opaque-revision-ciphertext",
        updated_at=20,
    )
    values.update(changes)
    return AtlasPageEditCommand(**values)


def page_row(**changes):
    content = b"synthetic-opaque-head-ciphertext"
    values = dict(
        owner_id="atlas-owner",
        page_id=PAGE,
        project_id="atlas-project",
        slug="kickoff",
        revision=2,
        sources=["https://example.invalid/source"],
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        created_at=10,
        updated_at=20,
        deleted_at=None,
    )
    values.update(changes)
    return values


def revision_row(**changes):
    content = b"synthetic-opaque-revision-ciphertext"
    values = dict(
        owner_id="atlas-owner",
        page_id=PAGE,
        revision=2,
        sources=["https://example.invalid/source"],
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        created_at=20,
    )
    values.update(changes)
    return values


def receipt_row(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        command="edit",
        request_digest=edit_command().request_digest,
        result_revision=2,
        created_at=20,
        expected_revision=1,
    )
    values.update(changes)
    return values


def test_edit_command_digest_binds_owner_fence_sources_and_ciphertext():
    original = edit_command()
    assert original.content_sha256 == hashlib.sha256(original.content).hexdigest()
    assert original.request_digest == edit_command().request_digest
    assert original.request_digest != edit_command(content=b"different").request_digest
    assert original.request_digest != edit_command(expected_revision=2).request_digest
    assert original.request_digest != edit_command(sources=()).request_digest
    assert original.request_digest != edit_command(updated_at=21).request_digest
    assert original.request_digest != edit_command(owner_id="other-owner").request_digest
    assert (
        original.request_digest
        != edit_command(page_id=PAGE.replace("1111", "1112", 1)).request_digest
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"owner_id": " padded"},
        {"command_id": "not-a-uuid"},
        {"page_id": PAGE.replace("1111", "AAAA", 1)},
        {"expected_revision": 0},
        {"expected_revision": MAX_REVISION},
        {"sources": "one"},
        {"sources": ("",)},
        {"sources": ("same", "same")},
        {"sources": tuple(str(index) for index in range(9))},
        {"content": bytearray(b"mutable")},
        {"content": b""},
        {"content": b"x" * (MAX_CONTENT_BYTES + 1)},
        {"updated_at": True},
        {"updated_at": -1},
        {"updated_at": 2**53},
    ],
)
def test_edit_command_rejects_invalid_bounds_before_sql(changes):
    with pytest.raises(RepositoryValidationError):
        edit_command(**changes)


def test_revision_record_accepts_exact_immutable_snapshot_and_rejects_corruption():
    valid = AtlasPageRevisionRecord(
        "atlas-owner",
        PAGE,
        2,
        ("https://example.invalid/source",),
        b"synthetic",
        hashlib.sha256(b"synthetic").hexdigest(),
        20,
    )
    assert valid.content == b"synthetic"
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionRecord(
            "atlas-owner",
            PAGE,
            2,
            (),
            b"synthetic",
            hashlib.sha256(b"other").hexdigest(),
            20,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionRecord(
            "atlas-owner",
            PAGE,
            2,
            (),
            b"",
            hashlib.sha256(b"").hexdigest(),
            20,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionRecord(
            "atlas-owner",
            PAGE,
            0,
            (),
            b"synthetic",
            hashlib.sha256(b"synthetic").hexdigest(),
            20,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionRecord(
            "atlas-owner",
            PAGE,
            2,
            (),
            b"synthetic",
            hashlib.sha256(b"synthetic").hexdigest(),
            2**53,
        )


def test_revision_cursor_batch_and_change_require_consistent_detached_values():
    page = _page(page_row())
    revision = _revision(revision_row())
    assert page is not None and revision is not None
    receipt = AtlasPageCommandReceipt(
        "atlas-owner", COMMAND, PAGE, "edit", edit_command().request_digest, 2, 20, 1
    )
    change = AtlasPageEditChange(page, revision, receipt, False)
    assert change.revision == revision
    batch = AtlasPageRevisionBatch((revision,), AtlasPageRevisionCursor("atlas-owner", PAGE, 2))
    assert batch.next_cursor == AtlasPageRevisionCursor("atlas-owner", PAGE, 2)
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionCursor("", PAGE, 2)
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionCursor("atlas-owner", "bad", 2)
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionCursor("atlas-owner", PAGE, 0)
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionBatch([revision], None)
    with pytest.raises(RepositoryValidationError):
        AtlasPageRevisionBatch((revision,), object())
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(object(), revision, receipt, False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(page, object(), receipt, False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(page, revision, object(), False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(page, revision, receipt, "no")
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(
            page,
            revision,
            AtlasPageCommandReceipt(
                "atlas-owner", COMMAND, PAGE, "create", edit_command().request_digest, 1, 20
            ),
            False,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(
            page,
            revision,
            AtlasPageCommandReceipt(
                "other-owner", COMMAND, PAGE, "edit", edit_command().request_digest, 2, 20, 1
            ),
            False,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(page, _revision(revision_row(revision=3)), receipt, False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageEditChange(
            _page(page_row(revision=1)),
            revision,
            receipt,
            False,
        )


def test_revision_parser_and_receipt_parser_fail_closed_on_stored_corruption():
    decoded = _revision(revision_row(sources=json.dumps(["json-source"])))
    assert decoded is not None and decoded.sources == ("json-source",)
    with pytest.raises(RepositoryDataError):
        _revision({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _revision(revision_row(content_sha256="f" * 64))
    with pytest.raises(RepositoryDataError):
        _revision(revision_row(sources="{not-json"))
    with pytest.raises(RepositoryDataError):
        _receipt({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _receipt(receipt_row(command="replace"))
    assert _receipt(receipt_row(expected_revision=None)).expected_revision is None


def test_receipt_accepts_exact_create_edit_and_delete_shapes():
    digest = "a" * 64
    assert (
        AtlasPageCommandReceipt("atlas-owner", COMMAND, PAGE, "create", digest, 1, 10).command
        == "create"
    )
    assert AtlasPageCommandReceipt(
        "atlas-owner", COMMAND, PAGE, "edit", digest, 3, 20, 2
    ).expected_revision == 2
    assert AtlasPageCommandReceipt(
        "atlas-owner", COMMAND, PAGE, "delete", digest, 3, 20, 2
    ).result_revision == 3
    for values in (
        ("atlas-owner", COMMAND, PAGE, "create", digest, 2, 10, None),
        ("atlas-owner", COMMAND, PAGE, "create", digest, 1, 10, 1),
        ("atlas-owner", COMMAND, PAGE, "edit", digest, 1, 10, None),
        ("atlas-owner", COMMAND, PAGE, "delete", digest, 1, 10, None),
        ("atlas-owner", COMMAND, PAGE, "edit", digest, 3, 10, 1),
        ("atlas-owner", COMMAND, PAGE, "edit", digest, 2, 10, 0),
        ("atlas-owner", COMMAND, PAGE, "edit", digest, 2, 2**53, 1),
    ):
        with pytest.raises(RepositoryValidationError):
            AtlasPageCommandReceipt(*values)


def test_repository_revision_reads_and_cursors_preflight_before_sql():
    repository = AtlasPageRepository()
    query = ScriptedTransaction()
    for changes in (
        {"owner_id": "", "page_id": PAGE, "revision": 1},
        {"owner_id": "atlas-owner", "page_id": "bad", "revision": 1},
        {"owner_id": "atlas-owner", "page_id": PAGE, "revision": 0},
        {"owner_id": "atlas-owner", "page_id": PAGE, "revision": True},
        {"owner_id": "atlas-owner", "page_id": PAGE, "revision": 1, "include_deleted": "no"},
    ):
        with pytest.raises(RepositoryValidationError):
            repository.get_revision(query, **changes)
    for changes in (
        {"owner_id": "", "page_id": PAGE},
        {"owner_id": "atlas-owner", "page_id": "bad"},
        {"owner_id": "atlas-owner", "page_id": PAGE, "limit": 0},
        {"owner_id": "atlas-owner", "page_id": PAGE, "limit": MAX_REVISION_LIST + 1},
        {
            "owner_id": "atlas-owner",
            "page_id": PAGE,
            "cursor": AtlasPageRevisionCursor("atlas-owner", PAGE.replace("1111", "1112", 1), 2),
        },
        {
            "owner_id": "atlas-owner",
            "page_id": PAGE,
            "cursor": AtlasPageRevisionCursor("other-owner", PAGE, 2),
        },
        {"owner_id": "atlas-owner", "page_id": PAGE, "include_deleted": "no"},
    ):
        with pytest.raises(RepositoryValidationError):
            repository.list_revisions(query, **changes)
    with pytest.raises(RepositoryValidationError):
        repository.get_page_revision(query, owner_id="", page_id=PAGE, revision=1)
    with pytest.raises(RepositoryValidationError):
        repository.list_page_revisions(query, owner_id="atlas-owner", page_id=PAGE, limit=0)
    assert query.calls == []


def test_repository_requires_typed_edit_command_before_sql():
    repository = AtlasPageRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.edit_page(query, command=object())
    with pytest.raises(RepositoryValidationError):
        repository.append_revision(query, command=object())
    assert query.calls == []


def test_exact_edit_replay_returns_original_revision_without_new_head_write():
    repository = AtlasPageRepository()
    command = edit_command()
    query = ScriptedTransaction(
        one=[
            {"acquired": None},
            receipt_row(request_digest=command.request_digest),
            page_row(),
            revision_row(),
        ]
    )
    result = repository.edit_page(query, command=command)
    assert result.replayed and result.page.revision == 2 and result.revision.revision == 2
    assert result.receipt.expected_revision == 1
    assert not any("UPDATE atlas_page" in statement for statement in query.fetch_sql().splitlines())


def test_edit_replay_and_preflight_failures_are_typed():
    repository = AtlasPageRepository()
    command = edit_command()
    with pytest.raises(RepositoryConflictError, match="identity was reused"):
        repository.edit_page(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(
                        command="create",
                        request_digest=command.request_digest,
                        result_revision=1,
                        expected_revision=None,
                    ),
                ]
            ),
            command=command,
        )
    with pytest.raises(RepositoryDataError, match="receipt has no page"):
        repository.edit_page(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(request_digest=command.request_digest),
                    None,
                ]
            ),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="deleted"):
        repository.edit_page(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(request_digest=command.request_digest),
                    page_row(
                        deleted_at=20,
                        updated_at=20,
                        revision=2,
                        content=None,
                        content_sha256=None,
                        sources=[],
                    ),
                ]
            ),
            command=command,
        )
    with pytest.raises(RepositoryDataError, match="receipt has no revision"):
        repository.edit_page(
            ScriptedTransaction(
                one=[
                    {"acquired": None},
                    receipt_row(request_digest=command.request_digest),
                    page_row(),
                    None,
                ]
            ),
            command=command,
        )


@pytest.mark.parametrize(
    "rows,error,match",
    [
        ([{"acquired": None}, None, None], RepositoryNotFoundError, "not found"),
        (
            [
                {"acquired": None},
                None,
                page_row(
                    deleted_at=20,
                    updated_at=20,
                    revision=2,
                    content=None,
                    content_sha256=None,
                    sources=[],
                ),
            ],
            RepositoryConflictError,
            "deleted",
        ),
        (
            [{"acquired": None}, None, page_row(revision=3)],
            RepositoryConflictError,
            "revision changed",
        ),
        (
            [{"acquired": None}, None, page_row(revision=1, updated_at=21)],
            RepositoryConflictError,
            "clock regressed",
        ),
        (
            [{"acquired": None}, None, page_row(revision=1), None],
            RepositoryConflictError,
            "revision changed",
        ),
    ],
)
def test_new_edit_fences_and_failed_head_write_are_typed(rows, error, match):
    with pytest.raises(error, match=match):
        AtlasPageRepository().edit_page(
            ScriptedTransaction(one=rows), command=edit_command()
        )


def test_successful_edit_advances_head_and_inserts_exact_revision_and_receipt():
    repository = AtlasPageRepository()
    command = edit_command()
    page = page_row(content=command.content, content_sha256=command.content_sha256)
    query = ScriptedTransaction(
        one=[{"acquired": None}, None, page_row(revision=1), page]
    )
    result = repository.edit_page(query, command=command)
    assert not result.replayed
    assert result.page.revision == 2 and result.revision.revision == 2
    assert result.receipt.expected_revision == 1
    sql = query.fetch_sql()
    assert "INSERT INTO atlas_page_revision" in sql
    assert "UPDATE atlas_page SET revision=%s" in sql
    assert "revision=%s AND deleted_at IS NULL RETURNING *" in sql
    assert "INSERT INTO atlas_page_command" in sql


def test_revision_listing_returns_bounded_next_cursor_and_handles_empty_rows():
    repository = AtlasPageRepository()
    first = _revision(revision_row(revision=3))
    second = _revision(revision_row(revision=2))
    query = ScriptedTransaction(all_rows=[(revision_row(revision=3), revision_row(revision=2))])
    batch = repository.list_revisions(query, owner_id="atlas-owner", page_id=PAGE, limit=1)
    assert batch.records == (first,)
    assert batch.next_cursor == AtlasPageRevisionCursor("atlas-owner", PAGE, 3)
    exact = repository.list_revisions(
        ScriptedTransaction(all_rows=[(revision_row(revision=2),)]),
        owner_id="atlas-owner",
        page_id=PAGE,
        limit=1,
    )
    assert exact.records == (second,) and exact.next_cursor is None
    empty = repository.list_revisions(
        ScriptedTransaction(all_rows=[()]), owner_id="atlas-owner", page_id=PAGE
    )
    assert empty.records == () and empty.next_cursor is None
    wrapped = repository.list_page_revisions(
        ScriptedTransaction(all_rows=[(revision_row(revision=2),)]),
        owner_id="atlas-owner",
        page_id=PAGE,
        include_deleted=True,
        cursor=AtlasPageRevisionCursor("atlas-owner", PAGE, 3),
    )
    assert wrapped.records == (second,)
