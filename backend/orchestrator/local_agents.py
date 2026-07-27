"""Feature 040 (US1) — in-process registry of the bundled first-party agents.

The nine first-party agents that ship with the product run *inside* the
orchestrator process (no per-agent uvicorn port) when ``FF_INPROCESS_AGENTS`` is
on. This module discovers and instantiates them (without calling ``.run()``),
then registers them through the orchestrator's normal ``register_agent`` path
(``websocket=None``) so the card, tool→scope map, security flags, ownership, and
ECIES public key are all set up exactly as for a networked agent — and records
each live instance in ``orchestrator.local_agents`` for the dispatch branch.

Externally-hosted A2A agents and user-created draft agents are untouched: the
in-process path is selected only by a positive ``local_agents`` membership check.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
from typing import List, Optional

logger = logging.getLogger("LocalAgents")

#: The nine bundled first-party agent directory names. ``etf_tracker_1`` was
#: retired in feature 040. This is the canonical built-in set (BUILT_IN_AGENT_IDS
#: equivalent) referenced by the in-process registry.
BUILT_IN_AGENT_DIRS = (
    "connectors",
    "dice_roller",
    "general",
    "journal_review",
    "medical",
    "ml_services",
    "summarizer",
    "weather",
    "web_research",
)

#: Feature 063 remote-compute agent dirs — registered ONLY when FF_REMOTE_COMPUTE
#: is on (see register_built_ins). remote_control (mutating) is added later.
_REMOTE_COMPUTE_AGENT_DIRS = (
    "remote_observe",
)


def _agents_root() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")


def discover_built_in_agent_dirs(agents_root: Optional[str] = None) -> List[str]:
    """Return the bundled agent dir names that are present with an agent module."""
    root = agents_root or _agents_root()
    found = []
    for name in BUILT_IN_AGENT_DIRS:
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, f"{name}_agent.py")):
            found.append(name)
    return found


def _load_agent_class(dir_name: str):
    """Import ``agents.<dir>.<dir>_agent`` and return its BaseA2AAgent subclass."""
    from shared.base_agent import BaseA2AAgent

    mod = importlib.import_module(f"agents.{dir_name}.{dir_name}_agent")
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, BaseA2AAgent) and obj is not BaseA2AAgent and obj.__module__ == mod.__name__:
            return obj
    return None


async def register_built_ins(orch) -> List[str]:
    """Instantiate + register every bundled built-in agent in-process.

    Returns the list of agent ids registered. Per-agent failures are logged and
    skipped (never fatal). Idempotent: re-registering an already-present agent
    simply refreshes its registration side-effects.
    """
    from shared.protocol import RegisterAgent

    registered: List[str] = []
    dirs = discover_built_in_agent_dirs()
    # Feature 063: the remote-compute agent(s) register ONLY when FF_REMOTE_COMPUTE
    # is on (fail-closed, FR-005). With the flag off they are absent from the fleet
    # and no verb is listed or invocable — byte-identical to the pre-063 product.
    try:
        from shared.feature_flags import flags
        if flags.is_enabled("remote_compute"):
            root = _agents_root()
            for name in _REMOTE_COMPUTE_AGENT_DIRS:
                d = os.path.join(root, name)
                if (name not in dirs and os.path.isdir(d)
                        and os.path.exists(os.path.join(d, f"{name}_agent.py"))):
                    dirs.append(name)
    except Exception:  # noqa: BLE001
        logger.debug("Feature 063 flag check failed (non-fatal)", exc_info=True)
    for dir_name in dirs:
        try:
            cls = _load_agent_class(dir_name)
            if cls is None:
                logger.warning("Feature 040: no BaseA2AAgent subclass found in '%s'", dir_name)
                continue
            agent = cls()  # builds the MCP server + ECIES keys; does NOT start uvicorn
            await orch.register_agent(
                None,
                RegisterAgent(agent_card=agent.card, api_key=os.getenv("AGENT_API_KEY") or None),
            )
            orch.local_agents[agent.card.agent_id] = agent
            registered.append(agent.card.agent_id)
        except Exception:  # noqa: BLE001 — a bad agent must not break the others or boot
            logger.exception("Feature 040: failed to load built-in agent '%s' in-process", dir_name)
    if registered:
        logger.info("Feature 040: %d built-in agents registered in-process: %s",
                    len(registered), registered)
    return registered
