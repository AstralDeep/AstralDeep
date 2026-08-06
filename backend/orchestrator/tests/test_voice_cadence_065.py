"""Deterministic cadence and playout evidence tests for Feature 065."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_coordinator import (
    APPROVED_PHRASE_TEXT,
    APPROVED_PHRASE_KEYS,
    CADENCE_HARD_GAP_SECONDS,
    CADENCE_TARGET_SECONDS,
    HANDOFF_BUDGET_SECONDS,
    PREACCEPTANCE_REJECTION_PHRASES,
    AnnouncementFence,
    AnnouncementClaimRequest,
    AnnouncementState,
    AnnouncementStateAdapter,
    CadenceTurnSnapshot,
    CapacityUnavailable,
    ClaimUnavailable,
    ControlProtocolError,
    CoordinatorClock,
    LifecyclePhraseSelector,
    PhraseBook,
    PlayoutCompletion,
    PlayoutEvidenceTracker,
    SpeechCadenceScheduler,
    StaleFence,
    VoiceCoordinatorError,
    deterministic_uuid4,
)


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)
SESSION = "10000000-0000-4000-8000-000000000001"
TURN_1 = "20000000-0000-4000-8000-000000000001"
TURN_2 = "20000000-0000-4000-8000-000000000002"
TURN_3 = "20000000-0000-4000-8000-000000000003"
DEVICE = "30000000-0000-4000-8000-000000000001"
CONNECTION = "40000000-0000-4000-8000-000000000001"


class FakeClock:
    def __init__(self) -> None:
        self.utc = NOW
        self.mono = 100.0

    def utcnow(self) -> datetime:
        return self.utc

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


def _clock(fake: FakeClock) -> CoordinatorClock:
    return CoordinatorClock(utcnow=fake.utcnow, monotonic=fake.monotonic)


def _completion(decision, fake: FakeClock) -> PlayoutCompletion:
    return PlayoutCompletion(
        announcement_id=decision.announcement_id,
        turn_id=decision.turn_id,
        source_finished_at=fake.utc,
        client_finished_at=fake.utc,
        completed_at=fake.utc,
        completed_monotonic=fake.mono,
    )


def _add_turn(
    scheduler: SpeechCadenceScheduler,
    turn_id: str = TURN_1,
    *,
    sequence: int = 0,
    next_due_at: datetime | None = None,
) -> None:
    scheduler.add_turn(
        session_id=SESSION,
        turn_id=turn_id,
        generation=2,
        media_grant_revision=3,
        announcement_sequence=sequence,
        next_due_at=next_due_at,
    )


def test_approved_catalog_and_sanitized_lifecycle_selection_are_truthful() -> None:
    selector = LifecyclePhraseSelector()

    assert APPROVED_PHRASE_TEXT["on_it"] == "On it!"
    assert selector.select(
        lifecycle="accepted",
        stable_id=TURN_1,
        sequence=1,
        last_phrase_key=None,
    ).kind == "acknowledgement"
    assert selector.select(
        lifecycle="waiting_on_user",
        stable_id=TURN_1,
        sequence=2,
        last_phrase_key=None,
        waiting_reason="login",
    ).phrase_key == "sign_in_needed"
    assert selector.select(
        lifecycle="failed",
        stable_id=TURN_1,
        sequence=3,
        last_phrase_key=None,
    ).kind == "failure"
    result = selector.select(
        lifecycle="succeeded",
        stable_id=TURN_1,
        sequence=4,
        last_phrase_key=None,
    )
    assert result.kind == "result"
    assert result.phrase_key is None and result.text is None

    first = selector.select(
        lifecycle="processing",
        stable_id=TURN_1,
        sequence=5,
        last_phrase_key=None,
    )
    second = selector.select(
        lifecycle="processing",
        stable_id=TURN_1,
        sequence=6,
        last_phrase_key=first.phrase_key,
    )
    assert second.phrase_key != first.phrase_key
    assert second.text == APPROVED_PHRASE_TEXT[second.phrase_key]
    with pytest.raises(ControlProtocolError, match="invalid_lifecycle_state"):
        selector.select(
            lifecycle="tool_completed_with_secret",
            stable_id=TURN_1,
            sequence=7,
            last_phrase_key=None,
        )
    with pytest.raises(ControlProtocolError, match="invalid_waiting_reason"):
        selector.select(
            lifecycle="waiting_on_user",
            stable_id=TURN_1,
            sequence=7,
            last_phrase_key=None,
            waiting_reason="paste_password_here",
        )


def test_phrase_catalog_rejects_unapproved_or_ambiguous_configuration() -> None:
    with pytest.raises(ValueError, match="invalid_phrase_book"):
        PhraseBook({})
    with pytest.raises(ValueError, match="invalid_phrase_kind"):
        PhraseBook({"result": ("on_it",)})
    with pytest.raises(ValueError, match="invalid_phrase_keys"):
        PhraseBook({"progress": []})
    with pytest.raises(ValueError, match="unapproved_phrase_key"):
        PhraseBook({"progress": ("on_it", "still_working")})
    with pytest.raises(ValueError, match="duplicate_phrase_key"):
        PhraseBook({"progress": ("still_working", "still_working")})
    with pytest.raises(ValueError, match="insufficient_phrase_variation"):
        PhraseBook({"progress": ("still_working",)})

    book = PhraseBook({"waiting": ("action_needed",)})
    with pytest.raises(ValueError, match="invalid_phrase_stable_id"):
        book.select(
            kind="waiting", stable_id="", sequence=1, last_phrase_key=None
        )
    with pytest.raises(ValueError, match="invalid_phrase_key"):
        book.text(1)  # type: ignore[arg-type]
    with pytest.raises(ClaimUnavailable, match="phrase_key_unavailable"):
        book.text("unknown_key")


def test_preacceptance_phrases_never_contaminate_normal_lifecycle_selection(
) -> None:
    selector = LifecyclePhraseSelector()
    preacceptance_keys = {
        phrase_key
        for _kind, phrase_key in PREACCEPTANCE_REJECTION_PHRASES.values()
    }

    for sequence in range(1, 25):
        refused = selector.select(
            lifecycle="refused",
            stable_id=f"normal-refusal-{sequence}",
            sequence=sequence,
            last_phrase_key=None,
        )
        waiting = selector.select(
            lifecycle="waiting_on_user",
            stable_id=f"normal-waiting-{sequence}",
            sequence=sequence,
            last_phrase_key=None,
        )
        assert refused.phrase_key not in preacceptance_keys
        assert waiting.phrase_key not in preacceptance_keys

    request = AnnouncementClaimRequest(
        session_id=SESSION,
        turn_id=TURN_1,
        generation=2,
        claim_id=deterministic_uuid4("preacceptance-claim", TURN_1),
        kind="waiting",
        quantum_role="single",
        expected_sequence=0,
        expected_result_reserved_samples=0,
        expected_phrase_key="llm_setup_needed",
        expected_media_grant_revision=3,
        authorized_preacceptance_rejection_reason="permission_denied",
    )
    assert request.expected_phrase_key == "llm_setup_needed"
    with pytest.raises(
        ValueError,
        match="invalid_(expected_phrase_key|preacceptance_rejection_authorization)",
    ):
        replace(request, kind="refusal")

    selector = LifecyclePhraseSelector()
    with pytest.raises(ControlProtocolError, match="unexpected_waiting_reason"):
        selector.select(
            lifecycle="processing",
            stable_id=TURN_1,
            sequence=1,
            last_phrase_key=None,
            waiting_reason="approval",
        )


def test_claim_fence_preserves_the_scheduler_selected_waiting_phrase() -> None:
    adapter = AnnouncementStateAdapter(PhraseBook(APPROVED_PHRASE_KEYS))
    request = AnnouncementClaimRequest(
        session_id=SESSION,
        turn_id=TURN_1,
        generation=2,
        claim_id=deterministic_uuid4("waiting-claim", TURN_1),
        kind="waiting",
        quantum_role="single",
        expected_sequence=0,
        expected_result_reserved_samples=0,
        expected_phrase_key="sign_in_needed",
    )

    mutation = adapter.claim(AnnouncementState(generation=2), request, now=NOW)

    assert mutation.claim.phrase_key == "sign_in_needed"
    assert mutation.state.last_phrase_key == "sign_in_needed"
    replay = adapter.claim(
        mutation.state,
        replace(request, expected_sequence=1),
        now=NOW,
    )
    assert replay.claim.phrase_key == "sign_in_needed"
    with pytest.raises(ValueError, match="invalid_expected_phrase_key"):
        replace(request, expected_phrase_key="on_it")


def test_exactly_one_ack_then_14_second_target_and_hard_20_second_deadline() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler)

    acknowledgement = scheduler.next_decision()
    assert acknowledgement is not None
    assert acknowledgement.kind == "acknowledgement"
    assert acknowledgement.target_start_monotonic == 100.0
    assert acknowledgement.latest_start_monotonic == 101.5
    assert scheduler.next_decision() == acknowledgement
    scheduler.start(acknowledgement)
    assert scheduler.next_decision() is None
    fake.advance(0.5)
    scheduler.finish(acknowledgement, _completion(acknowledgement, fake))

    snapshot = scheduler.snapshot(TURN_1)
    assert snapshot.announcement_sequence == 1
    assert snapshot.acknowledgement_started is True
    assert snapshot.next_due_at == fake.utc + timedelta(seconds=14)
    fake.advance(CADENCE_TARGET_SECONDS - 0.001)
    assert scheduler.next_decision() is None
    fake.advance(0.001)
    progress = scheduler.next_decision()
    assert progress is not None and progress.kind == "progress"
    assert progress.target_start_monotonic == pytest.approx(114.5)
    assert progress.latest_start_monotonic == pytest.approx(
        100.5 + CADENCE_HARD_GAP_SECONDS
    )
    scheduler.start(progress)
    fake.advance(1)
    scheduler.finish(progress, _completion(progress, fake))
    fake.advance(CADENCE_TARGET_SECONDS)
    varied = scheduler.next_decision()
    assert varied is not None and varied.kind == "progress"
    assert varied.phrase_key != progress.phrase_key
    assert scheduler.snapshot(TURN_1).announcement_sequence == 2


def test_unavailable_origin_fences_active_cadence_without_terminalizing_work() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1)
    _add_turn(scheduler, TURN_2)
    active = scheduler.next_decision()
    assert active is not None
    scheduler.start(active)

    assert scheduler.abandon_turn(active.turn_id) is True
    assert not scheduler.has_turn(active.turn_id)
    assert scheduler.active_turn_count == 1
    survivor = TURN_2 if active.turn_id == TURN_1 else TURN_1
    assert scheduler.has_turn(survivor)


def test_restart_restores_due_time_without_repeating_acknowledgement() -> None:
    fake = FakeClock()
    first = SpeechCadenceScheduler(_clock(fake))
    _add_turn(first)
    acknowledgement = first.next_decision()
    assert acknowledgement is not None
    first.start(acknowledgement)
    fake.advance(0.25)
    first.finish(acknowledgement, _completion(acknowledgement, fake))
    durable = first.snapshot(TURN_1)

    restarted = SpeechCadenceScheduler(_clock(fake))
    restarted.restore_turn(durable)
    assert restarted.next_decision() is None
    fake.advance(CADENCE_TARGET_SECONDS)
    recovered = restarted.next_decision()
    assert recovered is not None
    assert recovered.kind == "progress"
    assert recovered.sequence == 2
    assert recovered.announcement_id == deterministic_uuid4(
        "voice-announcement-v1",
        SESSION,
        TURN_1,
        "2",
        "2",
        "progress",
        "single",
        "0",
    )

    with pytest.raises(ValueError, match="invalid_cadence_recovery"):
        replace(durable, acknowledgement_started=False)


def test_terminal_observed_before_first_poll_still_preserves_exactly_one_ack() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler)
    scheduler.set_lifecycle(TURN_1, "failed")

    acknowledgement = scheduler.next_decision()
    assert acknowledgement is not None
    assert acknowledgement.kind == "acknowledgement"
    scheduler.start(acknowledgement)
    scheduler.finish(acknowledgement, _completion(acknowledgement, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS)
    failure = scheduler.next_decision()
    assert failure is not None
    assert failure.kind == "failure"
    assert failure.sequence == 2


def test_scheduler_rejects_stale_and_untrusted_control_transitions() -> None:
    fake = FakeClock()
    with pytest.raises(TypeError, match="CoordinatorClock"):
        SpeechCadenceScheduler(object())  # type: ignore[arg-type]
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler)
    with pytest.raises(VoiceCoordinatorError, match="already_registered"):
        _add_turn(scheduler)
    with pytest.raises(StaleFence, match="not_registered"):
        scheduler.snapshot(TURN_2)
    with pytest.raises(ControlProtocolError, match="invalid_lifecycle_state"):
        scheduler.set_lifecycle(TURN_1, "accepted")
    with pytest.raises(ControlProtocolError, match="unexpected_waiting_reason"):
        scheduler.set_lifecycle(TURN_1, "processing", waiting_reason="approval")
    with pytest.raises(ValueError, match="invalid_speech_muted"):
        scheduler.set_muted(TURN_1, "yes")  # type: ignore[arg-type]
    assert scheduler.set_muted(TURN_1, False) is False

    offered = scheduler.next_decision()
    assert offered is not None
    with pytest.raises(StaleFence, match="stale_cadence_decision"):
        scheduler.start(replace(offered, sequence=99))
    scheduler.start(offered)
    with pytest.raises(StaleFence, match="stale_cadence_stream"):
        scheduler.finish(replace(offered, sequence=99), _completion(offered, fake))
    with pytest.raises(ControlProtocolError, match="playout_completion_mismatch"):
        scheduler.finish(
            offered,
            replace(_completion(offered, fake), announcement_id=SESSION),
        )
    with pytest.raises(ControlProtocolError, match="future_playout_completion"):
        scheduler.finish(
            offered,
            replace(
                _completion(offered, fake),
                completed_monotonic=fake.mono + 1,
            ),
        )


def test_wait_mute_terminal_and_progress_preemption_cancel_stale_audio() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler)
    acknowledgement = scheduler.next_decision()
    assert acknowledgement is not None
    scheduler.start(acknowledgement)
    scheduler.finish(acknowledgement, _completion(acknowledgement, fake))

    scheduler.set_lifecycle(TURN_1, "waiting_on_user", waiting_reason="approval")
    fake.advance(HANDOFF_BUDGET_SECONDS)
    waiting = scheduler.next_decision()
    assert waiting is not None and waiting.kind == "waiting"
    scheduler.start(waiting)
    fake.advance(0.5)
    scheduler.finish(waiting, _completion(waiting, fake))
    fake.advance(30)
    assert scheduler.next_decision() is None

    scheduler.set_lifecycle(TURN_1, "processing")
    fake.advance(CADENCE_TARGET_SECONDS)
    progress = scheduler.next_decision()
    assert progress is not None and progress.kind == "progress"
    scheduler.start(progress)
    assert scheduler.set_lifecycle(TURN_1, "succeeded") is True
    terminal = scheduler.next_decision()
    assert terminal is not None and terminal.kind == "result"

    scheduler.set_muted(TURN_1, True)
    assert scheduler.next_decision() is None
    scheduler.set_muted(TURN_1, False)
    assert scheduler.next_decision() is None  # muted terminal is not burst-replayed


def test_two_turn_arbitration_is_serial_and_preserves_positive_handoff_budget() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    due = NOW + timedelta(seconds=14)
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=due)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=due)
    with pytest.raises(CapacityUnavailable, match="active_voice_turn_limit"):
        _add_turn(scheduler, TURN_3, sequence=1, next_due_at=due)

    fake.advance(14)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    assert scheduler.next_decision() is None
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    second = scheduler.next_decision()
    assert second is not None and second.turn_id != first.turn_id
    assert fake.mono - 100.0 == pytest.approx(18.0)
    assert fake.mono <= second.latest_start_monotonic

    scheduler.start(second)
    fake.advance(4)
    scheduler.finish(second, _completion(second, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    # There is no equally-due pending turn, so ordinary scheduler latency does
    # not manufacture a false handoff violation.
    assert scheduler.next_decision() is None


def test_coincident_terminals_use_short_openings_and_second_starts_by_1_75s() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW + timedelta(seconds=14))
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW + timedelta(seconds=14))
    scheduler.set_lifecycle(TURN_1, "succeeded")
    scheduler.set_lifecycle(TURN_2, "failed")

    first = scheduler.next_decision()
    assert first is not None and first.terminal
    scheduler.start(first)
    fake.advance(1.5)
    scheduler.finish(first, _completion(first, fake))
    fake.advance(0.25)
    second = scheduler.next_decision()
    assert second is not None and second.terminal
    assert second.turn_id != first.turn_id
    assert fake.mono == pytest.approx(101.75)


def test_terminal_claims_reserve_only_a_1_5_second_opening() -> None:
    adapter = AnnouncementStateAdapter(PhraseBook(APPROVED_PHRASE_KEYS))
    mutation = adapter.claim(
        AnnouncementState(generation=2),
        AnnouncementClaimRequest(
            session_id=SESSION,
            turn_id=TURN_1,
            generation=2,
            claim_id=deterministic_uuid4("terminal-claim", TURN_1),
            kind="failure",
            quantum_role="single",
            expected_sequence=0,
            expected_result_reserved_samples=0,
        ),
        now=NOW,
    )
    assert mutation.claim.max_duration_samples == 36_000
    assert mutation.claim.result_reserved_samples_after is None


def _fence(
    *,
    announcement_id: str | None = None,
    announcement_sequence: int = 1,
    turn_id: str | None = TURN_1,
    transport: str = "livekit",
    kind: str = "progress",
    quantum_role: str = "single",
    quantum_index: int = 0,
    result_reserved_samples_after: int | None = None,
    max_duration_samples: int = 96_000,
) -> AnnouncementFence:
    return AnnouncementFence(
        session_id=SESSION,
        generation=2,
        media_grant_revision=3,
        announcement_id=announcement_id
        or deterministic_uuid4("playout", str(turn_id), str(announcement_sequence)),
        announcement_sequence=announcement_sequence,
        turn_id=turn_id,
        kind=kind,
        quantum_role=quantum_role,
        quantum_index=quantum_index,
        result_reserved_samples_after=result_reserved_samples_after,
        max_duration_samples=max_duration_samples,
        worker_identity="voice-worker-a",
        device_id=DEVICE,
        connection_generation=CONNECTION,
        transport=transport,
    )


def _manifest(fence: AnnouncementFence, **changes: object) -> dict[str, object]:
    frame: dict[str, object] = {
        "type": "voice_announcement_media",
        "schema_version": "1",
        "session_id": fence.session_id,
        "generation": fence.generation,
        "media_grant_revision": fence.media_grant_revision,
        "announcement_id": fence.announcement_id,
        "announcement_sequence": fence.announcement_sequence,
        "turn_id": fence.turn_id,
        "kind": fence.kind,
        "quantum_role": fence.quantum_role,
        "quantum_index": fence.quantum_index,
        "transport": fence.transport,
        "worker_identity": fence.worker_identity,
        "sample_rate_hz": 24_000,
        "duration_samples": 24_000,
    }
    if fence.result_reserved_samples_after is not None:
        frame["result_reserved_samples_after"] = fence.result_reserved_samples_after
    if fence.transport == "livekit":
        frame.update({"track_sid": "TR_voice_1", "track_name": "voice-1"})
    else:
        frame.update({"first_media_sequence": 100, "last_media_sequence": 24_099})
    frame.update(changes)
    return frame


def _source(
    fence: AnnouncementFence,
    phase: str,
    sequence: int,
    **changes: object,
) -> dict[str, object]:
    frame: dict[str, object] = {
        "type": f"speech_{phase}",
        "schema_version": "1",
        "message_id": deterministic_uuid4(
            "source-event", fence.announcement_id, str(sequence)
        ),
        "session_id": fence.session_id,
        "generation": fence.generation,
        "sequence": sequence,
        "sent_at": NOW.isoformat(),
        "announcement_id": fence.announcement_id,
        "announcement_sequence": fence.announcement_sequence,
        "media_grant_revision": fence.media_grant_revision,
        "turn_id": fence.turn_id,
        "kind": fence.kind,
        "quantum_role": fence.quantum_role,
        "quantum_index": fence.quantum_index,
        "occurred_at": NOW.isoformat(),
    }
    if fence.result_reserved_samples_after is not None:
        frame["result_reserved_samples_after"] = fence.result_reserved_samples_after
    if phase == "finished":
        frame["duration_ms"] = 1_000
    frame.update(changes)
    return frame


def _client(
    fence: AnnouncementFence,
    phase: str,
    sequence: int,
    **changes: object,
) -> dict[str, object]:
    frame: dict[str, object] = {
        "type": "voice_playout_event",
        "schema_version": "1",
        "device_id": fence.device_id,
        "connection_generation": fence.connection_generation,
        "session_id": fence.session_id,
        "generation": fence.generation,
        "media_grant_revision": fence.media_grant_revision,
        "announcement_id": fence.announcement_id,
        "announcement_sequence": fence.announcement_sequence,
        "turn_id": fence.turn_id,
        "kind": fence.kind,
        "quantum_role": fence.quantum_role,
        "quantum_index": fence.quantum_index,
        "phase": phase,
        "client_sequence": sequence,
        "observed_at": NOW.isoformat(),
    }
    if fence.result_reserved_samples_after is not None:
        frame["result_reserved_samples_after"] = fence.result_reserved_samples_after
    frame.update(changes)
    return frame


def test_announcement_fence_enforces_greeting_and_result_quantum_contracts() -> None:
    greeting = _fence(
        announcement_id=deterministic_uuid4("greeting", SESSION),
        turn_id=None,
        kind="greeting",
    )
    assert greeting.turn_id is None
    opening = _fence(
        kind="result",
        quantum_role="result_opening",
        result_reserved_samples_after=36_000,
        max_duration_samples=36_000,
    )
    continuation = _fence(
        announcement_id=deterministic_uuid4("continuation", TURN_1),
        kind="result",
        quantum_role="result_continuation",
        quantum_index=1,
        result_reserved_samples_after=132_000,
    )
    assert opening.quantum_index == 0
    assert continuation.quantum_index == 1

    cases = (
        ({"kind": "invented"}, "invalid_announcement_kind"),
        ({"kind": "greeting"}, "invalid_announcement_turn"),
        ({"kind": "result"}, "invalid_single_quantum"),
        (
            {
                "kind": "result",
                "quantum_role": "result_opening",
                "result_reserved_samples_after": 36_000,
            },
            "invalid_result_opening",
        ),
        (
            {
                "kind": "result",
                "quantum_role": "result_continuation",
                "result_reserved_samples_after": 96_000,
            },
            "invalid_result_continuation",
        ),
        ({"quantum_role": "other"}, "invalid_quantum_role"),
        ({"transport": "untrusted"}, "invalid_voice_transport"),
    )
    for changes, code in cases:
        values = {
            "announcement_id": deterministic_uuid4("invalid-fence", code),
            **changes,
        }
        with pytest.raises(ValueError, match=code):
            _fence(**values)


def test_playout_completion_uses_later_server_receipt_not_client_wall_clock() -> None:
    fake = FakeClock()
    tracker = PlayoutEvidenceTracker(_clock(fake))
    fence = _fence()
    tracker.register(fence)
    tracker.record_manifest(_manifest(fence))
    tracker.record_source(
        _source(fence, "started", 0), worker_identity="voice-worker-a"
    )
    fake.advance(0.1)
    tracker.record_client(
        _client(
            fence,
            "started",
            1,
            observed_at="2099-01-01T00:00:00+00:00",
        )
    )
    fake.advance(0.9)
    assert (
        tracker.record_source(
            _source(fence, "finished", 1), worker_identity="voice-worker-a"
        )
        is None
    )
    fake.advance(1)
    completion = tracker.record_client(
        _client(
            fence,
            "finished",
            2,
            observed_at="2000-01-01T00:00:00+00:00",
        )
    )

    assert completion is not None
    assert completion.completed_at == NOW + timedelta(seconds=2)
    assert completion.completed_monotonic == 102.0
    assert tracker.health(fence.announcement_id).status == "completed"


def test_playout_rejects_stale_oversized_duplicate_and_out_of_order_events() -> None:
    fake = FakeClock()
    tracker = PlayoutEvidenceTracker(_clock(fake))
    fence = _fence()
    tracker.register(fence)
    with pytest.raises(ControlProtocolError, match="manifest_sample_budget_exceeded"):
        tracker.record_manifest(_manifest(fence, duration_samples=96_001))
    tracker.record_manifest(_manifest(fence))
    with pytest.raises(ControlProtocolError, match="duplicate_manifest"):
        tracker.record_manifest(_manifest(fence))
    with pytest.raises(ControlProtocolError, match="source_event_out_of_order"):
        tracker.record_source(
            _source(fence, "finished", 0), worker_identity="voice-worker-a"
        )
    with pytest.raises(ControlProtocolError, match="stale_generation"):
        tracker.record_source(
            _source(fence, "started", 0, generation=1),
            worker_identity="voice-worker-a",
        )
    tracker.record_source(
        _source(fence, "started", 0), worker_identity="voice-worker-a"
    )
    with pytest.raises(ControlProtocolError, match="source_event_out_of_order"):
        tracker.record_source(
            _source(fence, "started", 1), worker_identity="voice-worker-a"
        )
    with pytest.raises(ControlProtocolError, match="client_frame_too_large"):
        tracker.record_client(_client(fence, "started", 1, padding="x" * 3_000))
    tracker.record_client(_client(fence, "started", 1))
    with pytest.raises(ControlProtocolError, match="client_sequence_out_of_order"):
        tracker.record_client(_client(fence, "finished", 1))
    with pytest.raises(ControlProtocolError, match="playout_fence_mismatch"):
        tracker.record_client(_client(fence, "finished", 2, quantum_index=1))


def test_missing_client_finish_degrades_and_never_advances_cadence() -> None:
    fake = FakeClock()
    tracker = PlayoutEvidenceTracker(_clock(fake), missing_event_timeout_seconds=5)
    fence = _fence()
    tracker.register(fence)
    tracker.record_manifest(_manifest(fence))
    tracker.record_source(
        _source(fence, "started", 0), worker_identity="voice-worker-a"
    )
    tracker.record_client(_client(fence, "started", 1))
    fake.advance(1)
    tracker.record_source(
        _source(fence, "finished", 1), worker_identity="voice-worker-a"
    )
    fake.advance(4)
    assert tracker.expire_missing() == (fence.announcement_id,)
    health = tracker.health(fence.announcement_id)
    assert health.status == "degraded"
    assert health.reason == "missing_client_finish"
    assert health.completion is None
    with pytest.raises(ControlProtocolError, match="playout_degraded"):
        tracker.record_client(_client(fence, "finished", 2))


def test_playout_failure_interruption_result_reservation_and_missing_matrix() -> None:
    fake = FakeClock()
    result = _fence(
        kind="result",
        quantum_role="result_opening",
        result_reserved_samples_after=36_000,
        max_duration_samples=36_000,
    )
    tracker = PlayoutEvidenceTracker(_clock(fake))
    tracker.register(result)
    with pytest.raises(ControlProtocolError, match="result_reservation_mismatch"):
        tracker.record_manifest(
            _manifest(result, result_reserved_samples_after=35_999)
        )
    tracker.record_source(
        _source(result, "failed", 0, reason="synthesis_failed"),
        worker_identity="voice-worker-a",
    )
    assert tracker.health(result.announcement_id).status == "failed"

    interrupted = _fence(
        announcement_id=deterministic_uuid4("interrupted", TURN_1),
        announcement_sequence=2,
    )
    tracker = PlayoutEvidenceTracker(_clock(fake))
    tracker.register(interrupted)
    tracker.record_manifest(_manifest(interrupted))
    tracker.record_source(
        _source(interrupted, "started", 0), worker_identity="voice-worker-a"
    )
    tracker.record_client(_client(interrupted, "started", 1))
    tracker.record_source(
        _source(interrupted, "interrupted", 1, reason="terminal"),
        worker_identity="voice-worker-a",
    )
    assert tracker.health(interrupted.announcement_id).status == "interrupted"

    fake = FakeClock()
    tracker = PlayoutEvidenceTracker(_clock(fake), missing_event_timeout_seconds=5)
    missing_manifest = _fence(
        announcement_id=deterministic_uuid4("missing", "manifest"),
        announcement_sequence=10,
    )
    missing_source = _fence(
        announcement_id=deterministic_uuid4("missing", "source"),
        announcement_sequence=11,
    )
    missing_client = _fence(
        announcement_id=deterministic_uuid4("missing", "client"),
        announcement_sequence=12,
    )
    missing_source_finish = _fence(
        announcement_id=deterministic_uuid4("missing", "source-finish"),
        announcement_sequence=13,
    )
    for fence in (
        missing_manifest,
        missing_source,
        missing_client,
        missing_source_finish,
    ):
        tracker.register(fence)
    for fence in (missing_source, missing_client, missing_source_finish):
        tracker.record_manifest(_manifest(fence))
    tracker.record_source(
        _source(missing_client, "started", 0), worker_identity="voice-worker-a"
    )
    tracker.record_source(
        _source(missing_source_finish, "started", 1),
        worker_identity="voice-worker-a",
    )
    tracker.record_client(_client(missing_source_finish, "started", 1))
    tracker.record_client(_client(missing_source_finish, "finished", 2))
    fake.advance(5)
    assert set(tracker.expire_missing()) == {
        missing_manifest.announcement_id,
        missing_source.announcement_id,
        missing_client.announcement_id,
        missing_source_finish.announcement_id,
    }
    assert tracker.health(missing_manifest.announcement_id).reason == "missing_manifest"
    assert tracker.health(missing_source.announcement_id).reason == "missing_source_start"
    assert tracker.health(missing_client.announcement_id).reason == "missing_client_start"
    assert (
        tracker.health(missing_source_finish.announcement_id).reason
        == "missing_source_finish"
    )


def test_client_playout_rate_is_eight_per_second_and_watch_range_is_exact() -> None:
    fake = FakeClock()
    tracker = PlayoutEvidenceTracker(_clock(fake))
    for index in range(9):
        fence = _fence(
            announcement_id=deterministic_uuid4("rate", str(index)),
            announcement_sequence=index + 1,
        )
        tracker.register(fence)
        tracker.record_manifest(_manifest(fence))
        event = _client(fence, "started", index + 1)
        if index < 8:
            tracker.record_client(event)
        else:
            with pytest.raises(ControlProtocolError, match="client_playout_rate_exceeded"):
                tracker.record_client(event)

    watch = _fence(
        announcement_id=deterministic_uuid4("watch", "1"),
        announcement_sequence=20,
        transport="watch_pcm_websocket",
    )
    tracker.register(watch)
    with pytest.raises(ControlProtocolError, match="watch_sample_range_mismatch"):
        tracker.record_manifest(_manifest(watch, last_media_sequence=24_100))


def test_scheduler_fails_honestly_after_hard_deadline() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    fake.advance(CADENCE_HARD_GAP_SECONDS + 0.001)
    with pytest.raises(VoiceCoordinatorError, match="cadence_deadline_exceeded"):
        scheduler.next_decision()


def test_scheduler_starts_due_handoff_without_consuming_its_latency_budget() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    second = scheduler.next_decision()
    assert second is not None
    assert second.turn_id != first.turn_id
    assert fake.mono <= second.latest_start_monotonic


def test_missed_handoff_budget_defers_stale_progress_instead_of_failing() -> None:
    # One missed 250 ms stream handoff used to latch the scheduler failed
    # forever (every later call raised speech_scheduler_failed), killing all
    # speech for the rest of the session. It now degrades: the stale
    # ordinary progress quantum is dropped, its cadence re-anchors at now,
    # and the scheduler keeps working.
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    assert scheduler.next_decision() is None
    delay = scheduler.next_wake_delay()
    assert delay is not None and delay <= CADENCE_TARGET_SECONDS
    fake.advance(CADENCE_TARGET_SECONDS + 0.001)
    resumed = scheduler.next_decision()
    assert resumed is not None
    scheduler.start(resumed)
    scheduler.finish(resumed, _completion(resumed, fake))


def test_start_forgives_handoff_budget_consumed_by_reservation() -> None:
    # Reservation/preparation occurs between offer and start in the runner
    # and holds a durable announcement claim by then. A slow reservation
    # therefore speaks the already-selected quantum late instead of failing
    # the stream; the genuinely-unusable bound is the latest-start check.
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    second = scheduler.next_decision()
    assert second is not None and second.turn_id != first.turn_id

    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    scheduler.start(second)
    fake.advance(4)
    scheduler.finish(second, _completion(second, fake))
    assert scheduler.next_decision() is None


def test_second_consecutive_handoff_miss_speaks_deferred_progress_late() -> None:
    # Starvation bound: under a sustained missed-handoff regime a turn is
    # never deferred twice in a row — on the second consecutive miss its
    # progress quantum stays due and is spoken late. The degrade counters
    # exist so the runner can log a recurrence (the pre-fix raise was the
    # signal that diagnosed the 2026-08-05 live failure).
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    assert scheduler.next_decision() is None
    assert scheduler.handoff_degrades == 1
    assert scheduler.deferred_quanta == 1

    fake.advance(CADENCE_TARGET_SECONDS + 0.001)
    second = scheduler.next_decision()
    assert second is not None and second.turn_id == first.turn_id
    scheduler.start(second)
    fake.advance(4)
    scheduler.finish(second, _completion(second, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    third = scheduler.next_decision()
    assert third is not None and third.turn_id != first.turn_id
    assert scheduler.handoff_degrades == 2
    assert scheduler.deferred_quanta == 1
    scheduler.start(third)


def test_terminal_announcement_survives_missed_handoff_budget() -> None:
    # The live 2026-08-05 failure: contention between overlapping turns
    # missed one handoff deadline and the terminal announcement was lost
    # (voice_terminal_announcement_unavailable). The terminal quantum must
    # survive the missed budget and still be spoken.
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.set_lifecycle(TURN_2, "succeeded")
    scheduler.finish(first, _completion(first, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS + 0.001)
    decision = scheduler.next_decision()
    assert decision is not None
    assert decision.turn_id == TURN_2
    assert decision.terminal
    scheduler.start(decision)
    fake.advance(1)
    scheduler.finish(decision, _completion(decision, fake))


def test_scheduler_still_fails_when_handoff_wake_misses_true_hard_deadline() -> None:
    fake = FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    _add_turn(scheduler, TURN_1, sequence=1, next_due_at=NOW)
    _add_turn(scheduler, TURN_2, sequence=1, next_due_at=NOW)
    first = scheduler.next_decision()
    assert first is not None
    scheduler.start(first)
    fake.advance(4)
    scheduler.finish(first, _completion(first, fake))
    fake.advance(2.001)
    with pytest.raises(VoiceCoordinatorError, match="cadence_deadline_exceeded"):
        scheduler.next_decision()


def test_cadence_snapshot_validation_rejects_untrusted_recovery_state() -> None:
    with pytest.raises(ValueError, match="invalid_cadence_recovery"):
        CadenceTurnSnapshot(
            session_id=SESSION,
            turn_id=TURN_1,
            generation=2,
            media_grant_revision=3,
            announcement_sequence=0,
            last_phrase_key=None,
            next_due_at=NOW,
            lifecycle="processing",
            acknowledgement_started=True,
            muted=False,
            terminal=False,
        )
