"""Closes outstanding assignments after their Python turn engine was retired.
Plane recovers expired reservations and keeps uncertain actions available for reconciliation.
Work subscribers observe terminal outcomes in the same transaction as the assignment update.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from orchestrator.work_admission import ExecutionFence, OperationState, StaleExecutionFenceError
from orchestrator.work_subscriptions import fire_completions, redrive_pending

from persistent_agents.config import RunnerConfig

logger = logging.getLogger(__name__)


class AssignmentRunner:
    def __init__(self, orchestrator, service, *, config=None):
        self.orch = orchestrator
        self.service = service
        self.store = service.store
        self.config = config or RunnerConfig.from_environment()
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop = None

    def start(self):
        if self._loop is not None:
            raise RuntimeError("persistent assignment runner already started")
        self._loop = asyncio.create_task(self.run(), name="persistent-assignments")

    def notify(self, assignment_id=None):
        self._wake.set()

    async def stop(self):
        self._stopping = True
        self._wake.set()
        if self._loop is not None:
            await self._loop

    async def run(self):
        while not self._stopping:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                logger.error("persistent_assignment_tick_failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.config.tick_seconds)
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self):
        await asyncio.to_thread(self.orch.work_admission.expire_execution_leases)
        for profile in ("persistent", "one_shot"):
            await self.store.transaction(
                lambda tx, repo, profile=profile: self._recover(tx, repo, profile),
                bound_session_waits=True,
            )

        def retire(tx, repo):
            for record in repo.retire_unleased_assignments(tx, reason="turn_engine_retired", limit=100):
                if record.execution_profile == "one_shot" and record.lifecycle == "completed":
                    self._fire(tx, repo, record)

        await self.store.transaction(retire, bound_session_waits=True)
        await redrive_pending(self.service)

    def _recover(self, tx, repo, profile):
        recover = (repo.recover_expired_for_administration if profile == "persistent"
                   else repo.recover_expired_operations_for_administration)
        recovered = recover(tx, limit=100)
        for binding in recovered.operation_bindings:
            fence = ExecutionFence(
                uuid.UUID(binding["operation_id"]),
                binding["execution_generation"],
                uuid.UUID(binding["execution_lease_token"]),
            )
            try:
                self.orch.work_admission.terminalize(
                    fence, state=OperationState.FAILED, terminal_code="assignment_interrupted",
                    safe_summary=None, retry_after_ms=None, transaction=tx,
                )
            except StaleExecutionFenceError:
                pass
        for record in recovered.terminal_assignments:
            self._fire(tx, repo, record)

    def _fire(self, tx, repo, record):
        fire_completions(self.service, tx, repo, owner_id=record.owner_id,
                         source_id=record.assignment_id, source_revision=record.state_version,
                         outcome=record.operation["terminal_outcome"])
