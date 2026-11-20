"""Verifies context controls and owner-bound host integration through ordinary dispatch.
The feature modules preserve authority and histories under failed or stale work.
"""

import pytest

from shared.feature_flags import FeatureFlags


@pytest.mark.parametrize("packing,compaction", [(False, False), (True, False), (False, True), (True, True)])
def test_independent_context_controls(monkeypatch, packing, compaction):
    monkeypatch.setenv("FF_OBSERVATION_PACKING", str(packing))
    monkeypatch.setenv("FF_SAFE_COMPACTION", str(compaction))
    registry = FeatureFlags()
    assert registry.is_enabled("observation_packing") is packing
    assert registry.is_enabled("safe_compaction") is compaction


def test_context_controls_default_off(monkeypatch):
    monkeypatch.delenv("FF_OBSERVATION_PACKING", raising=False)
    monkeypatch.delenv("FF_SAFE_COMPACTION", raising=False)
    registry = FeatureFlags()
    assert not registry.is_enabled("observation_packing")
    assert not registry.is_enabled("safe_compaction")


@pytest.mark.asyncio
async def test_model_guard_requires_current_owner_authority_at_preparation_and_send(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from llm_config.data_sharing import DataSharingStore
    from llm_config.user_store import UserLLMConfigStore
    from orchestrator import context_authority
    from orchestrator.evidence_archive import EvidenceDenied
    from orchestrator.evidence_context import EvidenceContext
    from persistent_agents.dispatch_context import PersistentDispatchContext, bind_dispatch
    from persistent_agents.models import AssignmentError
    from shared.feature_flags import flags
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    owner, chat, socket = "evidence-owner", "chat", object()
    route = {"provider": "custom", "base_url": "https://model.invalid/v1", "model": "fixture-model"}
    budget = tmp_path / "budget.json"
    budget.write_text(json.dumps([{"owner_id": owner, **route, "context_tokens": 4096,
                                  "max_output_tokens": 128, "output_parameter": "max_tokens"}]))
    monkeypatch.setenv("ASTRAL_CONTEXT_BUDGET_POLICY", str(budget))
    monkeypatch.setitem(flags._flags, "observation_packing", True)
    with isolated_plane_runtime("ad_evidence_model_guard") as plane:
        store = UserLLMConfigStore(plane_runtime=plane, data_dir=str(tmp_path))
        sharing = DataSharingStore(plane_runtime=plane)
        host = SimpleNamespace(_llm_store=store, _data_sharing_store=sharing)
        context = EvidenceContext(host)
        await store.set(owner, **route, api_key="fixture-key")
        await sharing.acknowledge(owner)
        capture = await store.capture_user(owner)
        call = {"owner": owner, "chat": chat, "websocket": socket, "model": route["model"],
                "base_url": route["base_url"], "provider_capture": capture,
                "request": {"model": route["model"], "messages": [{"role": "user", "content": "Summary"}]}}
        reservation = PersistentDispatchContext(owner, "model", None, None, {}, 1, 1024, 128,
                                                AsyncMock(), AsyncMock(), AsyncMock(), conversation_id=chat)
        with bind_dispatch(reservation), pytest.raises(EvidenceDenied):
            await context.prepare_model(**call)
        with pytest.raises(AssignmentError, match="context_authority_unavailable"):
            await context.prepare_model(**call)

        lease = Mock(spec=context_authority.ContextAuthorityLease)
        lease.verify.return_value = SimpleNamespace(owner_id=owner)
        monkeypatch.setattr(context_authority, "current_context_authority", lambda **kwargs: lease)
        guard = await context.prepare_model(**call)
        await guard()
        lease.verify.assert_awaited_once_with(orchestrator=host, websocket=socket, chat_id=chat)
        lease.assert_current.assert_called_once_with(orchestrator=host, websocket=socket, chat_id=chat)
        with bind_dispatch(reservation), pytest.raises(EvidenceDenied):
            await guard()
        assert lease.verify.await_count == 1
        lease.verify.return_value = SimpleNamespace(owner_id="different-owner")
        with pytest.raises(EvidenceDenied):
            await guard()
