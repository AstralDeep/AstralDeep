"""Bug-B (2026-08-03) regression tests: voice chat-switch and restart wedge.

Live repro on main: switching to a new chat during an active voice session
left every client on "Waiting for the voice chat context" because the server
never emitted the ``voice_session_state`` frame all four clients shipped
reducers for in feature 065; a stalled client's session was then lease-reaped
silently, and the owner's follow-up DELETE returned 503 via unmapped
repository conflict codes.

Covers:
- REST problem-status mapping for previously unmapped conflict codes.
- The ``voice_session_state`` frame builder (manifest-exact fields, composer-
  consistent state/reason derivation, reaper reason honesty).
- Runtime emission on session PATCH and user end.
- Maintenance-sweep emission for lease/idle-reaped sessions.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator.voice_api import _error_response, _status_for_code
from orchestrator.voice_bootstrap import VoiceServices
from orchestrator.voice_runtime import (
    ActivatedVoiceMedia,
    VoiceSessionRuntime,
    session_state_frame,
)
from orchestrator.voice_sessions import (
    ContextSyncPending,
    SessionMutation,
    StaleSessionFence,
    VoiceSessionRecord,
)
from orchestrator.voice_worker_endpoint import WorkerControlSettings


NOW = datetime(2026, 8, 3, 22, 0, tzinfo=UTC)
DEVICE = "00000000-0000-4000-8000-000000000001"
CONNECTION = "00000000-0000-4000-8000-000000000002"
BINDING = "00000000-0000-4000-8000-000000000003"
CHAT = "00000000-0000-4000-8000-000000000004"
ACTIVATION = "00000000-0000-4000-8000-000000000005"
SESSION = "00000000-0000-4000-8000-000000000006"
NEXT_CHAT = "00000000-0000-4000-8000-000000000010"

_MANIFEST = (
    pathlib.Path(__file__).resolve().parents[2] / "shared" / "ui_protocol.json"
)


def _manifest_session_state_fields() -> list[str]:
    contract = json.loads(_MANIFEST.read_text())
    return contract["frame_contracts"]["voice_065"]["required_server_pushes"][
        "voice_session_state"
    ]


def _session(**changes: Any) -> VoiceSessionRecord:
    value = VoiceSessionRecord(
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
        applied_visible_chat_id=CHAT,
        applied_chat_context_revision=1,
        state="active",
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
    return replace(value, **changes) if changes else value


class _CapabilityValue:
    status = "ready"
    reason = "ready"

    def to_dict(self) -> dict[str, str]:
        return {"schema_version": "1", "status": "ready", "reason": "ready"}


class _Capability:
    async def readiness(self) -> _CapabilityValue:
        return _CapabilityValue()


class _Repository:
    def __init__(self) -> None:
        self.session = _session()

    def update_session(self, request, *, now):
        self.session = replace(
            self.session,
            visible_chat_id=request.visible_chat_id or self.session.visible_chat_id,
            chat_context_revision=(
                self.session.chat_context_revision + 1
                if request.visible_chat_id
                and request.visible_chat_id != self.session.visible_chat_id
                else self.session.chat_context_revision
            ),
            speech_muted=(
                self.session.speech_muted
                if request.speech_muted is None
                else request.speech_muted
            ),
        )
        return self.session

    def renew_session_lease(self, **kwargs):
        self.session = replace(
            self.session,
            lease_expires_at=kwargs["now"] + kwargs["lease_duration"],
        )
        return self.session

    async def claim_control_lease(self, **kwargs):
        return SimpleNamespace(
            generation=kwargs["generation"],
            owner_id=kwargs["owner_id"],
            expires_at=kwargs["now"] + timedelta(seconds=15),
        )

    def apply_chat_context(self, **kwargs):
        self.session = replace(
            self.session,
            applied_visible_chat_id=kwargs["visible_chat_id"],
            applied_chat_context_revision=kwargs["chat_context_revision"],
        )
        return SessionMutation(self.session)

    def mark_session_active(self, **kwargs):
        self.session = replace(self.session, state="active")
        return self.session

    def end_session(self, **kwargs):
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


class _Media:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def activate(self, session):
        return ActivatedVoiceMedia(
            assignment_id="00000000-0000-4000-8000-000000000007",
            worker_identity="voice-worker-a",
            worker_grant_issued_at=NOW,
            worker_grant_expires_at=NOW + timedelta(minutes=5),
            client_grant={"grant_id": "grant-a", "transport": "livekit"},
        )

    async def assignment_is_current(self, session, **kwargs):
        return True

    async def apply_context(self, session):
        self.calls.append(("context", session.visible_chat_id))

    async def set_capture(self, session, enabled):
        self.calls.append(("capture", enabled))

    async def stop_speech(self, session):
        self.calls.append(("stop", session.session_id))

    async def barge_in(self, session):
        self.calls.append(("barge", session.session_id))

    async def end(self, session, reason):
        self.calls.append(("end", reason))

    async def abort(self, session):
        self.calls.append(("abort", session.session_id))

    async def rotate_media_grant(self, previous, session, *, refresh_id):
        return {}


def _control() -> dict[str, Any]:
    return {
        "subject": "user-a",
        "device_id": DEVICE,
        "connection_generation": CONNECTION,
        "binding_id": BINDING,
        "binding_expires_at": NOW + timedelta(minutes=10),
    }


def _runtime() -> tuple[VoiceSessionRuntime, _Repository, _Media]:
    repository = _Repository()
    media = _Media()
    runtime = VoiceSessionRuntime(
        repository=repository,
        capability=_Capability(),
        media=media,
        replica_id="replica-a",
        clock=lambda: NOW,
    )
    return runtime, repository, media


# ---------------------------------------------------------------------------
# REST problem-status mapping (observed live: DELETE of a reaped session 503)
# ---------------------------------------------------------------------------


def test_repository_conflict_codes_map_to_409_not_503() -> None:
    for code in (
        "chat_context_sync_pending",
        "session_already_ended",
        "stale_chat_context_revision",
    ):
        assert _status_for_code(code) == 409, code


def test_raised_repository_conflicts_produce_409_problem_responses() -> None:
    pending = _error_response(ContextSyncPending("chat_context_sync_pending"))
    assert pending.status_code == 409
    ended = _error_response(StaleSessionFence("session_already_ended"))
    assert ended.status_code == 409


# ---------------------------------------------------------------------------
# voice_session_state frame builder
# ---------------------------------------------------------------------------


def test_session_state_frame_field_set_matches_manifest_exactly() -> None:
    frame = session_state_frame(_session(), now=NOW)
    assert list(frame) == _manifest_session_state_fields()
    assert frame["type"] == "voice_session_state"
    assert frame["schema_version"] == "1"
    assert frame["connection_generation"] == CONNECTION
    assert frame["occurred_at"] == "2026-08-03T22:00:00Z"


def test_session_state_frame_reports_synced_listening_after_chat_switch() -> None:
    session = _session(
        visible_chat_id=NEXT_CHAT,
        chat_context_revision=2,
        applied_visible_chat_id=NEXT_CHAT,
        applied_chat_context_revision=2,
    )
    frame = session_state_frame(session, now=NOW)
    assert frame["state"] == "listening"
    assert frame["reason"] == "ready"
    assert frame["visible_chat_id"] == NEXT_CHAT
    assert frame["chat_context_synced"] is True
    assert frame["microphone_enabled"] is True
    assert frame["foreground_active"] is True


def test_session_state_frame_reports_honest_reaper_end_reasons() -> None:
    reaped = _session(
        state="ended",
        ended_at=NOW,
        end_reason="lease_expired",
        foreground_active=False,
        foreground_reason="connection_lost",
        microphone_enabled=False,
    )
    frame = session_state_frame(reaped, now=NOW)
    assert frame["state"] == "ended"
    assert frame["reason"] == "network_interrupted"
    assert frame["foreground_active"] is False
    assert frame["microphone_enabled"] is False

    idle = session_state_frame(
        replace(reaped, end_reason="idle"),
        now=NOW,
    )
    assert idle["reason"] == "idle_expired"

    user = session_state_frame(
        replace(reaped, end_reason="user"),
        now=NOW,
    )
    assert user["reason"] == "ended_by_user"


def test_session_state_frame_never_pairs_background_with_live_microphone() -> None:
    backgrounded = _session(
        state="suspended",
        foreground_active=False,
        foreground_reason="backgrounded",
        microphone_enabled=True,  # defensive: durable writes force this False
    )
    frame = session_state_frame(backgrounded, now=NOW)
    assert frame["state"] == "suspended"
    assert frame["microphone_enabled"] is False


# ---------------------------------------------------------------------------
# Runtime emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_switch_patch_publishes_session_state_to_owner() -> None:
    runtime, repository, media = _runtime()
    published: list[VoiceSessionRecord] = []

    async def record(session: VoiceSessionRecord) -> None:
        published.append(session)

    with pytest.raises(TypeError, match="session state publisher"):
        runtime.bind_session_state_publisher(None)  # type: ignore[arg-type]
    runtime.bind_session_state_publisher(record)
    with pytest.raises(RuntimeError, match="already_bound"):
        runtime.bind_session_state_publisher(record)

    await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "visible_chat_id": NEXT_CHAT,
        },
    )

    assert ("context", NEXT_CHAT) in media.calls
    assert len(published) == 1
    session = published[0]
    assert session.visible_chat_id == NEXT_CHAT
    assert session.chat_context_synced is True
    frame = session_state_frame(session, now=NOW)
    assert frame["state"] == "listening"
    assert frame["chat_context_synced"] is True


@pytest.mark.asyncio
async def test_publisher_failure_never_breaks_the_patch(caplog) -> None:
    runtime, _repository, _media = _runtime()

    async def broken(session: VoiceSessionRecord) -> None:
        raise RuntimeError("socket detail must not propagate")

    runtime.bind_session_state_publisher(broken)
    with caplog.at_level("WARNING"):
        result = await runtime.update_session(
            user_id="user-a",
            session_id=SESSION,
            control=_control(),
            request={
                "expected_generation": 1,
                "expected_media_grant_revision": 1,
                "visible_chat_id": NEXT_CHAT,
            },
        )
    assert result["visible_chat_id"] == NEXT_CHAT
    assert "voice_session_state_publish_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_user_end_publishes_ended_session_state() -> None:
    runtime, _repository, _media = _runtime()
    published: list[VoiceSessionRecord] = []

    async def record(session: VoiceSessionRecord) -> None:
        published.append(session)

    runtime.bind_session_state_publisher(record)
    await runtime.end_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={"expected_generation": 1, "expected_media_grant_revision": 1},
    )

    assert len(published) == 1
    assert published[0].ended_at is not None
    frame = session_state_frame(published[0], now=NOW)
    assert frame["state"] == "ended"
    assert frame["reason"] == "ended_by_user"


# ---------------------------------------------------------------------------
# Maintenance sweep (silent reaper end must reach the owner device)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_publishes_session_state_for_reaped_sessions() -> None:
    lease = SimpleNamespace(
        session_id="lease-session", generation=1, end_reason="lease_expired"
    )
    idle = SimpleNamespace(
        session_id="idle-session", generation=2, end_reason="idle"
    )

    class Repository:
        def renew_owned_control_leases(self, *, owner_id, now):
            return ()

        def expire_session_leases(self, *, now):
            return (lease,)

        def expire_true_idle(self, *, now):
            return (idle,)

        def reconcile_ended_unaccepted_turns(self, *, now):
            return ()

        def reconcile_ended_terminal_operation_turns(self, *, now):
            return ()

    class Media:
        async def end(self, session, reason):
            return None

    published: list[str] = []

    class Runtime:
        def release_worker_assignment_fence(self, session):
            return None

        async def publish_session_state(self, session):
            published.append(session.session_id)

    services = VoiceServices(
        livekit=object(),  # type: ignore[arg-type]
        worker_pool=object(),  # type: ignore[arg-type]
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=SimpleNamespace(replica_id="voice-replica-test"),
        capability=object(),  # type: ignore[arg-type]
        media=Media(),  # type: ignore[arg-type]
        runtime=Runtime(),  # type: ignore[arg-type]
        worker_control_settings=WorkerControlSettings(
            secret=b"voice-control-test-secret-with-32-bytes-minimum"
        ),
    )

    await services._sweep_sessions()

    assert published == ["lease-session", "idle-session"]
