"""Tests for card-button UI operation refusals (orchestrator.py): every refusal on the
component-action/refine/restore/unconfigured-provider paths registers its reason in the
connection operation context, and the connection operation runner turns it into a failed
terminal operation_status that idle clients already render.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.orchestrator import (
    GateRefusal,
    Orchestrator,
    PreparedDispatch,
    _CONNECTION_OPERATION_CONTEXT,
    _UiOperationRefusal,
)
from orchestrator.workspace import WorkspaceManager
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)
from tests.test_conversation_snapshot_060 import (
    _coordinator,
    _repository,
    _workspace_manager,
    database as database,
    postgres_database as postgres_database,
)


class _FakeWS:
    def __init__(self, label: str = ""):
        self.label = label


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("ui_operation_refusal") as runtime:
        yield runtime


@pytest.fixture
def chat_env(plane_runtime):
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture
def operation_context():
    context: dict = {"operation_kind": "connection_frame"}
    token = _CONNECTION_OPERATION_CONTEXT.set(context)
    yield context
    _CONNECTION_OPERATION_CONTEXT.reset(token)


@pytest.fixture
def refine_enabled(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "component_refine", True)


def _make_fake(history, user_id, *, allowed=True, security_flags=None,
               exec_result=None, llm_configured=True, authorize=None):
    from rote.rote import ROTE

    sent = []
    renders = []
    exec_calls = []

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    async def _execute_with_retry(ws, agent_id, tool_name, args):
        exec_calls.append((agent_id, tool_name, args))
        return exec_result

    async def _execute_with_retry_audited(
        ws,
        agent_id,
        tool_name,
        args,
        chat_id=None,
        user_id=None,
        **_dispatch_context,
    ):
        del chat_id, user_id
        return await fake._execute_with_retry(ws, agent_id, tool_name, args)

    async def _authorize_and_prepare(ws, agent_id, tool_name, args,
                                     chat_id=None, user_id=None, **kw):
        if authorize is not None:
            return authorize
        return PreparedDispatch(args=args, stream_params=dict(args),
                                cap_job_id=None, delegation_token=None)

    async def _llm_configured_for(_uid):
        return llm_configured

    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(history),
        history=history,
        runtime_composition=types.SimpleNamespace(
            plane=types.SimpleNamespace(
                runtime=history.plane_runtime,
                repositories=history.plane_repositories,
            )
        ),
        _ws_active_chat={},
        _ws_timeline_mode={},
        _workspace_locks={},
        ui_clients=[],
        ui_sessions={},
        rote=ROTE(),
        security_flags=security_flags or {},
        tool_permissions=types.SimpleNamespace(is_tool_allowed=lambda u, a, t: allowed),
        credential_manager=types.SimpleNamespace(
            get_agent_credentials_encrypted=lambda u, a: None),
        llm_configured_for=_llm_configured_for,
        _llm_audit_principals=lambda ws: (user_id, user_id),
        _record_llm_unconfigured=AsyncMock(),
        audit_recorder=None,
        _get_user_id=lambda ws: user_id,
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
        _execute_with_retry=_execute_with_retry,
        _execute_with_retry_audited=_execute_with_retry_audited,
        _authorize_and_prepare=_authorize_and_prepare,
    )
    for name in ("_send_or_replace_components", "send_ui_upsert",
                 "_handle_component_action", "_audit_workspace_denial",
                 "_component_action_allowed",
                 "_refine_restore_gate", "_handle_component_refine",
                 "_handle_component_restore"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._register_ui_operation_refusal = Orchestrator._register_ui_operation_refusal
    fake._sent = sent
    fake._renders = renders
    fake._exec_calls = exec_calls
    return fake


def _seed_component(workspace, chat_id, user_id, *, agent="agent-x",
                    tool="list_patients", params=None):
    comp = {
        "type": "table",
        "title": "Patients",
        "headers": ["Name"],
        "rows": [["Alice"]],
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": params if params is not None else {"page": 1},
    }
    ops = workspace.upsert(chat_id, user_id, [comp])
    assert len(ops) == 1
    return ops[0]["component_id"]


def _run(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapper())


def test_component_action_permission_denial_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == (
        "This action is not permitted. Check your tool permissions.")


def test_component_action_tool_error_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(
        history, user_id,
        exec_result=types.SimpleNamespace(
            ui_components=[], error={"message": "The tool exploded."}))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "The action failed. Try again."


def test_component_action_missing_component_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef", "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This component is no longer available."


def test_component_action_unsupported_kind_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef", "kind": "detonate",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This component action is not supported."


def test_component_action_timeline_refusal_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("timeline")
    fake._ws_timeline_mode[id(ws)] = True

    _run(fake._handle_component_action(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == (
        "You are viewing a past workspace state — return to live to interact.")


def test_component_action_retired_agent_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    from orchestrator.orchestrator import RETIRED_AGENT_IDS
    retired = sorted(RETIRED_AGENT_IDS)[0]
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id, agent=retired)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert "retired" in refusal.message


def test_component_action_sourceless_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id, agent="", tool="")

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This component has no refreshable source."


def test_component_action_gate_refusal_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    refusal_response = types.SimpleNamespace(error={"message": "PHI gate refused the tool."})
    fake = _make_fake(
        history, user_id,
        authorize=GateRefusal(response=refusal_response, render_components=None),
        exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This action was refused by a policy gate."


def test_component_action_gate_refusal_without_message_registers_fallback(
        chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(
        history, user_id,
        authorize=GateRefusal(response=types.SimpleNamespace(error=None),
                              render_components=None),
        exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This action was refused by a policy gate."


def test_component_action_handler_exception_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("backend socket closed")

    fake._execute_with_retry_audited = _explode

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "The action failed. Try again."


def test_component_action_success_registers_no_refusal(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(
        history, user_id,
        exec_result=types.SimpleNamespace(
            ui_components=[{"type": "table", "title": "Patients",
                            "headers": ["Name"], "rows": [["Bob"]]}],
            error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    assert "ui_refusal" not in operation_context
    assert len(fake._exec_calls) == 1


def test_action_after_refusal_recovers_on_next_operation(chat_env, refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    refused: dict = {"operation_kind": "connection_frame"}
    token = _CONNECTION_OPERATION_CONTEXT.set(refused)
    try:
        _run(fake._handle_component_action(_FakeWS(), user_id, {
            "chat_id": chat_id, "component_id": cid, "kind": "refresh",
        }))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)
    assert "ui_refusal" in refused

    fake.tool_permissions = types.SimpleNamespace(
        is_tool_allowed=lambda u, a, t: True)
    recovered: dict = {"operation_kind": "connection_frame"}
    token = _CONNECTION_OPERATION_CONTEXT.set(recovered)
    try:
        _run(fake._handle_component_action(_FakeWS(), user_id, {
            "chat_id": chat_id, "component_id": cid, "kind": "refresh",
        }))
    finally:
        _CONNECTION_OPERATION_CONTEXT.reset(token)

    assert "ui_refusal" not in recovered
    assert len(fake._exec_calls) == 1


def test_component_action_refusal_without_operation_context_is_ignored(chat_env):
    history, user_id, chat_id = chat_env
    assert _CONNECTION_OPERATION_CONTEXT.get() is None
    fake = _make_fake(history, user_id, allowed=False,
                      exec_result=types.SimpleNamespace(ui_components=[], error=None))
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "refresh",
    }))

    assert _CONNECTION_OPERATION_CONTEXT.get() is None


def test_component_action_second_refusal_does_not_overwrite_first(chat_env, operation_context):
    operation_context["ui_refusal"] = _UiOperationRefusal(message="First reason.")
    Orchestrator._register_ui_operation_refusal("action_failed")
    assert operation_context["ui_refusal"].message == "First reason."


def test_refusal_registration_uses_fixed_fallback_for_unknown_code(operation_context):
    Orchestrator._register_ui_operation_refusal("x" * 600)
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "The action could not be completed. Try again."


def test_refusal_registration_ignores_blank_message(operation_context):
    Orchestrator._register_ui_operation_refusal("   ")
    assert "ui_refusal" not in operation_context


@pytest.mark.parametrize("detail", [
    "Bearer synthetic-secret-token",
    "patient=synthetic-patient credential=synthetic-provider-key",
    "https://synthetic-user:synthetic-password@provider.invalid/private",
])
def test_refusal_registration_never_accepts_untrusted_detail(operation_context, detail):
    Orchestrator._register_ui_operation_refusal(detail)
    refusal = operation_context["ui_refusal"]
    assert refusal.message == "The action could not be completed. Try again."
    assert detail not in refusal.message


def test_refine_restore_gate_flag_off_registers_reason(chat_env, operation_context,
                                                       monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "component_refine", False)
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)

    result = _run(fake._refine_restore_gate(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef"}))

    assert result is None
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "Component refine is not enabled on this server."


def test_refine_restore_gate_watch_registers_reason(chat_env, operation_context):
    from rote.capabilities import DeviceProfile, DeviceType
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    ws = _FakeWS("watch")

    class _WatchRote:
        def get_profile(self, _socket):
            profile = DeviceProfile.default()
            profile.device_type = DeviceType.WATCH
            return profile

    fake.rote = _WatchRote()

    result = _run(fake._refine_restore_gate(ws, user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef"}))

    assert result is None
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This action isn't available on the watch."


def test_refine_restore_gate_permission_denied_registers_reason(chat_env, operation_context):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    result = _run(fake._refine_restore_gate(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid}))

    assert result is None
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == (
        "This action is not permitted. Check your tool permissions.")


def test_refine_restore_gate_success_registers_no_refusal(chat_env, operation_context,
                                                          refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    result = _run(fake._refine_restore_gate(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid}))

    assert result is not None
    assert result[0] == chat_id and result[1] == cid
    assert "ui_refusal" not in operation_context


def test_refine_missing_instruction_registers_reason(chat_env, operation_context,
                                                     refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "   "}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "Tell me how this component should change."


def test_refine_unconfigured_provider_registers_reason(chat_env, operation_context,
                                                       refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_configured=False)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "sort rows"}))

    assert fake._record_llm_unconfigured.await_count == 1
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "Set up your AI provider to use this."


def test_restore_unknown_version_registers_reason(chat_env, operation_context,
                                                  refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 99}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "That version is no longer available."


def test_refine_restore_gate_missing_context_registers_reason(chat_env, operation_context,
                                                              refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)

    result = _run(fake._refine_restore_gate(_FakeWS(), user_id, {
        "chat_id": chat_id}))

    assert result is None
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This action is missing its component context."


def test_refine_restore_gate_retired_registers_reason(chat_env, operation_context,
                                                      refine_enabled):
    from orchestrator.orchestrator import RETIRED_AGENT_IDS
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    retired = sorted(RETIRED_AGENT_IDS)[0]
    cid = _seed_component(fake.workspace, chat_id, user_id, agent=retired)

    result = _run(fake._refine_restore_gate(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid}))

    assert result is None
    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert "retired" in refusal.message


def _vanishing_workspace(inner, vanish_on_call: int):
    calls = {"n": 0}

    class _Wrapper:
        async def aget_by_component_id(self, chat_id, user_id, component_id):
            calls["n"] += 1
            if calls["n"] >= vanish_on_call:
                return None
            return await inner.aget_by_component_id(chat_id, user_id, component_id)

        def __getattr__(self, name):
            return getattr(inner, name)

    return _Wrapper()


def test_refine_component_vanishes_in_lock_registers_reason(chat_env, operation_context,
                                                            refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    fake.workspace = _vanishing_workspace(fake.workspace, vanish_on_call=2)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "sort"}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This component is no longer available."


def test_refine_exception_registers_reason(chat_env, operation_context, refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)
    sensitive = "patient=synthetic-patient credential=synthetic-provider-key"

    async def fail_refine(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    fake._refine_component_llm = fail_refine

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "sort"}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "The component could not be refined. Try again."
    assert sensitive not in json.dumps(fake._renders, default=str)


async def _seed_one_version(fake, chat_id, user_id, cid):
    from orchestrator import artifact_versions
    from orchestrator.plane_repository_context import (
        plane_source_from_orchestrator,
    )

    plane_source = plane_source_from_orchestrator(fake)
    row = await fake.workspace.aget_by_component_id(chat_id, user_id, cid)
    assert row is not None
    return await artifact_versions.aarchive(
        plane_source, chat_id, user_id, cid, row["component_data"], "refine")


def test_restore_component_vanishes_in_lock_registers_reason(chat_env, operation_context,
                                                             refine_enabled):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    fake.workspace = _vanishing_workspace(fake.workspace, vanish_on_call=3)
    cid = _seed_component(fake.workspace, chat_id, user_id)
    version_no = _run(_seed_one_version(fake, chat_id, user_id, cid))

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": version_no}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "This component is no longer available."


def test_restore_exception_registers_reason(chat_env, operation_context, refine_enabled,
                                            monkeypatch):
    import orchestrator.artifact_versions as artifact_versions
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)
    version_no = _run(_seed_one_version(fake, chat_id, user_id, cid))

    sensitive = "patient=synthetic-patient credential=synthetic-provider-key"

    async def _explode(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    monkeypatch.setattr(artifact_versions, "aarchive", _explode)
    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": version_no}))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "The component could not be restored. Try again."
    assert sensitive not in json.dumps(fake._renders, default=str)


def test_unconfigured_provider_component_action_registers_reason_via_dispatcher(
        chat_env, operation_context, monkeypatch):
    import audit.hooks

    async def _no_audit(**_kwargs):
        return None

    monkeypatch.setattr(audit.hooks, "record_ws_action", _no_audit)
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_configured=False)
    ws = _FakeWS("origin")
    fake.ui_sessions[ws] = {"sub": user_id}
    fake._parsed_ui_frame = Orchestrator._parsed_ui_frame
    fake.handle_ui_message = types.MethodType(Orchestrator.handle_ui_message, fake)

    message = {
        "type": "ui_event",
        "action": "component_action",
        "session_id": chat_id,
        "submission_id": str(uuid.uuid4()),
        "request_generation": str(uuid.uuid4()),
        "payload": {"chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef",
                    "kind": "refresh"},
    }
    _run(fake.handle_ui_message(ws, json.dumps(message)))

    refusal = operation_context.get("ui_refusal")
    assert isinstance(refusal, _UiOperationRefusal)
    assert refusal.message == "Set up your AI provider to use this."
    assert fake._exec_calls == []


OWNER = "ui-refusal-owner"
FOREIGN = "ui-refusal-foreign"


class _Socket:
    def __init__(self):
        self.frames = []

    async def send_text(self, raw):
        self.frames.append(json.loads(raw))


class _Rote:
    def get_profile(self, _socket):
        from rote.capabilities import DeviceProfile
        return DeviceProfile.default()

    def adapt(self, _socket, components, *, cache=True):
        import copy
        return copy.deepcopy(components)


def _integration_runtime(database=None):
    host = object.__new__(Orchestrator)
    socket = _Socket()
    host.ui_sessions = {socket: {"sub": OWNER}}
    host.ui_clients = [socket]
    host._ws_active_chat = {}
    host._conversation_scopes = {}
    host._ws_timeline_mode = {}
    host._workspace_locks = {}
    host.rote = _Rote()
    from orchestrator.orchestrator import ConnectionContext
    context = ConnectionContext(socket, uuid.uuid4(), asyncio.get_running_loop().time() + 60,
                                connection_generation=uuid.uuid4(), registered=True)
    host._connection_contexts = {id(socket): context}
    host.audit_repo = None
    host._llm_store = None
    if database is not None:
        host.runtime_composition = types.SimpleNamespace(plane=types.SimpleNamespace(
            runtime=database, repositories=database.repositories))
        from orchestrator.harness.selection_service import HarnessSelectionService
        host.harness_selection = HarnessSelectionService(orchestrator=host)
    return host, socket, context


def _integration_frame(host, context, *, action="component_action", chat_id=None, **payload):
    request, submission = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"type": "ui_event", "action": action, "submission_id": submission,
            "request_generation": request,
            "connection_generation": str(context.connection_generation),
            "payload": payload | {"request_generation": request,
                                  "submission_id": submission}}
    if chat_id is not None:
        body["session_id"] = chat_id
        body["payload"]["chat_id"] = chat_id
    result = host._connection_frame(context, json.dumps(body), body)
    assert result is not None
    return result


async def _integration_admitted(host, context, ingress):
    results = await asyncio.to_thread(
        host._submit_connection_batch, context, [ingress])
    _frame, owner, acceptance, projection = results[0]
    assert acceptance.accepted
    from orchestrator.orchestrator import _ConnectionOperation
    from orchestrator.work_admission import OperationOwner, OwnerScope
    operation = _ConnectionOperation(
        ingress, OperationOwner(OwnerScope.CONNECTION, None,
                                context.connection_scope_id),
        acceptance.operation_id)
    return operation, acceptance.operation_id


@pytest.mark.asyncio
async def test_refused_component_action_fails_operation_with_reason_in_terminal_status(
        database):
    host, socket, context = _integration_runtime(database)
    from orchestrator.history import HistoryManager
    host.history = HistoryManager(plane_runtime=database,
                                  plane_repositories=database.repositories)
    host.workspace = _workspace_manager(host.history, database)
    host.work_admission = _coordinator(database)
    host.conversation_commits = _repository(database, host.work_admission)
    host.tool_permissions = types.SimpleNamespace(
        is_tool_allowed=lambda u, a, t: False)
    host.security_flags = {}
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)

    comp = {
        "type": "table", "title": "Patients", "headers": ["Name"],
        "rows": [["Alice"]], "_source_agent": "agent-x",
        "_source_tool": "list_patients", "_source_params": {"page": 1},
    }
    ops = await asyncio.to_thread(host.workspace.upsert, chat, OWNER, [comp])
    component_id = ops[0]["component_id"]

    ingress = _integration_frame(host, context, chat_id=chat,
                                 component_id=component_id, kind="refresh")
    operation, _operation_id = await _integration_admitted(host, context, ingress)

    async def handle(websocket, _raw):
        payload = json.loads(_raw).get("payload") or {}
        await Orchestrator._handle_component_action(host, websocket, OWNER, payload)

    host.handle_ui_message = handle

    async def _renew(_context, _work, stop, _worker):
        await stop.wait()

    host._renew_connection_lease = _renew
    await Orchestrator._run_connection_operation(host, context, operation)

    terminals = [f for f in socket.frames if f.get("type") == "operation_status"]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal["state"] == "failed"
    assert terminal["terminal"] is True
    assert terminal["retryable"] is False
    assert terminal["error"]["code"] == "operation_failed"
    assert terminal["error"]["message"] == (
        "This action is not permitted. Check your tool permissions.")
    assert terminal["chat_id"] == chat
    assert terminal["connection_generation"] == str(context.connection_generation)
    assert terminal["request_generation"] == str(operation.frame.request_generation)
    assert terminal["action"] == "component_action"

    record = await asyncio.to_thread(
        host.work_admission.query_operation, owner=operation.owner,
        operation_id=operation.operation_id)
    from orchestrator.work_admission import OperationState
    assert record.state is OperationState.FAILED
    assert record.terminal_code == "ui_action_refused"
    assert record.safe_summary == (
        "This action is not permitted. Check your tool permissions.")

    alerts = [f for f in socket.frames if f.get("type") == "ui_render"]
    assert alerts and all(
        a["request_generation"] == str(operation.frame.request_generation)
        for a in alerts)
    scope = host._conversation_scopes[id(socket)]
    assert scope["request_generation"] == str(operation.frame.request_generation)

    assert [f for f in socket.frames
            if f.get("type") in {"conversation_commit_ready",
                                 "conversation_snapshot"}] == []


@pytest.mark.asyncio
async def test_refused_component_action_reaches_no_other_owner_socket(database):
    host, socket, context = _integration_runtime(database)
    from orchestrator.history import HistoryManager
    host.history = HistoryManager(plane_runtime=database,
                                  plane_repositories=database.repositories)
    host.workspace = _workspace_manager(host.history, database)
    host.work_admission = _coordinator(database)
    host.conversation_commits = _repository(database, host.work_admission)
    host.tool_permissions = types.SimpleNamespace(
        is_tool_allowed=lambda u, a, t: False)
    host.security_flags = {}
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)

    foreign = _Socket()
    host.ui_clients.append(foreign)
    host.ui_sessions[foreign] = {"sub": FOREIGN}

    comp = {
        "type": "table", "title": "Patients", "headers": ["Name"],
        "rows": [["Alice"]], "_source_agent": "agent-x",
        "_source_tool": "list_patients", "_source_params": {"page": 1},
    }
    ops = await asyncio.to_thread(host.workspace.upsert, chat, OWNER, [comp])
    component_id = ops[0]["component_id"]

    ingress = _integration_frame(host, context, chat_id=chat,
                                 component_id=component_id, kind="refresh")
    operation, _operation_id = await _integration_admitted(host, context, ingress)

    async def handle(websocket, _raw):
        payload = json.loads(_raw).get("payload") or {}
        await Orchestrator._handle_component_action(host, websocket, OWNER, payload)

    host.handle_ui_message = handle

    async def _renew(_context, _work, stop, _worker):
        await stop.wait()

    host._renew_connection_lease = _renew
    await Orchestrator._run_connection_operation(host, context, operation)

    assert foreign.frames == []
    terminals = [f for f in socket.frames if f.get("type") == "operation_status"]
    assert len(terminals) == 1 and terminals[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_successful_component_action_still_completes(database):
    host, socket, context = _integration_runtime(database)
    from orchestrator.history import HistoryManager
    host.history = HistoryManager(plane_runtime=database,
                                  plane_repositories=database.repositories)
    host.workspace = _workspace_manager(host.history, database)
    host.work_admission = _coordinator(database)
    host.conversation_commits = _repository(database, host.work_admission)
    host.tool_permissions = types.SimpleNamespace(
        is_tool_allowed=lambda u, a, t: True)
    host.security_flags = {}

    async def _authorize_and_prepare(_ws, _agent, _tool, args, *_rest, **_kw):
        from orchestrator.orchestrator import PreparedDispatch as Dispatch
        return Dispatch(args=args, stream_params=dict(args), cap_job_id=None,
                        delegation_token=None)

    host._authorize_and_prepare = _authorize_and_prepare

    async def _execute_with_retry_audited(_ws, _agent, _tool, _args, *_rest, **_kw):
        return types.SimpleNamespace(ui_components=[], error=None)

    host._execute_with_retry_audited = _execute_with_retry_audited
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)

    comp = {
        "type": "table", "title": "Patients", "headers": ["Name"],
        "rows": [["Alice"]], "_source_agent": "agent-x",
        "_source_tool": "list_patients", "_source_params": {"page": 1},
    }
    ops = await asyncio.to_thread(host.workspace.upsert, chat, OWNER, [comp])
    component_id = ops[0]["component_id"]

    ingress = _integration_frame(host, context, chat_id=chat,
                                 component_id=component_id, kind="refresh")
    operation, _operation_id = await _integration_admitted(host, context, ingress)

    async def handle(websocket, _raw):
        payload = json.loads(_raw).get("payload") or {}
        await Orchestrator._handle_component_action(host, websocket, OWNER, payload)

    host.handle_ui_message = handle

    async def _renew(_context, _work, stop, _worker):
        await stop.wait()

    host._renew_connection_lease = _renew
    await Orchestrator._run_connection_operation(host, context, operation)

    terminals = [f for f in socket.frames if f.get("type") == "operation_status"]
    assert len(terminals) == 1
    assert terminals[0]["state"] == "completed"
    assert terminals[0]["error"] is None
    record = await asyncio.to_thread(
        host.work_admission.query_operation, owner=operation.owner,
        operation_id=operation.operation_id)
    from orchestrator.work_admission import OperationState
    assert record.state is OperationState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    ("tool_error", "The action failed. Try again."),
    ("handler_exception", "The action failed. Try again."),
    ("gate_refusal", "This action was refused by a policy gate."),
    ("unsupported_kind", "This component action is not supported."),
    ("missing_context", "This action is missing its component context."),
    ("missing_component", "This component is no longer available."),
    ("unconfigured_provider", "Set up your AI provider to use this."),
])
async def test_dispatch_refusal_persists_only_safe_text_and_replays_once(
        database, monkeypatch, failure, expected):
    import audit.hooks
    from orchestrator.history import HistoryManager
    from orchestrator.work_admission import OperationState

    sensitive = "patient=synthetic-patient Bearer synthetic-secret credential=synthetic-key"
    monkeypatch.setattr(audit.hooks, "record_ws_action", AsyncMock())
    host, socket, context = _integration_runtime(database)
    host.history = HistoryManager(plane_runtime=database,
                                  plane_repositories=database.repositories)
    host.workspace = _workspace_manager(host.history, database)
    host.work_admission = _coordinator(database)
    host.conversation_commits = _repository(database, host.work_admission)
    host.tool_permissions = types.SimpleNamespace(is_tool_allowed=lambda *_args: True)
    host.security_flags = {}
    host.audit_recorder = None
    host.llm_configured_for = AsyncMock(return_value=failure != "unconfigured_provider")
    host._record_llm_unconfigured = AsyncMock()

    async def authorize(_socket, _agent, _tool, args, *_rest, **_kwargs):
        if failure == "gate_refusal":
            return GateRefusal(response=types.SimpleNamespace(error={"message": sensitive}),
                               render_components=None)
        return PreparedDispatch(args=args, stream_params=dict(args), cap_job_id=None,
                                delegation_token=None)

    async def execute(*_args, **_kwargs):
        if failure == "handler_exception":
            raise RuntimeError(sensitive)
        return types.SimpleNamespace(ui_components=[], error={"message": sensitive})

    async def renew(_context, _work, stop, _worker):
        await stop.wait()

    host._authorize_and_prepare = authorize
    host._execute_with_retry_audited = execute
    host._renew_connection_lease = renew
    chat = await asyncio.to_thread(host.history.create_chat, user_id=OWNER)
    component_id = await asyncio.to_thread(_seed_component, host.workspace, chat, OWNER)
    if failure == "missing_component":
        component_id = "wc_deadbeefdeadbeef"
    payload = {"component_id": component_id,
               "kind": sensitive if failure == "unsupported_kind" else "refresh"}
    if failure == "missing_context":
        payload = {"kind": "refresh"}
    ingress = _integration_frame(host, context, chat_id=chat, **payload)
    work, _operation_id = await _integration_admitted(host, context, ingress)

    foreign = _Socket()
    other_chat = _Socket()
    host.ui_clients.extend([foreign, other_chat])
    host.ui_sessions.update({foreign: {"sub": FOREIGN}, other_chat: {"sub": OWNER}})
    host._ws_active_chat[id(other_chat)] = str(uuid.uuid4())
    await host._run_connection_operation(context, work)

    terminals = [frame for frame in socket.frames if frame.get("type") == "operation_status"]
    assert len(terminals) == 1
    assert terminals[0]["state"] == "failed"
    assert terminals[0]["terminal"] is True
    assert terminals[0]["error"] == {"code": "operation_failed", "message": expected}
    assert sensitive not in json.dumps(socket.frames)
    assert foreign.frames == [] and other_chat.frames == []
    assert not any(frame.get("type") in {"conversation_commit_ready", "conversation_snapshot"}
                   for frame in socket.frames)
    record = await asyncio.to_thread(
        host.work_admission.query_operation, owner=work.owner, operation_id=work.operation_id)
    assert record.state is OperationState.FAILED
    assert record.safe_summary == expected

    replay = _Socket()
    replay_context = types.SimpleNamespace(
        websocket=replay, terminal_emitted=set(),
        connection_generation=context.connection_generation)
    await host._send_operation_terminal_to_context(replay_context, work.frame, work, record)
    await host._send_operation_terminal_to_context(replay_context, work.frame, work, record)
    assert len(replay.frames) == 1
    assert replay.frames[0]["error"]["message"] == expected
    assert sensitive not in json.dumps(replay.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [None, "", "Bearer synthetic-secret", "x" * 600])
async def test_terminal_replay_rejects_untrusted_retained_summary(summary):
    from orchestrator.work_admission import OperationState

    host, socket, context = _integration_runtime()
    ingress = _integration_frame(host, context, chat_id=str(uuid.uuid4()), kind="refresh")
    work = types.SimpleNamespace(frame=ingress)
    record = types.SimpleNamespace(
        operation_id=uuid.uuid4(), state=OperationState.FAILED,
        state_revision=2, terminal_code="ui_action_refused", safe_summary=summary)
    await host._send_operation_terminal_to_context(context, ingress, work, record)
    assert len(socket.frames) == 1
    assert socket.frames[0]["error"]["message"] == (
        "The action could not be completed. Try again.")


@pytest.mark.parametrize("action,state", [("status", "running"), ("stop", "interrupted"), ("start", "running")])
def test_component_action_consumed_preview_dispatch_updates_card_once(chat_env, operation_context, action, state):
    from shared.protocol import MCPResponse

    history, user_id, chat_id = chat_env
    card = {"type": "card", "title": f"Preview {state}", "content": []}
    response = MCPResponse(result={"_data": {"state": state, "job_id": "owned-job"}}, ui_components=[card])
    fake = _make_fake(history, user_id, authorize=GateRefusal(response=response, dispatch_completed=True))
    cid = _seed_component(fake.workspace, chat_id, user_id, agent="computer-use-1",
                          tool="project_preview", params={"cwd": "/tmp/project", "action": "start"})
    _run(fake._handle_component_action(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "kind": "invoke",
        "params_patch": {"action": action, "job_id": "owned-job"},
    }))
    assert "ui_refusal" not in operation_context
    assert not fake._exec_calls
    row = fake.workspace.get_by_component_id(chat_id, user_id, cid)
    assert row["component_data"]["title"] == f"Preview {state}"
    assert row["component_data"]["_source_params"]["action"] == action


def test_component_action_pending_confirmation_is_rendered_without_refusal(chat_env, operation_context):
    from shared.protocol import MCPResponse

    history, user_id, chat_id = chat_env
    cards = [{"type": "card", "title": "Confirm preview", "content": []}]
    fake = _make_fake(history, user_id, authorize=GateRefusal(response=MCPResponse(
        result={"_data": {"status": "confirmation_required"}}, ui_components=cards)))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    _run(fake._handle_component_action(_FakeWS(), user_id, {"chat_id": chat_id, "component_id": cid}))
    assert "ui_refusal" not in operation_context
    assert not fake._exec_calls
    assert fake._renders


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,completed", [("executed", True), ("pending", False), ("denied", False)])
async def test_hitl_gate_marks_only_completed_dispatch(gate_orchestrator, monkeypatch, outcome, completed):  # noqa: F811
    from orchestrator import hitl_confirmation, source_bound
    from shared.protocol import MCPResponse

    orch, ws = gate_orchestrator
    monkeypatch.setattr(source_bound, "registered_action", lambda *args: source_bound.ActionDeclaration(
        "agent-a", "preview", "tools:execute", source_bound.Effect.PRIVILEGED))
    orch._source_ledger = lambda *args: types.SimpleNamespace(origin_of=lambda args: source_bound.Origin.HUMAN)
    response = (MCPResponse(error={"message": "Denied"}) if outcome == "denied" else MCPResponse(
        result={"_data": {"status": "confirmation_required"} if outcome == "pending" else {"state": "running"}}))
    evaluate = AsyncMock(return_value=response)
    monkeypatch.setattr(hitl_confirmation, "evaluate", evaluate)
    gate = await orch._run_project_bound_gate_stack(ws, "agent-a", "preview", {}, "chat-a", "owner-a")
    assert isinstance(gate, GateRefusal)
    assert gate.response is response
    assert gate.dispatch_completed is completed
    evaluate.assert_awaited_once()
