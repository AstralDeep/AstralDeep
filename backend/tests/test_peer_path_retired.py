"""T036 (056-delegated-agent-chaining): the direct peer-call path is retired.

048's audit found ``BaseA2AAgent.call_peer_tool`` forwarded the caller's
delegation token UNATTENUATED to a peer — a confused-deputy seam bypassing
the orchestrator's entire gate stack. Feature 056 removed it (FR-010, D12);
these tests pin the removal so an agent can never bypass orchestrator
mediation (SC-010). The sanctioned replacement is the mediated
``AgentRuntime.call_agent_tool``.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.base_agent import BaseA2AAgent  # noqa: E402


RETIRED_SURFACE = (
    "call_peer_tool",
    "_call_peer_via_ws",
    "_call_peer_via_a2a",
    "connect_to_peer",
    "_peer_listen_loop",
)


def test_peer_call_surface_removed():
    for name in RETIRED_SURFACE:
        assert not hasattr(BaseA2AAgent, name), (
            f"{name} must stay retired — direct peer transport bypasses "
            f"orchestrator mediation (FR-010)")


def test_peer_state_removed_from_source():
    """No peer connection registry / pending-future state remains."""
    src = inspect.getsource(sys.modules["shared.base_agent"])
    for symbol in ("peer_connections", "peer_pending", "_peer_registry"):
        live = [ln for ln in src.splitlines()
                if symbol in ln and not ln.lstrip().startswith("#")]
        assert not live, f"live reference to retired peer state: {live}"


def test_no_live_call_sites_repo_wide():
    """Nothing under backend/ (outside comments/specs) invokes the retired
    path — an attempted call is an AttributeError, 100% failure (SC-010)."""
    backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(backend_root):
        if any(part in {"__pycache__", "tests"} for part in Path(dirpath).parts):
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh, 1):
                    if "call_peer_tool(" in line and not line.lstrip().startswith("#"):
                        offenders.append(f"{path}:{i}")
    assert not offenders, f"live call sites of retired peer path: {offenders}"


def test_mediated_replacement_exists():
    from shared.agent_runtime import AgentRuntime
    assert hasattr(AgentRuntime, "call_agent_tool")
    doc = AgentRuntime.call_agent_tool.__doc__ or ""
    assert "mediated" in doc.lower() or "orchestrator" in doc.lower()
