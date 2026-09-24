"""Registers or pauses each user's recurring dreaming job (agent_id '__dreaming__') via
scheduler/store.py and scheduler/cron.py, routing it to the local consolidation
sweep. Wired into dreaming/api.py's enable/disable/status endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from agentic_settings import DREAMING_DEFAULT_CRON
from scheduler.cron import compute_next_run_ms
from scheduler.store import ScheduledJobStore

logger = logging.getLogger("Dreaming.Scheduling")

DREAMING_AGENT_ID = "__dreaming__"
DREAMING_JOB_NAME = "Memory consolidation"
DREAMING_INSTRUCTION = "(internal) Consolidate recurring short-term memory signals into durable memory."


def _store(plane_source) -> ScheduledJobStore:
    return ScheduledJobStore(
        plane_runtime=plane_source.plane_runtime,
        plane_repositories=plane_source.plane_repositories,
    )


def ensure_dreaming_job(plane_source, user_id: str) -> Optional[dict]:
    store = _store(plane_source)
    paused = None
    for job in store.list_jobs(user_id):
        if job.get("agent_id") != DREAMING_AGENT_ID:
            continue
        if job.get("status") == "active":
            return job
        if job.get("status") == "paused" and paused is None:
            paused = job

    if paused is not None:
        store.set_status(user_id, paused["id"], "active")
        logger.info("dreaming.job_resumed", extra={"user_id": user_id, "job_id": paused["id"]})
        return store.get_job(user_id, paused["id"])

    next_run = compute_next_run_ms("cron", DREAMING_DEFAULT_CRON, "UTC", int(time.time() * 1000))
    job = store.create_job(
        user_id, name=DREAMING_JOB_NAME, instruction=DREAMING_INSTRUCTION,
        schedule_kind="cron", schedule_expr=DREAMING_DEFAULT_CRON, timezone="UTC",
        consented_scopes=[], agent_id=DREAMING_AGENT_ID, target_chat_id=None,
        next_run_at=next_run, offline_grant_id=None,
    )
    logger.info("dreaming.job_registered",
                extra={"user_id": user_id, "job_id": job["id"], "cron": DREAMING_DEFAULT_CRON})
    return job


def remove_dreaming_job(plane_source, user_id: str) -> int:
    store = _store(plane_source)
    paused = 0
    for job in store.list_jobs(user_id):
        if job.get("agent_id") == DREAMING_AGENT_ID and job.get("status") == "active":
            if store.set_status(user_id, job["id"], "paused"):
                paused += 1
    if paused:
        logger.info("dreaming.job_paused", extra={"user_id": user_id, "count": paused})
    return paused
