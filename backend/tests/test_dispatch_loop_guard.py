"""The dispatch paths' draft-agent auto-fix lookup must not block the event loop.

`execute_parallel_tools` (and the single-tool path) consult
`lifecycle_manager._get_draft_by_agent_id` when a tool result errors — a
Database read. Feature 052's rule: no synchronous DB call on the loop thread
(`tests/plugins/event_loop_guard.py`, empty allowlist). This test drives the
error branch (via the malformed-arguments hard gate, which needs no dispatch
machinery) with a lifecycle db double that records which thread served the
read — pinning the `asyncio.to_thread` routing.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.orchestrator import Orchestrator


def _loop_running_here() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _LoopRecordingDraftDB:
    """Draft-lookup double recording whether each read ran on the loop thread."""

    def __init__(self):
        self.on_loop = []

    def get_draft_agent_by_slug(self, slug):
        self.on_loop.append(_loop_running_here())
        return None


@pytest.fixture()
def orch(monkeypatch):
    o = Orchestrator()
    o.send_ui_render = AsyncMock()
    o._safe_send = AsyncMock()
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    db = _LoopRecordingDraftDB()

    def _find(agent_id):
        if not agent_id.endswith("-1"):
            return None
        return db.get_draft_agent_by_slug(agent_id[:-2].replace("-", "_"))

    o.lifecycle_manager = SimpleNamespace(
        db=db,
        _find_draft_by_agent_id=_find,
        _get_draft_by_agent_id=_find,
        auto_fix_tool_error=AsyncMock(return_value=False),
    )
    return o, db


async def test_parallel_dispatch_draft_lookup_runs_off_the_loop(orch):
    o, db = orch
    # Malformed JSON arguments hit the hard gate: an error result with no
    # dispatch/authorization machinery, which is exactly what reaches the
    # auto-fix draft lookup afterwards.
    bad_call = SimpleNamespace(
        id="tc-1",
        function=SimpleNamespace(name="roll_dice", arguments="{not json"))
    results = await o.execute_parallel_tools(
        AsyncMock(), [bad_call], {"roll_dice": "dice-roller-1"}, "c1", "u1")
    assert results and results[0].error
    assert db.on_loop, "the error branch must consult the draft lookup"
    assert not any(db.on_loop), (
        "draft lookup ran a sync DB read ON the event-loop thread — route it "
        "through asyncio.to_thread (feature-052 rule)")
