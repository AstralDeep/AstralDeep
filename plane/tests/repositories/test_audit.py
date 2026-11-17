"""Tests for astralplane.repositories.audit: canonical-JSON hash-chain construction,
genesis/continuation append, tamper detection, owner-scoped trajectory queries, and
conversation range reads with chain-segment proofs against real PostgreSQL.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from _support import ScriptedTransaction

from astralplane.database.transaction import PlaneDatabase
from astralplane.errors import PlaneError
from astralplane.repositories.audit import (
    AUDIT_CONVERSATION_READ_MAX_EVENTS,
    GENESIS_DIGEST,
    AuditCursor,
    AuditEvent,
    AuditPage,
    AuditRecord,
    AuditRepository,
    AuditSegmentProof,
    AuditSegmentRun,
    ToolTrajectoryEvent,
    canonical_event_bytes,
    canonical_json,
    verify_records,
    verify_segment_proof,
)
from tests.fixtures.migrated_template import MigratedDatabase

NOW = datetime(2026, 8, 13, 20, tzinfo=UTC)
KEY = b"k" * 32


@pytest.fixture
def audit_postgres_database(migrated_clone: MigratedDatabase) -> PlaneDatabase:
    return migrated_clone.database


def authenticate(key_id: str, payload: bytes) -> bytes:
    assert key_id == "audit-key-1"
    return hmac.new(KEY, payload, hashlib.sha256).digest()


def event(**overrides: object) -> AuditEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "chain_id": "owner-1",
        "auth_principal": "principal-1",
        "agent_id": "agent-1",
        "event_class": "tool_call",
        "action_type": "tool.execute",
        "description": "A bounded event",
        "conversation_id": "chat-1",
        "correlation_id": "correlation-1",
        "outcome": "success",
        "outcome_detail": None,
        "inputs_json": '{"b":2,"a":1}',
        "outputs_json": "{}",
        "artifact_pointers_json": "[]",
        "started_at": NOW,
        "completed_at": NOW + timedelta(seconds=1),
        "key_id": "audit-key-1",
        "schema_version": 2,
    }
    values.update(overrides)
    return AuditEvent(**values)  # type: ignore[arg-type]


def event_row(
    value: AuditEvent | None = None,
    *,
    sequence: int = 1,
    previous: bytes = GENESIS_DIGEST,
    entry: bytes | None = None,
    **overrides: object,
) -> dict[str, object]:
    value = value or event()
    digest = entry or authenticate(value.key_id, previous + canonical_event_bytes(value, sequence))
    row: dict[str, object] = {
        "event_id": value.event_id,
        "actor_user_id": value.chain_id,
        "auth_principal": value.auth_principal,
        "agent_id": value.agent_id,
        "event_class": value.event_class,
        "action_type": value.action_type,
        "description": value.description,
        "conversation_id": value.conversation_id,
        "correlation_id": value.correlation_id,
        "outcome": value.outcome,
        "outcome_detail": value.outcome_detail,
        "inputs_meta": {"a": 1, "b": 2},
        "outputs_meta": {},
        "artifact_pointers": [],
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "key_id": value.key_id,
        "schema_version": value.schema_version,
        "chain_sequence": sequence,
        "recorded_at": NOW + timedelta(seconds=2),
        "prev_hash": previous,
        "entry_hash": digest,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": ""},
        {"outcome": "unknown"},
        {"schema_version": 0},
        {"schema_version": 3},
        {"started_at": NOW.replace(tzinfo=None)},
        {"completed_at": NOW - timedelta(seconds=1)},
        {"inputs_json": "[]"},
        {"artifact_pointers_json": "{}"},
    ],
)
def test_event_validation(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        event(**changes)


def test_canonical_json_rejects_invalid_and_sorts_keys() -> None:
    assert canonical_json('{"b":2,"a":1}', expected_type=dict) == '{"a":1,"b":2}'
    assert canonical_json([2, 1], expected_type=list) == "[2,1]"
    with pytest.raises(ValueError):
        canonical_json("not-json", expected_type=dict)


def test_canonical_json_normalizes_detached_nested_containers() -> None:
    detached_object = MappingProxyType(
        {
            "z": (
                MappingProxyType(
                    {
                        "items": [2, MappingProxyType({"nested": (3, 4)})],
                    }
                ),
            ),
            "a": MappingProxyType({"enabled": True}),
        }
    )
    detached_list = (
        MappingProxyType({"b": (2, 1)}),
        [MappingProxyType({"a": None})],
    )

    assert canonical_json(detached_object, expected_type=dict) == (
        '{"a":{"enabled":true},"z":[{"items":[2,{"nested":[3,4]}]}]}'
    )
    assert canonical_json(detached_list, expected_type=list) == (
        '[{"b":[2,1]},[{"a":null}]]'
    )


@pytest.mark.parametrize(
    ("value", "expected_type"),
    [
        (MappingProxyType({"unsupported": frozenset({"value"})}), dict),
        ({1: "non-string-key"}, dict),
        ([b"bytes"], list),
        ([float("nan")], list),
        (MappingProxyType({"object": True}), list),
    ],
)
def test_canonical_json_rejects_unsupported_detached_values(
    value: object, expected_type: type
) -> None:
    with pytest.raises(ValueError):
        canonical_json(value, expected_type=expected_type)  # type: ignore[arg-type]


def test_real_postgresql_append_normalizes_detached_json(
    audit_postgres_database: PlaneDatabase,
) -> None:
    value = event(
        event_id=str(uuid.uuid4()),
        correlation_id=str(uuid.uuid4()),
        inputs_json='{"outer":{"items":[{"b":2,"a":1},[3,4]]}}',
        outputs_json='{"summary":{"ok":true}}',
        artifact_pointers_json='[{"meta":{"segments":[1,2]},"kind":"file"}]',
    )
    repository = AuditRepository()

    with audit_postgres_database.transaction() as transaction:
        appended = repository.append(transaction, value, authenticate)
    with audit_postgres_database.transaction() as transaction:
        reloaded = repository.get(
            transaction,
            chain_id=value.chain_id,
            event_id=value.event_id,
        )

    assert appended.event.inputs_json == '{"outer":{"items":[{"a":1,"b":2},[3,4]]}}'
    assert appended.event.outputs_json == '{"summary":{"ok":true}}'
    assert appended.event.artifact_pointers_json == (
        '[{"kind":"file","meta":{"segments":[1,2]}}]'
    )
    assert reloaded == appended


def test_schema_v1_canonical_bytes_preserve_legacy_chain_format() -> None:
    value = event(
        schema_version=1,
        description="caf\u00e9",
        inputs_json='{"label":"caf\u00e9"}',
        started_at=datetime(2026, 8, 13, 16, tzinfo=UTC),
        completed_at=None,
    )

    canonical = canonical_event_bytes(value, 47)

    assert b'"chain_sequence"' not in canonical
    assert b"caf\\u00e9" in canonical
    assert b"2026-08-13T16:00:00+00:00" in canonical
    assert b"2026-08-13T16:00:00Z" not in canonical


def test_schema_v2_canonical_bytes_bind_sequence_and_utf8() -> None:
    value = event(description="caf\u00e9", inputs_json='{"label":"caf\u00e9"}')

    canonical = canonical_event_bytes(value, 47)

    assert b'"chain_sequence":47' in canonical
    assert "caf\u00e9".encode() in canonical
    assert b"2026-08-13T20:00:00Z" in canonical


def test_append_genesis_uses_lock_and_returns_detached_record() -> None:
    value = event()
    row = event_row(value)
    transaction = ScriptedTransaction(one=[{"locked": True}, None, None, row])

    result = AuditRepository().append(transaction, value, authenticate)

    assert result.sequence == 1
    assert result.previous_digest == GENESIS_DIGEST
    assert result.entry_digest == row["entry_hash"]
    assert "pg_advisory_xact_lock" in transaction.calls[0][1]
    assert transaction.calls[-1][2][1] == "owner-1"  # type: ignore[index]


def test_append_continues_chain_and_is_idempotent() -> None:
    value = event(event_id="event-2")
    previous = b"p" * 32
    row = event_row(value, sequence=2, previous=previous)
    transaction = ScriptedTransaction(
        one=[{"locked": True}, None, {"chain_sequence": 1, "entry_hash": previous}, row]
    )
    result = AuditRepository().append(transaction, value, authenticate)
    assert result.sequence == 2
    assert result.previous_digest == previous

    replay = ScriptedTransaction(one=[{"locked": True}, row])
    assert AuditRepository().append(replay, value, authenticate) == result


def test_append_conflict_and_inconsistent_return_are_visible() -> None:
    value = event()
    conflicting = event_row(value, entry=b"x" * 32)
    with pytest.raises(PlaneError) as raised:
        AuditRepository().append(
            ScriptedTransaction(one=[{"locked": True}, conflicting]), value, authenticate
        )
    assert raised.value.code == "audit_idempotency_conflict"

    with pytest.raises(PlaneError) as raised:
        AuditRepository().append(
            ScriptedTransaction(one=[{"locked": True}, None, None, None]),
            value,
            authenticate,
        )
    assert raised.value.code == "audit_append_failed"

    wrong = event_row(value, previous=b"z" * 32)
    with pytest.raises(PlaneError) as raised:
        AuditRepository().append(
            ScriptedTransaction(one=[{"locked": True}, None, None, wrong]),
            value,
            authenticate,
        )
    assert raised.value.code == "audit_append_inconsistent"


def test_owner_scoped_reads_and_limits() -> None:
    repository = AuditRepository()
    record = repository.get(
        ScriptedTransaction(one=[event_row()]), chain_id="owner-1", event_id="event-1"
    )
    assert record is not None
    assert (
        repository.get(ScriptedTransaction(one=[None]), chain_id="other", event_id="event-1")
        is None
    )
    assert repository.list_for_chain(
        ScriptedTransaction(all_rows=[(event_row(),)]), chain_id="owner-1", limit=1
    ) == (record,)
    assert repository.load_chain(
        ScriptedTransaction(all_rows=[(event_row(),)]), chain_id="owner-1"
    ) == (record,)
    with pytest.raises(ValueError):
        repository.list_for_chain(ScriptedTransaction(), chain_id="owner-1", after_sequence=-1)
    with pytest.raises(ValueError):
        repository.list_for_chain(ScriptedTransaction(), chain_id="owner-1", limit=0)
    with pytest.raises(ValueError):
        repository.load_chain(ScriptedTransaction(), chain_id="owner-1", start_sequence=0)


def test_descending_owner_page_supports_typed_filters_and_cursor() -> None:
    first = event_row(recorded_at=NOW + timedelta(seconds=4))
    second = event_row(
        event(event_id="event-2", outcome="failure"),
        recorded_at=NOW + timedelta(seconds=3),
    )
    third = event_row(
        event(event_id="event-3"),
        recorded_at=NOW + timedelta(seconds=2),
    )
    transaction = ScriptedTransaction(all_rows=[(first, second, third)])
    cursor = AuditCursor(recorded_at=NOW + timedelta(seconds=5), event_id="event-9")

    page = AuditRepository().list_page(
        transaction,
        owner_id="owner-1",
        event_classes=("tool_call",),
        outcomes=("failure",),
        from_ts=NOW,
        to_ts=NOW + timedelta(hours=1),
        keyword=r"100%_safe\\path",
        cursor=cursor,
        limit=2,
    )

    assert isinstance(page, AuditPage)
    assert [record.event.event_id for record in page.records] == ["event-1", "event-2"]
    assert page.next_cursor == AuditCursor(
        recorded_at=NOW + timedelta(seconds=3), event_id="event-2"
    )
    statement = transaction.calls[0][1]
    parameters = transaction.calls[0][2]
    assert "actor_user_id = %s" in statement
    assert "recorded_at >= %s" in statement and "recorded_at < %s" in statement
    assert "ORDER BY recorded_at DESC, event_id DESC" in statement
    assert "ESCAPE E'\\\\'" in statement
    assert parameters[-3:] == (
        "%100\\%\\_safe\\\\\\\\path%",
        "%100\\%\\_safe\\\\\\\\path%",
        3,
    )


def test_descending_owner_page_empty_and_validation() -> None:
    repository = AuditRepository()
    assert repository.list_page(
        ScriptedTransaction(all_rows=[()]), owner_id="owner-1"
    ) == AuditPage(records=(), next_cursor=None)
    invalid_calls = (
        {"owner_id": "", "limit": 1},
        {"owner_id": "owner-1", "limit": 0},
        {"owner_id": "owner-1", "event_classes": ("",)},
        {"owner_id": "owner-1", "outcomes": ("other",)},
        {"owner_id": "owner-1", "from_ts": NOW.replace(tzinfo=None)},
        {
            "owner_id": "owner-1",
            "from_ts": NOW + timedelta(seconds=1),
            "to_ts": NOW,
        },
        {"owner_id": "owner-1", "keyword": " "},
        {"owner_id": "owner-1", "conversation_id": ""},
        {"owner_id": "owner-1", "conversation_id": " "},
        {"owner_id": "owner-1", "conversation_id": "c" * 257},
        {"owner_id": "owner-1", "conversation_id": "chat\x00-1"},
        {"owner_id": "owner-1", "conversation_id": 7},
        {"owner_id": "owner-1", "cursor": "encoded"},
    )
    for arguments in invalid_calls:
        with pytest.raises(ValueError):
            repository.list_page(ScriptedTransaction(), **arguments)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        AuditCursor(recorded_at=NOW.replace(tzinfo=None), event_id="event-1")
    with pytest.raises(ValueError):
        AuditCursor(recorded_at=NOW, event_id="")


def test_conversation_page_binds_an_exact_owner_scoped_filter_before_the_cursor() -> None:
    transaction = ScriptedTransaction(all_rows=[(event_row(),)])
    cursor = AuditCursor(recorded_at=NOW + timedelta(seconds=5), event_id="event-9")

    page = AuditRepository().list_page(
        transaction,
        owner_id="owner-1",
        conversation_id="c" * 256,
        cursor=cursor,
        limit=1,
    )

    assert [record.event.event_id for record in page.records] == ["event-1"]
    assert page.next_cursor is None
    statement = transaction.calls[0][1]
    assert "actor_user_id = %s AND conversation_id = %s AND (recorded_at, event_id) < (%s, %s)" in (
        statement
    )
    assert "LIKE" not in statement
    assert transaction.calls[0][2] == (
        "owner-1",
        "c" * 256,
        NOW + timedelta(seconds=5),
        "event-9",
        2,
    )


def test_real_postgresql_conversation_page_is_owner_isolated_and_keeps_its_filter(
    audit_postgres_database: PlaneDatabase,
) -> None:
    repository = AuditRepository()
    seeded = []
    with audit_postgres_database.transaction() as transaction:
        for number, (owner, conversation) in enumerate(
            (
                ("owner-1", "chat-a"),
                ("owner-1", "chat-b"),
                ("owner-1", "chat-a"),
                ("owner-1", None),
                ("owner-2", "chat-a"),
                ("owner-1", "chat-a"),
                ("owner-1", "CHAT-A"),
            )
        ):
            value = event(
                event_id=str(uuid.uuid4()),
                chain_id=owner,
                correlation_id=str(uuid.uuid4()),
                conversation_id=conversation,
                started_at=NOW + timedelta(seconds=number),
                completed_at=NOW + timedelta(seconds=number),
            )
            repository.append(transaction, value, authenticate)
            seeded.append(value)

    expected = [value.event_id for value in reversed(seeded) if value.chain_id == "owner-1"
                and value.conversation_id == "chat-a"]
    with audit_postgres_database.transaction() as transaction:
        first = repository.list_page(
            transaction, owner_id="owner-1", conversation_id="chat-a", limit=2
        )
        assert first.next_cursor is not None
        second = repository.list_page(
            transaction,
            owner_id="owner-1",
            conversation_id="chat-a",
            cursor=first.next_cursor,
            limit=2,
        )
        foreign = repository.list_page(transaction, owner_id="owner-3", conversation_id="chat-a")
        unknown = repository.list_page(
            transaction, owner_id="owner-1", conversation_id="chat-unknown"
        )
        narrowed = repository.list_page(
            transaction,
            owner_id="owner-2",
            conversation_id="chat-a",
            outcomes=("failure",),
        )
        if not transaction.dialect.is_sqlite:
            transaction.execute("SET LOCAL enable_seqscan = off")
        plan = transaction.fetch_all(
            (
                "EXPLAIN QUERY PLAN SELECT * FROM audit_events "
                "INDEXED BY idx_audit_user_conversation_recorded "
                if transaction.dialect.is_sqlite
                else "EXPLAIN SELECT * FROM audit_events "
            )
            + "WHERE actor_user_id = %s "
            "AND conversation_id = %s ORDER BY recorded_at DESC, event_id DESC LIMIT 51",
            ("owner-1", "chat-a"),
        )

    assert [record.event.event_id for record in (*first.records, *second.records)] == expected
    assert second.next_cursor is None
    assert {record.event.chain_id for record in (*first.records, *second.records)} == {"owner-1"}
    assert foreign == AuditPage(records=(), next_cursor=None)
    assert unknown == AuditPage(records=(), next_cursor=None)
    assert narrowed == AuditPage(records=(), next_cursor=None)
    assert "idx_audit_user_conversation_recorded" in " ".join(
        str(value) for row in plan for value in row.values()
    )


def test_admin_trajectory_query_is_fixed_bounded_and_typed() -> None:
    transaction = ScriptedTransaction(
        all_rows=[
            (
                {
                    "agent_id": "agent-1",
                    "correlation_id": "turn-1",
                    "tool_name": "search",
                },
            )
        ]
    )
    result = AuditRepository().list_tool_trajectory_events_for_administration(
        transaction,
        from_ts=NOW,
        to_ts=NOW + timedelta(hours=1),
        limit=12,
    )
    assert result == (
        ToolTrajectoryEvent(
            agent_id="agent-1", correlation_id="turn-1", tool_name="search"
        ),
    )
    assert "event_class = 'agent_tool_call'" in transaction.calls[0][1]
    assert "action_type LIKE 'tool.%.end'" in transaction.calls[0][1]
    assert transaction.calls[0][2] == (NOW, NOW + timedelta(hours=1), 12)


def test_admin_trajectory_query_validates_bounds_and_corrupt_rows() -> None:
    repository = AuditRepository()
    for arguments in (
        {"from_ts": NOW.replace(tzinfo=None), "to_ts": NOW, "limit": 1},
        {"from_ts": NOW + timedelta(seconds=1), "to_ts": NOW, "limit": 1},
        {"from_ts": NOW, "to_ts": NOW, "limit": 0},
    ):
        with pytest.raises(ValueError):
            repository.list_tool_trajectory_events_for_administration(
                ScriptedTransaction(), **arguments
            )
    with pytest.raises(ValueError):
        repository.list_tool_trajectory_events_for_administration(
            ScriptedTransaction(
                all_rows=[
                    (
                        {
                            "agent_id": None,
                            "correlation_id": "turn-1",
                            "tool_name": "search",
                        },
                    )
                ]
            ),
            from_ts=NOW,
            to_ts=NOW,
        )


def records() -> tuple[AuditRecord, AuditRecord]:
    first_row = event_row()
    first = AuditRepository().get(
        ScriptedTransaction(one=[first_row]), chain_id="owner-1", event_id="event-1"
    )
    assert first is not None
    second_event = event(event_id="event-2", action_type="tool.finish")
    second_row = event_row(
        second_event,
        sequence=2,
        previous=first.entry_digest,
    )
    second = AuditRepository().get(
        ScriptedTransaction(one=[second_row]), chain_id="owner-1", event_id="event-2"
    )
    assert second is not None
    return first, second


def test_chain_verification_accepts_genesis_and_detects_each_tamper_shape() -> None:
    first, second = records()
    verified = verify_records((first, second), chain_id="owner-1", authenticate=authenticate)
    assert verified.valid
    assert verified.last_sequence == 2
    assert verified.last_digest == second.entry_digest

    cases = (
        (replace(first, event=replace(first.event, chain_id="other")), "wrong_chain"),
        (replace(first, sequence=2), "non_contiguous_sequence"),
        (replace(first, previous_digest=b"q" * 32), "previous_digest_mismatch"),
        (replace(first, entry_digest=b"q" * 32), "entry_digest_mismatch"),
    )
    for damaged, reason in cases:
        result = verify_records((damaged,), chain_id="owner-1", authenticate=authenticate)
        assert not result.valid
        assert result.reason == reason
        assert result.first_invalid_event_id == "event-1"


def test_repository_chain_verification_supports_a_retained_start() -> None:
    first, second = records()
    transaction = ScriptedTransaction(
        all_rows=[(event_row(second.event, sequence=2, previous=first.entry_digest),)]
    )
    result = AuditRepository().verify_chain(
        transaction,
        chain_id="owner-1",
        authenticate=authenticate,
        start_sequence=2,
        expected_previous_digest=first.entry_digest,
    )
    assert result.valid and result.last_sequence == 2
    with pytest.raises(ValueError):
        verify_records(
            (), chain_id="owner-1", authenticate=authenticate, expected_previous_digest=b"x"
        )


def test_authenticator_must_return_sha256_sized_bytes() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        AuditRepository().append(
            ScriptedTransaction(one=[{"locked": True}, None, None]),
            event(),
            lambda _key, _payload: b"short",
        )


def segment_authenticate(key_id: str, payload: bytes) -> bytes:
    return hmac.new(key_id.encode("utf-8") * 4, payload, hashlib.sha256).digest()


def chained_rows(
    *conversation_ids: str | None,
    row_overrides: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous = GENESIS_DIGEST
    for sequence, conversation_id in enumerate(conversation_ids, start=1):
        value = event(
            event_id=f"event-{sequence}",
            correlation_id=f"correlation-{sequence}",
            conversation_id=conversation_id,
        )
        merged: dict[str, object] = {
            "recorded_at": NOW + timedelta(seconds=sequence),
        }
        merged.update(row_overrides or {})
        row = event_row(
            value,
            sequence=sequence,
            previous=previous,
            entry=segment_authenticate(
                value.key_id, previous + canonical_event_bytes(value, sequence)
            ),
            **merged,
        )
        previous = row["entry_hash"]
        rows.append(row)
    return rows


def test_conversation_range_read_is_owner_scoped_bounded_and_paginated() -> None:
    rows = chained_rows("chat-1", "chat-1", "chat-1")
    transaction = ScriptedTransaction(one=[{"matched": 3}], all_rows=[(rows[2], rows[1], rows[0])])
    page = AuditRepository().list_conversation_range(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        limit=2,
    )

    assert isinstance(page, AuditPage)
    assert [record.event.event_id for record in page.records] == ["event-3", "event-2"]
    assert page.next_cursor == AuditCursor(
        recorded_at=NOW + timedelta(seconds=2), event_id="event-2"
    )
    count_statement = transaction.calls[0][1]
    count_parameters = transaction.calls[0][2]
    assert "count(*) AS matched" in count_statement
    assert "actor_user_id = %s" in count_statement
    assert "conversation_id = %s" in count_statement
    assert count_parameters[:2] == ("owner-1", "chat-1")
    page_statement = transaction.calls[1][1]
    page_parameters = transaction.calls[1][2]
    assert "ORDER BY recorded_at DESC, event_id DESC" in page_statement
    assert "LIMIT %s" in page_statement
    assert page_parameters[-1] == 3


def test_conversation_range_narrows_to_turn_metadata_and_time_window() -> None:
    transaction = ScriptedTransaction(one=[{"matched": 0}], all_rows=[()])

    AuditRepository().list_conversation_range(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        turn_id="turn-9",
        from_ts=NOW,
        to_ts=NOW + timedelta(hours=1),
    )

    statement = transaction.calls[0][1]
    parameters = transaction.calls[0][2]
    assert "(inputs_meta->>%s = %s OR outputs_meta->>%s = %s)" in statement
    assert parameters[2:6] == ("turn_id", "turn-9", "turn_id", "turn-9")
    assert "recorded_at >= %s" in statement and "recorded_at < %s" in statement


def test_conversation_range_above_bound_is_refused_not_truncated() -> None:
    transaction = ScriptedTransaction(one=[{"matched": 3}])

    with pytest.raises(PlaneError) as caught:
        AuditRepository().list_conversation_range(
            transaction,
            owner_id="owner-1",
            conversation_id="chat-1",
            max_events=2,
        )

    assert caught.value.code == "audit_range_exceeds_bound"
    assert len(transaction.calls) == 1


def test_conversation_range_read_validates_arguments() -> None:
    repository = AuditRepository()
    transaction = ScriptedTransaction()
    cases: tuple[tuple[dict[str, object]], ...] = (
        {"conversation_id": ""},
        {"conversation_id": "x" * 257},
        {"conversation_id": "chat\x00-1"},
        {"conversation_id": "chat-1", "turn_id": ""},
        {"conversation_id": "chat-1", "turn_id": "turn\x00-9"},
        {"conversation_id": "chat-1", "limit": 0},
        {"conversation_id": "chat-1", "limit": 201},
        {"conversation_id": "chat-1", "max_events": 0},
        {
            "conversation_id": "chat-1",
            "max_events": AUDIT_CONVERSATION_READ_MAX_EVENTS + 1,
        },
        {"conversation_id": "chat-1", "from_ts": NOW.replace(tzinfo=None)},
        {"conversation_id": "chat-1", "to_ts": NOW.replace(tzinfo=None)},
        {
            "conversation_id": "chat-1",
            "from_ts": NOW + timedelta(hours=1),
            "to_ts": NOW,
        },
        {"conversation_id": "chat-1", "cursor": "not-a-cursor"},
    )
    for changes in cases:
        with pytest.raises(ValueError):
            repository.list_conversation_range(transaction, owner_id="owner-1", **changes)  # type: ignore[arg-type]
    assert transaction.calls == []


def test_conversation_segment_groups_interleaved_events_into_anchored_runs() -> None:
    rows = chained_rows("chat-1", "chat-2", "chat-1", "chat-1", "chat-2")
    transaction = ScriptedTransaction(all_rows=[(rows[0], rows[2], rows[3])])

    proof = AuditRepository().load_conversation_segment(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
    )

    assert proof.chain_id == "owner-1"
    assert proof.key_ids == ("audit-key-1",)
    assert len(proof.runs) == 2
    first, second = proof.runs
    assert [record.sequence for record in first.records] == [1]
    assert first.anchor_digest == GENESIS_DIGEST
    assert [record.sequence for record in second.records] == [3, 4]
    assert second.anchor_digest == rows[1]["entry_hash"]
    statement = transaction.calls[0][1]
    parameters = transaction.calls[0][2]
    assert "actor_user_id = %s" in statement and "conversation_id = %s" in statement
    assert "ORDER BY chain_sequence, event_id" in statement
    assert parameters[-1] == AUDIT_CONVERSATION_READ_MAX_EVENTS + 1
    result = verify_segment_proof(proof, authenticate=segment_authenticate)
    assert result.valid
    assert result.last_sequence == 4
    assert result.last_digest == rows[3]["entry_hash"]


def test_conversation_segment_narrows_by_turn_and_empty_selection_verifies() -> None:
    transaction = ScriptedTransaction(all_rows=[()])

    proof = AuditRepository().load_conversation_segment(
        transaction,
        owner_id="owner-1",
        conversation_id="chat-1",
        turn_id="turn-9",
    )

    assert proof.runs == ()
    assert proof.key_ids == ()
    result = verify_segment_proof(proof, authenticate=segment_authenticate)
    assert result.valid
    assert result.last_sequence == 0
    assert result.last_digest == GENESIS_DIGEST
    statement = transaction.calls[0][1]
    assert "(inputs_meta->>%s = %s OR outputs_meta->>%s = %s)" in statement


def test_conversation_segment_above_bound_is_refused_not_truncated() -> None:
    rows = chained_rows("chat-1", "chat-1", "chat-1")
    transaction = ScriptedTransaction(all_rows=[tuple(rows)])

    with pytest.raises(PlaneError) as caught:
        AuditRepository().load_conversation_segment(
            transaction,
            owner_id="owner-1",
            conversation_id="chat-1",
            max_events=2,
        )

    assert caught.value.code == "audit_segment_exceeds_bound"
    assert transaction.calls[0][2][-1] == 3


def test_conversation_segment_read_validates_arguments() -> None:
    repository = AuditRepository()
    transaction = ScriptedTransaction()
    cases: tuple[tuple[dict[str, object]], ...] = (
        {"conversation_id": ""},
        {"conversation_id": "chat\x00-1"},
        {"conversation_id": "chat-1", "turn_id": "turn\x00-9"},
        {"conversation_id": "chat-1", "max_events": 0},
        {
            "conversation_id": "chat-1",
            "max_events": AUDIT_CONVERSATION_READ_MAX_EVENTS + 1,
        },
    )
    for changes in cases:
        with pytest.raises(ValueError):
            repository.load_conversation_segment(transaction, owner_id="owner-1", **changes)  # type: ignore[arg-type]
    assert transaction.calls == []


def segment_proof() -> tuple[AuditRepository, object]:
    rows = chained_rows("chat-1", "chat-2", "chat-1", "chat-1")
    transaction = ScriptedTransaction(all_rows=[(rows[0], rows[2], rows[3])])
    proof = AuditRepository().load_conversation_segment(
        transaction, owner_id="owner-1", conversation_id="chat-1"
    )
    return proof, rows  # type: ignore[return-value]


def test_segment_verification_fails_when_any_event_field_changes() -> None:
    proof, _rows = segment_proof()
    run = proof.runs[1]
    record = run.records[0]
    tampered_record = replace(record, event=replace(record.event, description="changed"))
    tampered = replace(
        proof,
        runs=(proof.runs[0], replace(run, records=(tampered_record, *run.records[1:]))),
    )

    result = verify_segment_proof(tampered, authenticate=segment_authenticate)

    assert not result.valid
    assert result.first_invalid_event_id == "event-3"
    assert result.reason == "entry_digest_mismatch"
    assert result.last_sequence == 2


def test_segment_verification_fails_when_entry_hash_changes() -> None:
    proof, _rows = segment_proof()
    run = proof.runs[0]
    record = run.records[0]
    tampered = replace(
        proof,
        runs=(replace(run, records=(replace(record, entry_digest=b"\x02" * 32),)),),
    )

    result = verify_segment_proof(tampered, authenticate=segment_authenticate)

    assert not result.valid
    assert result.first_invalid_event_id == "event-1"
    assert result.reason == "entry_digest_mismatch"


def test_segment_verification_fails_when_chained_hash_changes() -> None:
    proof, _rows = segment_proof()
    run = proof.runs[1]
    head, follower = run.records
    tampered = replace(
        proof,
        runs=(
            proof.runs[0],
            replace(run, records=(head, replace(follower, previous_digest=b"\x03" * 32))),
        ),
    )

    result = verify_segment_proof(tampered, authenticate=segment_authenticate)

    assert not result.valid
    assert result.first_invalid_event_id == "event-4"
    assert result.reason == "previous_digest_mismatch"


def test_segment_verification_fails_under_a_different_authenticator() -> None:
    proof, _rows = segment_proof()

    result = verify_segment_proof(proof, authenticate=authenticate)

    assert not result.valid
    assert result.reason == "entry_digest_mismatch"


def test_segment_run_structure_is_validated_fail_closed() -> None:
    proof, rows = segment_proof()
    run = proof.runs[1]

    with pytest.raises(ValueError):
        replace(run, anchor_digest=b"\x04" * 32)
    with pytest.raises(ValueError):
        replace(run, records=(run.records[1], run.records[0]))
    with pytest.raises(ValueError):
        replace(proof, runs=(proof.runs[1], proof.runs[0]))
    with pytest.raises(ValueError):
        replace(proof, chain_id="owner-2")
    with pytest.raises(ValueError):
        AuditSegmentRun(anchor_digest=rows[0]["prev_hash"], records=())
    with pytest.raises(ValueError):
        verify_segment_proof("not-a-proof", authenticate=segment_authenticate)  # type: ignore[arg-type]


def test_conversation_reads_are_owner_isolated_on_real_postgresql(
    audit_postgres_database: PlaneDatabase,
) -> None:
    repository = AuditRepository()
    owners = ("owner-iso-1", "owner-iso-2")
    expected: dict[str, list[str]] = {}
    with audit_postgres_database.transaction() as transaction:
        for owner in owners:
            expected[owner] = []
            for number in range(1, 5):
                conversation = "chat-shared" if number % 2 else "chat-other"
                turn = '{"turn_id":"turn-1"}' if number % 2 else "{}"
                appended = repository.append(
                    transaction,
                    event(
                        event_id=str(uuid.uuid4()),
                        correlation_id=str(uuid.uuid4()),
                        chain_id=owner,
                        conversation_id=conversation,
                        inputs_json=turn,
                    ),
                    authenticate,
                )
                expected[owner].append(appended.event.event_id)

    with audit_postgres_database.transaction() as query:
        page = repository.list_conversation_range(
            query,
            owner_id="owner-iso-1",
            conversation_id="chat-shared",
        )
    with audit_postgres_database.transaction() as transaction:
        proof = repository.load_conversation_segment(
            transaction,
            owner_id="owner-iso-1",
            conversation_id="chat-shared",
        )

    assert {record.event.event_id for record in page.records} == set(expected["owner-iso-1"][::2])
    assert len(page.records) == 2
    assert page.records[0].sequence == 3 and page.records[1].sequence == 1
    assert {record.event.chain_id for record in page.records} == {"owner-iso-1"}
    assert [record.sequence for record in proof.runs[0].records] == [1]
    assert [record.sequence for record in proof.runs[1].records] == [3]
    with audit_postgres_database.transaction() as transaction:
        chain = repository.load_chain(transaction, chain_id="owner-iso-1")
    assert proof.runs[1].anchor_digest == chain[1].entry_digest
    assert all(
        record.event.chain_id == "owner-iso-1" for run in proof.runs for record in run.records
    )
    assert verify_segment_proof(proof, authenticate=authenticate).valid

    with audit_postgres_database.transaction() as query:
        foreign_page = repository.list_conversation_range(
            query,
            owner_id="owner-iso-2",
            conversation_id="chat-shared",
        )
    assert {record.event.event_id for record in foreign_page.records} == set(
        expected["owner-iso-2"][::2]
    )
    assert {record.event.chain_id for record in foreign_page.records} == {"owner-iso-2"}
    with pytest.raises(PlaneError) as caught, audit_postgres_database.transaction() as query:
        repository.list_conversation_range(
            query,
            owner_id="owner-iso-1",
            conversation_id="chat-shared",
            max_events=1,
        )
    assert caught.value.code == "audit_range_exceeds_bound"


def test_conversation_range_paginates_and_narrows_by_turn_on_real_postgresql(
    audit_postgres_database: PlaneDatabase,
) -> None:
    repository = AuditRepository()
    with audit_postgres_database.transaction() as transaction:
        for number in range(1, 4):
            repository.append(
                transaction,
                event(
                    event_id=str(uuid.uuid4()),
                    correlation_id=str(uuid.uuid4()),
                    chain_id="owner-page-1",
                    conversation_id="chat-page",
                    inputs_json='{"turn_id":"turn-7"}' if number == 2 else "{}",
                ),
                authenticate,
            )

    with audit_postgres_database.transaction() as query:
        first = repository.list_conversation_range(
            query, owner_id="owner-page-1", conversation_id="chat-page", limit=2
        )
    assert first.next_cursor is not None
    with audit_postgres_database.transaction() as query:
        second = repository.list_conversation_range(
            query,
            owner_id="owner-page-1",
            conversation_id="chat-page",
            cursor=first.next_cursor,
        )
    assert len(first.records) == 2 and len(second.records) == 1
    assert first.next_cursor.event_id == first.records[-1].event.event_id
    assert {record.event.event_id for record in first.records + second.records} == {
        record.event.event_id for record in first.records + second.records
    }
    assert not {record.event.event_id for record in second.records} & {
        record.event.event_id for record in first.records
    }

    with audit_postgres_database.transaction() as query:
        turn_page = repository.list_conversation_range(
            query,
            owner_id="owner-page-1",
            conversation_id="chat-page",
            turn_id="turn-7",
        )
    with audit_postgres_database.transaction() as transaction:
        turn_proof = repository.load_conversation_segment(
            transaction,
            owner_id="owner-page-1",
            conversation_id="chat-page",
            turn_id="turn-7",
        )
    assert [record.event.inputs_json for record in turn_page.records] == ['{"turn_id":"turn-7"}']
    assert len(turn_proof.runs) == 1
    assert verify_segment_proof(turn_proof, authenticate=authenticate).valid

    with audit_postgres_database.transaction() as query:
        unknown = repository.list_conversation_range(
            query, owner_id="owner-page-1", conversation_id="chat-unknown"
        )
    with audit_postgres_database.transaction() as transaction:
        unknown_proof = repository.load_conversation_segment(
            transaction, owner_id="owner-page-1", conversation_id="chat-unknown"
        )
    assert unknown.records == () and unknown.next_cursor is None
    assert unknown_proof.runs == ()


def test_segment_structures_reject_wrong_types_and_shapes() -> None:
    proof, _rows = segment_proof()
    record = proof.runs[0].records[0]

    with pytest.raises(ValueError):
        AuditSegmentRun(anchor_digest=record.previous_digest, records=(record, "not-a-record"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AuditSegmentRun(
            anchor_digest=record.previous_digest,
            records=(record, replace(record, sequence=record.sequence + 2)),
        )
    with pytest.raises(ValueError):
        AuditSegmentProof(chain_id="owner-1", runs=[proof.runs[0]])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AuditSegmentProof(chain_id="owner-1", runs=("not-a-run",))  # type: ignore[arg-type]
