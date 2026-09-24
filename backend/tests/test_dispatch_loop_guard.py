"""Tests that the dispatch paths' draft-agent auto-fix lookup never runs a synchronous
DB read on the event loop thread, using a lifecycle DB double that records which
thread served each read.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

def _loop_running_here() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _LoopRecordingDraftDB:
    def __init__(self):
        self.on_loop = []

    def get_draft_agent_by_slug(self, slug):
        self.on_loop.append(_loop_running_here())
        return None


@pytest.fixture()
def orch(monkeypatch, orchestrator_factory):
    o = orchestrator_factory()
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
