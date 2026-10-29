"""Real manager-admitted parent identity for delegated-work tests."""

from __future__ import annotations

import asyncio
import types
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from orchestrator.async_tasks import BackgroundTaskManager, TaskStatus
from orchestrator.connection_context import _CONNECTION_OPERATION_CONTEXT
from orchestrator.work_admission import (
    AdmissionClass,
    AdmissionClassConfig,
    InMemoryWorkAdmissionRepository,
    WorkAdmissionCoordinator,
)


@asynccontextmanager
async def admitted_parent(*, user_id: str = "u1", chat_id: str = "c1",
                          orch: Any | None = None):
    coordinator = WorkAdmissionCoordinator(
        admission_classes=(AdmissionClassConfig(
            class_name=AdmissionClass.BACKGROUND,
            parent_class_name=None,
            active_limit=2,
            queue_limit=0,
            max_wait_ms=None,
            config_revision="subtask-test",
        ),),
        repository=InMemoryWorkAdmissionRepository(),
        clock=lambda: datetime.now(UTC),
    )
    manager = BackgroundTaskManager(coordinator=coordinator)
    entered, release = asyncio.Event(), asyncio.Event()
    parent_sockets = {}

    async def hold(websocket):
        parent_sockets["websocket"] = websocket
        entered.set()
        await release.wait()

    task = await manager.submit(chat_id, user_id, hold)
    context_token = None
    try:
        await entered.wait()
        parent_ws = parent_sockets["websocket"]
        if orch is not None:
            from orchestrator.orchestrator import Orchestrator

            orch._llm_context_user_id = types.MethodType(
                Orchestrator._llm_context_user_id, orch
            )
        context_token = _CONNECTION_OPERATION_CONTEXT.set({
            "owner": task._owner,
            "operation": task._operation,
            "execution_fence": task._execution_fence,
        })
        yield parent_ws
    finally:
        if context_token is not None:
            _CONNECTION_OPERATION_CONTEXT.reset(context_token)
        release.set()
        if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            await manager.cancel(task.task_id)
        if task.asyncio_task is not None:
            await asyncio.gather(task.asyncio_task, return_exceptions=True)
