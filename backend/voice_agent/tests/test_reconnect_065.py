"""Direct-RTC reconnect, publisher, and replay-window guards for Feature 065."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from voice_agent.session import RtcSessionError, SessionNotice
from voice_agent.tests.test_session_start_065 import (
    NOW,
    FakeLocalPublication,
    FakeParticipant,
    FakePublication,
    FakeRoom,
    FakeRtcFactory,
    FakeTrack,
    _session,
    _set_capture,
    _set_recognition_binding,
    _speak,
    _turn_bound_frame,
    _uuid,
    _wait_for,
)


@pytest.mark.asyncio
async def test_sdk_resume_reconciles_new_sid_and_terminal_disconnect_uses_fresh_room() -> (
    None
):
    first = FakePublication("TR_first", track=FakeTrack("first"))
    participant = FakeParticipant("client-a", [first])
    room = FakeRoom([participant])
    factory = FakeRtcFactory(room)
    session = _session(factory)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)

    room.emit("reconnecting")
    await _wait_for(lambda: session.media_state == "reconnecting")
    assert session.capture_open is False
    assert factory.streams[0].closed == 1
    replacement = FakePublication("TR_replacement", track=FakeTrack("replacement"))
    participant.track_publications = {replacement.sid: replacement}
    room.emit("reconnected")
    await _wait_for(lambda: len(factory.streams) == 2)
    assert replacement.subscription_calls == [True]
    assert session.capture_open is True

    room.emit("disconnected")
    with pytest.raises(RtcSessionError, match="rtc_disconnected"):
        await task
    assert room.disconnect_calls == 1
    assert session.binding.worker_rtc_grant.join_token == ""

    fresh_room = FakeRoom()
    fresh_session = _session(FakeRtcFactory(fresh_room))
    fresh_task = asyncio.create_task(fresh_session.run())
    await fresh_session.wait_started()
    assert fresh_room is not room
    assert len(fresh_room.connect_calls) == 1
    assert fresh_session.media_state == "ready"
    await fresh_session.close("terminal_recovery_complete")
    await fresh_task


@pytest.mark.asyncio
async def test_reconnect_clears_pending_playout_hold_before_fresh_capture() -> None:
    first = FakePublication("TR_first", track=FakeTrack("first"))
    participant = FakeParticipant("client-a", [first])
    room = FakeRoom([participant])
    session = _session(FakeRtcFactory(room))
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())
    await _wait_for(lambda: session._playout_confirmation_task is not None)
    assert session.capture_open is False

    room.emit("reconnecting")
    await _wait_for(lambda: session.media_state == "reconnecting")
    assert session.capture_open is False
    assert session._playout_capture_hold is False
    assert session._playout_confirmation_task is None

    replacement = FakePublication("TR_replacement", track=FakeTrack("replacement"))
    participant.track_publications = {replacement.sid: replacement}
    room.emit("reconnected")
    await _wait_for(lambda: session.capture_open)
    assert session._playout_confirmation_task is None

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_rotation_applies_before_context_and_stale_output_publisher_is_ignored() -> (
    None
):
    first = FakePublication("TR_first", track=FakeTrack("first"))
    next_publication = FakePublication("TR_next", track=FakeTrack("next"))
    room = FakeRoom(
        [
            FakeParticipant("client-a", [first]),
            FakeParticipant("client-next", [next_publication]),
        ]
    )
    factory = FakeRtcFactory(room, block_output=True)
    notices: list[SessionNotice] = []
    session = _session(factory, notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()
    session.deliver(_set_capture(True))
    await _wait_for(lambda: session.capture_open)
    session.deliver(_speak())
    await _wait_for(lambda: session.output_track_sid == "TR_output_1")

    stale = FakeLocalPublication("TR_stale")
    room.emit("local_track_republished", stale, "TR_output_1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.output_track_sid == "TR_output_1"
    room.local_participant.publication.sid = "TR_output_2"
    room.emit(
        "local_track_republished",
        room.local_participant.publication,
        "TR_output_1",
    )
    await _wait_for(lambda: session.output_track_sid == "TR_output_2")

    rotation = {
        "type": "media_grant_rotated",
        "session_id": _uuid(1),
        "generation": 1,
        "refresh_id": _uuid(41),
        "previous_media_grant_revision": 1,
        "media_grant_revision": 2,
        "client_participant_identity": "client-next",
        "transport": "livekit",
        "grant_expires_at": (NOW + timedelta(minutes=3)).isoformat(),
    }
    session.deliver(dict(rotation))
    await _wait_for(lambda: session.binding.media_grant_revision == 2)
    session.deliver(
        {
            "type": "session_context_update",
            "session_id": _uuid(1),
            "generation": 1,
            "media_grant_revision": 2,
            "visible_chat_id": _uuid(40),
            "chat_context_revision": 2,
        }
    )
    await _wait_for(lambda: session.binding.chat_context_revision == 2)

    media_index = next(
        index
        for index, notice in enumerate(notices)
        if notice.kind == "media_grant_applied"
        and notice.metadata.get("media_grant_revision") == 2
    )
    context_index = next(
        index
        for index, notice in enumerate(notices)
        if notice.kind == "session_context_applied"
        and notice.metadata.get("chat_context_revision") == 2
    )
    assert media_index < context_index
    assert next_publication.subscription_calls == [True]
    assert first.subscription_calls[-1] is False
    session.deliver(dict(rotation))
    await _wait_for(
        lambda: [notice.kind for notice in notices].count("media_grant_applied") == 2
    )
    assert session.binding.media_grant_revision == 2
    assert len(factory.streams) == 2

    await session.close("test")
    await task


@pytest.mark.asyncio
async def test_final_replay_expires_and_cannot_republish_after_window() -> None:
    class Clock:
        now = NOW

    clock = Clock()
    room = FakeRoom()
    session = _session(FakeRtcFactory(room))
    session._utcnow = lambda: clock.now
    task = asyncio.create_task(session.run())
    await session.wait_started()
    client_turn_id = _uuid(30)
    _set_recognition_binding(session, client_turn_id)
    session.deliver(_turn_bound_frame(client_turn_id))
    await _wait_for(
        lambda: session._recognition_bindings[client_turn_id].turn_id is not None
    )
    recognition = session._recognition_bindings[client_turn_id]
    await session._retain_final(recognition, "bounded replay", "en")
    assert session.retained_final_count == 1
    assert len(room.local_participant.published_data) == 1

    clock.now = NOW + timedelta(minutes=2, seconds=1)
    await session._expire_retained_final(client_turn_id)
    await _wait_for(lambda: session.retained_final_count == 0)
    room.emit("reconnected")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session.retained_final_bytes == 0
    assert len(room.local_participant.published_data) == 1
    await session.close("test")
    await task
