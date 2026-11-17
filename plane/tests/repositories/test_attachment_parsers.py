"""Tests for astralplane.repositories.attachment_parsers: pending-claim atomicity and
dedup, global coverage redaction, owner-scoped reads and lists, and CAS-fenced status
transitions.
"""

from __future__ import annotations

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)
from astralplane.repositories.attachment_parsers import (
    AttachmentParserClaimDisposition,
    AttachmentParserRepository,
    AttachmentParserStatus,
)
from tests.repositories._support import Result, ScriptedTransaction


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "parser-1",
        "extension": "avro",
        "category": "data",
        "gap_fingerprint": "gap-1",
        "status": "pending",
        "draft_agent_id": "draft-1",
        "live_agent_id": None,
        "tool_name": None,
        "source_attachment_id": "attachment-1",
        "source_chat_id": "chat-1",
        "requested_by": "owner-1",
        "approved_by": None,
        "created_at": 100,
        "updated_at": 100,
    }
    row.update(overrides)
    return row


def _claim(
    transaction: ScriptedTransaction,
    *,
    owner_id: str = "owner-1",
) -> object:
    return AttachmentParserRepository().claim_pending(
        transaction,  # type: ignore[arg-type]
        owner_id=owner_id,
        gap_fingerprint="gap-1",
        category="data",
        extension="avro",
        draft_agent_id="draft-1",
        source_attachment_id="attachment-1",
        source_conversation_id="chat-1",
        claimed_at=100,
    )


def test_claim_pending_atomically_inserts_or_reclaims_terminal_gap() -> None:
    transaction = ScriptedTransaction(execute=[Result(returned_records=(_row(),))])

    result = _claim(transaction)

    assert result.disposition is AttachmentParserClaimDisposition.CLAIMED
    assert result.owner_record is not None
    assert result.owner_record.status is AttachmentParserStatus.PENDING
    assert result.coverage == result.owner_record.coverage
    assert not result.coverage.covered
    assert "owner-1" not in repr(result.owner_record)
    assert "attachment-1" not in repr(result.owner_record)
    statement = transaction.calls[0][1]
    assert "ON CONFLICT (gap_fingerprint) DO UPDATE" in statement
    assert "status IN ('failed', 'discarded')" in statement
    parameters = transaction.calls[0][2]
    assert parameters[1:] == (  # type: ignore[index]
        "avro",
        "data",
        "gap-1",
        "draft-1",
        "attachment-1",
        "chat-1",
        "owner-1",
        100,
        100,
    )


def test_claim_pending_accepts_an_owner_replay_without_another_write() -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[_row()])

    result = _claim(transaction)

    assert result.disposition is AttachmentParserClaimDisposition.OWNER_REPLAY
    assert result.owner_record is not None
    assert result.owner_record.requested_by == "owner-1"


def test_claim_pending_deduplicates_foreign_gap_without_leaking_provenance() -> None:
    transaction = ScriptedTransaction(
        execute=[Result(rowcount=0)],
        one=[
            _row(
                requested_by="owner-2",
                draft_agent_id="private-draft",
                source_attachment_id="private-attachment",
                source_chat_id="private-chat",
            )
        ],
    )

    result = _claim(transaction)

    assert result.disposition is AttachmentParserClaimDisposition.GAP_ALREADY_CLAIMED
    assert result.owner_record is None
    rendered = repr(result)
    assert "owner-2" not in rendered
    assert "private-draft" not in rendered
    assert result.coverage.gap_fingerprint == "gap-1"


def test_claim_pending_fails_closed_on_impossible_result_shapes() -> None:
    with pytest.raises(RepositoryDataError, match="row count"):
        _claim(ScriptedTransaction(execute=[Result(rowcount=2)]))

    with pytest.raises(RepositoryConflictError, match="unique-row race"):
        _claim(ScriptedTransaction(execute=[Result(rowcount=0)], one=[None]))

    with pytest.raises(RepositoryDataError, match="another owner's"):
        _claim(
            ScriptedTransaction(
                execute=[Result(returned_records=(_row(requested_by="owner-2"),))]
            )
        )


def test_global_coverage_is_redacted_and_live_only_with_complete_tool_binding() -> None:
    transaction = ScriptedTransaction(
        one=[
            _row(
                status="live",
                live_agent_id="agent-1",
                tool_name="parse_avro",
                approved_by="admin-1",
                updated_at=200,
            )
        ]
    )

    coverage = AttachmentParserRepository().get_coverage(
        transaction,  # type: ignore[arg-type]
        gap_fingerprint="gap-1",
    )

    assert coverage is not None and coverage.covered
    assert coverage.tool_name == "parse_avro"
    assert not hasattr(coverage, "requested_by")
    assert transaction.calls[0][2] == ("gap-1",)
    assert "requested_by" not in transaction.calls[0][1]


