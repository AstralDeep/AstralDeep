"""Verifies auxiliary model calls retain current evidence admission across provider awaits.
Signed turn fixtures and public Plane captures isolate the missing-service boundary without external model traffic.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from llm_config import CredentialSource, ResolvedConfig
from orchestrator import context_authority, evidence_context
from orchestrator.evidence_archive import EvidenceDenied
from orchestrator.orchestrator import Orchestrator
from shared.feature_flags import flags
from tests.test_evidence_dispatch import response
from tests.test_evidence_model import (
    MODEL, admitted_model, bound as bound, configured_model as configured_model,
    fixture as fixture, human as human, invoke, model_controls as model_controls,
    model_request, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


def disable(monkeypatch):
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)


@pytest.mark.parametrize("auxiliary", ["summary", "title"])
async def test_enabled_auxiliary_capture_without_service_refuses_after_flag_disable(
    configured_model, human, bound, fixture, monkeypatch, auxiliary,
):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        host = state.host
        del host._evidence_context
        assert evidence_context.enabled() and not hasattr(host, "_evidence_context")
        original_capture = state.store.capture_user
        captured = []

        async def capture_and_disable(owner):
            assert evidence_context.enabled() and not hasattr(host, "_evidence_context")
            current = await original_capture(owner)
            assert current.owner_id == state.owner and current.matches(turn.capture._record)
            captured.append(current)
            disable(monkeypatch)
            assert not evidence_context.enabled() and not hasattr(host, "_evidence_context")
            return current

        monkeypatch.setattr(state.store, "capture_user", capture_and_disable)
        physical = MagicMock(return_value=response("Synthetic unqualified result"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=physical)))

        def build(config, source):
            assert source is CredentialSource.USER and config.model == MODEL
            return client, source, ResolvedConfig(base_url=config.base_url, model=config.model)

        host._build_llm_client = build
        host._llm_audit_principals = Orchestrator._llm_audit_principals.__get__(host)
        host._accumulate_usage = Orchestrator._accumulate_usage.__get__(host)
        host._safe_llm_error_metadata = Orchestrator._safe_llm_error_metadata
        host._derive_chat_title = Orchestrator._derive_chat_title
        host.audit_recorder = None
        host.token_usage = {}
        host.history = MagicMock()
        host._record_llm_call = AsyncMock()
        host._record_llm_unconfigured = AsyncMock()
        host._emit_llm_usage_report = AsyncMock()
        host._broadcast_user_history = AsyncMock()
        host._ws_active_chat = {id(turn.socket): turn.chat}
        original = context_authority.current_context_authority(
            orchestrator=host, websocket=turn.socket, chat_id=turn.chat,
        )
        assert original is turn.lease

        if auxiliary == "summary":
            result = await Orchestrator._generate_tool_summary(
                host, turn.socket, [{"role": "tool", "content": "Synthetic untrusted source"}],
                turn.chat, state.owner,
            )
            assert result is None
            host.history.update_chat_title.assert_not_called()
        else:
            result = await Orchestrator.summarize_chat_title(
                host, turn.chat, "Synthetic user request", user_id=state.owner, websocket=turn.socket,
            )
            assert result is None
            host.history.update_chat_title.assert_called_once_with(
                turn.chat, "Synthetic user request", user_id=state.owner,
            )
        assert len(captured) == 1 and client._evidence_provider_capture is captured[0]
        physical.assert_not_called()
        assert not hasattr(host, "_evidence_context")
        assert state.events == [] and host.token_usage == {}
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["attempts"] == totals["model_calls"] == 0
        assert host._record_llm_call.await_args.kwargs["outcome"] == "failure"
        assert host._record_llm_call.await_args.kwargs["total_tokens"] is None
        assert host._emit_llm_usage_report.await_args.kwargs["usage"] is None


async def test_captured_request_with_orphaned_reference_and_no_service_refuses_physical_call(
    configured_model, human, bound, fixture, monkeypatch,
):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        del state.host._evidence_context
        disable(monkeypatch)
        request = model_request()
        request["messages"] = [{"role": "user", "content": "Inspect obs_" + "x" * 43}]
        physical = AsyncMock(return_value=response())
        with pytest.raises(EvidenceDenied):
            await invoke(state, turn, request, physical)
        physical.assert_not_awaited()
        assert state.events == [] and not hasattr(state.host, "_evidence_context")
