"""Ordered reconnect and credential-free recovery guards for Feature 065."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from orchestrator.tests.test_voice_coordinator_065 import (
    CHAT,
    SESSION_1,
    FakeClock,
    FakeSocket,
    _grant,
    _policy,
    _registration,
    _request as _worker_request,
    _worker_ready,
)
from orchestrator.tests.test_voice_session_lifecycle_065 import (
    CHAT as INITIAL_CHAT,
)
from orchestrator.tests.test_voice_session_lifecycle_065 import (
    REFRESH,
    SESSION,
    _control,
    _request as _session_request,
    _runtime,
)
from orchestrator.voice_coordinator import StaleFence, WorkerPool


@pytest.mark.asyncio
async def test_uuid4_refresh_replay_keeps_one_revision_and_recovery_state_has_no_bearer() -> (
    None
):
    runtime, repository, _capability, media = _runtime()
    await runtime.create_session(
        user_id="user-a",
        control=_control(),
        request=_session_request(),
    )
    request = {
        "refresh_id": REFRESH,
        "expected_generation": 1,
        "expected_media_grant_revision": 1,
        "device_id": repository.session.device_id,
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
    current = await runtime.get_media_grant_state(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
    )

    assert uuid.UUID(REFRESH).version == 4
    assert (first.status_code, replay.status_code) == (201, 200)
    assert first.payload["refresh_id"] == replay.payload["refresh_id"] == REFRESH
    assert first.payload["session"]["media_grant_revision"] == 2
    assert replay.payload["session"]["media_grant_revision"] == 2
    assert first.payload["replayed"] is False
    assert replay.payload["replayed"] is True
    assert [call for call in media.calls if call[0] == "rotate"] == [
        ("rotate", (1, 2, REFRESH)),
        ("rotate", (2, 2, REFRESH)),
    ]
    rendered = json.dumps(current, sort_keys=True)
    assert set(current["grant_state"]) == {
        "transport",
        "media_grant_revision",
        "status",
        "expires_at",
    }
    assert "join_token" not in rendered
    assert "participant_identity" not in rendered
    assert "grant_id" not in rendered
    assert "livekit" in rendered


@pytest.mark.asyncio
async def test_worker_rtc_remint_resets_applied_state_and_rejects_old_revision() -> (
    None
):
    clock = FakeClock()
    socket = FakeSocket()
    pool = WorkerPool(_policy(), utcnow=clock.utcnow, monotonic=clock.monotonic)
    await pool.register_worker(
        _registration("worker-a"),
        socket,
        authenticated_identity="worker-a",
    )
    original = _worker_request(SESSION_1, worker_revision=1)
    reservation = await pool.reserve_session(original)
    first_bind = await pool.deliver_session_bind(
        reservation,
        original,
        _grant("worker-a", original),
    )
    await pool.receive_worker_frame(
        reservation.connection_id,
        json.dumps(_worker_ready(reservation, sequence=0, revision=1)),
    )
    assert pool.assignment_snapshot(SESSION_1).ready is True

    clock.advance(1)
    reminted = _worker_request(SESSION_1, worker_revision=2)
    same = await pool.reserve_session(reminted)
    pending = pool.assignment_snapshot(SESSION_1)
    assert same.assignment_id == reservation.assignment_id
    assert pending.ready is False
    assert pending.media_state == "reconnecting"
    assert pending.applied_visible_chat_id is None
    assert pending.applied_chat_context_revision is None
    assert pending.applied_media_refresh_id is None
    second_bind = await pool.deliver_session_bind(
        same,
        reminted,
        {
            **_grant("worker-a", reminted),
            "issued_at": "2026-07-31T18:00:01Z",
            "expires_at": "2026-07-31T18:05:01Z",
        },
    )
    assert (first_bind["sequence"], second_bind["sequence"]) == (0, 1)
    assert second_bind["worker_rtc_grant_revision"] == 2
    with pytest.raises(StaleFence, match="stale_worker_grant_revision"):
        await pool.reserve_session(original)


@pytest.mark.asyncio
async def test_navigation_context_is_applied_before_capture_resumes() -> None:
    runtime, repository, _capability, media = _runtime()
    repository.session = replace(
        repository.session,
        state="reconnecting",
        applied_visible_chat_id=INITIAL_CHAT,
        applied_chat_context_revision=1,
        foreground_active=False,
        foreground_reason="backgrounded",
        microphone_enabled=False,
    )
    next_chat = CHAT
    order: list[str] = []
    original_update = repository.update_session
    original_apply = repository.apply_chat_context
    original_context = media.apply_context
    original_capture = media.set_capture

    def update_session(*args, **kwargs):
        order.append("desired_context")
        return original_update(*args, **kwargs)

    async def apply_context(session):
        order.append("worker_context_update")
        await original_context(session)

    def acknowledge_context(*args, **kwargs):
        order.append("worker_context_applied")
        return original_apply(*args, **kwargs)

    async def set_capture(session, enabled):
        order.append("capture_resume")
        await original_capture(session, enabled)

    repository.update_session = update_session
    repository.apply_chat_context = acknowledge_context
    media.apply_context = apply_context
    media.set_capture = set_capture
    result = await runtime.update_session(
        user_id="user-a",
        session_id=SESSION,
        control=_control(),
        request={
            "expected_generation": 1,
            "expected_media_grant_revision": 1,
            "visible_chat_id": next_chat,
            "foreground_active": True,
            "foreground_reason": "foreground",
            "microphone_enabled": True,
        },
    )

    assert order == [
        "desired_context",
        "worker_context_update",
        "worker_context_applied",
        "capture_resume",
    ]
    assert result["visible_chat_id"] == next_chat
    assert result["chat_context_synced"] is True
    assert result["state"] == "active"