def test_absent_coverage_is_explicit() -> None:
    transaction = ScriptedTransaction(one=[None])
    assert (
        AttachmentParserRepository().get_coverage(
            transaction,  # type: ignore[arg-type]
            gap_fingerprint="missing",
        )
        is None
    )


def test_live_coverage_rejects_incomplete_persisted_binding() -> None:
    transaction = ScriptedTransaction(one=[_row(status="live", live_agent_id="agent-1")])
    with pytest.raises(RepositoryDataError, match="incomplete coverage"):
        AttachmentParserRepository().get_coverage(
            transaction,  # type: ignore[arg-type]
            gap_fingerprint="gap-1",
        )


def test_owner_reads_bind_both_gap_or_draft_to_requester() -> None:
    repository = AttachmentParserRepository()
    transaction = ScriptedTransaction(one=[_row(), _row(), _row()])

    by_gap = repository.get_owner_claim_by_gap(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        gap_fingerprint="gap-1",
    )
    by_draft = repository.get_owner_claim_by_draft(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        draft_agent_id="draft-1",
    )
    administrative = repository.get_by_draft_for_administration(
        transaction,  # type: ignore[arg-type]
        draft_agent_id="draft-1",
    )

    assert by_gap is not None and by_draft is not None and administrative is not None
    assert transaction.calls[0][2] == ("gap-1", "owner-1")
    assert transaction.calls[1][2] == ("draft-1", "owner-1")
    assert transaction.calls[2][2] == ("draft-1",)
    assert "requested_by = %s" in transaction.calls[0][1]
    assert "requested_by = %s" in transaction.calls[1][1]


@pytest.mark.parametrize("method", ["gap", "draft", "admin"])
def test_absent_claim_reads_are_explicit(method: str) -> None:
    repository = AttachmentParserRepository()
    transaction = ScriptedTransaction(one=[None])
    if method == "gap":
        result = repository.get_owner_claim_by_gap(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            gap_fingerprint="gap-1",
        )
    elif method == "draft":
        result = repository.get_owner_claim_by_draft(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            draft_agent_id="draft-1",
        )
    else:
        result = repository.get_by_draft_for_administration(
            transaction,  # type: ignore[arg-type]
            draft_agent_id="draft-1",
        )
    assert result is None


def test_owner_read_rejects_a_driver_that_returns_foreign_provenance() -> None:
    transaction = ScriptedTransaction(one=[_row(requested_by="owner-2")])
    with pytest.raises(RepositoryDataError, match="foreign provenance"):
        AttachmentParserRepository().get_owner_claim_by_gap(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            gap_fingerprint="gap-1",
        )


def test_owner_and_administrative_lists_are_bounded_and_deterministic() -> None:
    repository = AttachmentParserRepository()
    transaction = ScriptedTransaction(all_rows=[(_row(),), (_row(),)])

    owned = repository.list_owner_claims(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        status=AttachmentParserStatus.PENDING,
        limit=7,
    )
    administrative = repository.list_by_status_for_administration(
        transaction,  # type: ignore[arg-type]
        status="pending",
        limit=8,
    )

    assert len(owned) == len(administrative) == 1
    assert transaction.calls[0][2] == ("owner-1", "pending", 7)
    assert transaction.calls[1][2] == ("pending", 8)
    assert all("ORDER BY created_at DESC, id ASC" in call[1] for call in transaction.calls)


@pytest.mark.parametrize("target", [AttachmentParserStatus.FAILED, "discarded"])
def test_owner_terminal_transition_uses_owner_status_and_timestamp_cas(
    target: AttachmentParserStatus | str,
) -> None:
    terminal = str(target)
    transaction = ScriptedTransaction(
        execute=[Result(returned_records=(_row(status=terminal, updated_at=101),))]
    )

    record = AttachmentParserRepository().mark_status_for_owner(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        gap_fingerprint="gap-1",
        expected_status=AttachmentParserStatus.PENDING,
        expected_updated_at=100,
        status=target,
        updated_at=101,
    )

    assert record.status.value == terminal
    assert transaction.calls[0][2] == (
        terminal,
        101,
        "gap-1",
        "owner-1",
        "pending",
        100,
    )
    assert "requested_by = %s" in transaction.calls[0][1]


@pytest.mark.parametrize(
    ("expected", "target"),
    [("live", "failed"), ("pending", "pending"), ("pending", "live")],
)
def test_owner_transition_rejects_unsupported_edges(expected: str, target: str) -> None:
    with pytest.raises(RepositoryValidationError, match="pending"):
        AttachmentParserRepository().mark_status_for_owner(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            gap_fingerprint="gap-1",
            expected_status=expected,
            expected_updated_at=100,
            status=target,
            updated_at=101,
        )


