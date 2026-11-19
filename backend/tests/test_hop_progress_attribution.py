"""Tests confirming chained-hop progress (orchestrator.py) rides the existing chat_status
frame with per-hop attribution, introducing no new client-visible frame type.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MANIFEST = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "components",
    "AstralProjection",
    "contracts",
    "ui_protocol.json",
)


def test_no_new_frame_type_in_the_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    names = {e["name"] for e in manifest["push_types"]}
    assert "agent_hop_request" not in names
    assert "agent_hop_response" not in names
    assert "subtask_progress" not in names
    assert "chat_status" in names


def test_hop_frames_are_agent_channel_only():
    import inspect

    from orchestrator import orchestrator as orch_mod

    src = inspect.getsource(orch_mod.Orchestrator._deliver_hop_response)
    assert "ui_clients" not in src
    assert "send_ui_render" not in src
