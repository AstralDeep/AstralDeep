"""Exercises canonical clinical placement with the normal host graph and durable context accounting.
Synthetic IAM and bounded provider transports remain developer tests, not clinical or staging qualification.
"""

import asyncio
import importlib
import importlib.util
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.fhir.fhir_agent import FhirAgent
from agents.fhir.tests.test_extraction import NOTE, NOTE_ID, golden_payload
from orchestrator.evidence_context import EvidenceContext
from shared.feature_flags import flags
from shared.isolated_http import IsolatedHttpResponse
from shared.protocol import RegisterAgent
from tests.test_request_session_authority_088 import signing_key as signing_key


def clinical_module():
    assert importlib.util.find_spec("orchestrator.clinical_extraction") is not None
    return importlib.import_module("orchestrator.clinical_extraction")


@pytest.fixture
async def host(orchestrator_factory, monkeypatch, tmp_path):
    for name in (
        "fhir",
        "clinical_note_extract",
        "inprocess_agents",
        "observation_packing",
    ):
        monkeypatch.setitem(flags._flags, name, True)
    monkeypatch.setitem(flags._flags, "user_skills", False)
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
    orch = await asyncio.to_thread(orchestrator_factory)
    agent = FhirAgent(port=0)
    await orch.register_agent(None, RegisterAgent(agent_card=agent.card))
    orch.local_agents[agent.agent_id] = agent
    orch._evidence_context = EvidenceContext(orch)
    return SimpleNamespace(orch=orch, agent=agent)


@pytest.mark.asyncio
async def test_canonical_host_service_registers_exact_fhir_implementation(host):
    module = clinical_module()
    assert module.register(host.orch) is True
    assert module.current(host.orch) is True
    host.orch.local_agents["fhir-1"] = object()
    assert module.current(host.orch) is False


@pytest.mark.asyncio
async def test_clinical_only_mode_refuses_before_capture_or_send(host, monkeypatch):
    module = clinical_module()
    assert module.register(host.orch)
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    calls = []

    async def capture(_):
        calls.append("capture")

    monkeypatch.setattr(host.orch._llm_store, "capture_user", capture)
    response = await module.invoke(
        host.orch,
        object(),
        {"note_text": NOTE},
        owner="unused",
        chat=str(uuid4()),
        timeout=60,
    )
    assert response.error["code"] == "EXTRACT_CONTEXT_UNAVAILABLE"
    assert calls == []


def wire_response(note=NOTE, note_id=NOTE_ID, **changes):
    value = {
        "id": "synthetic-completion",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-clinical-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(golden_payload(note, note_id)),
                },
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    }
    value.update(changes)
    return IsolatedHttpResponse(
        200,
        json.dumps(value).encode(),
        "https://clinical.invalid/v1/chat/completions",
        "application/json",
    )


