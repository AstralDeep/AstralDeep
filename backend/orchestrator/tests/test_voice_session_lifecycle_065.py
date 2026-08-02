"""Feature-065 runtime lifecycle, media ordering, and failure tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.voice_api import VoiceApiError
from orchestrator.voice_bootstrap import VoiceServices
from orchestrator.voice_coordinator import (
    FIXED_VOICE_PROFILE,
    ControlLeaseState,
    SessionBindRequest,
    WorkerPool,
    WorkerPoolPolicy,
)
from orchestrator.runtime_observability import RuntimeObservability
from orchestrator.voice_runtime import ActivatedVoiceMedia, VoiceSessionRuntime
from orchestrator.voice_sessions import (
    SessionMutation,
    TakeoverRequired,
    VoiceSessionRecord,
)
from orchestrator.voice_worker_endpoint import (
    WorkerControlEndpoint,
    WorkerControlSettings,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
BINDING = "00000000-0000-4000-8000-000000000003"
CHAT = "00000000-0000-4000-8000-000000000004"
ACTIVATION = "00000000-0000-4000-8000-000000000005"
SESSION = "00000000-0000-4000-8000-000000000006"
ASSIGNMENT = "00000000-0000-4000-8000-000000000007"
REFRESH = "00000000-0000-4000-8000-000000000011"


class _CapabilityValue:
    def __init__(self, status: str = "ready", reason: str = "ready") -> None:
        self.status = status
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {"schema_version": "1", "status": self.status, "reason": self.reason}


class _Capability:
    def __init__(self) -> None:
        self.value = _CapabilityValue()

    async def readiness(self) -> _CapabilityValue:
        return self.value


class _Repository:
    def __init__(self) -> None:
        self.session = _session()
        self.replayed = False
        self.takeover: TakeoverRequired | None = None
        self.get_failure: Exception | None = None
        self.end_failure: Exception | None = None
        self.calls: list[tuple[str, Any]] = []

    def create_session(self, request, *, now):
        self.calls.append(("create", request))
        if self.takeover is not None:
            raise self.takeover
        return SessionMutation(self.session, replayed=self.replayed)

    def take_over_session(self, request, *, now):
        self.calls.append(("takeover", request))
        self.session = replace(
            self.session,
            session_id="00000000-0000-4000-8000-000000000009",
            generation=2,
            activation_id=request.create.activation_id,
            device_id=request.create.device_id,
            owner_connection_generation=request.create.owner_connection_generation,
            control_binding_id=request.create.control_binding_id,
            state="starting",
            applied_visible_chat_id=None,
            applied_chat_context_revision=None,
        )
        return SessionMutation(self.session, replayed=self.replayed)

    def get_session(self, *, user_id, session_id):
        self.calls.append(("get", session_id))
        if self.get_failure is not None:
            raise self.get_failure
        return self.session

    async def claim_control_lease(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return ControlLeaseState(
            generation=kwargs["generation"],
            owner_id=kwargs["owner_id"],
            expires_at=kwargs["now"] + timedelta(seconds=15),
        )

    def assign_worker(self, **kwargs):
        self.calls.append(("assign", kwargs))
        self.session = replace(
            self.session,
            worker_identity=kwargs["worker_identity"],
            worker_assignment_id=kwargs["assignment_id"],
        )
        return SessionMutation(self.session)

    def apply_chat_context(self, **kwargs):
        self.calls.append(("apply_context", kwargs))
        self.session = replace(
            self.session,
            applied_visible_chat_id=kwargs["visible_chat_id"],
            applied_chat_context_revision=kwargs["chat_context_revision"],
        )
        return SessionMutation(self.session)

    def mark_session_active(self, **kwargs):
        self.calls.append(("active", kwargs))
        self.session = replace(self.session, state="active")
        return self.session

    def update_session(self, request, *, now):
        self.calls.append(("update", request))
        state = self.session.state
        if request.foreground_active is False:
            state = "suspended"
        elif request.foreground_active is True:
            state = "reconnecting"
        self.session = replace(
            self.session,
            state=state,
            foreground_active=(
                self.session.foreground_active
                if request.foreground_active is None
                else request.foreground_active
            ),
            foreground_reason=request.foreground_reason
            or self.session.foreground_reason,
            microphone_enabled=(
                self.session.microphone_enabled
                if request.microphone_enabled is None
                else request.microphone_enabled
            ),
            speech_muted=(
                self.session.speech_muted
                if request.speech_muted is None
                else request.speech_muted
            ),
            visible_chat_id=request.visible_chat_id or self.session.visible_chat_id,
            chat_context_revision=(
                self.session.chat_context_revision + 1
                if request.visible_chat_id
                and request.visible_chat_id != self.session.visible_chat_id
                else self.session.chat_context_revision
            ),
        )
        return self.session

    def renew_session_lease(self, **kwargs):
        self.calls.append(("renew", kwargs))
        self.session = replace(
            self.session,
            lease_expires_at=kwargs["now"] + kwargs["lease_duration"],
        )
        return self.session

    def end_session(self, **kwargs):
        self.calls.append(("end", kwargs))
        if self.end_failure is not None:
            raise self.end_failure
        self.session = replace(
            self.session,
            state="ended",
            ended_at=kwargs["now"],
            end_reason=kwargs["reason"],
            foreground_active=False,
            foreground_reason="connection_lost",
            microphone_enabled=False,
        )
        return self.session

    def get_controlled_session(self, **kwargs):
        self.calls.append(("controlled", kwargs))
        return self.session

    def refresh_media_grant(self, request, *, now):
        self.calls.append(("refresh", request))
        if self.session.last_media_refresh_id == request.refresh_id:
            return SessionMutation(self.session, replayed=True)
        self.session = replace(
            self.session,
            media_grant_revision=self.session.media_grant_revision + 1,
            participant_identity=request.participant_identity,
            media_grant_nonce_hash=request.nonce_hash,
            media_grant_issued_at=request.issued_at,
            media_grant_expires_at=request.expires_at,
            last_media_refresh_id=request.refresh_id,
        )
        return SessionMutation(self.session)


class _Media:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.failure: Exception | None = None
        self.end_failure: Exception | None = None
        self.assignment_current = True
        self.assignment_checks: list[tuple[int, str, str]] = []
        self.drop_assignment_during_rotate = False

    async def activate(self, session):
        self.calls.append(("activate", session.session_id))
        if self.failure is not None:
            raise self.failure
        return ActivatedVoiceMedia(
            assignment_id=ASSIGNMENT,
            worker_identity="voice-worker-a",
            worker_grant_issued_at=NOW,
            worker_grant_expires_at=NOW + timedelta(minutes=5),
            client_grant={
                "grant_id": "grant-a",
                "transport": "livekit",
                "join_token": "secret-client-grant",
            },
        )

    async def assignment_is_current(
        self,
        session,
        *,
        assignment_id,
        worker_identity,
    ):
        self.assignment_checks.append(
            (
                session.media_grant_revision,
                assignment_id,
                worker_identity,
            )
        )
        return self.assignment_current

    async def apply_context(self, session):
        self.calls.append(("context", session.visible_chat_id))

    async def set_capture(self, session, enabled):
        self.calls.append(("capture", enabled))

    async def stop_speech(self, session):
        self.calls.append(("stop", session.session_id))

    async def end(self, session, reason):
        self.calls.append(("end", reason))
        if self.end_failure is not None:
            raise self.end_failure

    async def abort(self, session):
        self.calls.append(("abort", session.session_id))

    async def rotate_media_grant(self, previous, session, *, refresh_id):
        self.calls.append(
            (
                "rotate",
                (
                    previous.media_grant_revision,
                    session.media_grant_revision,
                    refresh_id,
                ),
            )
        )
        if self.drop_assignment_during_rotate:
            self.assignment_current = False
        return {
            "grant_id": "grant-refresh",
            "transport": session.transport,
            "session_id": session.session_id,
            "generation": session.generation,
            "media_grant_revision": session.media_grant_revision,
            "join_token": "regenerated-secret-client-grant",
        }


def _session() -> VoiceSessionRecord:
    return VoiceSessionRecord(
        session_id=SESSION,
        user_id="user-a",
        activation_id=ACTIVATION,
        device_id=DEVICE,
        device_kind="web",
        transport="livekit",
        room_name="voice-room",
        participant_identity="voice-client",
        worker_identity=None,
        visible_chat_id=CHAT,
        chat_context_revision=1,
        applied_visible_chat_id=None,
        applied_chat_context_revision=None,
        state="starting",
        speech_muted=False,
        microphone_enabled=True,
        foreground_active=True,
        foreground_reason="foreground",
        generation=1,
        media_grant_revision=1,
        owner_connection_generation=CONNECTION,
        control_binding_id=BINDING,
        control_binding_expires_at=NOW + timedelta(minutes=10),
        lease_expires_at=NOW + timedelta(seconds=45),
        control_owner_id=None,
        control_lease_expires_at=None,
        last_interaction_at=NOW,
        idle_started_at=None,
        started_at=NOW,
        updated_at=NOW,
        ended_at=None,
        end_reason=None,
        chat_unavailable_at=None,
        takeover_of_session_id=None,
        media_grant_nonce_hash=b"m" * 32,
        media_grant_expires_at=NOW + timedelta(minutes=5),
        media_grant_consumed_at=None,
        last_media_refresh_id=None,
        media_grant_issued_at=NOW,
        worker_assignment_id=None,
        worker_rtc_grant_revision=1,
        worker_rtc_grant_issued_at=None,
        worker_rtc_grant_expires_at=None,
    )


def _control() -> dict[str, Any]:
    return {
        "subject": "user-a",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "binding_id": BINDING,
        "binding_expires_at": NOW + timedelta(minutes=10),
    }


def _request(**changes: Any) -> dict[str, Any]:
    value = {
        "device_id": DEVICE,
        "device_kind": "web",
        "visible_chat_id": CHAT,
        "activation_id": ACTIVATION,
        "capability": {
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "full_duplex": True,
            "transport": "livekit",
        },
        "foreground_active": True,
    }
    value.update(changes)
    return value


def _runtime(observability=None):
    repository = _Repository()
    capability = _Capability()
    media = _Media()
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=capability,
        media=media,
        replica_id="replica-a",
        clock=lambda: NOW,
        observability=observability,
    )
    return runtime, repository, capability, media


def test_runtime_uses_45_second_launch_lease_and_rejects_out_of_range_values() -> None:
    runtime, _repository, _capability, _media = _runtime()
    assert runtime._lease == timedelta(seconds=45)

    for invalid in (14, 301):
        with pytest.raises(ValueError, match="invalid_voice_lease"):
            VoiceSessionRuntime(
                repository=_Repository(),
                capability=_Capability(),
                media=_Media(),
                replica_id="replica-a",
                lease_seconds=invalid,
            )


@pytest.mark.asyncio
async def test_runtime_session_and_replay_paths_emit_bounded_metrics() -> None:
    metrics = RuntimeObservability(deployment_instance="test")
    runtime, repository, _capability, _media = _runtime(metrics)
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    repository.replayed = True
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )

    snapshot = metrics.snapshot()
    names = {sample.name for sample in snapshot}
    assert {
        "voice_activation_seconds",
        "voice_deduplication_total",
        "voice_session_total",
        "voice_state_transition_total",
    } <= names
    rendered = repr(snapshot)
    assert SESSION not in rendered
    assert "user-a" not in rendered
    assert "secret-client-grant" not in rendered


@pytest.mark.asyncio
async def test_create_orders_control_worker_context_active_then_returns_grant() -> None:
    runtime, repository, _capability, media = _runtime()
    result = await runtime.create_session(
        user_id="user-a", control=_control(), request=_request()
    )
    assert result.status_code == 201
    assert result.payload["session"]["state"] == "active"
    assert result.payload["session"]["chat_context_synced"] is True
    assert result.payload["grant"]["join_token"] == "secret-client-grant"
    assert [name for name, _ in repository.calls] == [
        "create",
        "claim",
        "assign",
        "apply_context",
        "active",
    ]
    assert media.calls == [("activate", SESSION)]


@pytest.mark.asyncio
async def test_activation_lost_assignment_returns_no_grant_and_ends_media() -> None:
    runtime, repository, _capability, media = _runtime()
    media.assignment_current = False

    with pytest.raises(VoiceApiError, match="worker_assignment_unavailable") as caught:
        await runtime.create_session(
            user_id="user-a",
            control=_control(),
            request=_request(),
        )

    assert caught.value.status_code == 503
    assert repository.session.end_reason == "media_error"
    assert ("abort", SESSION) in media.calls
    assert runtime._active_worker_assignments == {}


@pytest.mark.asyncio
async def test_exact_activation_retry_is_idempotent_and_changed_owner_requires_takeover() -> None:
    runtime, repository, _capability, _media = _runtime()
    repository.replayed = True
    replay = await runtime.create_session(
        user_id="user-a", control=_control(), request=_request()
    )
    assert replay.status_code == 200

    repository.takeover = TakeoverRequired(repository.session)
    with pytest.raises(VoiceApiError) as caught:
        await runtime.create_session(
            user_id="user-a",
            control=_control(),
            request=_request(activation_id="00000000-0000-4000-8000-000000000008"),
        )
    assert caught.value.status_code == 409
    assert caught.value.payload["owner"]["session_id"] == SESSION
    assert "room_name" not in caught.value.payload["owner"]


@pytest.mark.asyncio
async def test_takeover_ends_only_prior_media_and_preserves_generation() -> None:
    runtime, repository, _capability, media = _runtime()
    ended: list[tuple[str, int, str]] = []

    async def handle_end(session, reason) -> None:
        ended.append((session.session_id, session.generation, reason))

    runtime.bind_session_end_handler(handle_end)
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_session_end_handler(handle_end)
    request = _request(
        activation_id="00000000-0000-4000-8000-000000000008",
        expected_generation=1,
        expected_media_grant_revision=1,
    )
    result = await runtime.take_over_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=request,
    )
    assert result.payload["session"]["generation"] == 2
    assert ("end", "takeover") in media.calls
    assert ended == [(SESSION, 1, "takeover")]
    assert [name for name, _ in repository.calls].count("takeover") == 1


@pytest.mark.asyncio
async def test_background_update_stops_capture_and_playout_without_end() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    result = await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "foreground_active": False,
            "foreground_reason": "backgrounded",
            "microphone_enabled": False,
        },
    )
    assert result["state"] == "suspended"
    assert ("capture", False) in media.calls
    assert ("stop", SESSION) in media.calls
    assert not any(name == "end" for name, _ in media.calls)


@pytest.mark.asyncio
async def test_context_change_waits_for_media_application_before_projection() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(
        repository.session,
        state="active",
        applied_visible_chat_id=CHAT,
        applied_chat_context_revision=1,
    )
    next_chat = "00000000-0000-4000-8000-000000000010"
    result = await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "visible_chat_id": next_chat,
        },
    )
    assert result["visible_chat_id"] == next_chat
    assert result["chat_context_synced"] is True
    assert ("context", next_chat) in media.calls


@pytest.mark.asyncio
async def test_mute_is_no_burst_and_interrupts_current_speech() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "speech_muted": True,
        },
    )
    assert ("stop", SESSION) in media.calls


@pytest.mark.asyncio
async def test_mute_and_unmute_route_through_server_owned_cadence_stream() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    changes: list[tuple[str, int, bool]] = []

    async def apply_mute(session_id: str, generation: int, muted: bool) -> None:
        changes.append((session_id, generation, muted))

    runtime.bind_speech_mute_handler(apply_mute)
    fences = {
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
    }
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={**fences, "speech_muted": True},
    )
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={**fences, "speech_muted": False},
    )

    assert changes == [(SESSION, 1, True), (SESSION, 1, False)]
    assert ("stop", SESSION) not in media.calls
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_speech_mute_handler(apply_mute)


@pytest.mark.asyncio
async def test_stop_and_background_route_through_exact_speech_stop_handler() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    stops: list[tuple[str, int]] = []
    suspensions: list[tuple[str, int, bool]] = []
    lifecycle_order: list[tuple[str, bool]] = []

    original_set_capture = media.set_capture

    async def record_capture(session: Any, enabled: bool) -> None:
        lifecycle_order.append(("capture", enabled))
        await original_set_capture(session, enabled)

    media.set_capture = record_capture  # type: ignore[method-assign]

    async def stop_exact_session(session_id: str, generation: int) -> None:
        stops.append((session_id, generation))

    async def suspend_exact_session(
        session_id: str,
        generation: int,
        suspended: bool,
    ) -> None:
        suspensions.append((session_id, generation, suspended))
        lifecycle_order.append(("suspend", suspended))

    runtime.bind_speech_stop_handler(stop_exact_session)
    runtime.bind_speech_suspend_handler(suspend_exact_session)
    fences = {
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
    }
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            **fences,
            "foreground_active": False,
            "foreground_reason": "backgrounded",
            "microphone_enabled": False,
        },
    )
    await runtime.stop_speech(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=fences,
    )
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            **fences,
            "foreground_active": True,
            "foreground_reason": "foreground",
        },
    )

    assert stops == [(SESSION, 1)]
    assert suspensions == [(SESSION, 1, True), (SESSION, 1, False)]
    assert lifecycle_order == [
        ("suspend", True),
        ("capture", False),
        ("capture", False),
        ("suspend", False),
    ]
    assert ("capture", False) in media.calls
    assert ("stop", SESSION) not in media.calls
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_speech_stop_handler(stop_exact_session)
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_speech_suspend_handler(suspend_exact_session)


@pytest.mark.asyncio
async def test_failed_foreground_transition_retains_server_speech_suspension() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    suspensions: list[bool] = []

    async def suspend_exact_session(
        _session_id: str,
        _generation: int,
        suspended: bool,
    ) -> None:
        suspensions.append(suspended)

    async def fail_capture(_session: Any, _enabled: bool) -> None:
        raise RuntimeError("capture transition failed")

    runtime.bind_speech_suspend_handler(suspend_exact_session)
    media.set_capture = fail_capture  # type: ignore[method-assign]
    fences = {
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
    }

    with pytest.raises(RuntimeError, match="capture transition failed"):
        await runtime.update_session(
            user_id="user-a",
            session_id=SESSION,
            control=_control(),
            request={
                **fences,
                "foreground_active": False,
                "foreground_reason": "backgrounded",
                "microphone_enabled": False,
            },
        )
    assert suspensions == [True]

    with pytest.raises(RuntimeError, match="capture transition failed"):
        await runtime.update_session(
            user_id="user-a",
            session_id=SESSION,
            control=_control(),
            request={
                **fences,
                "foreground_active": True,
                "foreground_reason": "foreground",
            },
        )
    # Foreground capture never recovered, so queued speech remains fenced.
    assert suspensions == [True]


@pytest.mark.asyncio
async def test_combined_foreground_and_mute_changes_never_release_speech_early() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(repository.session, state="active")
    lifecycle_order: list[tuple[str, bool]] = []

    async def apply_mute(
        _session_id: str,
        _generation: int,
        muted: bool,
    ) -> None:
        lifecycle_order.append(("mute", muted))

    async def apply_suspension(
        _session_id: str,
        _generation: int,
        suspended: bool,
    ) -> None:
        lifecycle_order.append(("suspend", suspended))

    original_set_capture = media.set_capture

    async def record_capture(session: Any, enabled: bool) -> None:
        lifecycle_order.append(("capture", enabled))
        await original_set_capture(session, enabled)

    runtime.bind_speech_mute_handler(apply_mute)
    runtime.bind_speech_suspend_handler(apply_suspension)
    media.set_capture = record_capture  # type: ignore[method-assign]
    fences = {
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
    }

    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            **fences,
            "foreground_active": False,
            "foreground_reason": "backgrounded",
            "microphone_enabled": False,
            "speech_muted": False,
        },
    )
    assert lifecycle_order == [
        ("suspend", True),
        ("mute", False),
        ("capture", False),
    ]

    lifecycle_order.clear()
    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            **fences,
            "foreground_active": True,
            "foreground_reason": "foreground",
            "speech_muted": True,
        },
    )
    assert lifecycle_order == [
        ("mute", True),
        ("capture", False),
        ("suspend", False),
    ]


@pytest.mark.asyncio
async def test_stop_and_end_are_fenced_media_controls_not_task_cancellation() -> None:
    runtime, repository, _capability, media = _runtime()
    ended: list[tuple[str, int, str]] = []

    async def handle_end(session, reason) -> None:
        ended.append((session.session_id, session.generation, reason))

    runtime.bind_session_end_handler(handle_end)
    repository.session = replace(repository.session, state="active")
    fences = {"expected_generation": 1, "expected_media_grant_revision": 1}
    await runtime.stop_speech(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=fences,
    )
    await runtime.end_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=fences,
    )
    assert ("stop", SESSION) in media.calls
    assert ("end", "user") in media.calls
    assert ended == [(SESSION, 1, "user")]
    assert not any(name == "cancel" for name, _ in repository.calls)


@pytest.mark.asyncio
async def test_durable_user_end_is_not_rolled_back_by_stale_media_cleanup(
    caplog,
) -> None:
    runtime, repository, _capability, media = _runtime()
    media.end_failure = RuntimeError("worker assignment already released")
    repository.session = replace(repository.session, state="active")

    async def stale_end_handler(_session, _reason) -> None:
        raise RuntimeError("announcement runner already closed")

    runtime.bind_session_end_handler(stale_end_handler)

    await runtime.end_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={"expected_generation": 1, "expected_media_grant_revision": 1},
    )

    assert repository.session.ended_at == NOW
    assert repository.session.end_reason == "user"
    assert ("end", "user") in media.calls
    assert "voice_media_cleanup_unavailable reason=media_end_failed" in caplog.text
    assert (
        "voice_session_cleanup_unavailable reason=end_handler_failed"
        in caplog.text
    )


@pytest.mark.asyncio
async def test_worker_disconnect_ends_only_the_exact_retained_assignment() -> None:
    runtime, repository, _capability, media = _runtime()
    ended: list[tuple[str, int, str]] = []

    async def handle_end(session, reason) -> None:
        ended.append((session.session_id, session.generation, reason))

    runtime.bind_session_end_handler(handle_end)
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(SESSION,),
        released_assignment_ids=(),
    )

    assert [(item.session_id, item.generation) for item in reconciled] == [
        (SESSION, 1)
    ]
    assert repository.session.end_reason == "media_error"
    assert ended == [(SESSION, 1, "media_error")]
    assert ("end", "media_error") in media.calls
    assert not any(name == "cancel" for name, _ in repository.calls)


@pytest.mark.asyncio
async def test_worker_disconnect_cannot_end_a_newer_durable_assignment() -> None:
    runtime, repository, _capability, _media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    repository.session = replace(
        repository.session,
        worker_identity="voice-worker-b",
        worker_assignment_id="00000000-0000-4000-8000-000000000099",
    )

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(SESSION,),
        released_assignment_ids=(ASSIGNMENT,),
    )

    assert reconciled == ()
    assert repository.session.ended_at is None
    assert not any(
        name == "end" and value["reason"] == "media_error"
        for name, value in repository.calls
    )


@pytest.mark.asyncio
async def test_worker_disconnect_assignment_id_cannot_race_a_current_rebind() -> None:
    runtime, repository, _capability, _media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(),
        released_assignment_ids=(ASSIGNMENT,),
        assignment_is_current=lambda session_id: session_id == SESSION,
    )

    assert reconciled == ()
    assert repository.session.ended_at is None
    assert not any(
        name == "end" and value["reason"] == "media_error"
        for name, value in repository.calls
    )


@pytest.mark.asyncio
async def test_worker_disconnect_rechecks_assignment_after_durable_read() -> None:
    runtime, repository, _capability, _media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    checks = iter((False, True))

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(SESSION,),
        released_assignment_ids=(),
        assignment_is_current=lambda _session_id: next(checks),
    )

    assert reconciled == ()
    assert repository.session.ended_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("read", "end"))
async def test_worker_disconnect_fails_media_closed_when_durable_repair_is_unavailable(
    failure_stage: str,
    caplog,
) -> None:
    runtime, repository, _capability, media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    failure = RuntimeError("database unavailable")
    if failure_stage == "read":
        repository.get_failure = failure
    else:
        repository.end_failure = failure

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(SESSION,),
        released_assignment_ids=(),
    )

    assert reconciled == ()
    assert ("end", "media_error") in media.calls
    assert "voice_worker_disconnect_reconcile_unavailable" in caplog.text
    assert SESSION not in runtime._active_worker_assignments


@pytest.mark.asyncio
@pytest.mark.parametrize("reassign_before_cleanup", (False, True))
async def test_endpoint_lease_expiry_durably_reconciles_only_unreassigned_media(
    reassign_before_cleanup: bool,
) -> None:
    class LeaseClock:
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

    class PoolSocket:
        async def send(self, _payload: str) -> None:
            return None

        async def close(self, code: int = 1000, reason: str = "") -> None:
            del code, reason
            return None

    class SilentWebSocket:
        async def receive(self):
            await asyncio.Event().wait()

    lease_clock = LeaseClock()
    pool = WorkerPool(
        WorkerPoolPolicy(
            runtime_closure_sha256="a" * 64,
            heartbeat_interval_seconds=5,
            connection_lease_seconds=6,
            send_timeout_seconds=0.1,
            allow_insecure_livekit_url=True,
        ),
        utcnow=lease_clock.utcnow,
        monotonic=lease_clock.monotonic,
    )

    def registration(identity: str, message_id: str) -> dict[str, object]:
        return {
            "type": "worker_register",
            "schema_version": "1",
            "message_id": message_id,
            "sequence": 0,
            "sent_at": "2026-07-31T18:00:00Z",
            "worker_identity": identity,
            "max_sessions": 2,
            "runtime_closure_sha256": "a" * 64,
            "profile": dict(FIXED_VOICE_PROFILE),
        }

    original_receipt = await pool.register_worker(
        registration(
            "voice-worker-a",
            "00000000-0000-4000-8000-000000000071",
        ),
        PoolSocket(),
        authenticated_identity="voice-worker-a",
    )
    bind = SessionBindRequest(
        session_id=SESSION,
        generation=1,
        room_name="voice-room",
        transport="livekit",
        media_grant_revision=1,
        worker_rtc_grant_revision=1,
        client_participant_identity="voice-client",
        visible_chat_id=CHAT,
        chat_context_revision=1,
    )
    await pool.reserve_session(bind)

    runtime, repository, capability, media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=pool,
        repository=repository,  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        capability=capability,  # type: ignore[arg-type]
        media=media,  # type: ignore[arg-type]
        runtime=runtime,
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum",
            lease_sweep_seconds=0.1,
        ),
    )
    runtime.bind_session_end_handler(services.handle_runtime_session_end)

    async def reconcile(receipt, released_session_ids) -> None:
        if reassign_before_cleanup:
            await pool.register_worker(
                registration(
                    "voice-worker-b",
                    "00000000-0000-4000-8000-000000000072",
                ),
                PoolSocket(),
                authenticated_identity="voice-worker-b",
            )
            await pool.reserve_session(bind)
        await services.handle_worker_disconnect(receipt, released_session_ids)

    endpoint = WorkerControlEndpoint(
        pool,
        services.worker_control_settings,
        disconnect_hook=reconcile,
    )
    lease_clock.advance(7)

    await endpoint._run_registered(
        SilentWebSocket(),  # type: ignore[arg-type]
        original_receipt,
    )

    if reassign_before_cleanup:
        assert repository.session.ended_at is None
        assert ("end", "media_error") not in media.calls
        assert pool.assignment_snapshot(SESSION).worker_identity == "voice-worker-b"
    else:
        assert repository.session.end_reason == "media_error"
        assert ("end", "media_error") in media.calls


@pytest.mark.asyncio
async def test_media_grant_state_and_exact_refresh_replay_rotate_only_once() -> None:
    runtime, repository, _capability, media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    state = await runtime.get_media_grant_state(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
    )
    assert state["grant_state"] == {
        "transport": "livekit",
        "media_grant_revision": 1,
        "status": "active",
        "expires_at": "2026-07-31T18:05:00Z",
    }
    assert "participant_identity" not in state["grant_state"]

    request = {
        "refresh_id": REFRESH,
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
        "device_id": DEVICE,
    }
    first = await runtime.refresh_media_grant(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=request,
    )
    replay = await runtime.refresh_media_grant(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request=request,
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.payload["session"]["media_grant_revision"] == 2
    assert replay.payload["session"]["media_grant_revision"] == 2
    assert repository.session.media_grant_revision == 2
    assert [call for call in media.calls if call[0] == "rotate"] == [
        ("rotate", (1, 2, REFRESH)),
        ("rotate", (2, 2, REFRESH)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("loss_point", ("before_rotate", "during_rotate"))
async def test_refresh_worker_loss_returns_no_grant_and_ends_exact_media(
    loss_point: str,
) -> None:
    runtime, repository, _capability, media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    if loss_point == "before_rotate":
        media.assignment_current = False
    else:
        media.drop_assignment_during_rotate = True

    with pytest.raises(VoiceApiError, match="worker_assignment_unavailable") as caught:
        await runtime.refresh_media_grant(
            user_id="user-a",
            session_id=SESSION,
            control=_control(),
            request={
                "refresh_id": REFRESH,
                "expected_generation": 1,
                "expected_media_grant_revision": 1,
                "device_id": DEVICE,
            },
        )

    assert caught.value.status_code == 503
    assert repository.session.media_grant_revision == 2
    assert repository.session.end_reason == "media_error"
    assert ("end", "media_error") in media.calls
    assert runtime._active_worker_assignments == {}


@pytest.mark.asyncio
async def test_refreshed_assignment_fence_remains_disconnect_reconcilable() -> None:
    runtime, repository, _capability, _media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_request(),
    )
    await runtime.refresh_media_grant(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "refresh_id": REFRESH,
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "device_id": DEVICE,
        },
    )

    reconciled = await runtime.reconcile_worker_disconnect(
        worker_identity="voice-worker-a",
        released_session_ids=(SESSION,),
        released_assignment_ids=(),
    )

    assert len(reconciled) == 1
    assert reconciled[0].media_grant_revision == 2
    assert repository.session.end_reason == "media_error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_change", "code"),
    (
        ({"has_microphone": False}, "no_microphone"),
        ({"has_audio_output": False}, "no_audio_output"),
        ({"microphone_permission": "denied"}, "permission_denied"),
        ({"microphone_permission": "restricted"}, "permission_restricted"),
    ),
)
async def test_activation_permission_and_device_failures_create_no_session(
    capability_change: dict[str, Any], code: str
) -> None:
    runtime, repository, _capability, _media = _runtime()
    request = _request()
    request["capability"].update(capability_change)
    with pytest.raises(VoiceApiError, match=code):
        await runtime.create_session(
            user_id="user-a", control=_control(), request=request
        )
    assert repository.calls == []


@pytest.mark.asyncio
async def test_unready_capacity_refuses_before_session_or_acknowledgement() -> None:
    runtime, repository, capability, media = _runtime()
    capability.value = _CapabilityValue("degraded", "capacity_exhausted")
    with pytest.raises(VoiceApiError) as caught:
        await runtime.create_session(
            user_id="user-a", control=_control(), request=_request()
        )
    assert caught.value.status_code == 429
    assert repository.calls == []
    assert media.calls == []


@pytest.mark.asyncio
async def test_activation_failure_aborts_media_and_durably_ends_session() -> None:
    runtime, repository, _capability, media = _runtime()
    media.failure = RuntimeError("transport detail that must not reach HTTP")
    with pytest.raises(RuntimeError):
        await runtime.create_session(
            user_id="user-a", control=_control(), request=_request()
        )
    assert ("abort", SESSION) in media.calls
    assert any(name == "end" for name, _ in repository.calls)
    assert repository.session.end_reason == "media_error"
