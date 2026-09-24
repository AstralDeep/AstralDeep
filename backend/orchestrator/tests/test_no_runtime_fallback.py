"""Tests for orchestrator/orchestrator.py's _resolve_llm_client_for: a user-context
resolution reads only that user's llm_config, a system-context (no websocket)
resolution reads only the system record, and neither ever falls back to the other.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_config.client_factory import build_llm_client
from llm_config.types import CredentialSource, LLMUnavailable
from llm_config.user_store import PersistedLLMConfig
from orchestrator.orchestrator import Orchestrator


def _system_config() -> PersistedLLMConfig:
    return PersistedLLMConfig(
        provider="custom",
        base_url="https://system.example/v1",
        model="sys-model",
        api_key="sk-system1234567890abcdef",
    )


def _user_config() -> PersistedLLMConfig:
    return PersistedLLMConfig(
        provider="custom",
        base_url="https://user.example/v1",
        model="user-model",
        api_key="sk-userkey1234567890abcdef",
    )


def _bare_orch(*, user_record=None, system_record=None):
    orch = Orchestrator.__new__(Orchestrator)
    orch._CredentialSource = CredentialSource
    orch._LLMUnavailable = LLMUnavailable
    orch._build_llm_client = build_llm_client
    orch.ui_sessions = {}
    store = MagicMock()
    store.get = AsyncMock(return_value=user_record)
    store.get_system = AsyncMock(return_value=system_record)
    store.pop_discard_note = MagicMock(return_value=None)
    orch._llm_store = store
    return orch


def _user_ws(orch, user_id="u1"):
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


@pytest.mark.asyncio
async def test_user_context_without_record_never_falls_back_to_system():
    orch = _bare_orch(user_record=None, system_record=_system_config())
    ws = _user_ws(orch)

    with pytest.raises(LLMUnavailable):
        await orch._resolve_llm_client_for(ws)

    orch._llm_store.get.assert_awaited_once_with("u1")
    orch._llm_store.get_system.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_context_resolves_only_the_callers_record():
    orch = _bare_orch(user_record=_user_config(),
                      system_record=_system_config())
    ws = _user_ws(orch)

    client, source, resolved = await orch._resolve_llm_client_for(ws)

    assert source == CredentialSource.USER
    assert resolved.base_url == "https://user.example/v1"
    assert resolved.model == "user-model"
    orch._llm_store.get_system.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_context_never_reads_user_records():
    orch = _bare_orch(user_record=_user_config(),
                      system_record=_system_config())

    client, source, resolved = await orch._resolve_llm_client_for(None)

    assert source == CredentialSource.SYSTEM
    assert resolved.base_url == "https://system.example/v1"
    orch._llm_store.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_context_without_record_is_unavailable_not_user_fallback():
    orch = _bare_orch(user_record=_user_config(), system_record=None)

    with pytest.raises(LLMUnavailable):
        await orch._resolve_llm_client_for(None)

    orch._llm_store.get.assert_not_awaited()


def test_factory_refuses_retired_operator_default_source():
    with pytest.raises(ValueError):
        build_llm_client(_user_config(), CredentialSource.OPERATOR_DEFAULT)


@pytest.mark.asyncio
async def test_scheduled_turn_virtualwebsocket_is_system_context():
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket

    orch = _bare_orch(user_record=_user_config(),
                      system_record=_system_config())
    vws = VirtualWebSocket(BackgroundTask(task_id="t1", chat_id="", user_id="u1"))

    client, source, resolved = await orch._resolve_llm_client_for(vws)

    assert source == CredentialSource.SYSTEM
    orch._llm_store.get.assert_not_awaited()