@pytest.fixture
async def normal_turn(host, monkeypatch, signing_key, tmp_path):
    from contextlib import asynccontextmanager
    import time
    from jose import jwt
    from orchestrator import (
        auth,
        context_authority,
        human_request_authority as human,
        turn_guidance_authority as guidance,
        web_auth,
    )
    from orchestrator.orchestrator import ConnectionContext
    from orchestrator.work_admission import (
        AdmissionClass,
        OperationOwner,
        OperationRequest,
        OwnerScope,
        OperationState,
    )
    from tests.test_human_socket_authority_088 import Socket

    authority = "https://clinical-authority.invalid/realm"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.invalid")
    monkeypatch.setenv("WEB_SESSION_SECRET", "synthetic-clinical-cookie-key")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", authority)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-web")
    monkeypatch.setenv("KEYCLOAK_ALLOWED_AZP", "astral-web")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    for name in ("policy_engine", "hook_system", "taint_tracking", "hitl_highrisk"):
        monkeypatch.setitem(flags._flags, name, False)

    async def keys(*_args, **_kwargs):
        return signing_key[1]

    monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
    orch = host.orch
    owner, sid, chat = "__bench__clinical-" + uuid4().hex, uuid4().hex, str(uuid4())
    token = jwt.encode(
        {
            "sub": owner,
            "iss": authority,
            "azp": "astral-web",
            "aud": "account",
            "exp": int(time.time()) + 300,
            "realm_access": {"roles": ["user"]},
        },
        signing_key[0],
        algorithm="RS256",
        headers={"kid": "synthetic-request-authority"},
    )
    orch.web_sessions.create(
        sid,
        user_id=owner,
        access_token=token,
        refresh_token="synthetic-refresh",
        hard_max_seconds=3600,
    )
    await asyncio.to_thread(orch.history.create_chat, chat, owner)
    await asyncio.to_thread(
        orch.history.update_chat_title, chat, "Synthetic clinical", user_id=owner
    )
    orch._llm_store.set_sync(
        owner,
        provider="custom",
        base_url="https://clinical.invalid/v1",
        model="synthetic-clinical-model",
        api_key="synthetic-configured-key",
    )
    orch._data_sharing_store.acknowledge_sync(owner)
    orch.tool_permissions.set_agent_scopes(owner, "fhir-1", {"tools:read": True})
    policy = tmp_path / "clinical-budget.json"
    policy.write_text(
        json.dumps(
            [
                dict(
                    owner_id=owner,
                    provider="custom",
                    base_url="https://clinical.invalid/v1",
                    model="synthetic-clinical-model",
                    context_tokens=32768,
                    max_output_tokens=2048,
                    output_parameter="max_tokens",
                )
            ]
        )
    )
    monkeypatch.setenv("ASTRAL_CONTEXT_BUDGET_POLICY", str(policy))
    socket = Socket(
        {
            "type": "websocket",
            "path": "/ws",
            "query_string": b"",
            "headers": [
                (b"origin", b"https://app.invalid"),
                (b"cookie", f"astral_session={web_auth._sign(sid)}".encode()),
            ],
        }
    )
    claims = await auth.verify_user(await auth.verify_production_token(token))
    orch.ui_sessions[socket] = {**claims, "_raw_token": token}
    connection = ConnectionContext(
        socket,
        uuid4(),
        time.monotonic() + 30,
        registered=True,
        connection_generation=uuid4(),
    )
    orch._connection_contexts[id(socket)] = connection
    message = dict(
        type="ui_event",
        action="chat_message",
        submission_id=str(uuid4()),
        request_generation=str(uuid4()),
        connection_generation=str(connection.connection_generation),
        session_id=chat,
        payload={"text": "Extract this synthetic clinical note"},
    )
    pending = human.capture_human_socket_request(
        orch.human_request_boundary,
        websocket=socket,
        context=connection,
        message=message,
        purpose="skill_lookup",
    )
    await pending.capture_session()
    coordinator = orch.work_admission
    accepted = coordinator.submit(
        OperationRequest(
            "connection_frame",
            AdmissionClass.INTERACTIVE,
            OperationOwner(OwnerScope.USER, owner, None),
            uuid4(),
            "clinical_test",
            str(uuid4()),
            "a" * 64,
            chat,
            None,
            connection.connection_generation,
            uuid4(),
        )
    )
    claim = coordinator.claim_operation(
        AdmissionClass.INTERACTIVE, accepted.operation_id
    )
    context = {"operation": claim.operation, "execution_fence": claim.fence}
    state = SimpleNamespace(
        **vars(host),
        owner=owner,
        socket=socket,
        chat=chat,
        policy=policy,
        connection=connection,
        pending=pending,
        token=token,
    )

    @asynccontextmanager
    async def enter():
        caller = await pending.authenticate()
        origin = await guidance.capture_turn_guidance_from_human(
            caller, expected_orchestrator=orch
        )
        binding = guidance.bind_foreground_guidance(
            origin,
            expected_orchestrator=orch,
            websocket=socket,
            operation_context=context,
            chat_id=chat,
        )
        human.retire_socket_human_read(caller)
        state.origin = origin
        with guidance.use_turn_guidance(binding, expected_orchestrator=orch):
            lease = await context_authority.capture_context_authority(
                orchestrator=orch, websocket=socket, chat_id=chat
            )
            with context_authority.use_context_authority(lease):
                state.lease = lease
                yield state

    enter.state = state
    try:
        yield enter
    finally:
        await asyncio.to_thread(
            coordinator.terminalize,
            claim.fence,
            state=OperationState.CANCELLED,
            terminal_code="test_finished",
            safe_summary="Fixture finished",
            retry_after_ms=None,
        )
        if hasattr(state, "origin"):
            state.origin.close()
        pending.close()
        orch.web_sessions.delete(sid)
        orch.ui_sessions.pop(socket, None)
        orch._connection_contexts.pop(id(socket), None)