@pytest.mark.parametrize("existing", [_row(), None])
def test_owner_transition_distinguishes_stale_fence_from_missing_scope(
    existing: dict[str, object] | None,
) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])
    error = RepositoryConflictError if existing is not None else RepositoryNotFoundError
    with pytest.raises(error):
        AttachmentParserRepository().mark_status_for_owner(
            transaction,  # type: ignore[arg-type]
            owner_id="owner-1",
            gap_fingerprint="gap-1",
            expected_status="pending",
            expected_updated_at=100,
            status="failed",
            updated_at=101,
        )


def test_administrative_promotion_is_a_pending_timestamp_cas() -> None:
    live = _row(
        status="live",
        live_agent_id="agent-1",
        tool_name="parse_avro",
        approved_by="admin-1",
        updated_at=101,
    )
    transaction = ScriptedTransaction(execute=[Result(returned_records=(live,))])

    record = AttachmentParserRepository().mark_live_for_administration(
        transaction,  # type: ignore[arg-type]
        gap_fingerprint="gap-1",
        expected_status="pending",
        expected_updated_at=100,
        live_agent_id="agent-1",
        tool_name="parse_avro",
        approved_by="admin-1",
        updated_at=101,
    )

    assert record.coverage.covered
    assert transaction.calls[0][2] == (
        "agent-1",
        "parse_avro",
        "admin-1",
        101,
        "gap-1",
        "pending",
        100,
    )


def test_administrative_promotion_rejects_nonpending_or_nonadvancing_fence() -> None:
    repository = AttachmentParserRepository()
    with pytest.raises(RepositoryValidationError, match="pending"):
        repository.mark_live_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            gap_fingerprint="gap-1",
            expected_status="failed",
            expected_updated_at=100,
            live_agent_id="agent-1",
            tool_name="parse_avro",
            approved_by="admin-1",
            updated_at=101,
        )
    with pytest.raises(RepositoryValidationError, match="advance"):
        repository.mark_live_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            gap_fingerprint="gap-1",
            expected_status="pending",
            expected_updated_at=100,
            live_agent_id="agent-1",
            tool_name="parse_avro",
            approved_by="admin-1",
            updated_at=100,
        )


@pytest.mark.parametrize("existing", [_row(), None])
def test_administrative_promotion_distinguishes_stale_from_missing(
    existing: dict[str, object] | None,
) -> None:
    transaction = ScriptedTransaction(execute=[Result(rowcount=0)], one=[existing])
    error = RepositoryConflictError if existing is not None else RepositoryNotFoundError
    with pytest.raises(error):
        AttachmentParserRepository().mark_live_for_administration(
            transaction,  # type: ignore[arg-type]
            gap_fingerprint="gap-1",
            expected_status="pending",
            expected_updated_at=100,
            live_agent_id="agent-1",
            tool_name="parse_avro",
            approved_by="admin-1",
            updated_at=101,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"owner_id": ""},
        {"gap_fingerprint": ""},
        {"category": ""},
        {"extension": 7},
        {"draft_agent_id": ""},
        {"source_attachment_id": ""},
        {"source_conversation_id": ""},
        {"claimed_at": -1},
    ],
)
def test_claim_validation_is_bounded(overrides: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "owner_id": "owner-1",
        "gap_fingerprint": "gap-1",
        "category": "data",
        "extension": "avro",
        "draft_agent_id": "draft-1",
        "source_attachment_id": "attachment-1",
        "source_conversation_id": "chat-1",
        "claimed_at": 100,
    }
    arguments.update(overrides)
    with pytest.raises(RepositoryValidationError):
        AttachmentParserRepository().claim_pending(
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "unknown"},
        {"created_at": -1},
        {"updated_at": 99},
        {"id": ""},
        {"extension": 7},
        {"requested_by": ""},
    ],
)
def test_persisted_parser_validation_fails_closed(overrides: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[_row(**overrides)])
    with pytest.raises(RepositoryDataError):
        AttachmentParserRepository().get_by_draft_for_administration(
            transaction,  # type: ignore[arg-type]
            draft_agent_id="draft-1",
        )


def test_unknown_status_and_out_of_range_list_are_rejected() -> None:
    repository = AttachmentParserRepository()
    with pytest.raises(RepositoryValidationError, match="unsupported"):
        repository.list_owner_claims(
            ScriptedTransaction(),  # type: ignore[arg-type]
            owner_id="owner-1",
            status="unknown",
        )
    with pytest.raises(RepositoryValidationError, match="range"):
        repository.list_by_status_for_administration(
            ScriptedTransaction(),  # type: ignore[arg-type]
            status="pending",
            limit=1001,
        )
