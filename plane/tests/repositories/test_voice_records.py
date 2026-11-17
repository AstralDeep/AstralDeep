"""Tests for src/astralplane/repositories/voice.py: neutral record-level contracts used
by Deep's voice coordinator, covering identity locks, owner-scoped reads, and dynamic
patch fencing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _support import Result, ScriptedTransaction

import astralplane.repositories.voice as voice_module
from astralplane.repositories import RepositoryValidationError
from astralplane.repositories.voice import VoiceRepository

NOW = datetime(2026, 8, 14, 20, tzinfo=UTC)


def test_identity_lock_is_deterministic_and_transaction_owned() -> None:
    first = ScriptedTransaction()
    second = ScriptedTransaction()
    repository = VoiceRepository()

    repository.lock_identity(first, namespace="owner", parts=("owner-1", "session-1"))
    repository.lock_identity(second, namespace="owner", parts=("owner-1", "session-1"))

    assert first.calls == second.calls
    assert first.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in first.fetch_sql()
    with pytest.raises(RepositoryValidationError):
        repository.lock_identity(ScriptedTransaction(), namespace="owner", parts=())


def test_record_reads_keep_owner_queries_separate_from_administration() -> None:
    repository = VoiceRepository()
    owner = ScriptedTransaction(one=[{"session_id": "session-1"}])
    admin = ScriptedTransaction(one=[{"session_id": "session-1"}])

    assert repository.get_session_record(
        owner,
        owner_id="owner-1",
        session_id="session-1",
        for_update=True,
    ) == {"session_id": "session-1"}
    assert repository.get_session_record_for_administration(
        admin,
        session_id="session-1",
    ) == {"session_id": "session-1"}

    assert "user_id = %s" in owner.fetch_sql()
    assert "FOR UPDATE" in owner.fetch_sql()
    assert "user_id" not in admin.fetch_sql()


@pytest.mark.parametrize(
    ("method", "kwargs", "owner_scoped"),
    [
        ("get_activation_record", {"owner_id": "owner-1", "activation_id": "a"}, True),
        ("get_live_session_record", {"owner_id": "owner-1"}, True),
        ("get_turn_record", {"owner_id": "owner-1", "turn_id": "t"}, True),
        ("get_turn_record_for_administration", {"turn_id": "t"}, False),
        ("get_client_turn_record", {"owner_id": "owner-1", "client_turn_id": "c"}, True),
        (
            "get_submission_record",
            {
                "owner_id": "owner-1",
                "submission_id": "s",
                "request_generation": "g",
            },
            True,
        ),
    ],
)
def test_record_identity_selectors(
    method: str,
    kwargs: dict[str, object],
    owner_scoped: bool,
) -> None:
    transaction = ScriptedTransaction(one=[{"identity": method}])

    result = getattr(VoiceRepository(), method)(transaction, **kwargs)

    assert result == {"identity": method}
    assert ("user_id" in transaction.fetch_sql()) is owner_scoped


def test_turn_and_idle_inventory_are_bounded_and_db_locking() -> None:
    repository = VoiceRepository()
    maximum = ScriptedTransaction(one=[{"sequence": 7}])
    present = ScriptedTransaction(one=[{"present": 1}])
    idle = ScriptedTransaction(all_rows=[({"session_id": "session-1"},)])

    assert repository.max_client_playout_sequence(
        maximum,
        owner_id="owner-1",
        session_id="session-1",
    ) == 7
    assert repository.has_turn_in_states(
        present,
        owner_id="owner-1",
        session_id="session-1",
        states=("processing", "waiting_on_user"),
    )
    assert repository.list_true_idle_session_records_for_administration(
        idle, cutoff=NOW
    ) == (
        {"session_id": "session-1"},
    )
    assert "FOR UPDATE SKIP LOCKED" in idle.fetch_sql()
    with pytest.raises(RepositoryValidationError):
        repository.has_turn_in_states(
            ScriptedTransaction(),
            owner_id="owner-1",
            session_id="session-1",
            states=(),
        )


def test_chat_reads_and_locked_inventory_are_owner_scoped() -> None:
    repository = VoiceRepository()
    exists = ScriptedTransaction(one=[{"id": "chat-1"}])
    revision = ScriptedTransaction(one=[{"render_revision": 4}])
    turns = ScriptedTransaction(all_rows=[({"turn_id": "turn-1"},)])
    sessions = ScriptedTransaction(all_rows=[({"session_id": "session-1"},)])

    assert repository.chat_exists(exists, owner_id="owner-1", chat_id="chat-1")
    assert repository.get_chat_render_revision(
        revision,
        owner_id="owner-1",
        chat_id="chat-1",
        for_share=True,
    ) == 4
    assert repository.list_chat_turn_records_for_update(
        turns,
        owner_id="owner-1",
        chat_id="chat-1",
    ) == ({"turn_id": "turn-1"},)
    assert repository.list_chat_session_records_for_update(
        sessions,
        owner_id="owner-1",
        chat_id="chat-1",
    ) == ({"session_id": "session-1"},)
    assert "user_id = %s" in "\n".join(
        transaction.fetch_sql() for transaction in (exists, revision, turns, sessions)
    )
    assert "FOR SHARE" in revision.fetch_sql()
    assert "FOR UPDATE" in turns.fetch_sql()
    assert "FOR UPDATE" in sessions.fetch_sql()


def test_chat_turn_abandonment_preserves_accepted_and_unaccepted_semantics() -> None:
    repository = VoiceRepository()
    accepted = ScriptedTransaction(execute=[Result(rowcount=2)])
    unaccepted = ScriptedTransaction(execute=[Result(rowcount=1)])

    assert repository.abandon_chat_turns(
        accepted,
        owner_id="owner-1",
        turn_ids=("turn-1", "turn-2"),
        reason="deleted",
        now=NOW,
        accepted=True,
    ) == 2
    assert repository.abandon_chat_turns(
        unaccepted,
        owner_id="owner-1",
        turn_ids=("turn-3",),
        reason="deleted",
        now=NOW,
        accepted=False,
    ) == 1
    assert "origin_chat_unavailable_at = %s" in accepted.fetch_sql()
    assert "rejection_reason = 'chat_unavailable'" in unaccepted.fetch_sql()
    assert repository.abandon_chat_turns(
        ScriptedTransaction(),
        owner_id="owner-1",
        turn_ids=(),
        reason="deleted",
        now=NOW,
        accepted=False,
    ) == 0


def test_staged_result_cleanup_is_ordered_and_chat_delete_is_owner_scoped() -> None:
    repository = VoiceRepository()
    cleanup = ScriptedTransaction(
        all_rows=[({"commit_id": "commit-1"}, {"commit_id": "commit-2"})]
    )

    assert repository.abort_staged_chat_result_commits(
        cleanup,
        owner_id="owner-1",
        chat_id="chat-1",
        now=NOW,
    ) == ("commit-1", "commit-2")
    assert sum(call[0] == "execute" for call in cleanup.calls) == 8
    assert cleanup.calls[-1][2] == (NOW, "commit-2")

    deleted = ScriptedTransaction(execute=[Result(rowcount=1)])
    assert repository.delete_owned_chat(
        deleted,
        owner_id="owner-1",
        chat_id="chat-1",
    )
    assert deleted.calls[0][2] == ("chat-1", "owner-1")


@pytest.mark.parametrize(
    ("method", "fields", "table"),
    [
        ("insert_session_record", voice_module._SESSION_INSERT_FIELDS, "voice_session"),
        ("insert_turn_record", voice_module._TURN_INSERT_FIELDS, "voice_turn"),
    ],
)
def test_exact_record_inserts_reject_missing_or_unknown_fields(
    method: str,
    fields: tuple[str, ...],
    table: str,
) -> None:
    repository = VoiceRepository()
    values = (
        _session_insert_values()
        if table == "voice_session"
        else {field: f"value-{index}" for index, field in enumerate(fields)}
    )
    transaction = ScriptedTransaction(one=[{"stored": table}])

    assert getattr(repository, method)(transaction, values=values) == {"stored": table}
    assert f"INSERT INTO {table}" in transaction.fetch_sql()
    assert transaction.calls[0][2] == tuple(values[field] for field in fields)

    missing = dict(values)
    missing.pop(fields[-1])
    with pytest.raises(RepositoryValidationError):
        getattr(repository, method)(ScriptedTransaction(), values=missing)
    with pytest.raises(RepositoryValidationError):
        getattr(repository, method)(
            ScriptedTransaction(),
            values={**values, "unsupported": True},
        )


def _session_insert_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        field: f"value-{index}"
        for index, field in enumerate(voice_module._SESSION_INSERT_FIELDS)
    }
    values.update(
        {
            "speech_backend": "llm_factory",
            "transport": "livekit",
            "room_name": "room-1",
            "participant_identity": "participant-1",
            "media_grant_nonce_hash": b"n" * 32,
            "media_grant_issued_at": NOW,
            "media_grant_expires_at": NOW + timedelta(seconds=30),
            "worker_rtc_grant_revision": 1,
        }
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize("transport", ["livekit", "watch_pcm_websocket"])
def test_session_record_insert_accepts_only_remote_backend_transports(
    transport: str,
) -> None:
    transaction = ScriptedTransaction(one=[{"speech_backend": "llm_factory"}])

    row = VoiceRepository().insert_session_record(
        transaction,
        values=_session_insert_values(transport=transport),
    )

    assert row == {"speech_backend": "llm_factory"}
    assert "speech_backend" in transaction.fetch_sql()


def test_session_record_insert_accepts_exact_client_local_shape() -> None:
    transaction = ScriptedTransaction(one=[{"speech_backend": "client_local"}])

    row = VoiceRepository().insert_session_record(
        transaction,
        values=_session_insert_values(
            speech_backend="client_local",
            transport="client_local",
            room_name=None,
            participant_identity=None,
            media_grant_nonce_hash=None,
            media_grant_issued_at=None,
            media_grant_expires_at=None,
            worker_rtc_grant_revision=None,
        ),
    )

    assert row == {"speech_backend": "client_local"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"speech_backend": "server_media"},
        {"speech_backend": "llm_factory", "transport": "client_local"},
        {"speech_backend": "client_local", "transport": "livekit"},
        {"speech_backend": "client_local", "transport": "watch_pcm_websocket"},
        {"speech_backend": "client_local", "room_name": "remote-room"},
        {"speech_backend": "client_local", "participant_identity": "remote-participant"},
        {"speech_backend": "client_local", "media_grant_nonce_hash": b"n" * 32},
        {"speech_backend": "client_local", "media_grant_issued_at": NOW},
        {
            "speech_backend": "client_local",
            "media_grant_expires_at": NOW + timedelta(seconds=30),
        },
        {"speech_backend": "client_local", "worker_rtc_grant_revision": 1},
        {"speech_backend": "llm_factory", "room_name": None},
        {"speech_backend": "llm_factory", "participant_identity": None},
        {"speech_backend": "llm_factory", "media_grant_nonce_hash": None},
        {"speech_backend": "llm_factory", "media_grant_issued_at": None},
        {"speech_backend": "llm_factory", "media_grant_expires_at": None},
        {"speech_backend": "llm_factory", "worker_rtc_grant_revision": None},
    ],
)
def test_session_record_insert_rejects_every_mixed_backend_shape(
    overrides: dict[str, object],
) -> None:
    local_defaults = {
        "transport": "client_local",
        "room_name": None,
        "participant_identity": None,
        "media_grant_nonce_hash": None,
        "media_grant_issued_at": None,
        "media_grant_expires_at": None,
        "worker_rtc_grant_revision": None,
    }
    selected = local_defaults if overrides.get("speech_backend") == "client_local" else {}
    values = _session_insert_values(**{**selected, **overrides})

    with pytest.raises(RepositoryValidationError, match="speech backend"):
        VoiceRepository().insert_session_record(ScriptedTransaction(), values=values)


def test_dynamic_record_patches_are_allowlisted_and_fenced() -> None:
    repository = VoiceRepository()
    session = ScriptedTransaction(one=[{"session_id": "session-1"}])
    turn = ScriptedTransaction(one=[{"turn_id": "turn-1"}])

    assert repository.patch_session_record(
        session,
        owner_id="owner-1",
        session_id="session-1",
        updates={"state": "active", "updated_at": NOW},
        require_live=True,
    ) == {"session_id": "session-1"}
    assert repository.patch_turn_record(
        turn,
        owner_id="owner-1",
        turn_id="turn-1",
        updates={"state": "processing", "updated_at": NOW},
        expected_states=("accepted",),
    ) == {"turn_id": "turn-1"}
    assert "ended_at IS NULL" in session.fetch_sql()
    assert "state = ANY(%s)" in turn.fetch_sql()
    assert "user_id = %s" in session.fetch_sql()
    assert "user_id = %s" in turn.fetch_sql()
    assert turn.calls[0][2][-1] == ["accepted"]  # type: ignore[index]

    for updates in ({}, {"session_id": "replacement"}):
        with pytest.raises(RepositoryValidationError):
            repository.patch_session_record(
                ScriptedTransaction(),
                owner_id="owner-1",
                session_id="session-1",
                updates=updates,
            )
    for immutable in ("speech_backend", "transport"):
        with pytest.raises(RepositoryValidationError):
            repository.patch_session_record(
                ScriptedTransaction(),
                owner_id="owner-1",
                session_id="session-1",
                updates={immutable: "client_local"},
            )
    with pytest.raises(RepositoryValidationError):
        repository.patch_turn_record(
            ScriptedTransaction(),
            owner_id="owner-1",
            turn_id="turn-1",
            updates={"state": "processing"},
            expected_states=(),
        )


def test_claim_and_control_mutations_use_database_clock_where_required() -> None:
    repository = VoiceRepository()
    claim = ScriptedTransaction(execute=[Result(rowcount=1)])
    released = ScriptedTransaction(execute=[Result(rowcount=1)])
    abandoned = ScriptedTransaction(execute=[Result(rowcount=3)])
    foreground = ScriptedTransaction(execute=[Result(rowcount=2)])

    assert repository.complete_announcement_claim(
        claim,
        owner_id="owner-1",
        turn_id="turn-1",
        claim_id="claim-1",
        claim_expires_at=NOW + timedelta(seconds=5),
    )
    assert repository.release_control_lease_record(
        released,
        owner_id="owner-1",
        session_id="session-1",
    )
    assert repository.abandon_unaccepted_session_turns(
        abandoned,
        owner_id="owner-1",
        session_id="session-1",
        generation=2,
        now=NOW,
    ) == 3
    assert repository.clear_foreground_turns(
        foreground,
        owner_id="owner-1",
        session_id="session-1",
        now=NOW,
        except_turn_id="turn-2",
    ) == 2
    assert "CURRENT_TIMESTAMP" in claim.fetch_sql()
    assert "CURRENT_TIMESTAMP" in released.fetch_sql()
    assert "turn_id <> %s" in foreground.fetch_sql()


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        (
            "list_owned_live_session_records_for_administration",
            {"control_owner_id": "replica-1", "limit": 10},
        ),
        ("list_expired_session_records_for_administration", {"now": NOW}),
        (
            "list_renewable_control_session_records_for_administration",
            {"control_owner_id": "replica-1", "now": NOW, "limit": 10},
        ),
    ],
)
def test_session_batch_selectors_are_lock_skipping(
    method: str,
    kwargs: dict[str, object],
) -> None:
    transaction = ScriptedTransaction(all_rows=[({"session_id": "session-1"},)])

    assert getattr(VoiceRepository(), method)(transaction, **kwargs) == (
        {"session_id": "session-1"},
    )
    assert "FOR UPDATE SKIP LOCKED" in transaction.fetch_sql()


@pytest.mark.parametrize(
    "method",
    [
        "reconcile_ended_unaccepted_turns_for_administration",
        "reconcile_ended_terminal_operation_turns_for_administration",
    ],
)
def test_reconciliation_batches_are_lock_skipping_and_return_updated_rows(
    method: str,
) -> None:
    transaction = ScriptedTransaction(all_rows=[({"turn_id": "turn-1"},)])

    assert getattr(VoiceRepository(), method)(transaction, now=NOW, limit=5) == (
        {"turn_id": "turn-1"},
    )
    assert "FOR UPDATE" in transaction.fetch_sql()
    assert "SKIP LOCKED" in transaction.fetch_sql()
    assert "RETURNING turn.*" in transaction.fetch_sql()
