"""Synthetic staging proof for the clinical dispatcher: a signed identity registers through
the real UI handler, a scripted model completes one ordinary turn, a second owner cannot
adopt the first owner's chat, and the real tool-permission gate refuses without transport.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from shared.protocol import AgentCard, AgentSkill

pytestmark = pytest.mark.asyncio

ANSWER = "Synthetic clinical answer with the required fact."
RESTRICTED_AGENT = "web-research-1"
RESTRICTED_TOOL = "web_search"


class _Socket:
    def __init__(self) -> None:
        self.scope = {"headers": [(b"origin", b"https://app.invalid")]}
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


def _assistant(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


def _tool_call():
    return SimpleNamespace(
        id="call-1", function=SimpleNamespace(name=RESTRICTED_TOOL, arguments="{}")
    )


@pytest.fixture
async def staging(orchestrator_factory, monkeypatch):
    import shared.jwks_cache as jwks_cache
    from shared.feature_flags import flags

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_jwk = jwk.construct(public, algorithm="RS256").to_dict()
    public_jwk["kid"] = "synthetic-clinical-staging"
    authority = "https://clinical-staging.invalid/realm"

    async def keys(*_args, **_kwargs):
        return {"keys": [public_jwk]}

    monkeypatch.setattr(jwks_cache, "get_jwks", keys)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", authority)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-web")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setitem(flags._flags, "user_skills", False)

    def token(owner: str) -> str:
        return jwt.encode(
            {
                "sub": owner,
                "iss": authority,
                "azp": "astral-web",
                "aud": "account",
                "exp": int(time.time()) + 300,
                "realm_access": {"roles": ["user"]},
            },
            key,
            algorithm="RS256",
            headers={"kid": "synthetic-clinical-staging"},
        )

    orch = await asyncio.to_thread(orchestrator_factory)
    orch._evidence_context = None
    replies = [_assistant(content=ANSWER)]

    async def call_llm(_websocket, _messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        return (replies.pop(0) if len(replies) > 1 else replies[0]), _usage()

    async def resolve(_websocket, *_args, **_kwargs):
        return SimpleNamespace(client=None)

    async def noop(*_args, **_kwargs):
        return None

    orch._call_llm = call_llm
    orch._resolve_llm_client_for = resolve
    orch._notify_phi_if_detected = noop
    orch.summarize_chat_title = noop
    orch._deliver_round_components = AsyncMock(return_value=[])
    orch._send_or_replace_components = AsyncMock(return_value=[])
    orch._emit_llm_usage_report = AsyncMock()
    sockets = {"a": _Socket(), "b": _Socket()}

    async def safe_send(websocket, data):
        assert websocket in sockets.values()
        websocket.sent.append(json.loads(data))
        return True

    orch._safe_send = safe_send
    owner_a = f"__bench__clinical-a-{uuid.uuid4().hex}"
    owner_b = f"__bench__clinical-b-{uuid.uuid4().hex}"
    yield SimpleNamespace(
        orch=orch, sockets=sockets, token=token, owner_a=owner_a, owner_b=owner_b,
    )


def _frames(socket, frame_type=None):
    return [
        frame for frame in socket.sent
        if frame_type is None or frame.get("type") == frame_type
    ]


async def _wait_done(socket, timeout=30.0):
    async with asyncio.timeout(timeout):
        while True:
            errors = _frames(socket, "error")
            if errors:
                return errors[-1]
            done = [
                frame for frame in _frames(socket, "chat_status")
                if frame.get("status") in ("done", "failed", "cancelled", "error")
            ]
            if done:
                return done[-1]
            await asyncio.sleep(0.02)


async def _register(staging, socket, owner):
    await staging.orch.handle_ui_message(
        socket,
        json.dumps({"type": "register_ui", "token": staging.token(owner)}),
    )
    assert staging.orch.ui_sessions[socket]["sub"] == owner


async def _send_chat(staging, socket, message, chat_id=None):
    payload = {"message": message}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    await staging.orch.handle_ui_message(
        socket,
        json.dumps({
            "type": "ui_event",
            "action": "chat_message",
            "submission_id": str(uuid.uuid4()),
            "request_generation": str(uuid.uuid4()),
            "connection_generation": str(uuid.uuid4()),
            "payload": payload,
        }),
    )


async def test_real_turn_keeps_identity_audit_and_owner_isolation(staging):
    owner_a, owner_b = staging.sockets["a"], staging.sockets["b"]
    await _register(staging, owner_a, staging.owner_a)
    await _send_chat(staging, owner_a, "Synthetic clinical staging question?")
    assert (await _wait_done(owner_a))["status"] == "done"
    created = _frames(owner_a, "chat_created")
    assert created, "the real dispatcher must create a chat for an ordinary turn"
    chat_id = created[0]["payload"]["chat_id"]
    renders = _frames(owner_a, "ui_render")
    assert any(ANSWER in json.dumps(frame) for frame in renders)
    assert any(
        frame["event"]["action_type"] == "ws.chat_message"
        for frame in _frames(owner_a, "audit_append")
    )
    assert await asyncio.to_thread(staging.orch.audit_repo.verify_chain, staging.owner_a) is None
    assert await asyncio.to_thread(
        staging.orch.history.get_chat, chat_id, user_id=staging.owner_b
    ) is None

    await _register(staging, owner_b, staging.owner_b)
    await _send_chat(staging, owner_b, "Synthetic foreign chat attempt.", chat_id=chat_id)
    await _wait_done(owner_b)
    assert _frames(owner_b, "error")
    assert ANSWER not in json.dumps(_frames(owner_b, "error"))
    assert ANSWER not in json.dumps(_frames(owner_b, "ui_render"))
    assert await asyncio.to_thread(
        staging.orch.history.get_chat, chat_id, user_id=staging.owner_b
    ) is None
    assert await asyncio.to_thread(
        staging.orch.history.get_chat, chat_id, user_id=staging.owner_a
    ) is not None


async def test_real_tool_permission_gate_refuses_without_transport(staging):
    owner_a = staging.sockets["a"]
    await _register(staging, owner_a, staging.owner_a)
    orch = staging.orch
    orch.agent_cards[RESTRICTED_AGENT] = AgentCard(
        name="Web research", description="public research",
        agent_id=RESTRICTED_AGENT,
        skills=[AgentSkill(
            name="search", description="search the public web",
            id=RESTRICTED_TOOL, input_schema={"type": "object"},
        )],
    )
    orch.agents[RESTRICTED_AGENT] = object()
    orch.tool_permissions.is_tool_allowed = lambda *_args, **_kwargs: False
    transported = []

    async def forbidden(*args, **kwargs):
        transported.append((args, kwargs))
        return None

    orch.send_to_agent = forbidden
    orch._execute_in_process = forbidden
    response = await orch.execute_single_tool(
        owner_a,
        _tool_call(),
        {RESTRICTED_TOOL: RESTRICTED_AGENT},
        "synthetic-chat",
        user_id=staging.owner_a,
        tool_to_unqualified={},
    )
    assert response is not None and response.error is not None
    assert "restricted for this agent" in response.error["message"]
    assert transported == []
    alert = _frames(owner_a, "ui_render")
    assert any("restricted for this agent" in json.dumps(frame) for frame in alert)
    assert await asyncio.to_thread(
        orch.audit_repo.verify_chain, staging.owner_a
    ) is None