"""Feature 040 (US1) — in-process built-in agent dispatch.

Verifies the bundled-agent set excludes the retired etf_tracker_1, that a
built-in agent instantiates without uvicorn, and that the in-process executor
runs the agent's real handler through a LoopbackSocket — producing an
MCPResponse identical in shape to the networked path, with the orchestrator
only ever seeing the result frame (not the agent's internal credential/runtime
handling). dice_roller is used because it is pure-compute (imports only
astralprims), so the test needs no DB and no heavy optional deps.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def isolated_agent_key(monkeypatch, tmp_path):
    """Exercise real crypto with an ephemeral key, never an installed agent key."""
    from shared import base_agent

    path = tmp_path / "agent-key.pem"
    monkeypatch.setenv("AGENT_KEY_PATH", str(path))
    accesses = []
    save, load = base_agent.save_private_key, base_agent.load_private_key

    def save_selected(key, selected):
        assert Path(selected) == path
        accesses.append("save")
        return save(key, selected)

    def load_selected(selected):
        assert Path(selected) == path
        accesses.append("load")
        return load(selected)

    monkeypatch.setattr(base_agent, "save_private_key", save_selected)
    monkeypatch.setattr(base_agent, "load_private_key", load_selected)
    yield path, accesses
    path.unlink(missing_ok=True)


def test_agent_crypto_loads_only_the_test_selected_key(isolated_agent_key):
    from agents.dice_roller.dice_roller_agent import DiceRollerAgent

    path, accesses = isolated_agent_key
    first = DiceRollerAgent(port=8003)
    second = DiceRollerAgent(port=8003)
    assert accesses == ["save", "load"]
    assert path.is_file()
    assert first._public_key_jwk == second._public_key_jwk


def test_built_in_dirs_exclude_etf_tracker():
    from orchestrator.local_agents import BUILT_IN_AGENT_DIRS, discover_built_in_agent_dirs

    assert "etf_tracker_1" not in BUILT_IN_AGENT_DIRS
    found = discover_built_in_agent_dirs()
    assert "etf_tracker_1" not in found
    # The nine bundled agents are present on disk.
    assert set(found) == set(BUILT_IN_AGENT_DIRS)
    assert len(found) == 9


def test_load_and_instantiate_built_in_without_uvicorn():
    from orchestrator.local_agents import _load_agent_class

    cls = _load_agent_class("dice_roller")
    assert cls is not None
    agent = cls()  # must not start a server
    assert agent.card.agent_id == "dice-roller-1"
    assert hasattr(agent, "mcp_server")


class _FakeOrch:
    """Minimal orchestrator surface for binding _execute_in_process."""

    def __init__(self):
        self.local_agents = {}
        self.pending_requests = {}
        self.pending_ui_sockets = {}
        self.stream_manager = None
        self.seen_frames = []  # every frame the loopback routed back
        # 056: dispatch-context bookkeeping surface used by _execute_in_process
        self._dispatch_context = {}
        from orchestrator.orchestrator import Orchestrator as _O
        self._register_dispatch_context = types.MethodType(
            _O._register_dispatch_context, self)

    async def handle_agent_message(self, websocket, message):
        from shared.protocol import Message, MCPResponse, ToolProgress

        self.seen_frames.append(message)
        msg = Message.from_json(message)
        if isinstance(msg, MCPResponse):
            fut = self.pending_requests.get(msg.request_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif isinstance(msg, ToolProgress):
            pass  # progress frames would route to _handle_tool_progress in prod


@pytest.mark.asyncio
async def test_inprocess_unary_parity_dice_roller():
    from orchestrator.local_agents import _load_agent_class
    from orchestrator.orchestrator import Orchestrator

    agent = _load_agent_class("dice_roller")()
    fake = _FakeOrch()
    fake.local_agents[agent.card.agent_id] = agent
    fake._execute_in_process = types.MethodType(Orchestrator._execute_in_process, fake)

    resp = await fake._execute_in_process(
        agent.card.agent_id, "roll_dice", {"n": 3}, timeout=10.0
    )

    # Identical MCPResponse shape to the WS path: result + ui_components, no error.
    assert resp is not None
    assert resp.error is None
    assert resp.ui_components, "dice roll should return UI components"
    assert resp.result and resp.result.get("n") == 3
    assert len(resp.result.get("rolls", [])) == 3
    # The orchestrator only ever saw the agent's result frame — its internal
    # _runtime/credential handling never crossed the loopback boundary.
    assert len(fake.seen_frames) == 1


def test_credential_decryption_is_agent_side():
    """FR-030: credential decryption lives on the agent, not the orchestrator.

    The in-process path reuses the agent's own ``handle_mcp_request``, which
    calls ``_decrypt_credentials_if_needed`` inside the agent boundary — so the
    orchestrator never materializes plaintext per-user secrets.
    """
    from orchestrator.local_agents import _load_agent_class

    agent = _load_agent_class("dice_roller")()
    assert hasattr(agent, "_decrypt_credentials_if_needed")


@pytest.mark.asyncio
async def test_inprocess_runtime_injection_is_kwarg_safe():
    """_runtime is injected agent-side; a tool that accepts **kwargs runs clean."""
    from orchestrator.local_agents import _load_agent_class
    from orchestrator.orchestrator import Orchestrator

    agent = _load_agent_class("dice_roller")()
    fake = _FakeOrch()
    fake.local_agents[agent.card.agent_id] = agent
    fake._execute_in_process = types.MethodType(Orchestrator._execute_in_process, fake)

    resp = await fake._execute_in_process(agent.card.agent_id, "roll_dice", {}, timeout=10.0)
    assert resp.error is None  # injected _runtime did not break the call
