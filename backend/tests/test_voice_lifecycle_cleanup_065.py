"""Feature-065 lifecycle wiring at chat and authentication boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import web_auth
from orchestrator.api import delete_chat
from orchestrator.auth import auth_router, get_current_user_payload
from orchestrator.voice_sessions import ChatUnavailableMutation


class _VoiceServices:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events

    async def end_user_voice_session(self, *, user_id: str, reason: str) -> None:
        self.events.append(("voice_end", (user_id, reason)))

    async def handle_chat_unavailable(self, mutation) -> None:
        self.events.append(("chat_unavailable", mutation))


def test_native_logout_ends_identity_voice_before_token_revocation(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    app = FastAPI()
    app.include_router(auth_router)
    app.state.orchestrator = SimpleNamespace(voice_services=_VoiceServices(events))
    app.dependency_overrides[get_current_user_payload] = lambda: {"sub": "user-a"}
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-desktop")

    async def revoke(user_id, refresh_token, *, client_id=None):
        events.append(("revoke", (user_id, refresh_token, client_id)))
        return "revoked"

    async def destroy(user_id, reason):
        events.append(("destroy", (user_id, reason)))

    monkeypatch.setattr(web_auth, "_revoke_or_queue", revoke)
    monkeypatch.setattr(web_auth, "_destroy_machine_credentials", destroy)

    response = TestClient(app).post(
        "/api/auth/logout",
        json={"refresh_token": "refresh-a", "client_id": "astral-desktop"},
    )

    assert response.status_code == 200
    assert events[:2] == [
        ("voice_end", ("user-a", "logout")),
        ("revoke", ("user-a", "refresh-a", "astral-desktop")),
    ]


def test_web_logout_ends_voice_once_after_local_session_fence(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    sessions = [
        {"sub": "user-a", "refresh_token": "refresh-a"},
        None,
    ]

    async def session_by_sid(_sid):
        return sessions.pop(0)

    async def kill_session(sid, _session):
        events.append(("local_end", sid))

    monkeypatch.setattr(web_auth, "_unsign", lambda _raw: "session-a")
    monkeypatch.setattr(web_auth, "_asession_by_sid", session_by_sid)
    monkeypatch.setattr(web_auth, "_kill_session", kill_session)
    monkeypatch.setattr(web_auth, "_is_mock", lambda: True)
    request = SimpleNamespace(
        cookies={web_auth.COOKIE_NAME: "signed-session"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                orchestrator=SimpleNamespace(voice_services=_VoiceServices(events))
            )
        ),
        base_url="http://localhost:8001/",
    )

    first = asyncio.run(web_auth.auth_logout(request))
    second = asyncio.run(web_auth.auth_logout(request))

    assert first.status_code == second.status_code == 303
    assert events == [
        ("local_end", "session-a"),
        ("voice_end", ("user-a", "logout")),
    ]


def test_web_auth_expiry_ends_identity_voice_session(monkeypatch) -> None:
    events: list[tuple[str, object]] = []

    async def session(_request):
        return {
            "sid": "session-a",
            "sub": "user-a",
            "access_token": "expired-access-token",
        }

    async def refresh(_sid, _session):
        return None

    monkeypatch.setattr(web_auth, "_is_mock", lambda: False)
    monkeypatch.setattr(web_auth, "aget_session", session)
    monkeypatch.setattr(web_auth, "_token_expires_at", lambda _token: 0.0)
    monkeypatch.setattr(web_auth, "_refresh_session", refresh)
    request = SimpleNamespace(
        cookies={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                orchestrator=SimpleNamespace(voice_services=_VoiceServices(events))
            )
        ),
    )

    assert asyncio.run(web_auth.ensure_session(request)) is None
    assert events == [("voice_end", ("user-a", "auth_expired"))]


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
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(orchestrator=orchestrator))
    )

    response = asyncio.run(delete_chat(request, "chat-a", "user-a"))

    assert response.message == "Chat chat-a deleted"
    assert events == [
        ("durable_delete", ("user-a", "chat-a")),
        ("chat_unavailable", mutation),
    ]