@pytest.mark.asyncio
async def test_retired_metadata_turn_uses_its_existing_context_and_durable_attempt(
    normal_turn, monkeypatch
):
    module = clinical_module()
    calls = []

    async def physical(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error is None
        assert response.result["outcome"] == "extracted"
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["model_calls"] == 1 and totals["pending"] == 0
        assert totals["usage"]["total_tokens"] == {"known": 50, "unknown": 0}
        assert len(calls) == 1
        request = calls[0][2]["json_body"]
        assert request["messages"][1]["content"].count(NOTE) == 1
        assert not {"tools", "tool_choice"} & request.keys()
        assert request["max_tokens"] == 2048 and request["stream"] is False
        assert calls[0][2]["timeout_seconds"] <= 60
        assert calls[0][2]["max_response_bytes"] <= 2 * 1024 * 1024


@pytest.mark.parametrize("wrapped", ["```json\n%s\n```", "<think>ignore</think>%s"])
def test_wire_envelope_requires_strict_json(wrapped):
    module = clinical_module()
    response = wire_response()
    altered = IsolatedHttpResponse(
        200,
        (wrapped % response.body.decode()).encode(),
        response.final_url,
        response.content_type,
    )
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(altered, "synthetic-clinical-model", 2048, 32768)


@pytest.mark.asyncio
async def test_normal_authorized_dispatch_retains_tool_and_model_audits(
    normal_turn, monkeypatch
):
    module = clinical_module()
    calls = []

    async def physical(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        response = await state.orch.execute_authorized_tool(
            claims=state.orch.ui_sessions[state.socket],
            user_id=state.owner,
            agent_id="fhir-1",
            tool_name="extract_note_fields",
            arguments={"note_text": NOTE, "note_id": NOTE_ID},
            channel="websocket",
            chat_id=state.chat,
            websocket=state.socket,
            timeout=60,
        )
        assert response.error is None
        assert response.result["outcome"] == "extracted"
        assert len(calls) == 1
        rows, _ = await asyncio.to_thread(
            state.orch.audit_repo.list_for_user, state.owner
        )
        assert any(row.event_class == "agent_tool_call" for row in rows)
        assert any(row.action_type == "llm.call.clinical_note_extract" for row in rows)
        assert any(row.action_type == "context.usage" for row in rows)
        stored = "".join(
            row.model_dump_json() for row in rows if row.event_class == "llm_call"
        )
        assert (
            NOTE not in stored
            and NOTE_ID not in stored
            and "synthetic-configured-key" not in stored
        )


@pytest.mark.asyncio
async def test_preparation_keeps_provider_secrets_out_of_clinical_arguments(
    normal_turn,
):
    from orchestrator.orchestrator import GateRefusal

    async with normal_turn() as state:
        prepared = await state.orch._authorize_and_prepare(
            state.socket,
            "fhir-1",
            "extract_note_fields",
            {"note_text": NOTE, "note_id": NOTE_ID},
            state.chat,
            state.owner,
        )
        assert not isinstance(prepared, GateRefusal)
        assert set(prepared.args) == {"note_text", "note_id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loss",
    [
        "agent",
        "card",
        "registry",
        "server_tools",
        "service",
        "config",
        "consent",
        "runtime",
        "admission",
        "audit",
        "recorder",
        "permissions",
        "lets",
        "context",
        "ledger",
        "observer",
        "fhir",
        "inprocess_agents",
        "clinical_note_extract",
        "context_flags",
    ],
)
async def test_current_host_dependencies_fail_closed_without_provider_effect(
    normal_turn, monkeypatch, loss
):
    from agents.fhir import mcp_tools

    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        orch = state.orch
        assert module.register(orch)
        with monkeypatch.context() as change:
            changes = {
                "config": "_llm_store",
                "consent": "_data_sharing_store",
                "runtime": "runtime_composition",
                "admission": "work_admission",
                "audit": "audit_repo",
                "recorder": "audit_recorder",
                "permissions": "tool_permissions",
                "lets": "lets_runtime",
                "context": "_evidence_context",
                "service": "_clinical_extraction",
            }
            if loss in changes:
                change.setattr(orch, changes[loss], object())
            elif loss == "agent":
                change.setitem(orch.local_agents, "fhir-1", object())
            elif loss == "card":
                change.setitem(orch.agent_cards, "fhir-1", object())
            elif loss == "registry":
                change.setitem(
                    mcp_tools.TOOL_REGISTRY["extract_note_fields"],
                    "function",
                    lambda **_: {},
                )
            elif loss == "server_tools":
                change.setattr(
                    state.agent.mcp_server, "tools", dict(mcp_tools.TOOL_REGISTRY)
                )
            elif loss == "ledger":
                change.setattr(orch._evidence_context, "usage", object())
            elif loss == "observer":
                change.setattr(orch._evidence_context.usage, "_record", lambda _: None)
            elif loss == "context_flags":
                change.setitem(flags._flags, "observation_packing", False)
                change.setitem(flags._flags, "safe_compaction", False)
            else:
                change.setitem(flags._flags, loss, False)
            response = await module.invoke(
                orch,
                state.socket,
                {"note_text": NOTE},
                owner=state.owner,
                chat=state.chat,
                timeout=60,
            )
            assert response.error and response.error["retryable"] is False
            assert not response.ui_components and calls == []


async def revoke(state, loss, monkeypatch):
    from datetime import UTC, datetime

    orch = state.orch
    if loss == "config":
        await orch._llm_store.set(
            state.owner,
            provider="custom",
            base_url="https://clinical.invalid/v1",
            model="synthetic-clinical-model",
            api_key="synthetic-rotated-key",
        )
    elif loss == "consent":

        def withdraw():
            runtime = orch.runtime_composition.plane.runtime
            with runtime.transaction() as tx:
                runtime.repositories.preferences.data_sharing.acknowledge(
                    tx,
                    owner_id=state.owner,
                    notice_version="withdrawn",
                    at=datetime.now(UTC),
                )

        await asyncio.to_thread(withdraw)
    elif loss == "permission":
        await asyncio.to_thread(
            orch.tool_permissions.set_agent_scopes,
            state.owner,
            "fhir-1",
            {"tools:read": False},
        )
    elif loss == "registration":
        orch.ui_sessions[state.socket] = dict(orch.ui_sessions[state.socket])
    elif loss == "connection":
        state.connection.closing = True
    elif loss == "flag":
        monkeypatch.setitem(flags._flags, "clinical_note_extract", False)
    elif loss == "context_flag":
        monkeypatch.setitem(flags._flags, "observation_packing", False)
    elif loss == "key":
        monkeypatch.setenv(
            "AUDIT_HMAC_SECRET", "synthetic-other-binding-secret-0123456789abcdef"
        )
    elif loss == "private_hosts":
        monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "localhost")
    elif loss == "service":
        orch._clinical_extraction = object()
    elif loss == "session":
        credential = state.origin.credential
        await asyncio.to_thread(orch.web_sessions.delete, credential.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_send", "after_send"])
@pytest.mark.parametrize(
    "loss",
    [
        "config",
        "consent",
        "permission",
        "registration",
        "connection",
        "flag",
        "context_flag",
        "private_hosts",
        "service",
        "session",
    ],
)
async def test_revoke_across_await_cannot_send_or_deliver(
    normal_turn, monkeypatch, phase, loss
):
    module = clinical_module()
    calls = []
    async with normal_turn() as state:
        assert module.register(state.orch)
        prepare = state.orch._evidence_context.prepare_model

        async def guarded_prepare(**kwargs):
            original = await prepare(**kwargs)

            async def guard():
                await original()
                if phase == "before_send":
                    await revoke(state, loss, monkeypatch)

            return guard

        async def physical(*_args, **_kwargs):
            calls.append(True)
            if phase == "after_send":
                await revoke(state, loss, monkeypatch)
            return wire_response()

        monkeypatch.setattr(
            state.orch._evidence_context, "prepare_model", guarded_prepare
        )
        monkeypatch.setattr("shared.isolated_http.request", physical)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and not response.ui_components
        assert len(calls) == (phase == "after_send")
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["pending"] == 0 and totals["model_calls"] == 1
        assert totals["usage"]["total_tokens"] == (
            {"known": 50, "unknown": 0} if calls else {"known": 0, "unknown": 1}
        )


@pytest.mark.asyncio
async def test_cancel_joins_supervised_provider_and_settles_unknown_spend(
    normal_turn, monkeypatch
):
    module = clinical_module()
    entered, cleaned = asyncio.Event(), asyncio.Event()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaned.set()
            from shared.isolated_http import IsolatedHttpCancelled

            raise IsolatedHttpCancelled(cleanup_confirmed=True) from None

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        task = asyncio.create_task(
            module.invoke(
                state.orch,
                state.socket,
                {"note_text": NOTE},
                owner=state.owner,
                chat=state.chat,
                timeout=60,
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert cleaned.is_set() and calls == [True]
        rows, _ = await asyncio.to_thread(
            state.orch.audit_repo.list_for_user, state.owner
        )
        assert any(
            row.action_type == "llm.call.clinical_note_extract"
            and row.outcome == "failure"
            for row in rows
        )
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["model_calls"] == 1 and totals["pending"] == 0
        assert totals["usage"]["total_tokens"] == {"known": 0, "unknown": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"model": "foreign"},
        {"object": "text_completion"},
        {"id": ""},
        {"created": True},
        {"choices": []},
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": True,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}", "tool_calls": []},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "{}", "refusal": "no"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": [], "annotations": []},
                }
            ]
        },
        {"usage": None},
        {"usage": {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}},
        {"usage": {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0}},
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 3000,
                "total_tokens": 3020,
            }
        },
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 21},
            }
        },
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "completion_tokens_details": {"other": 1},
            }
        },
        {"other": "unsupported"},
    ],
)
def test_strict_wire_shape_and_usage_deny_incomplete_or_unsupported_results(change):
    module = clinical_module()
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(
            wire_response(**change), "synthetic-clinical-model", 2048, 32768
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"choices":[],"choices":[]}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[]",
        b"\xff",
        b'{"x":"\\ud800"}',
        b'{"x":' + b"[" * 20 + b"0" + b"]" * 20 + b"}",
    ],
)
def test_wire_json_duplicate_nonfinite_encoding_and_depth_fail_closed(body):
    module = clinical_module()
    with pytest.raises(module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"):
        module.parse_response(
            IsolatedHttpResponse(
                200, body, "https://clinical.invalid", "application/json"
            ),
            "synthetic-clinical-model",
            2048,
            32768,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["allowed", "context_off"])
async def test_actual_registered_chat_ingress_keeps_provider_on_governed_host(
    normal_turn, monkeypatch, mode
):
    from openai.types.chat import (
        ChatCompletionMessage,
        ChatCompletionMessageFunctionToolCall,
    )
    from tests.test_clinical_registration_readiness import Socket

    module = clinical_module()
    state = normal_turn.state
    assert module.register(state.orch)
    if mode == "context_off":
        monkeypatch.setitem(flags._flags, "observation_packing", False)
        monkeypatch.setitem(flags._flags, "safe_compaction", False)
    socket = Socket(asyncio.get_running_loop())
    socket.scope = dict(state.socket.scope)
    called, result = [], asyncio.Event()
    models = []

    async def physical(*_args, **_kwargs):
        called.append(True)
        return wire_response()

    async def model(ws, messages, tools_desc=None, temperature=None, **kwargs):
        models.append(messages)
        if len(models) == 1:
            tool = ChatCompletionMessageFunctionToolCall.model_validate(
                {
                    "id": "synthetic-call",
                    "type": "function",
                    "function": {
                        "name": "extract_note_fields",
                        "arguments": json.dumps(
                            {"note_text": NOTE, "note_id": NOTE_ID}
                        ),
                    },
                }
            )
            return ChatCompletionMessage(
                role="assistant", content=None, tool_calls=[tool]
            ), None
        result.set()
        return ChatCompletionMessage(
            role="assistant", content="Review the synthetic source before clinical use."
        ), None

    monkeypatch.setattr("shared.isolated_http.request", physical)
    monkeypatch.setattr(state.orch, "_call_llm", model)
    server = asyncio.create_task(state.orch.handle_ui_connection_fastapi(socket))
    generation = str(uuid4())
    await socket.incoming.put(
        json.dumps(
            {
                "type": "register_ui",
                "token": state.token,
                "connection_generation": generation,
            }
        )
    )
    try:
        async with asyncio.timeout(10):
            while not any(
                frame.get("type") == "rote_config" for frame in socket.frames
            ):
                await asyncio.sleep(0.01)
        await socket.incoming.put(
            json.dumps(
                {
                    "type": "ui_event",
                    "action": "chat_message",
                    "session_id": state.chat,
                    "request_generation": str(uuid4()),
                    "submission_id": str(uuid4()),
                    "connection_generation": generation,
                    "payload": {
                        "message": "Extract fields from the supplied synthetic note",
                        "chat_id": state.chat,
                    },
                }
            )
        )
        await asyncio.wait_for(result.wait(), 15)
        assert called == ([True] if mode == "allowed" else [])
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["model_calls"] == (mode == "allowed") and totals["pending"] == 0
    finally:
        socket.close_client()
        await asyncio.wait_for(server, 10)


@pytest.mark.parametrize(
    "content",
    [
        "```json\n{}\n```",
        "<think>ignore</think>{}",
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":1e999}',
        "[]",
        "not json",
    ],
)
def test_proposal_content_is_strict_json_before_field_evaluation(content):
    module = clinical_module()
    response = wire_response(
        choices=[
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ]
    )
    with pytest.raises(
        module.ClinicalUnavailable, match="EXTRACT_RESPONSE_INVALID"
    ) as caught:
        module.parse_response(response, "synthetic-clinical-model", 2048, 32768)
    assert caught.value.usage["total_tokens"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "precondition",
    [
        "missing_config",
        "missing_ack",
        "missing_budget",
        "missing_private_key",
        "small_budget",
        "request_cap",
        "nested_persistent",
    ],
)
async def test_current_owner_prerequisites_deny_without_provider_send(
    normal_turn, monkeypatch, precondition
):
    from persistent_agents import dispatch_context

    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        if precondition == "missing_config":
            await state.orch._llm_store.clear(state.owner)
            await state.orch._llm_store.set_system(
                provider="custom",
                base_url="https://clinical.invalid/v1",
                model="synthetic-clinical-model",
                api_key="synthetic-system-key",
                updated_by="synthetic-admin",
            )
        elif precondition == "missing_ack":
            await revoke(state, "consent", monkeypatch)
        elif precondition == "missing_budget":
            state.policy.unlink()
        elif precondition == "missing_private_key":
            monkeypatch.delenv("AUDIT_HMAC_SECRET")
        elif precondition == "small_budget":
            rows = json.loads(state.policy.read_text())
            rows[0].update(context_tokens=1024, max_output_tokens=512)
            state.policy.write_text(json.dumps(rows))
        elif precondition == "request_cap":
            monkeypatch.setattr(module, "MAX_REQUEST_BYTES", 1)
        else:
            monkeypatch.setattr(dispatch_context, "current_dispatch", lambda: object())
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and response.error["retryable"] is False and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "deadline",
        "cleanup_uncertain",
        "redirect",
        "response_too_large",
        "egress_blocked",
        "authentication",
        "upstream_failure",
    ],
)
async def test_one_physical_attempt_keeps_unknown_spend_and_never_retries(
    normal_turn, monkeypatch, code
):
    from shared.isolated_http import IsolatedHttpError

    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        raise IsolatedHttpError(code)

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        response = await state.orch.execute_authorized_tool(
            claims=state.orch.ui_sessions[state.socket],
            user_id=state.owner,
            agent_id="fhir-1",
            tool_name="extract_note_fields",
            arguments={"note_text": NOTE},
            channel="websocket",
            chat_id=state.chat,
            websocket=state.socket,
            timeout=120,
        )
        assert (
            response.error and response.error["retryable"] is False and calls == [True]
        )
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["model_calls"] == 1 and totals["usage"]["total_tokens"] == {
            "known": 0,
            "unknown": 1,
        }


