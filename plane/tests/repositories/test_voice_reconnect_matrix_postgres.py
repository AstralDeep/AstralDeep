"""Composed voice reconnect scenario matrix over real PostgreSQL: barrier-raced stale/current
generation writers for two owners, duplicate client-turn replay, terminal-result retry, and
cross-owner isolation, reusing the migrated current-schema fixtures from
tests/repositories/test_assignments_postgres.py. It gives reusable hosts durable evidence that
reconnects never mutate the immutable speech backend, client retries compose to exactly one
logical accepted turn, stale fences refuse without state changes, and terminal results never
reopen; live iOS/Android microphone and foreground behavior remains those clients'
qualification responsibility.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from test_assignments_postgres import database as database
from test_assignments_postgres import independent_database, uid

from astralplane.errors import PlaneError
from astralplane.repositories.voice import (
    VoiceRepository,
    VoiceSessionCreate,
    VoiceSessionState,
    VoiceTurnCreate,
    VoiceTurnState,
)

OWNERS = ("matrix-client-local-owner", "matrix-llm-factory-owner")
BACKENDS = ("client_local", "llm_factory")
IMMUTABLE_BACKEND_FIELDS = (
    "speech_backend",
    "transport",
    "room_name",
    "participant_identity",
    "media_grant_nonce_hash",
    "media_grant_issued_at",
    "media_grant_expires_at",
    "worker_rtc_grant_revision",
)


@pytest.fixture(autouse=True)
def isolated_voice_rows(database):
    with database.transaction() as tx:
        tx.execute("DELETE FROM voice_turn")
        tx.execute("DELETE FROM voice_session")
    yield


def raced(database, operations):
    with database.transaction() as tx:
        schema = tx.fetch_one("SELECT current_schema() AS name")["name"]
    barrier = Barrier(len(operations))

    def run(operation):
        with independent_database(schema) as db:
            barrier.wait(timeout=10)
            try:
                with db.transaction() as tx:
                    return operation(tx)
            except PlaneError as error:
                return error

    with ThreadPoolExecutor(max_workers=len(operations)) as workers:
        return tuple(workers.map(run, operations))


def clock(tx):
    return tx.fetch_one("SELECT clock_timestamp() AS now")["now"]


def session_create(owner_id: str, backend: str, now) -> VoiceSessionCreate:
    remote = backend == "llm_factory"
    return VoiceSessionCreate(
        session_id=uid(),
        owner_id=owner_id,
        activation_id=uid(),
        device_id=uid(),
        device_kind="android" if not remote else "ios",
        speech_backend=backend,
        transport="client_local" if not remote else "livekit",
        room_name=None if not remote else "matrix-room-" + uid(),
        participant_identity=None if not remote else "matrix-participant-" + uid(),
        visible_chat_id="matrix-chat-" + owner_id,
        owner_connection_generation=uid(),
        control_binding_id=uid(),
        control_binding_expires_at=now + timedelta(minutes=2),
        lease_expires_at=now + timedelta(minutes=2),
        media_grant_nonce_hash=None if not remote else b"m" * 32,
        media_grant_issued_at=None if not remote else now,
        media_grant_expires_at=None if not remote else now + timedelta(minutes=1),
        started_at=now,
    )


def turn_create(session, **overrides: object) -> VoiceTurnCreate:
    values: dict[str, object] = {
        "turn_id": uid(),
        "client_turn_id": uid(),
        "session_id": session.session_id,
        "session_generation": session.generation,
        "media_grant_revision": 1,
        "owner_id": session.owner_id,
        "chat_id": session.visible_chat_id,
        "chat_context_revision": session.chat_context_revision,
        "execution_base_render_revision": 0,
        "submission_id": uid(),
        "request_generation": uid(),
    }
    values.update(overrides)
    return VoiceTurnCreate(**values)  # type: ignore[arg-type]


def advance(repo, tx, session, state):
    now = clock(tx)
    return repo.advance_session(
        tx,
        owner_id=session.owner_id,
        session_id=session.session_id,
        expected_generation=session.generation,
        expected_context_revision=session.chat_context_revision,
        state=state,
        visible_chat_id=session.visible_chat_id,
        lease_expires_at=now + timedelta(minutes=3),
        now=now,
    )


def start_active_session(repo, tx, owner_id: str, backend: str):
    created = repo.create_session(tx, session_create(owner_id, backend, clock(tx)))
    return advance(repo, tx, created, VoiceSessionState.ACTIVE)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reconnect_cycles_preserve_immutable_backend_and_replay_activation(database, backend):
    repo = VoiceRepository()
    owner_id = OWNERS[0] if backend == "client_local" else OWNERS[1]
    other_backend = "llm_factory" if backend == "client_local" else "client_local"
    with database.transaction() as tx:
        create = session_create(owner_id, backend, clock(tx))
        created = repo.create_session(tx, create)
        initial = repo.get_session_record(tx, owner_id=owner_id, session_id=created.session_id)
    with database.transaction() as tx:
        replay = repo.create_session(tx, create)
        assert (replay.session_id, replay.state) == (
            created.session_id,
            VoiceSessionState.STARTING,
        )
    with database.transaction() as tx:
        reconnected = advance(repo, tx, created, VoiceSessionState.RECONNECTING)
    assert (reconnected.generation, reconnected.chat_context_revision) == (2, 2)
    with database.transaction() as tx:
        active = advance(repo, tx, reconnected, VoiceSessionState.ACTIVE)
    assert (active.generation, active.chat_context_revision) == (3, 3)
    with database.transaction() as tx:
        for replayed in (
            create,
            replace(
                session_create(owner_id, other_backend, clock(tx)),
                activation_id=create.activation_id,
            ),
            replace(
                session_create(owner_id, backend, clock(tx)),
                activation_id=create.activation_id,
            ),
        ):
            with pytest.raises(PlaneError) as raised:
                repo.create_session(tx, replayed)
            assert raised.value.code == "voice_session_idempotency_conflict"
        after = repo.get_session_record(tx, owner_id=owner_id, session_id=created.session_id)
    assert all(after[field] == initial[field] for field in IMMUTABLE_BACKEND_FIELDS)
    assert (after["generation"], after["state"]) == (3, "active")


def test_barrier_raced_reconnect_refuses_stale_generation_for_both_owners(database):
    repo = VoiceRepository()
    with database.transaction() as tx:
        sessions = {
            owner: start_active_session(repo, tx, owner, backend)
            for owner, backend in zip(OWNERS, BACKENDS, strict=True)
        }

    def reconnect(owner: str, expected_generation: int, expected_context: int):
        return lambda tx: advance(
            repo,
            tx,
            replace(
                sessions[owner],
                generation=expected_generation,
                chat_context_revision=expected_context,
            ),
            VoiceSessionState.ACTIVE,
        )

    results = raced(
        database,
        (
            reconnect(OWNERS[0], 1, 1),
            reconnect(OWNERS[0], 2, 2),
            reconnect(OWNERS[1], 1, 1),
            reconnect(OWNERS[1], 2, 2),
        ),
    )
    for stale in (results[0], results[2]):
        assert isinstance(stale, PlaneError)
        assert stale.code == "voice_session_state_conflict"
    for winner in (results[1], results[3]):
        assert (winner.generation, winner.chat_context_revision) == (3, 3)
        assert winner.state is VoiceSessionState.ACTIVE
    with database.transaction() as tx:
        for owner in OWNERS:
            row = repo.get_session_record(tx, owner_id=owner, session_id=sessions[owner].session_id)
            assert (row["generation"], row["chat_context_revision"]) == (3, 3)
            assert row["state"] == "active" and row["ended_at"] is None


@pytest.mark.parametrize("backend", BACKENDS)
def test_barrier_raced_duplicate_client_turn_commits_one_accepted_logical_turn(
    database, backend
):
    repo = VoiceRepository()
    owner_id = OWNERS[0] if backend == "client_local" else OWNERS[1]
    with database.transaction() as tx:
        session = start_active_session(repo, tx, owner_id, backend)
        create = turn_create(session)
        rival = turn_create(session)
        rival_twin = replace(rival, turn_id=uid(), client_turn_id=uid())

    def serialized(tx):
        repo.lock_identity(
            tx, namespace="voice_reconnect_matrix", parts=(owner_id, session.session_id)
        )
        return repo.create_turn(tx, create)

    committed = raced(database, (serialized, serialized))
    assert committed[0] == committed[1]
    rival_results = raced(
        database,
        (
            lambda tx: repo.create_turn(tx, rival),
            lambda tx: repo.create_turn(tx, rival_twin),
        ),
    )
    succeeded = [item for item in rival_results if hasattr(item, "turn_id")]
    refused = [item for item in rival_results if isinstance(item, PlaneError)]
    assert len(succeeded) == 1 and len(refused) == 1
    assert refused[0].code == "voice_turn_idempotency_conflict"
    assert succeeded[0].submission_id == rival.submission_id
    assert succeeded[0].client_turn_id in (rival.client_turn_id, rival_twin.client_turn_id)
    with database.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM voice_turn")["n"] == 2
        found = repo.get_client_turn_record(
            tx, owner_id=owner_id, client_turn_id=create.client_turn_id
        )
        assert str(found["turn_id"]) == committed[0].turn_id
        accepted = repo.transition_turn(
            tx,
            owner_id=owner_id,
            turn_id=committed[0].turn_id,
            expected_state=VoiceTurnState.RECOGNIZING,
            state=VoiceTurnState.ACCEPTED,
            operation_id=None,
            result_id=None,
            now=clock(tx),
        )
        assert accepted.state is VoiceTurnState.ACCEPTED
    with database.transaction() as tx:
        retried = repo.create_turn(tx, create)
        assert (retried.turn_id, retried.state) == (committed[0].turn_id, VoiceTurnState.ACCEPTED)
        assert tx.fetch_one("SELECT count(*) AS n FROM voice_turn")["n"] == 2
        row = repo.get_turn_record(tx, owner_id=owner_id, turn_id=committed[0].turn_id)
        assert row["state"] == "accepted" and row["accepted_at"] is not None


@pytest.mark.parametrize("backend", BACKENDS)
def test_reconnect_retry_resolves_original_turn_and_refuses_stale_binding(database, backend):
    repo = VoiceRepository()
    owner_id = OWNERS[0] if backend == "client_local" else OWNERS[1]
    with database.transaction() as tx:
        session = start_active_session(repo, tx, owner_id, backend)
        create = turn_create(session)
        original = repo.create_turn(tx, create)
    with database.transaction() as tx:
        reconnected = advance(repo, tx, session, VoiceSessionState.RECONNECTING)
    assert (reconnected.generation, reconnected.chat_context_revision) == (3, 3)
    with database.transaction() as tx:
        replayed = repo.create_turn(tx, create)
        assert (replayed.turn_id, replayed.session_id) == (original.turn_id, session.session_id)
        with pytest.raises(PlaneError) as raised:
            repo.create_turn(tx, turn_create(session))
        assert raised.value.code == "voice_turn_idempotency_conflict"
        with pytest.raises(PlaneError) as raised:
            repo.create_turn(tx, replace(create, request_generation=uid()))
        assert raised.value.code == "voice_turn_idempotency_conflict"
        current = repo.create_turn(tx, turn_create(reconnected))
        assert current.turn_id != original.turn_id
        found = repo.get_client_turn_record(
            tx, owner_id=owner_id, client_turn_id=create.client_turn_id
        )
        assert str(found["turn_id"]) == original.turn_id
        assert int(found["session_generation"]) == session.generation
    with database.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM voice_turn")["n"] == 2


@pytest.mark.parametrize("backend", BACKENDS)
def test_terminal_result_retry_never_reopens_turn_or_session(database, backend):
    repo = VoiceRepository()
    owner_id = OWNERS[0] if backend == "client_local" else OWNERS[1]
    with database.transaction() as tx:
        session = start_active_session(repo, tx, owner_id, backend)
        create = turn_create(session)
        turn = repo.create_turn(tx, create)
        accepted = repo.transition_turn(
            tx,
            owner_id=owner_id,
            turn_id=turn.turn_id,
            expected_state=VoiceTurnState.RECOGNIZING,
            state=VoiceTurnState.ACCEPTED,
            operation_id=None,
            result_id=None,
            now=clock(tx),
        )
        terminal = repo.transition_turn(
            tx,
            owner_id=owner_id,
            turn_id=turn.turn_id,
            expected_state=VoiceTurnState.ACCEPTED,
            state=VoiceTurnState.SUCCEEDED,
            operation_id=None,
            result_id=uid(),
            now=clock(tx),
        )
    assert accepted.accepted_at is not None and terminal.terminal_kind == "succeeded"
    with database.transaction() as tx:
        before = repo.get_turn_record(tx, owner_id=owner_id, turn_id=turn.turn_id)
        with pytest.raises(PlaneError) as raised:
            repo.transition_turn(
                tx,
                owner_id=owner_id,
                turn_id=turn.turn_id,
                expected_state=VoiceTurnState.PROCESSING,
                state=VoiceTurnState.PROCESSING,
                operation_id=None,
                result_id=None,
                now=clock(tx),
            )
        assert raised.value.code == "voice_turn_state_conflict"
        assert dict(repo.get_turn_record(tx, owner_id=owner_id, turn_id=turn.turn_id)) == dict(
            before
        )
        replayed = repo.create_turn(tx, create)
        assert replayed.turn_id == turn.turn_id
        assert replayed.state is VoiceTurnState.SUCCEEDED
        found = repo.get_client_turn_record(
            tx, owner_id=owner_id, client_turn_id=create.client_turn_id
        )
        assert found["state"] == "succeeded" and found["terminal_kind"] == "succeeded"
        ended = repo.end_session(
            tx,
            owner_id=owner_id,
            session_id=session.session_id,
            expected_generation=session.generation,
            reason="user",
            ended_at=clock(tx),
        )
    assert ended.state is VoiceSessionState.ENDED
    with database.transaction() as tx:
        with pytest.raises(PlaneError) as raised:
            advance(repo, tx, ended, VoiceSessionState.ACTIVE)
        assert raised.value.code == "voice_session_state_conflict"
        with pytest.raises(PlaneError) as raised:
            repo.create_turn(tx, turn_create(ended))
        assert raised.value.code == "voice_turn_idempotency_conflict"
        assert tx.fetch_one("SELECT count(*) AS n FROM voice_turn")["n"] == 1
        row = repo.get_session_record(tx, owner_id=owner_id, session_id=session.session_id)
        assert row["state"] == "ended" and row["end_reason"] == "user"


def test_cross_owner_retries_stay_owner_scoped(database):
    repo = VoiceRepository()
    with database.transaction() as tx:
        client_session = start_active_session(repo, tx, OWNERS[0], "client_local")
        factory_session = start_active_session(repo, tx, OWNERS[1], "llm_factory")
        create = turn_create(client_session)
        owned = repo.create_turn(tx, create)
        owned_before = repo.get_turn_record(tx, owner_id=OWNERS[0], turn_id=owned.turn_id)
        assert (
            repo.get_client_turn_record(
                tx, owner_id=OWNERS[1], client_turn_id=create.client_turn_id
            )
            is None
        )
        assert repo.get_turn_record(tx, owner_id=OWNERS[1], turn_id=owned.turn_id) is None
        assert (
            repo.get_session_record(tx, owner_id=OWNERS[1], session_id=client_session.session_id)
            is None
        )
        with pytest.raises(PlaneError) as raised:
            repo.create_turn(tx, replace(create, owner_id=OWNERS[1], turn_id=uid()))
        assert raised.value.code == "voice_turn_idempotency_conflict"
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM voice_turn WHERE user_id = %s", (OWNERS[1],))[
                "n"
            ]
            == 0
        )
        foreign = repo.create_turn(
            tx,
            turn_create(
                factory_session,
                client_turn_id=create.client_turn_id,
                submission_id=create.submission_id,
            ),
        )
        assert foreign.client_turn_id == create.client_turn_id
        assert foreign.owner_id == OWNERS[1]
        assert foreign.turn_id != owned.turn_id
    with database.transaction() as tx:
        assert dict(repo.get_turn_record(tx, owner_id=OWNERS[0], turn_id=owned.turn_id)) == dict(
            owned_before
        )
        assert (
            tx.fetch_one("SELECT count(*) AS n FROM voice_turn WHERE user_id = %s", (OWNERS[0],))[
                "n"
            ]
            == 1
        )
        assert (
            repo.get_client_turn_record(
                tx, owner_id=OWNERS[1], client_turn_id=create.client_turn_id
            )["turn_id"]
            == foreign.turn_id
        )
