"""Regression tests for the agent_cards readers that orchestrator/orchestrator.py runs on
worker threads: each must finish when an agent registers or leaves on the event loop mid-read.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard, AgentSkill, CandidateCapabilityMap

_TIMEOUT = 30.0
_REGISTERED = ("alpha-1", "beta-1")


class _MidReadPause:
    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self._pending = True

    def __call__(self) -> None:
        if self._pending:
            self._pending = False
            self.reached.set()
            assert self.release.wait(_TIMEOUT), "reader was never released"


class _Permissions:
    def __init__(self, pause: _MidReadPause, allowed: frozenset[str]) -> None:
        self._pause = pause
        self._allowed = allowed

    def get_agent_scopes(self, user_id: str, agent_id: str) -> dict:
        self._pause()
        return {"tools:read": True}

    def get_tool_scope_map(self, agent_id: str) -> dict:
        return {}

    def get_effective_permissions(self, user_id: str, agent_id: str, tools: list) -> dict:
        return {tool: True for tool in tools}

    def is_tool_allowed(self, user_id: str, agent_id: str, tool: str) -> bool:
        self._pause()
        return tool in self._allowed


class _PausingSkill:
    def __init__(self, pause: _MidReadPause) -> None:
        self._pause = pause

    @property
    def id(self) -> str:
        self._pause()
        return "alpha_tool"


def _tool(agent_id: str) -> str:
    return f"{agent_id.split('-')[0]}_tool"


def _card(agent_id: str) -> AgentCard:
    tool = _tool(agent_id)
    return AgentCard(
        name=agent_id,
        description="test agent",
        agent_id=agent_id,
        skills=[AgentSkill(id=tool, name=tool, description=tool, input_schema={})],
        metadata={},
    )


def _orchestrator(pause: _MidReadPause, *, allowed: frozenset[str] = frozenset()) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {agent_id: _card(agent_id) for agent_id in _REGISTERED}
    orch.agents = dict.fromkeys((*_REGISTERED, "late-1"), object())
    orch.local_agents = {}
    orch.security_flags = {}
    orch.user_agent_registry = SimpleNamespace(get_all_agent_ownership=lambda: [])
    orch.tool_permissions = _Permissions(pause, allowed)
    return orch


def _register(cards: dict) -> None:
    cards["late-1"] = _card("late-1")


def _deregister(cards: dict) -> None:
    del cards["beta-1"]


def _read_while_mutating(cards: dict, pause: _MidReadPause, reader, mutate=_register):
    with ThreadPoolExecutor(max_workers=1) as pool:
        reading = pool.submit(reader)
        try:
            assert pause.reached.wait(_TIMEOUT), "reader never iterated agent_cards"
            mutate(cards)
        finally:
            pause.release.set()
        return reading.result(timeout=_TIMEOUT)


def _capture_frames(orch: Orchestrator):
    websocket = object()
    frames: list[dict] = []

    async def capture(_websocket, payload: str) -> None:
        frames.append(json.loads(payload))

    orch.ui_sessions = {websocket: {"sub": "user-1"}}
    orch._safe_send = capture
    return websocket, frames


async def _deliver_while_registering(orch: Orchestrator, pause: _MidReadPause, send) -> None:
    delivery = asyncio.create_task(send)
    try:
        assert await asyncio.to_thread(pause.reached.wait, _TIMEOUT), (
            "delivery never iterated agent_cards"
        )
        _register(orch.agent_cards)
    finally:
        pause.release.set()
    await asyncio.wait_for(delivery, _TIMEOUT)


@pytest.mark.parametrize("mutate", [_register, _deregister])
def test_dashboard_agent_list_survives_agent_cards_changing_on_another_thread(mutate) -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause)

    agents = _read_while_mutating(
        orch.agent_cards,
        pause,
        lambda: orch._build_dashboard_agent_list("user-1"),
        mutate,
    )

    assert [agent["id"] for agent in agents] == list(_REGISTERED)
    assert [agent["tools"] for agent in agents] == [["alpha_tool"], ["beta_tool"]]


@pytest.mark.asyncio
async def test_send_dashboard_delivers_when_an_agent_registers_mid_build() -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause)
    websocket, frames = _capture_frames(orch)
    orch._streamable_tools = {}
    orch.personal_agent_capabilities = CandidateCapabilityMap()

    await _deliver_while_registering(orch, pause, orch.send_dashboard(websocket))

    assert [frame["type"] for frame in frames] == ["system_config"]
    config = frames[0]["config"]
    assert [agent["id"] for agent in config["agents"]] == list(_REGISTERED)
    assert config["total_tools"] == 2


@pytest.mark.asyncio
async def test_send_agent_list_delivers_when_an_agent_registers_mid_build() -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause, allowed=frozenset({"beta_tool"}))
    websocket, frames = _capture_frames(orch)

    await _deliver_while_registering(orch, pause, orch.send_agent_list(websocket))

    assert [frame["type"] for frame in frames] == ["agent_list"]
    assert [agent["id"] for agent in frames[0]["agents"]] == list(_REGISTERED)
    assert frames[0]["tools_available_for_user"] is True


def test_tools_available_check_survives_registration_on_another_thread() -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause, allowed=frozenset({"beta_tool"}))

    available = _read_while_mutating(
        orch.agent_cards,
        pause,
        lambda: orch.compute_tools_available_for_user("user-1"),
    )

    assert available is True


def test_tool_owner_lookup_survives_registration_on_another_thread() -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause)
    orch.agent_cards["alpha-1"] = SimpleNamespace(skills=[_PausingSkill(pause)])

    owner = _read_while_mutating(
        orch.agent_cards,
        pause,
        lambda: orch._find_tool_owner("beta_tool"),
    )

    assert owner == "beta-1"

