"""Holds real manual-review preparation boundaries while host mode or authority changes.
The ordinary gate must retire obsolete proposals without publishing cards or dispatching effects.
"""

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from orchestrator import hitl_confirmation as hc, remote_confirmation as rc, write_review as wr
from orchestrator.computer_hosts import ComputerHostError
from tests.test_computer_grant_flow_102 import OWNER, CHAT, ROOT, SCOPES, _call, _mac, _fresh_pending_cards  # noqa: F401
from tests.test_hitl_public_research import runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401
from tests.test_write_review_diff import _host_reads


@pytest.mark.parametrize("boundary", ["hitl_crop", "hitl_diff", "hitl_privacy", "remote_worker", "remote_diff", "write_preview"])
@pytest.mark.parametrize("change", ["bypass", "bypass_roundtrip", "stop"])
async def test_manual_publication_cannot_outlive_held_preparation(task_runtime, monkeypatch, boundary, change):  # noqa: F811
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=True)
    orch.computer_sessions._orch = orch
    orch._safe_send = AsyncMock()
    orch.ui_clients = {ws}
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": "tools:files"}
    _host_reads(monkeypatch, orch, ComputerHostError("not_found", "missing"))
    entered, released = asyncio.Event(), asyncio.Event()
    thread_release = threading.Event()
    loop = asyncio.get_running_loop()
    args = {"path": ROOT + "/notes.md", "content": "new", "if_exists": "overwrite"}
    if boundary == "hitl_crop":
        def held_crop(_points):
            loop.call_soon_threadsafe(entered.set)
            assert thread_release.wait(10)
            return None
        monkeypatch.setattr(session, "pointer_crop", held_crop)
        tool, args = "click", {"x": 10, "y": 20, "hwnd": 42}
    else:
        tool = "write_file"
        if boundary in {"remote_worker", "hitl_privacy"}:
            module, function = (rc, "evaluate") if boundary == "remote_worker" else (hc, "_review_arguments")
            evaluate = getattr(module, function)
            def held_worker(*a, **kw):
                result = evaluate(*a, **kw)
                loop.call_soon_threadsafe(entered.set)
                assert thread_release.wait(10)
                return result
            monkeypatch.setattr(module, function, held_worker)
        else:
            name = {"hitl_diff": "review_components", "remote_diff": "attach_write_diff",
                    "write_preview": "prepare_write_review"}[boundary]
            prepare = getattr(wr, name)
            async def held(*a, **kw):
                result = await prepare(*a, **kw)
                entered.set()
                await released.wait()
                return result
            monkeypatch.setattr(wr, name, held)
    if boundary.startswith("hitl_"):
        invocation = hc.evaluate(orch, ws, OWNER, CHAT, "computer-use-1", tool, args, {"source_bound"})
    else:
        invocation = _call(task_runtime, tool, **args)
    task = asyncio.create_task(invocation)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if change == "stop":
            await orch.computer_sessions.end(session, "user_stop")
        else:
            await orch.computer_sessions.set_bypass(host, True)
            if change == "bypass_roundtrip":
                await orch.computer_sessions.set_bypass(host, False)
    finally:
        thread_release.set()
        released.set()
    response = await asyncio.wait_for(task, 10)
    assert response.error is not None
    assert not response.ui_components
    if change != "stop":
        from orchestrator.tool_feedback import tool_failure_content
        public = json.loads(tool_failure_content(tool, response.error))
        assert public["code"] == "COMPUTER_REVIEW_CHANGED"
        assert "fresh request" in public["message"] and "current" in public["message"]
        assert "No action ran" in public["message"] and public["retryable"] is False
        assert "wait for" not in public["message"]
    assert not effects
    assert not getattr(orch, "_hitl_pending_calls", {})
    assert not getattr(orch, "_computer_approval_authorities", {})
    if change != "stop":
        await orch.computer_sessions.end(session, "user_stop")
