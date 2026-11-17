"""Checks observation capture, head, page, purge, and cursor bounds without PostgreSQL.
Real database behavior is covered by test_observations_postgres.py in the same directory.
"""

from __future__ import annotations

import hashlib

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.observations import (
    MAX_CONVERSATION_OCTETS,
    MAX_OBSERVATION_LIST,
    MAX_OBSERVATION_OCTETS,
    MAX_PAGE_OCTETS,
    MAX_RETENTION_SECONDS,
    MAX_SOURCE_REFS,
    ObservationBatch,
    ObservationCapture,
    ObservationCaptureCommand,
    ObservationCursor,
    ObservationHead,
    ObservationPage,
    ObservationPurge,
    ObservationRepository,
    ObservationUsage,
    _head,
)
from tests.repositories._support import ScriptedTransaction

OBSERVATION = "70000000-0000-4000-8000-000000000001"


def capture_command(**changes):
    values = dict(
        owner_id="observation-owner",
        conversation_id="conversation-1",
        observation_id=OBSERVATION,
        operation_id="operation-1",
        source_refs=("synthetic/source-one",),
        seal="synthetic-seal-v1",
        content=b"synthetic-opaque-ciphertext",
        created_at=1000,
        expires_at=2000,
    )
    values.update(changes)
    return ObservationCaptureCommand(**values)


def head_row(**changes):
    content = b"synthetic-opaque-ciphertext"
    values = dict(
        owner_id="observation-owner",
        conversation_id="conversation-1",
        observation_id=OBSERVATION,
        capture_seq=1,
        operation_id="operation-1",
        source_refs=["synthetic/source-one"],
        seal="synthetic-seal-v1",
        page_count=1,
        observation_octets=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        capture_digest="a" * 64,
        created_at=1000,
        expires_at=2000,
        purged_at=None,
        purged_reason=None,
    )
    values.update(changes)
    return values


def tombstone_row(**changes):
    values = dict(
        owner_id="observation-owner",
        conversation_id="conversation-1",
        observation_id=OBSERVATION,
        capture_seq=1,
        operation_id="operation-1",
        source_refs=[],
        seal=None,
        page_count=0,
        observation_octets=0,
        content_sha256=None,
        capture_digest="a" * 64,
        created_at=1000,
        expires_at=2000,
        purged_at=3000,
        purged_reason="deleted",
    )
    values.update(changes)
    return values


def test_capture_command_digest_binds_identity_metadata_and_ciphertext():
    original = capture_command()
    assert original.content_sha256 == hashlib.sha256(original.content).hexdigest()
    assert original.request_digest == capture_command().request_digest
    assert original.request_digest != capture_command(content=b"different").request_digest
    assert original.request_digest != capture_command(operation_id="operation-2").request_digest
    assert original.request_digest != capture_command(seal="seal-v2").request_digest
    assert original.request_digest != capture_command(source_refs=()).request_digest
    assert original.request_digest != capture_command(expires_at=3000).request_digest


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": ""},
        {"owner_id": " padded"},
        {"conversation_id": ""},
        {"observation_id": "not-a-uuid"},
        {"operation_id": ""},
        {"source_refs": "one"},
        {"source_refs": ("",)},
        {"source_refs": ("same", "same")},
        {"source_refs": tuple(str(index) for index in range(MAX_SOURCE_REFS + 1))},
        {"seal": ""},
        {"seal": "x" * 513},
        {"content": bytearray(b"mutable")},
        {"content": b""},
        {"content": b"x" * (MAX_OBSERVATION_OCTETS + 1)},
        {"created_at": True},
        {"created_at": -1},
        {"expires_at": 1000},
        {"expires_at": 1000 + MAX_RETENTION_SECONDS + 1},
        {"expires_at": "later"},
    ],
)
def test_capture_command_rejects_invalid_bounds_before_sql(changes):
    with pytest.raises(RepositoryValidationError):
        capture_command(**changes)


def test_capture_command_accepts_the_exact_retention_ceiling():
    command = capture_command(expires_at=1000 + MAX_RETENTION_SECONDS)
    assert command.expires_at == 1000 + MAX_RETENTION_SECONDS


def test_head_accepts_live_and_tombstone_shapes_and_rejects_inconsistent_ones():
    live = _head(head_row())
    assert live is not None and not live.purged
    assert live.source_refs == ("synthetic/source-one",)
    tombstone = _head(tombstone_row())
    assert tombstone is not None and tombstone.purged and tombstone.seal is None
    for changes in (
        {"content_sha256": None},
        {"seal": None},
        {"page_count": 0},
        {"observation_octets": 0},
        {"purged_reason": "deleted"},
    ):
        with pytest.raises((RepositoryValidationError, RepositoryDataError)):
            _head(head_row(**changes))
    for changes in (
        {"page_count": 1},
        {"observation_octets": 27},
        {"content_sha256": "a" * 64},
        {"seal": "synthetic-seal-v1"},
        {"source_refs": ["synthetic/source-one"]},
        {"purged_reason": None},
        {"purged_reason": "unknown"},
        {"purged_at": 999},
    ):
        with pytest.raises((RepositoryValidationError, RepositoryDataError)):
            _head(tombstone_row(**changes))