@pytest.mark.asyncio
async def test_foreign_final_endpoint_never_delivers_a_proposal(
    normal_turn, monkeypatch
):
    module = clinical_module()

    async def physical(*_args, **_kwargs):
        response = wire_response()
        return IsolatedHttpResponse(
            200,
            response.body,
            "https://different.invalid/v1/chat/completions",
            "application/json",
        )

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and not response.ui_components


@pytest.mark.asyncio
async def test_exact_untrimmed_source_survives_the_configured_physical_request(
    normal_turn, monkeypatch
):
    from agents.fhir.extraction_validate import sha256_hex

    module = clinical_module()
    note = " \n" + NOTE + " é\t"
    calls = []

    async def physical(*_args, **kwargs):
        calls.append(kwargs["json_body"])
        return wire_response(note)

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": note, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error is None and len(calls) == 1
        assert note in calls[0]["messages"][1]["content"]
        assert response.result["note"] == {
            "note_id": NOTE_ID,
            "digest": sha256_hex(note),
            "length": len(note),
        }
        assert response.result["fields"]["patient_ref"]["evidence"][
            "start"
        ] == note.index("DEMO-1001")


@pytest.mark.asyncio
async def test_normal_registration_installs_only_the_canonical_host_service(
    orchestrator_factory, monkeypatch, tmp_path
):
    from orchestrator import local_agents

    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "registered-agent.pem"))
    for name in ("fhir", "inprocess_agents"):
        monkeypatch.setitem(flags._flags, name, True)
    for name in (
        "remote_compute",
        "computer_use",
        "terminal_shell",
        "gaiakeep",
        "cresco",
        "observation_packing",
        "safe_compaction",
    ):
        monkeypatch.setitem(flags._flags, name, False)
    orch = await asyncio.to_thread(orchestrator_factory)
    registered = await local_agents.register_built_ins(orch)
    assert registered == ["fhir-1"] and clinical_module().current(orch)


