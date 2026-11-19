"""Binds an attended approval task to a socket and chat for tests of orchestrator/hitl_confirmation.py, the binding a running turn used to make.
No production code makes it any more, so only these tests can reach a pending approval card."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from orchestrator import hitl_confirmation


def begin_task(orch, websocket, owner, chat) -> None:
    if not owner or not chat or not hitl_confirmation._attended(orch, websocket, owner):
        return
    tasks = getattr(orch, "_hitl_task_origins", {})
    tasks.pop((owner, chat), None)
    tasks[(owner, chat)] = (websocket, SimpleNamespace(task_id=uuid.uuid4().hex))
    getattr(orch, "_hitl_goal_contexts", {}).pop((owner, chat), None)
    orch._hitl_task_origins = tasks
