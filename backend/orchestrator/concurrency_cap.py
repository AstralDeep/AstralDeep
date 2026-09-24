"""In-memory per-(user, agent) concurrency limiter for long-running tool jobs;
orchestrator.py calls acquire()/release() to reject admissions beyond the configured
cap instead of queueing them silently.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Set, Tuple

logger = logging.getLogger("ConcurrencyCap")


class ConcurrencyCap:
    def __init__(self, max_per_user_agent: int = 3) -> None:
        self.max_per_user_agent = max_per_user_agent
        self._inflight: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str, agent_id: str, job_id: str) -> bool:
        async with self._lock:
            slot = self._inflight[(user_id, agent_id)]
            if job_id in slot:
                return True
            if len(slot) >= self.max_per_user_agent:
                return False
            slot.add(job_id)
            logger.debug(
                "ConcurrencyCap acquired: user=%s agent=%s job=%s (count=%d)",
                user_id, agent_id, job_id, len(slot),
            )
            return True

    async def release(self, user_id: str, agent_id: str, job_id: str) -> None:
        async with self._lock:
            slot = self._inflight.get((user_id, agent_id))
            if not slot or job_id not in slot:
                return
            slot.discard(job_id)
            logger.debug(
                "ConcurrencyCap released: user=%s agent=%s job=%s (count=%d)",
                user_id, agent_id, job_id, len(slot),
            )
            if not slot:
                self._inflight.pop((user_id, agent_id), None)

    def inflight_count(self, user_id: str, agent_id: str) -> int:
        return len(self._inflight.get((user_id, agent_id), ()))

    def inflight_jobs(self, user_id: str, agent_id: str) -> List[str]:
        return sorted(self._inflight.get((user_id, agent_id), ()))