@pytest.mark.asyncio
async def test_secret_shaped_route_metadata_never_enters_context_usage(
    normal_turn, monkeypatch
):
    module = clinical_module()
    calls = []
    secret = "sk-" + "SyntheticCredential" * 3

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        await state.orch._llm_store.set(
            state.owner,
            provider="custom",
            base_url="https://clinical.invalid/v1",
            model=secret,
            api_key="synthetic-current-key",
        )
        policy = json.loads(state.policy.read_text())
        policy[0]["model"] = secret
        state.policy.write_text(json.dumps(policy))
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and calls == []
        rows, _ = await asyncio.to_thread(
            state.orch.audit_repo.list_for_user, state.owner
        )
        assert secret not in "".join(row.model_dump_json() for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_capture", "after_send"])
@pytest.mark.parametrize("observer", ["_record", "_recover"])
async def test_changed_accounting_observer_cannot_authorize_or_deliver(
    normal_turn, monkeypatch, phase, observer
):
    from types import MethodType

    module = clinical_module()
    calls = []
    async with normal_turn() as state:
        assert module.register(state.orch)
        context = state.orch._evidence_context

        async def counterfeit(self, *_args):
            return []

        async def physical(*_args, **_kwargs):
            calls.append(True)
            if phase == "after_send":
                monkeypatch.setattr(
                    context.usage, observer, MethodType(counterfeit, context)
                )
            return wire_response()

        if phase == "before_capture":
            monkeypatch.setattr(
                context.usage, observer, MethodType(counterfeit, context)
            )
        monkeypatch.setattr("shared.isolated_http.request", physical)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and not response.ui_components
        assert len(calls) == (phase == "after_send")


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["context", "ledger"])
async def test_registered_accounting_identity_cannot_be_replaced_by_a_second_instance(
    normal_turn, monkeypatch, replacement
):
    from orchestrator.context_usage import ContextUsage

    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        original = state.orch._evidence_context
        with monkeypatch.context() as change:
            if replacement == "context":
                change.setattr(
                    state.orch, "_evidence_context", EvidenceContext(state.orch)
                )
            else:
                change.setattr(
                    original,
                    "usage",
                    ContextUsage(original._persist, original._recover),
                )
            response = await module.invoke(
                state.orch,
                state.socket,
                {"note_text": NOTE, "note_id": NOTE_ID},
                owner=state.owner,
                chat=state.chat,
                timeout=60,
            )
            assert response.error and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["http_private", "keyless_sentinel"])
