"""Trust transitions must be insertable into the UUID audit schema."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.mark.asyncio
async def test_trust_audit_uses_uuid_correlation(monkeypatch):
    from orchestrator import agent_trust, agentic_creation

    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(agentic_creation, "_audit", _capture)

    await agent_trust._emit_audit(
        actor="owner-1",
        action="marked_safe",
        agent_id="panatlas-1",
        prior=False,
        new=True,
        chat_id=None,
    )

    parsed = uuid.UUID(captured["correlation_id"])
    assert str(parsed) == captured["correlation_id"]
    assert captured["agent_id"] == "panatlas-1"
    assert captured["inputs_meta"] == {"prior_state": False, "is_safe": True}
