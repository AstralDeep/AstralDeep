"""Tests that logout, session expiry, and chat deletion each end a user's voice session
through voice_sessions.py exactly once, alongside their own boundary such as token
revocation or auth expiry.
"""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace


from orchestrator.api import delete_chat
from orchestrator.voice_sessions import ChatUnavailableMutation


class _VoiceServices:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events

    async def end_user_voice_session(self, *, user_id: str, reason: str) -> None:
        self.events.append(("voice_end", (user_id, reason)))

    async def handle_chat_unavailable(self, mutation) -> None:
        self.events.append(("chat_unavailable", mutation))


def test_chat_delete_passes_durable_lifecycle_receipt_to_voice_services() -> None:
    events: list[tuple[str, object]] = []
    mutation = ChatUnavailableMutation(
        user_id="user-a",
        chat_id="chat-a",
        reason="deleted",
        chat_deleted=True,
        replayed=False,
        ended_sessions=(),
        announcement_session_keys=(),
        unaccepted_turn_ids=(),
        accepted_turn_ids=(),
        aborted_result_commit_ids=(),
    )

    class History:
        def delete_chat(self, chat_id, *, user_id):
            events.append(("durable_delete", (user_id, chat_id)))
            return mutation

    orchestrator = SimpleNamespace(
        history=History(),
        voice_services=_VoiceServices(events),
        ui_clients=[],
    )
    from orchestrator.orchestrator import Orchestrator

    orchestrator._broadcast_user_history = MethodType(Orchestrator._broadcast_user_history, orchestrator)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator))
    )

    response = asyncio.run(delete_chat(request, "chat-a", "user-a"))

    assert response.message == "Chat chat-a deleted"
    assert events == [
        ("durable_delete", ("user-a", "chat-a")),
        ("chat_unavailable", mutation),
    ]
