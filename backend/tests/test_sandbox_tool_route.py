"""Checks that the standalone qualification diagnostic stays out of personal-node tool discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import auth, local_agents, sandbox_qualification
from orchestrator.orchestrator import Orchestrator
from shared.feature_flags import FeatureFlags
from shared.protocol import AgentCard
from start import _agent_entrypoint


def test_old_flag_cannot_publish_the_qualification_actuator(monkeypatch):
    monkeypatch.setenv("FF_SANDBOX_QUALIFICATION", "1")
    assert not FeatureFlags().is_enabled("sandbox_qualification")
    assert "sandbox_qualification" not in local_agents.FIRST_PARTY_AGENT_DIRS
    assert "sandbox_qualification" not in local_agents.discover_built_in_agent_dirs()
    assert sandbox_qualification.QUALIFICATION_AGENT_ID not in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS


def test_stale_qualification_agent_on_disk_is_never_started(tmp_path):
    source = tmp_path / "sandbox_qualification"
    source.mkdir()
    (source / "sandbox_qualification_agent.py").touch()
    assert _agent_entrypoint(str(tmp_path), source.name) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tunnel", [True, False])
async def test_registration_refuses_the_retired_actuator_id(monkeypatch, tunnel):
    monkeypatch.setattr(auth, "validate_agent_api_key", lambda _: True)
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards, orch.agents = {}, {}
    orch.user_agent_registry = object()
    orch._audit_user_agent = AsyncMock()
    socket = SimpleNamespace(close=AsyncMock(), is_user_agent_tunnel=tunnel, owner_sub="owner")
    card = AgentCard(name="Qualification", description="", agent_id=sandbox_qualification.QUALIFICATION_AGENT_ID,
                     skills=[])
    await orch.register_agent(socket, SimpleNamespace(agent_card=card, api_key="test-only"))
    reason = "user-agent registration refused" if tunnel else "agent id reserved"
    socket.close.assert_awaited_once_with(code=1008, reason=reason)
    if tunnel:
        assert "reserved agent" in orch._audit_user_agent.await_args.args[2]
    assert not orch.agent_cards and not orch.agents