def test_corrupt_stored_head_is_a_data_error():
    with pytest.raises(RepositoryDataError):
        _head({"owner_id": "observation-owner"})
    with pytest.raises(RepositoryDataError):
        _head(head_row(observation_id="not-a-uuid"))


def test_head_parses_string_source_refs_and_rejects_broken_json():
    assert _head(head_row(source_refs="[\"synthetic/source-one\"]")).source_refs == (
        "synthetic/source-one",
    )
    with pytest.raises(RepositoryDataError):
        _head(head_row(source_refs="{not json"))


def test_head_rejects_an_inconsistent_live_retention_window():
    with pytest.raises(RepositoryValidationError):
        ObservationHead(
            "observation-owner",
            "conversation-1",
            OBSERVATION,
            1,
            "operation-1",
            (),
            "seal",
            1,
            1,
            "a" * 64,
            "b" * 64,
            1000,
            1000 + MAX_RETENTION_SECONDS + 1,
        )


def test_head_rejects_a_malformed_digest():
    with pytest.raises(RepositoryValidationError):
        ObservationHead(
            "observation-owner",
            "conversation-1",
            OBSERVATION,
            1,
            "operation-1",
            (),
            "seal",
            1,
            1,
            "not-a-digest",
            "b" * 64,
            1000,
            2000,
        )


def test_text_fields_reject_control_and_surrogate_characters():
    with pytest.raises(RepositoryValidationError):
        capture_command(operation_id="bad\x00identity")
    with pytest.raises(RepositoryValidationError):
        capture_command(seal="bad" + chr(0xD800) + "seal")


def test_repository_preflights_reads_cursors_and_purge_before_querying():
    repository = ObservationRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.get_observation(
            query, owner_id="", conversation_id="c", observation_id=OBSERVATION
        )
    with pytest.raises(RepositoryValidationError):
        repository.get_observation(
            query, owner_id="observation-owner", conversation_id="c", observation_id="bad"
        )
    with pytest.raises(RepositoryValidationError):
        repository.read_page(
            query,
            owner_id="observation-owner",
            conversation_id="c",
            observation_id=OBSERVATION,
            page_no=0,
            now=5000,
        )
    with pytest.raises(RepositoryValidationError):
        repository.read_page(
            query,
            owner_id="observation-owner",
            conversation_id="c",
            observation_id=OBSERVATION,
            page_no=514,
            now=5000,
        )
    with pytest.raises(RepositoryValidationError):
        repository.list_observations(
            query, owner_id="observation-owner", conversation_id="c", now=5000, limit=0
        )
    cursor = ObservationCursor("other-owner", "c", 1)
    with pytest.raises(RepositoryValidationError):
        repository.list_observations(
            query,
            owner_id="observation-owner",
            conversation_id="c",
            now=5000,
            cursor=cursor,
        )
    with pytest.raises(RepositoryValidationError):
        repository.purge_observation(
            query,
            owner_id="observation-owner",
            conversation_id="c",
            observation_id=OBSERVATION,
            reason="unknown",
            at=3000,
        )
    with pytest.raises(RepositoryValidationError):
        repository.conversation_usage(
            query, owner_id="observation-owner", conversation_id="c", now="late"
        )
    assert query.calls == []


def test_capture_requires_a_typed_command_before_sql():
    repository = ObservationRepository()
    query = ScriptedTransaction()
    with pytest.raises(RepositoryValidationError):
        repository.capture(query, command=object())
    assert query.calls == []


def test_typed_results_and_batches_validate_their_inputs():
    repository = ObservationRepository()
    head = _head(head_row())
    tombstone = _head(tombstone_row())
    with pytest.raises(RepositoryValidationError):
        ObservationCapture(object(), False)
    assert ObservationCapture(head, False).replayed is False
    with pytest.raises(RepositoryValidationError):
        ObservationCapture(head, "no")
    with pytest.raises(RepositoryValidationError):
        ObservationPurge(head, False)
    with pytest.raises(RepositoryValidationError):
        ObservationPurge(tombstone, "no")
    assert ObservationPurge(tombstone, False).replayed is False
    with pytest.raises(RepositoryValidationError):
        ObservationBatch((head, object()), None)
    with pytest.raises(RepositoryValidationError):
        ObservationBatch((head,), object())
    assert ObservationBatch((head,), None).records == (head,)
    with pytest.raises(RepositoryValidationError):
        repository.purge_expired(
            ScriptedTransaction(),
            owner_id="observation-owner",
            conversation_id="c",
            now=5000,
            limit=0,
        )
    with pytest.raises(RepositoryValidationError):
        repository.purge_expired(
            ScriptedTransaction(),
            owner_id="observation-owner",
            conversation_id="c",
            now=5000,
            limit=MAX_OBSERVATION_LIST + 1,
        )


