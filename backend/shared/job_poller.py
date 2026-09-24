"""Generic background poller for upstream async jobs (CLASSify/Forecaster training):
polls a single job, emits ToolProgress via shared/agent_runtime.py's AgentRuntime,
and stops at a terminal state or repeated transport failures.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from shared.protocol import ToolProgress

logger = logging.getLogger("JobPoller")

TERMINAL_STATUSES = ("succeeded", "failed")
PHASE_FOR_STATUS = {
    "started": "started",
    "in_progress": "training",
    "training": "training",
    "forecasting": "forecasting",
    "evaluating": "evaluating",
    "succeeded": "completed",
    "failed": "failed",
}


@dataclass
class JobPoller:
    ws: Any
    request_id: str
    agent_id: str
    tool_name: str
    cap_job_id: Optional[str]
    poll_fn: Callable[[], Dict[str, Any]]
    poll_interval: float = 5.0
    failure_threshold: int = 5

    async def run(self) -> None:
        failure_streak = 0
        last_percentage: Optional[int] = None
        last_status: str = "started"
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                logger.info("JobPoller cancelled (req=%s)", self.request_id)
                await self._emit_terminal("status_unknown", "Polling cancelled.", None, None)
                return

            try:
                status_dict = await asyncio.to_thread(self.poll_fn)
            except Exception as e:
                failure_streak += 1
                logger.warning(
                    "JobPoller transport error (req=%s, streak=%d): %s",
                    self.request_id, failure_streak, e,
                )
                if failure_streak >= self.failure_threshold:
                    await self._emit_terminal(
                        "status_unknown",
                        "Couldn't reach the service to confirm job status — try again later.",
                        None, None,
                    )
                    return
                continue

            failure_streak = 0
            if not isinstance(status_dict, dict):
                logger.warning("JobPoller poll_fn returned non-dict; treating as in_progress")
                status_dict = {"status": last_status, "message": ""}

            status = status_dict.get("status") or last_status
            last_status = status
            percentage = status_dict.get("percentage")
            if percentage is not None:
                last_percentage = percentage
            else:
                percentage = last_percentage
            message = status_dict.get("message") or ""
            result = status_dict.get("result")

            if status in TERMINAL_STATUSES:
                phase = PHASE_FOR_STATUS.get(status, status)
                await self._emit_terminal(phase, message, percentage, result)
                return

            phase = PHASE_FOR_STATUS.get(status, "in_progress")
            try:
                await self._emit(phase, message, percentage)
            except Exception as e:
                logger.warning("JobPoller emit failed (req=%s): %s", self.request_id, e)
                failure_streak += 1
                if failure_streak >= self.failure_threshold:
                    return

    async def _emit(self, phase: str, message: str, percentage: Optional[int]) -> None:
        metadata: Dict[str, Any] = {"request_id": self.request_id, "phase": phase}
        if self.cap_job_id:
            metadata["cap_job_id"] = self.cap_job_id
        msg = ToolProgress(
            tool_name=self.tool_name,
            agent_id=self.agent_id,
            message=message,
            percentage=percentage,
            metadata=metadata,
        )
        await self.ws.send_text(msg.to_json())

    async def _emit_terminal(
        self,
        phase: str,
        message: str,
        percentage: Optional[int],
        result: Optional[Dict[str, Any]],
    ) -> None:
        metadata: Dict[str, Any] = {
            "request_id": self.request_id,
            "phase": phase,
            "terminal": True,
        }
        if self.cap_job_id:
            metadata["cap_job_id"] = self.cap_job_id
        if result is not None:
            metadata["result"] = result
        msg = ToolProgress(
            tool_name=self.tool_name,
            agent_id=self.agent_id,
            message=message,
            percentage=percentage if percentage is not None else (100 if phase == "completed" else None),
            metadata=metadata,
        )
        try:
            await self.ws.send_text(msg.to_json())
        except Exception as e:
            logger.warning("JobPoller terminal emit failed (req=%s): %s", self.request_id, e)
