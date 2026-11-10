"""Regression tests for the agent_cards readers that run on worker threads in
orchestrator/orchestrator.py, orchestrator/tool_visibility.py, and
orchestrator/projection_surfaces/agents.py: each must finish when an agent registers or
leaves on the event loop mid-read.
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from orchestrator.orchestrator import Orchestrator
from orchestrator.projection_surfaces.agents import _agent_rows, _render_agent_row
from orchestrator.tool_visibility import eligible_tool_pairs
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


def test_agent_rows_classify_server_registered_a2a_peer_and_ignore_metadata() -> None:
    from orchestrator.remote_mcp_security import connection_security_for_card

    native = _card("panatlas-1")
    native.metadata["connection_security"] = {
        "kind": "standard_a2a", "message": "untrusted card metadata",
    }
    a2a_only = _card("a2a-peer")
    a2a_only.metadata["connection_security"] = {
        "kind": "standard_mcp", "message": "forged protocol claim",
    }
    orch = SimpleNamespace(
        agent_cards={"remote-mcp-1": _card("remote-mcp-1"),
                     "panatlas-1": native, "a2a-peer": a2a_only},
        _standard_a2a_agent_cards={"a2a-peer": a2a_only},
        local_agents={"remote-mcp-1": object()},
        _is_draft_agent=lambda _agent_id: False,
    )

    rows = {row["id"]: row for row in _agent_rows(orch, {}, set())}

    assert rows["remote-mcp-1"]["connection_security"]["kind"] == "standard_mcp"
    assert rows["a2a-peer"]["connection_security"]["kind"] == "standard_a2a"
    assert "connection_security" not in rows["panatlas-1"]
    assert "external_mcp_security" in _render_agent_row(rows["remote-mcp-1"], "public", "")
    assert "external_a2a_security" in _render_agent_row(rows["a2a-peer"], "public", "")
    assert 'aria-label="Standard A2A connection"' in _render_agent_row(rows["a2a-peer"], "public", "")
    assert "external_mcp_security" not in _render_agent_row(rows["panatlas-1"], "public", "")
    assert connection_security_for_card("missing", None, {"missing": None}) is None
    assert connection_security_for_card(
        "remote-mcp-1", _card("remote-mcp-1"), {}, {},
    ) is None
    remote_id_a2a = _card("remote-mcp-1")
    assert connection_security_for_card(
        "remote-mcp-1", remote_id_a2a, {"remote-mcp-1": remote_id_a2a}, {},
    )["kind"] == "standard_a2a"

    replacement = _card("a2a-peer")
    orch.agent_cards["a2a-peer"] = replacement
    rows = {row["id"]: row for row in _agent_rows(orch, {}, set())}
    assert "connection_security" not in rows["a2a-peer"]

    del orch.agent_cards["a2a-peer"]
    rows = {row["id"]: row for row in _agent_rows(orch, {}, set())}
    assert "a2a-peer" not in rows


def test_agent_row_security_uses_the_card_from_its_iteration_snapshot() -> None:
    original = _card("racing-peer")
    original.name = "Original card"
    replacement = _card("racing-peer")
    replacement.name = "Replacement card"
    orch = SimpleNamespace(
        agent_cards={"racing-peer": original},
        _standard_a2a_agent_cards={},
        local_agents={},
    )

    def replace_card(agent_id):
        orch.agent_cards[agent_id] = replacement
        orch._standard_a2a_agent_cards[agent_id] = replacement
        return False

    orch._is_draft_agent = replace_card
    rows = _agent_rows(orch, {}, set())

    assert rows[0]["name"] == "Original card"
    assert "connection_security" not in rows[0]


def test_system_config_agent_catalog_carries_server_registered_a2a_notice() -> None:
    card = _card("partner-1")
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = {card.agent_id: card}
    orch._standard_a2a_agent_cards = {card.agent_id: card}
    orch.local_agents = {}
    orch.user_agent_registry = SimpleNamespace(get_all_agent_ownership=lambda: [])
    orch.tool_permissions = SimpleNamespace(
        get_agent_scopes=lambda *_args: {},
        get_tool_scope_map=lambda *_args: {},
        get_effective_permissions=lambda *_args: {},
    )
    orch.security_flags = {}
    orch._is_draft_agent = lambda _agent_id: False

    agents = orch._build_dashboard_agent_list("owner")

    assert agents[0]["connection_security"]["kind"] == "standard_a2a"


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


def test_eligible_tool_pairs_survive_registration_on_another_thread() -> None:
    pause = _MidReadPause()
    orch = _orchestrator(pause, allowed=frozenset({"alpha_tool", "beta_tool"}))

    pairs = _read_while_mutating(
        orch.agent_cards,
        pause,
        lambda: eligible_tool_pairs(orch, "user-1"),
    )

    assert [(agent_id, skill.id) for agent_id, skill in pairs] == [
        ("alpha-1", "alpha_tool"),
        ("beta-1", "beta_tool"),
    ]


def test_agent_surface_rows_survive_registration_on_another_thread() -> None:
    pause = _MidReadPause()

    def is_draft(agent_id: str) -> bool:
        pause()
        return False

    orch = SimpleNamespace(
        agent_cards={agent_id: _card(agent_id) for agent_id in _REGISTERED},
        _is_draft_agent=is_draft,
    )

    rows = _read_while_mutating(
        orch.agent_cards,
        pause,
        lambda: _agent_rows(orch, {}, set()),
    )

    assert [row["id"] for row in rows] == list(_REGISTERED)
