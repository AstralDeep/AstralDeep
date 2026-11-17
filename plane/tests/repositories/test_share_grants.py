"""Tests for astralplane.repositories.share_grants: digest-bound immutable snapshot
creation without repr disclosure, owner-scoped listing, and atomic
digest/revocation/expiry recheck on open.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryDataError,
    RepositoryValidationError,
)
from astralplane.repositories.share_grants import (
    ShareGrantRepository,
    ShareGrantRevocationState,
)
from tests.repositories._support import Result, ScriptedTransaction

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
DIGEST = "a" * 64


def _grant_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "token_sha256": DIGEST,
        "user_id": "owner-1",
        "chat_id": "chat-1",
        "scope": "component",
        "component_id": "component-1",
        "snapshot_html": "<section>safe rendition</section>",
        "snapshot_json": {"component": {"type": "text", "value": "safe"}},
        "created_at": NOW,
        "expires_at": LATER,
        "revoked_at": None,
        "open_count": 0,
    }
    row.update(overrides)
    return row


def _metadata_row(**overrides: object) -> dict[str, object]:
    row = _grant_row(**overrides)
    for field in ("token_sha256", "snapshot_html", "snapshot_json"):
        row.pop(field)
    return row


def test_create_stores_digest_and_immutable_snapshot_without_repr_disclosure() -> None:
    transaction = ScriptedTransaction(one=[_grant_row()])

    record = ShareGrantRepository().create_grant(
        transaction,  # type: ignore[arg-type]
        token_sha256=DIGEST,
        owner_id="owner-1",
        chat_id="chat-1",
        scope="component",
        component_id="component-1",
        snapshot_html="<section>safe rendition</section>",
        snapshot_json={"component": {"value": "safe", "type": "text"}},
        expires_at=LATER,
    )

    assert record.share_id == 7
    assert record.snapshot_json["component"]["type"] == "text"
    assert DIGEST not in repr(record)
    assert "safe rendition" not in repr(record)
    assert "ON CONFLICT (token_sha256) DO NOTHING" in transaction.fetch_sql()
    parameters = transaction.calls[0][2]
    assert parameters[:6] == (  # type: ignore[index]
        DIGEST,
        "owner-1",
        "chat-1",
        "component",
        "component-1",
        "<section>safe rendition</section>",
    )
    assert parameters[6] == '{"component":{"type":"text","value":"safe"}}'  # type: ignore[index]


def test_create_accepts_exact_digest_replay_after_open() -> None:
    transaction = ScriptedTransaction(one=[None, _grant_row(open_count=3)])

    record = ShareGrantRepository().create_grant(
        transaction,  # type: ignore[arg-type]
        token_sha256=DIGEST,
        owner_id="owner-1",
        chat_id="chat-1",
        scope="component",
        component_id="component-1",
        snapshot_html="<section>safe rendition</section>",
        snapshot_json={"component": {"type": "text", "value": "safe"}},
        expires_at=LATER,
    )

    assert record.open_count == 3
    assert transaction.calls[1][2] == (DIGEST, "owner-1")


@pytest.mark.parametrize(
    "replay",
    [None, _grant_row(chat_id="other-chat")],
)
def test_create_rejects_other_owner_or_changed_snapshot_identity(
    replay: dict[str, object] | None,
) -> None:
    transaction = ScriptedTransaction(one=[None, replay])

    with pytest.raises(RepositoryConflictError):
        ShareGrantRepository().create_grant(
            transaction,  # type: ignore[arg-type]
            token_sha256=DIGEST,
            owner_id="owner-1",
            chat_id="chat-1",
            scope="component",
            component_id="component-1",
            snapshot_html="<section>safe rendition</section>",
            snapshot_json={"component": {"type": "text", "value": "safe"}},
            expires_at=LATER,
        )


def test_owner_list_returns_metadata_only_and_is_bounded() -> None:
    transaction = ScriptedTransaction(
        all_rows=[(_metadata_row(), _metadata_row(id=8, component_id=None, expires_at=None))]
    )

    records = ShareGrantRepository().list_grants(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        limit=2,
    )

    assert [record.share_id for record in records] == [7, 8]
    assert records[1].component_id is None and records[1].expires_at is None
    assert not hasattr(records[0], "snapshot_html")
    assert not hasattr(records[0], "token_sha256")
    assert transaction.calls[0][2] == ("owner-1", 2)
    assert "snapshot_html" not in transaction.calls[0][1]


@pytest.mark.parametrize(
    ("execute_rowcount", "existing", "expected"),
    [
        (1, None, ShareGrantRevocationState.REVOKED),
        (0, {"revoked_at": NOW}, ShareGrantRevocationState.ALREADY_REVOKED),
        (0, None, ShareGrantRevocationState.MISSING),
    ],
)
def test_revoke_is_owner_scoped_and_idempotent(
    execute_rowcount: int,
    existing: dict[str, object] | None,
    expected: ShareGrantRevocationState,
) -> None:
    transaction = ScriptedTransaction(
        execute=[Result(rowcount=execute_rowcount)],
        one=[] if execute_rowcount else [existing],
    )

    state = ShareGrantRepository().revoke_grant(
        transaction,  # type: ignore[arg-type]
        owner_id="owner-1",
        share_id=7,
        revoked_at=NOW,
    )

    assert state is expected
    assert transaction.calls[0][2] == (NOW, 7, "owner-1")
    assert "user_id = %s" in transaction.calls[0][1]


def test_public_resolve_uses_digest_and_uniform_active_predicate() -> None:
    transaction = ScriptedTransaction(one=[_grant_row(), None])
    repository = ShareGrantRepository()

    resolved = repository.resolve_active_by_digest(
        transaction,  # type: ignore[arg-type]
        token_sha256=DIGEST,
        as_of=NOW,
    )
    refused = repository.resolve_active_by_digest(
        transaction,  # type: ignore[arg-type]
        token_sha256=DIGEST,
        as_of=LATER,
    )

    assert resolved is not None and refused is None
    assert transaction.calls[0][2] == (DIGEST, NOW)
    assert "revoked_at IS NULL" in transaction.calls[0][1]
    assert "expires_at > %s" in transaction.calls[0][1]


def test_record_open_rechecks_digest_revocation_and_expiry_atomically() -> None:
    transaction = ScriptedTransaction(one=[_grant_row(open_count=1), None])
    repository = ShareGrantRepository()

    opened = repository.record_open(
        transaction,  # type: ignore[arg-type]
        share_id=7,
        token_sha256=DIGEST,
        as_of=NOW,
    )
    raced = repository.record_open(
        transaction,  # type: ignore[arg-type]
        share_id=7,
        token_sha256=DIGEST,
        as_of=NOW,
    )

    assert opened is not None and opened.open_count == 1
    assert raced is None
    assert transaction.calls[0][2] == (7, DIGEST, NOW)
    assert "token_sha256 = %s" in transaction.calls[0][1]
    assert "revoked_at IS NULL" in transaction.calls[0][1]


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("token_sha256", "A" * 64),
        ("owner_id", ""),
        ("chat_id", ""),
        ("scope", ""),
        ("component_id", ""),
        ("snapshot_html", 3),
        ("snapshot_json", object()),
        ("expires_at", datetime(2026, 8, 14)),
    ],
)
def test_create_rejects_invalid_inputs(argument: str, value: object) -> None:
    arguments: dict[str, object] = {
        "token_sha256": DIGEST,
        "owner_id": "owner-1",
        "chat_id": "chat-1",
        "scope": "component",
        "component_id": "component-1",
        "snapshot_html": "safe",
        "snapshot_json": {"component": {}},
        "expires_at": LATER,
    }
    arguments[argument] = value

    with pytest.raises(RepositoryValidationError):
        ShareGrantRepository().create_grant(  # type: ignore[arg-type]
            ScriptedTransaction(),  # type: ignore[arg-type]
            **arguments,
        )


@pytest.mark.parametrize(
    "row",
    [
        _grant_row(id=0),
        _grant_row(token_sha256="bad"),
        _grant_row(snapshot_json="not-json"),
        _grant_row(created_at=datetime(2026, 8, 14)),
        _grant_row(open_count=-1),
    ],
)
def test_corrupt_persisted_grant_fails_closed(row: dict[str, object]) -> None:
    transaction = ScriptedTransaction(one=[row])

    with pytest.raises(RepositoryDataError):
        ShareGrantRepository().resolve_active_by_digest(
            transaction,  # type: ignore[arg-type]
            token_sha256=DIGEST,
            as_of=NOW,
        )


@pytest.mark.parametrize(
    ("share_id", "as_of"),
    [(0, NOW), (7, datetime(2026, 8, 14))],
)
def test_open_rejects_invalid_identity_or_timestamp(
    share_id: int,
    as_of: datetime,
) -> None:
    with pytest.raises(RepositoryValidationError):
        ShareGrantRepository().record_open(
            ScriptedTransaction(),  # type: ignore[arg-type]
            share_id=share_id,
            token_sha256=DIGEST,
            as_of=as_of,
        )