async def test_transport_requires_tls_and_never_sends_a_keyless_sentinel(
    normal_turn, monkeypatch, scenario
):
    module = clinical_module()
    base_url = ("http" if scenario == "http_private" else "https") + "://127.0.0.1/v1"
    calls = []

    async def physical(*_args, **kwargs):
        calls.append(kwargs)
        response = wire_response()
        return IsolatedHttpResponse(
            200, response.body, base_url + "/chat/completions", "application/json"
        )

    monkeypatch.setattr("shared.isolated_http.request", physical)
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    async with normal_turn() as state:
        await state.orch._llm_store.set(
            state.owner,
            provider="custom",
            base_url=base_url,
            model="synthetic-clinical-model",
            api_key="not-needed" if scenario == "keyless_sentinel" else "synthetic-key",
        )
        policy = json.loads(state.policy.read_text())
        policy[0]["base_url"] = base_url
        state.policy.write_text(json.dumps(policy))
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE, "note_id": NOTE_ID},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        if scenario == "http_private":
            assert response.error and calls == []
        else:
            assert response.error is None and len(calls) == 1
            assert calls[0]["api_key"] == ""


@pytest.mark.asyncio
async def test_normal_preparation_preserves_note_labels_that_match_file_names(
    normal_turn,
):
    from orchestrator.orchestrator import GateRefusal

    async with normal_turn() as state:
        await asyncio.to_thread(
            state.orch.history.add_file_mapping,
            state.chat,
            NOTE_ID,
            "/synthetic/retained-file.txt",
            user_id=state.owner,
        )
        prepared = await state.orch._authorize_and_prepare(
            state.socket,
            "fhir-1",
            "extract_note_fields",
            {"note_text": NOTE, "note_id": NOTE_ID},
            state.chat,
            state.owner,
        )
        assert not isinstance(prepared, GateRefusal)
        assert prepared.args == {"note_text": NOTE, "note_id": NOTE_ID}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_send", "after_send"])
