"""Checks Atlas page command, record, cursor, and preflight bounds without PostgreSQL.
Real database behavior is covered by test_atlas_pages_postgres.py in the same directory.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.atlas_pages import (
    MAX_CONTENT_BYTES,
    MAX_PAGE_LIST,
    AtlasPageBatch,
    AtlasPageChange,
    AtlasPageCommandReceipt,
    AtlasPageCreateCommand,
    AtlasPageCursor,
    AtlasPageDeleteCommand,
    AtlasPageRecord,
    AtlasPageRepository,
    _page,
    _receipt,
)
from tests.repositories._support import ScriptedTransaction

PAGE = "11111111-1111-4111-8111-111111111111"
COMMAND = "22222222-2222-4222-8222-222222222222"


def create_command(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        project_id="atlas-project",
        slug="kickoff",
        sources=("https://example.invalid/source",),
        content=b"synthetic-opaque-ciphertext",
        created_at=10,
    )
    values.update(changes)
    return AtlasPageCreateCommand(**values)


def delete_command(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        expected_revision=1,
        deleted_at=20,
    )
    values.update(changes)
    return AtlasPageDeleteCommand(**values)


def page_row(**changes):
    content = b"synthetic-opaque-ciphertext"
    values = dict(
        owner_id="atlas-owner",
        page_id=PAGE,
        project_id="atlas-project",
        slug="kickoff",
        revision=1,
        sources=["https://example.invalid/source"],
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        created_at=10,
        updated_at=10,
        deleted_at=None,
    )
    values.update(changes)
    return values


def test_create_command_digest_binds_owner_scope_metadata_and_ciphertext():
    original = create_command()
    assert original.content_sha256 == hashlib.sha256(original.content).hexdigest()
    assert original.request_digest == create_command().request_digest
    assert original.request_digest != create_command(content=b"different").request_digest
    assert original.request_digest != create_command(project_id=None).request_digest
    assert original.request_digest != create_command(sources=()).request_digest
    assert delete_command().request_digest != delete_command(expected_revision=2).request_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"owner_id": " padded"},
        {"command_id": "not-a-uuid"},
        {"page_id": PAGE.replace("1111", "AAAA", 1)},
        {"project_id": ""},
        {"project_id": "x" * 129},
        {"slug": "Kickoff"},
        {"slug": "-kickoff"},
        {"sources": "one"},
        {"sources": ("",)},
        {"sources": ("same", "same")},
        {"sources": tuple(str(index) for index in range(9))},
        {"content": bytearray(b"mutable")},
        {"content": b""},
        {"content": b"x" * (MAX_CONTENT_BYTES + 1)},
        {"created_at": True},
        {"created_at": -1},
    ],
)
def test_create_command_rejects_invalid_bounds_before_sql(changes):
    with pytest.raises(RepositoryValidationError):
        create_command(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": 0},
        {"expected_revision": 2**53 - 1},
        {"deleted_at": -1},
        {"deleted_at": "now"},
    ],
)
def test_delete_command_rejects_invalid_bounds_before_sql(changes):
    with pytest.raises(RepositoryValidationError):
        delete_command(**changes)


def test_record_accepts_live_and_tombstone_shapes_and_rejects_inconsistent_ones():
    live = _page(page_row())
    assert (
        live is not None
        and not live.deleted
        and live.sources == ("https://example.invalid/source",)
    )
    tombstone = _page(
        page_row(
            revision=2, sources=[], content=None, content_sha256=None, updated_at=20, deleted_at=20
        )
    )
    assert tombstone is not None and tombstone.deleted and tombstone.content is None
    for changes in (
        {"content_sha256": "f" * 64},
        {"content": None},
        {"deleted_at": 20},
    ):
        row = page_row(**changes)
        if changes.get("deleted_at") == 20 and changes.get("revision") != 2:
            row.update(content=None, content_sha256=None, sources=[])
        with pytest.raises((RepositoryValidationError, RepositoryDataError)):
            _page(row)


def test_corrupt_stored_page_and_receipt_are_data_errors():
    with pytest.raises(RepositoryDataError):
        _page({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _page(page_row(page_id="not-a-uuid"))
    with pytest.raises(RepositoryDataError):
        _receipt({"owner_id": "atlas-owner"})
    with pytest.raises(RepositoryDataError):
        _receipt(
            {
                "owner_id": "atlas-owner",
                "command_id": COMMAND,
                "page_id": PAGE,
                "command": "replace",
                "request_digest": "a" * 64,
                "result_revision": 1,
                "created_at": 10,
            }
        )


def test_receipt_requires_command_consistent_revision():
    with pytest.raises(RepositoryValidationError):
        AtlasPageCommandReceipt("atlas-owner", COMMAND, PAGE, "create", "a" * 64, 2, 10)
    with pytest.raises(RepositoryValidationError):
        AtlasPageCommandReceipt("atlas-owner", COMMAND, PAGE, "delete", "a" * 64, 1, 10)


def test_repository_preflights_reads_and_cursors_before_querying():
    repository = AtlasPageRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.get_page(query, owner_id="", page_id=PAGE)
    with pytest.raises(RepositoryValidationError):
        repository.get_page(query, owner_id="atlas-owner", page_id="bad")
    with pytest.raises(RepositoryValidationError):
        repository.get_page_by_slug(query, owner_id="atlas-owner", project_id=None, slug="Bad")
    with pytest.raises(RepositoryValidationError):
        repository.list_pages(query, owner_id="atlas-owner", project_id=None, limit=0)
    with pytest.raises(RepositoryValidationError):
        repository.list_pages(
            query, owner_id="atlas-owner", project_id=None, limit=MAX_PAGE_LIST + 1
        )
    cursor = AtlasPageCursor("atlas-owner", "other-project", 10, PAGE)
    with pytest.raises(RepositoryValidationError):
        repository.list_pages(query, owner_id="atlas-owner", project_id=None, cursor=cursor)
    owner_cursor = AtlasPageCursor("other-owner", None, 10, PAGE)
    with pytest.raises(RepositoryValidationError):
        repository.list_pages(query, owner_id="atlas-owner", project_id=None, cursor=owner_cursor)
    assert query.calls == []


def test_repository_requires_typed_commands_before_sql():
    repository = AtlasPageRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.create_page(query, command=object())
    with pytest.raises(RepositoryValidationError):
        repository.delete_page(query, command=object())
    assert query.calls == []


def test_live_atlas_page_requires_a_content_digest():
    with pytest.raises(RepositoryValidationError):
        AtlasPageRecord(
            "atlas-owner",
            PAGE,
            None,
            "kickoff",
            1,
            (),
            b"synthetic",
            None,
            10,
            10,
            None,
        )


def test_atlas_page_record_cannot_be_reconstructed_from_a_tombstone_with_content():
    with pytest.raises(RepositoryValidationError):
        AtlasPageRecord(
            "atlas-owner",
            PAGE,
            None,
            "kickoff",
            2,
            (),
            b"stale",
            hashlib.sha256(b"stale").hexdigest(),
            10,
            20,
            20,
        )


def receipt_row(**changes):
    values = dict(
        owner_id="atlas-owner",
        command_id=COMMAND,
        page_id=PAGE,
        command="create",
        request_digest="a" * 64,
        result_revision=1,
        created_at=10,
    )
    values.update(changes)
    return values


def test_page_and_receipt_optional_corruption_guards_are_exercised():
    with pytest.raises(RepositoryValidationError):
        create_command(sources=("bad\x00source",))
    with pytest.raises(RepositoryDataError):
        _page(page_row(content_sha256="not-a-digest"))
    with pytest.raises(RepositoryDataError):
        _page(
            page_row(
                revision=2,
                sources=["retained"],
                content=None,
                content_sha256=None,
                updated_at=20,
                deleted_at=20,
            )
        )
    with pytest.raises(RepositoryDataError):
        _page(
            page_row(
                revision=1,
                sources=[],
                content=None,
                content_sha256=None,
                updated_at=20,
                deleted_at=20,
            )
        )
    with pytest.raises(RepositoryDataError):
        _page(
            page_row(
                revision=2,
                sources=[],
                content=None,
                content_sha256=None,
                updated_at=20,
                deleted_at=20,
            )
            | {"content_sha256": "a" * 64}
        )
    decoded = _page(page_row(sources=json.dumps(["json-source"])))
    assert decoded is not None and decoded.sources == ("json-source",)


def test_change_and_batch_types_refuse_inconsistent_detached_values():
    page = _page(page_row())
    assert page is not None
    receipt = AtlasPageCommandReceipt("atlas-owner", COMMAND, PAGE, "create", "a" * 64, 1, 10)
    assert AtlasPageChange(page, receipt, False).page == page
    assert AtlasPageBatch((page,), None).records == (page,)
    with pytest.raises(RepositoryValidationError):
        AtlasPageChange(object(), receipt, False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageChange(page, object(), False)
    with pytest.raises(RepositoryValidationError):
        AtlasPageChange(page, receipt, "no")
    with pytest.raises(RepositoryValidationError):
        AtlasPageChange(
            page,
            AtlasPageCommandReceipt(
                "atlas-owner",
                COMMAND,
                "11111111-1111-4111-8111-111111111112",
                "create",
                "a" * 64,
                1,
                10,
            ),
            False,
        )
    with pytest.raises(RepositoryValidationError):
        AtlasPageBatch([page], None)
    with pytest.raises(RepositoryValidationError):
        AtlasPageBatch((page,), object())


def test_scripted_receipt_and_insert_failures_are_typed():
    repository = AtlasPageRepository()
    command = create_command(command_id=COMMAND)
    lock = {"acquired": None}
    with pytest.raises(RepositoryDataError, match="receipt has no page"):
        repository.create_page(
            ScriptedTransaction(
                one=[lock, receipt_row(request_digest=command.request_digest), None]
            ),
            command=command,
        )
    with pytest.raises(RepositoryDataError, match="returned no record"):
        repository.create_page(
            ScriptedTransaction(one=[lock, None, None, None, None]),
            command=command,
        )


def test_scripted_delete_receipt_and_update_failures_are_typed():
    repository = AtlasPageRepository()
    command = delete_command(command_id=COMMAND)
    lock = {"acquired": None}
    with pytest.raises(RepositoryConflictError, match="identity was reused"):
        repository.delete_page(
            ScriptedTransaction(
                one=[
                    lock,
                    receipt_row(command="create", request_digest=command.request_digest),
                ]
            ),
            command=command,
        )
    delete_receipt = receipt_row(
        command="delete", result_revision=2, request_digest=command.request_digest
    )
    with pytest.raises(RepositoryDataError, match="receipt has no page"):
        repository.delete_page(
            ScriptedTransaction(one=[lock, delete_receipt, None]),
            command=command,
        )
    with pytest.raises(RepositoryConflictError, match="revision changed"):
        repository.delete_page(
            ScriptedTransaction(one=[lock, None, page_row(), None]),
            command=command,
        )


def test_get_page_refuses_non_boolean_include_deleted():
    with pytest.raises(RepositoryValidationError):
        AtlasPageRepository().get_page(
            ScriptedTransaction(),
            owner_id="atlas-owner",
            page_id=PAGE,
            include_deleted="no",
        )


@pytest.mark.parametrize(
    "kind",
    ["create", "delete", "record-created", "record-updated", "record-deleted", "receipt", "cursor"],
)
def test_all_atlas_timestamp_types_refuse_above_safe_integer(kind):
    with pytest.raises(RepositoryValidationError):
        if kind == "create":
            create_command(created_at=2**53)
        elif kind == "delete":
            delete_command(deleted_at=2**53)
        elif kind.startswith("record-"):
            changes = {kind.removeprefix("record-") + "_at": 2**53, "updated_at": 2**53}
            if kind == "record-deleted":
                changes.update(content=None, content_sha256=None, sources=(), revision=2)
            AtlasPageRecord(**page_row(**changes))
        elif kind == "receipt":
            AtlasPageCommandReceipt("atlas-owner", COMMAND, PAGE, "create", "a" * 64, 1, 2**53)
        else:
            AtlasPageCursor("atlas-owner", None, 2**53, PAGE)
