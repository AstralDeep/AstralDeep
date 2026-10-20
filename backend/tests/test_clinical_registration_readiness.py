"""Exercises diagnostic registration ordering through ordinary UI ingress, real human
capture, operation admission and committed answers while registration delivery is held.
"""

import asyncio
import json
from queue import Empty, Queue
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from starlette.websockets import WebSocketDisconnect

from clinical_diagnostic.dispatcher import WebSocketDispatcher
from clinical_diagnostic.tests.test_dispatcher import case, render_dispatcher_prompt
from orchestrator import human_request_authority as human
from orchestrator.evidence_context import EvidenceContext
from orchestrator.turn_guidance_authority import current_turn_guidance


class Socket:
    def __init__(self, loop):
        self.scope = {"type": "websocket", "path": "/ws", "headers": [], "query_string": b""}
        self.closed = False
        self.incoming = asyncio.Queue()
        self.outgoing = Queue()
        self.frames = []
        self.sent = []
        self.loop = loop
        self.receiving = asyncio.Event()

    async def accept(self):
        return None

    async def send(self, value):
        await self.send_text(value)

    async def send_text(self, value):
        self.frames.append(json.loads(value))
        self.outgoing.put(value)

    async def receive_text(self):
        value = await self.incoming.get()
        if value is None:
            raise WebSocketDisconnect()
        return value

    def send_client(self, value):
        self.sent.append(json.loads(value))
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, value)

    def recv_client(self, timeout):
        self.loop.call_soon_threadsafe(self.receiving.set)
        try:
            value = self.outgoing.get(timeout=timeout)
            if value is None:
                raise ConnectionError("synthetic socket closed")
            return value
        except Empty:
            raise TimeoutError() from None

    def close_client(self):
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, None)
        self.outgoing.put(None)


@pytest.mark.asyncio
async def test_actual_ingress_waits_for_final_readiness_before_one_human_turn(
        orchestrator_factory, monkeypatch, tmp_path):
    import shared.jwks_cache as jwks_cache

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_jwk = jwk.construct(public, algorithm="RS256").to_dict()
    public_jwk["kid"] = "synthetic-readiness"
    authority = "https://readiness.invalid/realm"

    async def keys(*args, **kwargs):
        return {"keys": [public_jwk]}

    monkeypatch.setattr(jwks_cache, "get_jwks", keys)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", authority)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-web")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    owner = "__bench__readiness-" + uuid4().hex
    token = jwt.encode({"sub": owner, "iss": authority, "azp": "astral-web",
        "aud": "account", "exp": int(time.time()) + 300,
        "realm_access": {"roles": ["user"]}}, key, algorithm="RS256",
        headers={"kid": "synthetic-readiness"})
    orch = await asyncio.to_thread(orchestrator_factory)
    orch._evidence_context = EvidenceContext(orch)
    from orchestrator.knowledge_synthesis import KnowledgeIndex
    orch.knowledge_index = KnowledgeIndex(str(tmp_path))
    socket = Socket(asyncio.get_running_loop())
    entered, release = asyncio.Event(), asyncio.Event()
    dashboard = orch.send_dashboard
    calls, captures = [], []
    capture = human._HumanSocketRequest.capture_session

    async def held_dashboard(ws):
        entered.set()
        await release.wait()
        await dashboard(ws)

    async def captured(request):
        await capture(request)
        captures.append((request.purpose, request.owner_id,
                         request.connection.work_registrations_pending))

    async def configured(owner_id):
        return True

    async def resolve(*args, **kwargs):
        return SimpleNamespace(client=None)

    async def model(ws, messages, tools_desc=None, temperature=None, **kwargs):
        guidance = current_turn_guidance(expected_orchestrator=orch, websocket=ws,
                                         chat_id=chat_id)
        assert guidance is not None and guidance.origin.owner_id == owner
        calls.append(messages)
        return SimpleNamespace(content="Synthetic final fact.", tool_calls=None, reasoning_content=None), SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, total_tokens=2)

    monkeypatch.setattr(human._HumanSocketRequest, "capture_session", captured)
    monkeypatch.setattr(orch, "send_dashboard", held_dashboard)
    monkeypatch.setattr(orch, "llm_configured_for", configured)
    monkeypatch.setattr(orch, "_resolve_llm_client_for", resolve)
    monkeypatch.setattr(orch, "_call_llm", model)
    chat_id = str(uuid4())
    await asyncio.to_thread(orch.history.create_chat, chat_id=chat_id, user_id=owner)
    await asyncio.to_thread(orch.history.update_chat_title, chat_id, "Synthetic readiness", user_id=owner)

    def preflight(method, url, headers, body):
        assert headers["Authorization"] == "Bearer " + token
        chat = orch.history.get_chat(chat_id, user_id=owner)
        assert chat is not None
        payload = ({"chat_id": chat_id} if method == "POST" else
                   {"components": []} if url.endswith("/components") else
                   {"chat": {"id": chat_id, "messages": []}})
        return SimpleNamespace(status=201 if method == "POST" else 200,
                               body=json.dumps(payload).encode())

    transport = SimpleNamespace(send=socket.send_client, recv=socket.recv_client, close=socket.close_client)
    adapter = WebSocketDispatcher(token=token, ws_uri="wss://readiness.invalid/ws",
        http_uri="https://readiness.invalid", http=preflight, transport_factory=lambda: transport)
    server = asyncio.create_task(orch.handle_ui_connection_fastapi(socket))
    answer = asyncio.create_task(asyncio.to_thread(adapter.answer, case()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(orch._registered_events[id(socket)].wait(), 5)
        await asyncio.wait_for(socket.receiving.wait(), 5)
        context = orch._connection_contexts[id(socket)]
        assert context.registered and context.work_registrations_pending == 1
        assert not any(frame.get("type") == "rote_config" for frame in socket.frames)
        assert [frame["type"] for frame in socket.sent] == ["register_ui"]
        assert calls == []
        release.set()
        result = await asyncio.wait_for(answer, 20)
        assert result.text == "Synthetic final fact."
        assert result.chat_id == chat_id
        assert len([frame for frame in socket.sent if frame.get("action") == "chat_message"]) == 1
        assert captures and all(purpose == "skill_lookup" and identity == owner and pending == 0
                                for purpose, identity, pending in captures)
        assert calls
        commits = [frame for frame in socket.frames if frame.get("snapshot_purpose") == "commit"]
        assert len(commits) == 1
        request = socket.sent[1]
        assert commits[0]["connection_generation"] == request["connection_generation"]
        assert commits[0]["request_generation"] == request["request_generation"]
        assert commits[0]["transcript"][0]["parts"][0]["text"] == render_dispatcher_prompt(case())
        assert any(frame.get("type") == "operation_status" and frame.get("state") == "completed"
                   for frame in socket.frames)
        assert await asyncio.to_thread(orch.audit_repo.verify_chain, owner) is None
    finally:
        release.set()
        socket.close_client()
        await asyncio.gather(answer, return_exceptions=True)
        await asyncio.gather(server, return_exceptions=True)
