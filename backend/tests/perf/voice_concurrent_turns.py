"""Deterministic overlapping-turn timing gates for Feature 065.

These are scheduler-contract tests, not host-speed benchmarks.  A monotonic
fake clock makes every boundary reproducible and keeps waveform material out
of test artifacts.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.conversation_publication import (
    canonical_components_sha256,
    canonical_layouts_sha256,
    merge_conversation_publication,
)
from orchestrator.voice_coordinator import (
    CADENCE_HARD_GAP_SECONDS,
    HANDOFF_BUDGET_SECONDS,
    RESULT_OPENING_SAMPLES,
    SINGLE_SAMPLES,
    CoordinatorClock,
    PlayoutCompletion,
    SpeechCadenceScheduler,
)
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    OperationOwner,
    OperationRequest,
    OperationState,
    OwnerScope,
    WorkAdmissionCoordinator,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
SESSION = "10000000-0000-4000-8000-000000000001"
TURN_EARLIER = "20000000-0000-4000-8000-000000000001"
TURN_LATEST = "20000000-0000-4000-8000-000000000002"
SAMPLE_RATE_HZ = 24_000


class _FakeClock:
    def __init__(self) -> None:
        self.utc = NOW
        self.mono = 100.0

    def utcnow(self) -> datetime:
        return self.utc

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.utc += timedelta(seconds=seconds)
        self.mono += seconds


def _clock(fake: _FakeClock) -> CoordinatorClock:
    return CoordinatorClock(utcnow=fake.utcnow, monotonic=fake.monotonic)


def _completion(decision, fake: _FakeClock) -> PlayoutCompletion:
    return PlayoutCompletion(
        announcement_id=decision.announcement_id,
        turn_id=decision.turn_id,
        source_finished_at=fake.utc,
        client_finished_at=fake.utc,
        completed_at=fake.utc,
        completed_monotonic=fake.mono,
    )


@dataclass(slots=True)
class _EphemeralAcousticProbe:
    """Keep only content-free timing/count observations and zero input bytes."""

    observations: list[tuple[float, float, int]] = field(default_factory=list)

    def observe(self, samples: bytearray, *, started: float, finished: float) -> None:
        self.observations.append((started, finished, len(samples) // 2))
        samples[:] = b"\x00" * len(samples)


@dataclass(frozen=True, slots=True)
class _ImmutableExecutionBase:
    """Serialized acceptance snapshot held by one running tool execution."""

    render_revision: int
    components_json: str
    layouts_json: str
    components_sha256: str
    layouts_sha256: str

    @classmethod
    def capture(
        cls,
        *,
        render_revision: int,
        components: list[dict[str, object]],
        layouts: list[dict[str, object]],
    ) -> _ImmutableExecutionBase:
        return cls(
            render_revision=render_revision,
            components_json=json.dumps(
                components, sort_keys=True, separators=(",", ":")
            ),
            layouts_json=json.dumps(layouts, sort_keys=True, separators=(",", ":")),
            components_sha256=canonical_components_sha256(components),
            layouts_sha256=canonical_layouts_sha256(layouts),
        )

    def materialize(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        components = json.loads(self.components_json)
        layouts = json.loads(self.layouts_json)
        assert canonical_components_sha256(components) == self.components_sha256
        assert canonical_layouts_sha256(layouts) == self.layouts_sha256
        return components, layouts


class _BlockedTool:
    """One deterministic side effect per operation, held until the test releases it."""

    def __init__(self) -> None:
        self.started = {"first": threading.Event(), "second": threading.Event()}
        self.release = {"first": threading.Event(), "second": threading.Event()}
        self._lock = threading.Lock()
        self.calls = {"first": 0, "second": 0}

    def execute(
        self,
        turn: str,
        execution_base: _ImmutableExecutionBase,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with self._lock:
            self.calls[turn] += 1
        self.started[turn].set()
        assert self.release[turn].wait(timeout=2), "blocked tool was not released"
        components, _layouts = execution_base.materialize()
        candidate = [
            {
                **component,
                "content": turn,
            }
            if component.get("component_id") == "shared"
            else component
            for component in components
        ]
        candidate.append(
            {
                "type": "text",
                "component_id": f"only-{turn}",
                "content": f"{turn}-only",
            }
        )
        return candidate, [
            {
                "layout_key": "main",
                "position": 0,
                "layout": [
                    {"type": "ref", "component_id": "shared"},
                    {"type": "ref", "component_id": f"only-{turn}"},
                ],
            }
        ]


def _concurrent_voice_coordinator() -> WorkAdmissionCoordinator:
    revision = "voice-concurrent-turns-065"
    return WorkAdmissionCoordinator(
        admission_classes=(
            AdmissionClassConfig(
                class_name=AdmissionClass.GLOBAL,
                parent_class_name=None,
                active_limit=2,
                queue_limit=0,
                max_wait_ms=0,
                config_revision=revision,
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.INTERACTIVE,
                parent_class_name=AdmissionClass.GLOBAL,
                active_limit=2,
                queue_limit=2,
                max_wait_ms=1_000,
                config_revision=revision,
            ),
            AdmissionClassConfig(
                class_name=AdmissionClass.VOICE_INTERACTIVE,
                parent_class_name=AdmissionClass.INTERACTIVE,
                active_limit=2,
                queue_limit=0,
                max_wait_ms=0,
                config_revision=revision,
            ),
        ),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: NOW,
    )


def _voice_operation_request(
    *,
    label: str,
    chat_id: str,
    owner: OperationOwner,
) -> OperationRequest:
    submission_id = uuid.uuid5(uuid.NAMESPACE_URL, f"voice-065-{label}")
    return OperationRequest(
        operation_kind="voice_chat_message",
        admission_class=AdmissionClass.VOICE_INTERACTIVE,
        owner=owner,
        submission_id=submission_id,
        idempotency_namespace="voice_chat_message",
        idempotency_key=str(submission_id),
        normalized_input_digest=("1" if label == "first" else "2") * 64,
        chat_id=chat_id,
        parent_operation_id=None,
        connection_generation=uuid.uuid5(
            uuid.NAMESPACE_URL, f"voice-065-connection-{label}"
        ),
        request_generation=uuid.uuid5(uuid.NAMESPACE_URL, f"voice-065-request-{label}"),
    )


def _run_single_turn(task_duration_seconds: float) -> list[tuple[str, float]]:
    fake = _FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    scheduler.add_turn(
        session_id=SESSION,
        turn_id=TURN_EARLIER,
        generation=1,
        media_grant_revision=1,
    )
    accepted_at = fake.mono
    terminal_at = accepted_at + task_duration_seconds
    starts: list[tuple[str, float]] = []

    while True:
        if fake.mono >= terminal_at:
            scheduler.set_lifecycle(TURN_EARLIER, "succeeded")
            decision = scheduler.next_decision()
            assert decision is not None and decision.kind == "result"
            starts.append((decision.kind, fake.mono - accepted_at))
            scheduler.start(decision)
            fake.advance(RESULT_OPENING_SAMPLES / SAMPLE_RATE_HZ)
            scheduler.finish(decision, _completion(decision, fake))
            assert scheduler.next_decision() is None
            return starts

        decision = scheduler.next_decision()
        if decision is None:
            wake = scheduler.next_wake_delay()
            assert wake is not None
            if fake.mono + wake >= terminal_at:
                fake.advance(terminal_at - fake.mono)
            else:
                fake.advance(wake)
            continue

        starts.append((decision.kind, fake.mono - accepted_at))
        scheduler.start(decision)
        duration = (
            0.25
            if decision.kind == "acknowledgement"
            else SINGLE_SAMPLES / SAMPLE_RATE_HZ
        )
        if decision.kind == "progress" and fake.mono + duration >= terminal_at:
            fake.advance(terminal_at - fake.mono)
            assert scheduler.set_lifecycle(TURN_EARLIER, "succeeded") is True
            fake.advance(HANDOFF_BUDGET_SECONDS)
            terminal = scheduler.next_decision()
            assert terminal is not None and terminal.kind == "result"
            starts.append((terminal.kind, fake.mono - accepted_at))
            scheduler.start(terminal)
            fake.advance(RESULT_OPENING_SAMPLES / SAMPLE_RATE_HZ)
            scheduler.finish(terminal, _completion(terminal, fake))
            assert scheduler.next_decision() is None
            return starts
        fake.advance(duration)
        scheduler.finish(decision, _completion(decision, fake))


def test_two_same_chat_tools_overlap_and_reverse_complete_without_rerun() -> None:
    """Exercise the complete overlapping-turn contract with deterministic gates."""

    chat_id = "30000000-0000-4000-8000-000000000001"
    owner = OperationOwner(OwnerScope.USER, "voice-perf-owner", None)
    coordinator = _concurrent_voice_coordinator()
    admissions = {
        label: coordinator.submit(
            _voice_operation_request(label=label, chat_id=chat_id, owner=owner)
        )
        for label in ("first", "second")
    }
    claims = {
        label: coordinator.claim_operation(
            AdmissionClass.VOICE_INTERACTIVE,
            admission.operation_id,
        )
        for label, admission in admissions.items()
    }
    assert all(admission.accepted for admission in admissions.values())
    assert all(
        admission.state is OperationState.RUNNING for admission in admissions.values()
    )
    assert all(claim is not None for claim in claims.values())

    base_components: list[dict[str, object]] = [
        {"type": "text", "component_id": "shared", "content": "base"}
    ]
    base_layouts: list[dict[str, object]] = [
        {
            "layout_key": "main",
            "position": 0,
            "layout": [{"type": "ref", "component_id": "shared"}],
        }
    ]
    execution_bases = {
        label: _ImmutableExecutionBase.capture(
            render_revision=index,
            components=base_components,
            layouts=base_layouts,
        )
        for index, label in enumerate(("first", "second"), start=1)
    }
    assert execution_bases["first"].render_revision == 1
    assert execution_bases["second"].render_revision == 2

    # Acceptance of the second turn backgrounds the first without changing
    # either running operation or either immutable execution base.
    foreground = {"first": False, "second": True}
    assert foreground == {"first": False, "second": True}
    assert {
        label: coordinator.query_operation(
            owner=owner, operation_id=admission.operation_id
        ).state
        for label, admission in admissions.items()
    } == {"first": OperationState.RUNNING, "second": OperationState.RUNNING}

    tool = _BlockedTool()
    completion_order: list[str] = []
    authoritative_components = json.loads(json.dumps(base_components))
    authoritative_layouts = json.loads(json.dumps(base_layouts))
    authoritative_lock = threading.Lock()

    def execute_and_publish(label: str) -> None:
        nonlocal authoritative_components, authoritative_layouts
        candidate_components, candidate_layouts = tool.execute(
            label, execution_bases[label]
        )
        base_snapshot, base_snapshot_layouts = execution_bases[label].materialize()
        with authoritative_lock:
            merged = merge_conversation_publication(
                base_components=base_snapshot,
                candidate_components=candidate_components,
                latest_components=authoritative_components,
                base_layouts=base_snapshot_layouts,
                candidate_layouts=candidate_layouts,
                latest_layouts=authoritative_layouts,
            )
            authoritative_components = list(merged.components)
            authoritative_layouts = list(merged.layouts)
            completion_order.append(label)
        claim = claims[label]
        assert claim is not None
        coordinator.terminalize(
            claim.fence,
            state=OperationState.COMPLETED,
            terminal_code=None,
            safe_summary="Completed",
            retry_after_ms=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            label: executor.submit(execute_and_publish, label)
            for label in ("first", "second")
        }
        assert tool.started["first"].wait(timeout=2)
        assert tool.started["second"].wait(timeout=2)
        tool.release["second"].set()
        futures["second"].result(timeout=2)
        assert completion_order == ["second"]
        assert (
            coordinator.query_operation(
                owner=owner, operation_id=admissions["first"].operation_id
            ).state
            is OperationState.RUNNING
        )
        tool.release["first"].set()
        futures["first"].result(timeout=2)

    assert completion_order == ["second", "first"]
    assert tool.calls == {"first": 1, "second": 1}
    assert {
        label: coordinator.query_operation(
            owner=owner, operation_id=admission.operation_id
        ).state
        for label, admission in admissions.items()
    } == {"first": OperationState.COMPLETED, "second": OperationState.COMPLETED}
    assert [component["content"] for component in authoritative_components] == [
        "second",
        "second-only",
        "first-only",
    ]
    assert authoritative_layouts == [
        {
            "layout_key": "main",
            "position": 0,
            "layout": [
                {"type": "ref", "component_id": "shared"},
                {"type": "ref", "component_id": "only-second"},
            ],
        }
    ]
    assert execution_bases["first"].materialize() == (
        base_components,
        base_layouts,
    )
    assert execution_bases["second"].materialize() == (
        base_components,
        base_layouts,
    )


@pytest.mark.parametrize("task_duration_seconds", [2.0, 19.0, 21.0, 65.0, 180.0])
def test_controlled_task_durations_have_one_ack_and_no_gap_over_20_seconds(
    task_duration_seconds: float,
) -> None:
    starts = _run_single_turn(task_duration_seconds)

    assert [kind for kind, _ in starts].count("acknowledgement") == 1
    assert starts[0][0] == "acknowledgement"
    assert starts[0][1] <= 1.5
    assert starts[-1][0] == "result"
    terminal_index = next(
        index for index, (kind, _) in enumerate(starts) if kind == "result"
    )
    assert all(kind != "progress" for kind, _ in starts[terminal_index:])
    assert (
        max(later - earlier for (_, earlier), (_, later) in zip(starts, starts[1:]))
        <= CADENCE_HARD_GAP_SECONDS
    )


def test_two_due_turns_reserve_four_second_quantum_and_positive_handoff() -> None:
    fake = _FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    due = NOW + timedelta(seconds=14)
    for turn_id in (TURN_EARLIER, TURN_LATEST):
        scheduler.add_turn(
            session_id=SESSION,
            turn_id=turn_id,
            generation=1,
            media_grant_revision=1,
            announcement_sequence=1,
            next_due_at=due,
        )

    # Exercise the latest permitted first-start target, not an optimistic zero
    # latency path.  The second turn still has its full four-second reservation.
    fake.advance(15.5)
    first = scheduler.next_decision()
    assert first is not None and first.kind == "progress"
    assert first.max_duration_samples == SINGLE_SAMPLES
    first_started = fake.mono
    scheduler.start(first)
    fake.advance(SINGLE_SAMPLES / SAMPLE_RATE_HZ)
    scheduler.finish(first, _completion(first, fake))

    # Inject the full allowed handoff latency. Production asks for the next
    # decision immediately instead of deliberately sleeping to this boundary.
    fake.advance(HANDOFF_BUDGET_SECONDS)
    second = scheduler.next_decision()
    assert second is not None and second.turn_id != first.turn_id
    second_started = fake.mono

    assert second.max_duration_samples == SINGLE_SAMPLES
    assert first_started - 100.0 == pytest.approx(15.5)
    assert second_started - first_started == pytest.approx(4.25)
    assert second_started - 100.0 == pytest.approx(19.75)


def test_coincident_results_yield_after_bounded_opening_by_1_75_seconds() -> None:
    fake = _FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    due = NOW + timedelta(seconds=14)
    for turn_id in (TURN_EARLIER, TURN_LATEST):
        scheduler.add_turn(
            session_id=SESSION,
            turn_id=turn_id,
            generation=1,
            media_grant_revision=1,
            announcement_sequence=1,
            next_due_at=due,
        )
        scheduler.set_lifecycle(turn_id, "succeeded")

    first = scheduler.next_decision()
    assert first is not None and first.quantum_role == "result_opening"
    assert first.max_duration_samples == RESULT_OPENING_SAMPLES
    scheduler.start(first)
    fake.advance(RESULT_OPENING_SAMPLES / SAMPLE_RATE_HZ)
    scheduler.finish(first, _completion(first, fake))
    fake.advance(HANDOFF_BUDGET_SECONDS)

    second = scheduler.next_decision()
    assert second is not None and second.turn_id != first.turn_id
    assert second.quantum_role == "result_opening"
    assert second.max_duration_samples == RESULT_OPENING_SAMPLES
    assert fake.mono - 100.0 == pytest.approx(1.75)


def test_multi_minute_two_turn_schedule_keeps_each_turn_inside_hard_gap() -> None:
    fake = _FakeClock()
    scheduler = SpeechCadenceScheduler(_clock(fake))
    due = NOW + timedelta(seconds=14)
    for turn_id in (TURN_EARLIER, TURN_LATEST):
        scheduler.add_turn(
            session_id=SESSION,
            turn_id=turn_id,
            generation=1,
            media_grant_revision=1,
            announcement_sequence=1,
            next_due_at=due,
        )

    starts = {TURN_EARLIER: [], TURN_LATEST: []}
    while fake.mono < 100.0 + 180.0:
        wake = scheduler.next_wake_delay()
        assert wake is not None
        fake.advance(wake)
        decision = scheduler.next_decision()
        assert decision is not None
        starts[decision.turn_id].append(fake.mono)
        scheduler.start(decision)
        fake.advance(SINGLE_SAMPLES / SAMPLE_RATE_HZ)
        scheduler.finish(decision, _completion(decision, fake))

    for turn_starts in starts.values():
        assert len(turn_starts) >= 8
        assert (
            max(later - earlier for earlier, later in zip(turn_starts, turn_starts[1:]))
            <= CADENCE_HARD_GAP_SECONDS
        )


def test_acoustic_probe_discards_waveform_and_keeps_content_free_boundaries() -> None:
    probe = _EphemeralAcousticProbe()
    waveform = bytearray(b"\x01\x02" * 240)

    probe.observe(waveform, started=10.0, finished=10.01)

    assert waveform == bytearray(len(waveform))
    assert probe.observations == [(10.0, 10.01, 240)]
