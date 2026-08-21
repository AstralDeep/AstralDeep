"""Typed-versus-voice authorization parity proofs for Feature 065.

These tests intentionally enter the production credential, gate, retry, and
cancellation seams with two different transport objects.  A voice final is an
ordinary authenticated user turn after proof verification; it must not gain a
second authorization path or lose any of the typed path's protections.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from orchestrator import memory_chat
from orchestrator.async_tasks import DurableUserTurnWebSocket
from orchestrator.orchestrator import GateRefusal, Orchestrator, PreparedDispatch
from personalization.memory_tools import MemoryTools
from shared.protocol import MCPResponse

USER_ID = "voice-parity-owner"
USERNAME = "voice.parity"
RAW_KEYCLOAK_TOKEN = "memory-only-keycloak-token-065"
AGENT_ID = "parity-agent"
TOOL_ID = "read_record"


class _TypedSocket:
    client = ("typed-test", 65)


class _Permissions:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def is_tool_allowed(self, user_id: str, agent_id: str, tool_id: str) -> bool:
        assert (user_id, agent_id) == (USER_ID, AGENT_ID)
        return tool_id in self.allowed

    def get_enabled_scope_names(self, user_id: str, agent_id: str) -> list[str]:
        assert (user_id, agent_id) == (USER_ID, AGENT_ID)
        return ["tools:read"] if self.allowed else []


class _Delegation:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def exchange_token_for_agent(
        self,
        raw_token: str,
        agent_id: str,
        allowed_tools: list[str],
        user_id: str,
        scopes: list[str],
    ) -> dict[str, str]:
        self.calls.append(
            (raw_token, agent_id, tuple(allowed_tools), user_id, tuple(scopes))
        )
        return {"access_token": "attenuated-parity-token"}


def _sockets() -> tuple[_TypedSocket, DurableUserTurnWebSocket]:
    typed = _TypedSocket()
    voice = DurableUserTurnWebSocket(typed, user_id=USER_ID)
    return typed, voice


def _claims() -> dict[str, Any]:
    return {
        "sub": USER_ID,
        "preferred_username": USERNAME,
        "realm_access": {"roles": ["clinician"]},
        "_raw_token": RAW_KEYCLOAK_TOKEN,
    }


def _gate_runtime(
    typed: object,
    voice: object,
    *,
    allowed: set[str] | None = None,
) -> tuple[Orchestrator, _Delegation]:
    runtime = Orchestrator.__new__(Orchestrator)
    runtime.ui_sessions = {typed: _claims(), voice: _claims()}
    runtime.security_flags = {AGENT_ID: {"blocked_tool": {"blocked": True}}}
    runtime.tool_permissions = _Permissions(
        {TOOL_ID, "send_email"} if allowed is None else allowed
    )
    runtime.agent_cards = {
        AGENT_ID: SimpleNamespace(
            skills=[
                SimpleNamespace(id=TOOL_ID),
                SimpleNamespace(id="send_email"),
                SimpleNamespace(id="blocked_tool"),
            ],
            metadata={},
        )
    }
    runtime.agents = {AGENT_ID: object()}
    runtime.a2a_clients = {}
    runtime.local_agents = {}
    runtime.credential_manager = SimpleNamespace(
        get_agent_credentials_encrypted=lambda _user, _agent: None
    )
    runtime._delegation_failed_agents = set()
    delegation = _Delegation()
    runtime.delegation = delegation
    runtime._delegation_required = lambda: True  # type: ignore[method-assign]

    async def auto_subscribe(*_args: Any, **_kwargs: Any) -> None:
        return None

    runtime._auto_subscribe_stream_artifacts = auto_subscribe
    return runtime, delegation


@pytest.mark.asyncio
async def test_keycloak_owner_llm_and_rfc8693_scope_match_typed_turn() -> None:
    typed, voice = _sockets()
    runtime, delegation = _gate_runtime(typed, voice)

    class _Store:
        async def get(self, user_id: str) -> SimpleNamespace:
            return SimpleNamespace(owner=user_id, model="user-model")

        async def get_system(self) -> SimpleNamespace:
            return SimpleNamespace(owner="system", model="system-model")

    async def drain() -> None:
        return None

    runtime._llm_store = _Store()
    runtime._CredentialSource = SimpleNamespace(USER="user", SYSTEM="system")
    runtime._build_llm_client = lambda config, source: (config, source)
    runtime._drain_llm_discard_notes = drain

    typed_llm = await Orchestrator._resolve_llm_client_for(runtime, typed)
    voice_llm = await Orchestrator._resolve_llm_client_for(runtime, voice)
    system_llm = await Orchestrator._resolve_llm_client_for(runtime, None)

    assert typed_llm == voice_llm
    assert typed_llm[0].owner == USER_ID
    assert typed_llm[1] == "user"
    assert system_llm[0].owner == "system"
    assert system_llm[1] == "system"
    assert (
        Orchestrator._llm_audit_principals(runtime, typed)
        == Orchestrator._llm_audit_principals(runtime, voice)
        == (
            USER_ID,
            USERNAME,
        )
    )

    outcomes = [
        await Orchestrator._run_gate_stack(
            runtime,
            socket,
            AGENT_ID,
            TOOL_ID,
            {"record": "123"},
            user_id=USER_ID,
        )
        for socket in (typed, voice)
    ]
    assert all(isinstance(outcome, PreparedDispatch) for outcome in outcomes)
    assert (
        outcomes[0].args
        == outcomes[1].args
        == {
            "record": "123",
            "_delegation_token": "attenuated-parity-token",
        }
    )
    assert outcomes[0].delegation_token == outcomes[1].delegation_token
    assert delegation.calls == [
        (
            RAW_KEYCLOAK_TOKEN,
            AGENT_ID,
            (TOOL_ID, "send_email"),
            USER_ID,
            ("tools:read",),
        ),
        (
            RAW_KEYCLOAK_TOKEN,
            AGENT_ID,
            (TOOL_ID, "send_email"),
            USER_ID,
            ("tools:read",),
        ),
    ]


@pytest.mark.asyncio
async def test_tool_denial_and_egress_confirmation_have_identical_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed, voice = _sockets()
    runtime, delegation = _gate_runtime(typed, voice, allowed={"send_email"})

    denied = [
        await Orchestrator._run_gate_stack(
            runtime,
            socket,
            AGENT_ID,
            TOOL_ID,
            {"record": "123"},
            user_id=USER_ID,
        )
        for socket in (typed, voice)
    ]
    assert all(isinstance(outcome, GateRefusal) for outcome in denied)
    assert denied[0].response.error == denied[1].response.error
    assert "restricted" in denied[0].response.error["message"]
    assert delegation.calls == []

    monkeypatch.setenv("FF_RUNTIME_SUPERVISOR", "false")
    monkeypatch.setenv("FF_HITL_HIGHRISK", "true")
    confirmed = [
        await Orchestrator._run_gate_stack(
            runtime,
            socket,
            AGENT_ID,
            "send_email",
            {"to": "outside@example.test", "body": "bounded"},
            user_id=USER_ID,
        )
        for socket in (typed, voice)
    ]
    assert all(isinstance(outcome, GateRefusal) for outcome in confirmed)
    assert confirmed[0].response.error == confirmed[1].response.error
    assert confirmed[0].response.error == {
        "message": "This will send data off this system — confirm?",
        "retryable": False,
    }
    assert delegation.calls == []


@pytest.mark.asyncio
async def test_phi_memory_gate_and_audit_verdict_match_typed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed, voice = _sockets()
    phi = "Patient Jane Doe has record 123-45-6789"

    class _NeverPersist:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"PHI reached persistence method {name}")

    class _AlwaysPhi:
        @staticmethod
        def contains_phi(_value: str) -> bool:
            return True

    audited: list[tuple[str, str, str]] = []

    async def audit(
        user_id: str,
        action_type: str,
        _description: str,
        outcome: str = "success",
        **_kwargs: Any,
    ) -> None:
        audited.append((user_id, action_type, outcome))

    monkeypatch.setattr(memory_chat, "_audit", audit)
    results: list[MCPResponse] = []
    for socket in (typed, voice):
        runtime = SimpleNamespace(
            _memory_tools=MemoryTools(_NeverPersist(), phi_gate=_AlwaysPhi()),
        )
        result = await memory_chat.handle_meta_tool(
            runtime,
            "remember",
            {"category": "context", "value": phi},
            user_id=USER_ID,
            chat_id="chat-parity",
            websocket=socket,
        )
        results.append(result)

    assert [result.result["status"] for result in results] == [
        "refused",
        "refused",
    ]
    assert results[0].result == results[1].result
    assert phi not in repr(results)
    assert audited == [
        (USER_ID, "memory.remember_refused", "denied"),
        (USER_ID, "memory.remember_refused", "denied"),
    ]


@pytest.mark.asyncio
async def test_llm_retry_and_audit_attribution_match_typed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed, voice = _sockets()
    runtime = Orchestrator.__new__(Orchestrator)
    runtime.ui_sessions = {typed: _claims(), voice: _claims()}
    runtime._CredentialSource = SimpleNamespace(USER="user", SYSTEM="system")
    runtime._LLMUnavailable = RuntimeError
    runtime._llm_unsupported_params = {}
    runtime.MAX_RETRIES = 2
    runtime.audit_recorder = object()
    runtime.llm_reasoning_effort = None
    runtime.rote = SimpleNamespace()

    resolved = SimpleNamespace(
        model="parity-model",
        base_url="https://llm.example.test/v1",
    )

    class _TransientStatusError(RuntimeError):
        status_code = 503

    class _Completions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **_kwargs: Any) -> SimpleNamespace:
            self.calls += 1
            if self.calls == 1:
                raise _TransientStatusError("provider response body")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="done"))],
                usage=SimpleNamespace(total_tokens=7),
            )

    clients: list[_Completions] = []

    async def resolve(_socket: object) -> tuple[Any, str, Any]:
        completions = _Completions()
        clients.append(completions)
        return (
            SimpleNamespace(chat=SimpleNamespace(completions=completions)),
            "user",
            resolved,
        )

    audits: list[dict[str, Any]] = []
    usage_reports: list[tuple[object, str]] = []

    async def record(_recorder: object, **values: Any) -> None:
        audits.append(values)

    async def usage(socket: object, **values: Any) -> None:
        usage_reports.append((socket, values["outcome"]))

    async def no_sleep(_delay: float) -> None:
        return None

    runtime._resolve_llm_client_for = resolve
    runtime._record_llm_call = record
    runtime._emit_llm_usage_report = usage
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    monkeypatch.setenv("FF_MODEL_ROUTER", "false")
    monkeypatch.setattr("orchestrator.orchestrator.asyncio.sleep", no_sleep)

    responses = [
        await Orchestrator._call_llm(
            runtime,
            socket,
            [{"role": "user", "content": "same request"}],
            feature="voice_parity",
        )
        for socket in (typed, voice)
    ]

    assert [message.content for message, _usage in responses] == ["done", "done"]
    assert [client.calls for client in clients] == [2, 2]
    assert [
        (
            item["actor_user_id"],
            item["auth_principal"],
            item["credential_source"],
            item["outcome"],
        )
        for item in audits
    ] == [
        (USER_ID, USERNAME, "user", "success"),
        (USER_ID, USERNAME, "user", "success"),
    ]
    assert usage_reports == [(typed, "success"), (voice, "success")]


@pytest.mark.asyncio
async def test_tool_cancellation_propagates_without_a_voice_retry() -> None:
    typed, voice = _sockets()
    runtime = Orchestrator.__new__(Orchestrator)
    runtime.MAX_RETRIES = 3
    runtime.RETRY_BACKOFF = (0.01, 0.01)
    runtime.ui_sessions = {typed: _claims(), voice: _claims()}
    attempts: dict[object, int] = {typed: 0, voice: 0}
    entered: dict[object, asyncio.Event] = {
        typed: asyncio.Event(),
        voice: asyncio.Event(),
    }

    async def execute(
        _agent_id: str,
        _tool_name: str,
        _args: dict[str, Any],
        *,
        timeout: float,
        ui_websocket: object,
        protected_owner_id: str | None,
        protected_channel: str,
        protected_audit_correlation_id: str | None,
        protected_actor_user_id: str | None,
        protected_auth_principal: str | None,
        protected_conversation_id: str | None,
    ) -> MCPResponse:
        del timeout
        assert protected_owner_id is None
        assert protected_channel == "websocket"
        assert protected_audit_correlation_id is None
        assert protected_actor_user_id is None
        assert protected_auth_principal is None
        assert protected_conversation_id is None
        attempts[ui_websocket] += 1
        entered[ui_websocket].set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    runtime.execute_tool_and_wait = execute

    tasks = {
        socket: asyncio.create_task(
            Orchestrator._execute_with_retry(
                runtime,
                socket,
                AGENT_ID,
                TOOL_ID,
                {},
            )
        )
        for socket in (typed, voice)
    }
    try:
        # Fail quickly if a stale dispatch fake rejects the current protected
        # call shape before entering. The unconditional cleanup below keeps
        # either regression from stranding tasks and hanging the full suite.
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in entered.values())),
            timeout=1.0,
        )
        for task in tasks.values():
            task.cancel()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=1.0,
        )
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)

    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert attempts == {typed: 1, voice: 1}