async def test_real_turn_permission_memo_cannot_preserve_a_revoked_allow(
    normal_turn, monkeypatch, phase
):
    from orchestrator.tool_permissions import turn_permission_memo

    module = clinical_module()
    calls = []
    async with normal_turn() as state:
        assert module.register(state.orch)
        prepare = state.orch._evidence_context.prepare_model

        async def prepared(**kwargs):
            original = await prepare(**kwargs)

            async def guard():
                await original()
                if phase == "before_send":
                    await revoke(state, "permission", monkeypatch)

            return guard

        async def physical(*_args, **_kwargs):
            calls.append(True)
            if phase == "after_send":
                await revoke(state, "permission", monkeypatch)
            return wire_response()

        monkeypatch.setattr(state.orch._evidence_context, "prepare_model", prepared)
        monkeypatch.setattr("shared.isolated_http.request", physical)
        with turn_permission_memo():
            assert (
                state.orch.tool_permissions.is_tool_allowed(
                    state.owner, "fhir-1", "extract_note_fields"
                )
                is True
            )
            response = await module.invoke(
                state.orch,
                state.socket,
                {"note_text": NOTE, "note_id": NOTE_ID},
                owner=state.owner,
                chat=state.chat,
                timeout=60,
            )
            assert response.error and not response.ui_components
            assert (
                state.orch.tool_permissions.is_tool_allowed(
                    state.owner, "fhir-1", "extract_note_fields"
                )
                is True
            )
        assert len(calls) == (phase == "after_send")
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["pending"] == 0 and totals["model_calls"] == 1
        assert totals["usage"]["total_tokens"] == (
            {"known": 50, "unknown": 0} if calls else {"known": 0, "unknown": 1}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_send", "after_send"])
@pytest.mark.parametrize("loss", ["safe_trust", "public_flip", "safe_flag"])
async def test_current_safe_baseline_uses_public_store_truth_inside_an_active_turn_memo(
    normal_turn, monkeypatch, phase, loss
):
    from orchestrator.tool_permissions import turn_permission_memo

    module = clinical_module()
    calls = []
    async with normal_turn() as state:
        orch, registry = state.orch, state.orch.user_agent_registry
        prior_safe = registry.get_agent_is_safe("fhir-1")
        prior_ownership = registry.get_agent_ownership("fhir-1")
        registry.upsert_agent_safe("fhir-1", True, marked_by="synthetic-admin")
        if prior_ownership is not None:
            registry.set_agent_ownership(
                "fhir-1", "synthetic-owner@example.invalid", True
            )
        orch.tool_permissions.remove_agent_permissions(state.owner, "fhir-1")
        assert module.register(orch)
        prepare = orch._evidence_context.prepare_model

        async def withdraw():
            if loss == "safe_trust":
                await asyncio.to_thread(
                    registry.upsert_agent_safe,
                    "fhir-1",
                    False,
                    marked_by="synthetic-admin",
                )
                assert registry.get_agent_is_safe("fhir-1") is False
            elif loss == "public_flip":
                await asyncio.to_thread(
                    registry.set_agent_ownership,
                    "fhir-1",
                    "synthetic-owner@example.invalid",
                    False,
                )
                assert registry.get_agent_ownership("fhir-1")["is_public"] is False
            else:
                monkeypatch.setitem(flags._flags, "safe_agents", False)

        async def prepared(**kwargs):
            original = await prepare(**kwargs)

            async def guard():
                await original()
                if phase == "before_send":
                    await withdraw()

            return guard

        async def physical(*_args, **_kwargs):
            calls.append(True)
            if phase == "after_send":
                await withdraw()
            return wire_response()

        monkeypatch.setattr(orch._evidence_context, "prepare_model", prepared)
        monkeypatch.setattr("shared.isolated_http.request", physical)
        try:
            with turn_permission_memo():
                assert (
                    orch.tool_permissions.is_tool_allowed(
                        state.owner, "fhir-1", "extract_note_fields"
                    )
                    is True
                )
                response = await module.invoke(
                    orch,
                    state.socket,
                    {"note_text": NOTE, "note_id": NOTE_ID},
                    owner=state.owner,
                    chat=state.chat,
                    timeout=60,
                )
                assert response.error and not response.ui_components
                assert (
                    orch.tool_permissions.is_tool_allowed(
                        state.owner, "fhir-1", "extract_note_fields"
                    )
                    is True
                )
            assert len(calls) == (phase == "after_send")
        finally:
            registry.upsert_agent_safe(
                "fhir-1", prior_safe, marked_by="synthetic-admin"
            )
            if prior_ownership is None:
                runtime = orch.runtime_composition.plane.runtime
                with runtime.transaction() as tx:
                    runtime.repositories.agents.remove_ownership(tx, agent_id="fhir-1")
            else:
                registry.set_agent_ownership(
                    "fhir-1",
                    prior_ownership["owner_email"],
                    prior_ownership["is_public"],
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loss",
    [
        "registry",
        "malformed_trust",
        "malformed_ownership",
        "lookup_outage",
        "disabled",
        "selection",
    ],
)
async def test_current_registry_and_public_policy_reads_deny_unavailable_or_withdrawn_authority(
    normal_turn, monkeypatch, loss
):
    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        registry = state.orch.user_agent_registry
        with monkeypatch.context() as change:
            if loss == "registry":
                change.setattr(state.orch, "user_agent_registry", object())
            elif loss == "malformed_trust":
                change.setattr(registry, "get_agent_is_safe", lambda _: "true")
            elif loss == "malformed_ownership":
                change.setattr(
                    registry, "get_agent_ownership", lambda _: {"is_public": True}
                )
            elif loss == "lookup_outage":

                def unavailable(_):
                    raise OSError("synthetic store unavailable")

                change.setattr(registry, "get_agent_ownership", unavailable)
            elif loss == "disabled":
                registry = state.orch.tool_permissions
                registry.set_agent_disabled(state.owner, "fhir-1", True)
            else:
                state.orch.tool_permissions.set_tool_selection(
                    state.owner,
                    "fhir-1",
                    [
                        next(
                            name
                            for name in state.agent.mcp_server.tools
                            if name != "extract_note_fields"
                        )
                    ],
                )
            response = await module.invoke(
                state.orch,
                state.socket,
                {"note_text": NOTE},
                owner=state.owner,
                chat=state.chat,
                timeout=60,
            )
            assert response.error and not response.ui_components and calls == []


@pytest.mark.asyncio
async def test_real_selected_tool_reservation_cannot_authorize_a_nested_model(
    normal_turn, monkeypatch
):
    from persistent_agents.dispatch_context import (
        PersistentDispatchContext,
        bind_dispatch,
    )

    module = clinical_module()
    calls, stages = [], []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    async def authorize():
        stages.append("authorize")

    async def start():
        stages.append("tool_start")
        return "synthetic-tool-reservation"

    async def observe(*_args):
        stages.append("observe")

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        assert module.register(state.orch)
        args = {"note_text": NOTE}
        dispatch = PersistentDispatchContext(
            state.owner,
            "tool",
            "fhir-1",
            "extract_note_fields",
            args,
            60,
            262144,
            4096,
            authorize,
            start,
            observe,
            conversation_id=state.chat,
        )
        with bind_dispatch(dispatch):
            response = await state.orch.execute_authorized_tool(
                claims=state.orch.ui_sessions[state.socket],
                user_id=state.owner,
                agent_id="fhir-1",
                tool_name="extract_note_fields",
                arguments=args,
                channel="websocket",
                chat_id=state.chat,
                websocket=state.socket,
                timeout=60,
            )
        assert response.error["code"] == "EXTRACT_CONTEXT_UNAVAILABLE" and calls == []
        assert stages.count("tool_start") == 1
        totals = await state.orch._evidence_context.usage.totals(
            state.owner, state.chat
        )
        assert totals["attempts"] == 0


@pytest.mark.asyncio
async def test_public_selection_adapter_preserves_owner_isolation_and_empty_denial(
    normal_turn, monkeypatch
):
    from astralplane.repositories import RepositoryValidationError

    module = clinical_module()
    calls = []

    async def physical(*_args, **_kwargs):
        calls.append(True)
        return wire_response()

    monkeypatch.setattr("shared.isolated_http.request", physical)
    async with normal_turn() as state:
        manager = state.orch.tool_permissions
        choice = next(
            name
            for name in state.agent.mcp_server.tools
            if name != "extract_note_fields"
        )
        assert manager.set_tool_selection(state.owner, "fhir-1", [choice]) == [choice]
        assert manager.get_tool_selection(state.owner, "fhir-1") == [choice]
        assert manager.get_tool_selection(state.owner + "-foreign", "fhir-1") is None
        with pytest.raises(RepositoryValidationError):
            manager.set_tool_selection(state.owner, "fhir-1", [])
        assert manager.get_tool_selection(state.owner, "fhir-1") == [choice]
        assert module.register(state.orch)
        response = await module.invoke(
            state.orch,
            state.socket,
            {"note_text": NOTE},
            owner=state.owner,
            chat=state.chat,
            timeout=60,
        )
        assert response.error and calls == []