def test_page_record_binds_extent_and_continuation():
    def page(**changes):
        values = dict(
            owner_id="observation-owner",
            conversation_id="conversation-1",
            observation_id=OBSERVATION,
            capture_seq=1,
            content_sha256="a" * 64,
            page_no=1,
            content=b"synthetic-opaque-ciphertext",
            observation_octets=27,
            page_count=1,
            continuation=False,
        )
        values.update(changes)
        return ObservationPage(**values)

    assert page().content == b"synthetic-opaque-ciphertext"
    with pytest.raises(RepositoryValidationError):
        page(page_no=2, page_count=1)
    with pytest.raises(RepositoryValidationError):
        page(page_no=1, page_count=2, continuation=False)
    with pytest.raises(RepositoryValidationError):
        page(page_no=2, page_count=2, continuation=True)
    with pytest.raises(RepositoryValidationError):
        page(content=b"")
    with pytest.raises(RepositoryValidationError):
        page(content=b"x" * (MAX_PAGE_OCTETS + 1), observation_octets=MAX_PAGE_OCTETS + 1)


def test_usage_and_purge_results_validate_their_scopes():
    usage = ObservationUsage("observation-owner", "conversation-1", 3, 900, 1)
    assert usage.capacity_octets == MAX_CONVERSATION_OCTETS
    with pytest.raises(RepositoryValidationError):
        ObservationUsage("observation-owner", "conversation-1", 3, MAX_CONVERSATION_OCTETS + 1, 0)
    with pytest.raises(RepositoryValidationError):
        ObservationHead(
            "observation-owner",
            "conversation-1",
            OBSERVATION,
            1,
            "operation-1",
            (),
            "seal",
            1,
            1,
            "a" * 64,
            "b" * 64,
            1000,
            2000,
            3000,
            "deleted",
        )


def test_capture_insert_returning_no_record_is_a_data_error():
    repository = ObservationRepository()
    query = ScriptedTransaction(
        one=[{"pg_advisory_xact_lock": None}, None, {"retained": 0}, {"next": 1}, None],
    )
    with pytest.raises(RepositoryDataError, match="insert returned no record"):
        repository.capture(query, command=capture_command())


def test_verify_observation_fails_closed_on_page_gaps_and_digest_drift():
    repository = ObservationRepository()
    content = b"synthetic-opaque-ciphertext"
    page_row = {
        "page_no": 1,
        "content": content,
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }
    drift = head_row(observation_octets=1)
    query = ScriptedTransaction(
        one=[dict(drift)],
        all_rows=[(page_row,)],
    )
    with pytest.raises(RepositoryDataError, match="extent is inconsistent"):
        repository.verify_observation(
            query,
            owner_id="observation-owner",
            conversation_id="conversation-1",
            observation_id=OBSERVATION,
            now=5000,
        )
    gap = head_row(page_count=2)
    query = ScriptedTransaction(
        one=[dict(gap)],
        all_rows=[(page_row, dict(page_row, page_no=3))],
    )
    with pytest.raises(RepositoryDataError, match="pages are incomplete"):
        repository.verify_observation(
            query,
            owner_id="observation-owner",
            conversation_id="conversation-1",
            observation_id=OBSERVATION,
            now=5000,
        )
    mismatched = head_row(content_sha256="c" * 64)
    query = ScriptedTransaction(
        one=[dict(mismatched)],
        all_rows=[(page_row,)],
    )
    with pytest.raises(RepositoryDataError, match="observation failed its integrity check"):
        repository.verify_observation(
            query,
            owner_id="observation-owner",
            conversation_id="conversation-1",
            observation_id=OBSERVATION,
            now=5000,
        )


def test_purge_update_losing_its_fence_row_is_a_conflict():
    repository = ObservationRepository()
    query = ScriptedTransaction(
        one=[
            {"pg_advisory_xact_lock": None},
            head_row(),
            None,
        ],
    )
    with pytest.raises(RepositoryConflictError, match="state changed"):
        repository.purge_observation(
            query,
            owner_id="observation-owner",
            conversation_id="conversation-1",
            observation_id=OBSERVATION,
            reason="deleted",
            at=3000,
        )


def test_page_record_rejects_non_bytes_content():
    with pytest.raises(RepositoryValidationError):
        ObservationPage(
            "observation-owner",
            "conversation-1",
            OBSERVATION,
            1,
            "a" * 64,
            1,
            None,
            27,
            1,
            False,
        )
    with pytest.raises(RepositoryValidationError):
        ObservationPage(
            "observation-owner",
            "conversation-1",
            OBSERVATION,
            1,
            "a" * 64,
            3,
            b"x",
            27,
            2,
            False,
        )
