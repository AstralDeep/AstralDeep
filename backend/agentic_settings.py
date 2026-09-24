"""Environment-driven defaults for offline grants, the scheduler loop, and dreaming
consolidation, read by dreaming/scheduling.py, orchestrator/offline_grant.py,
scheduler/loop.py, and scheduler/api.py.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Absent key fails safe; never stores tokens unencrypted
OFFLINE_GRANT_ENC_KEY: str | None = os.getenv("OFFLINE_GRANT_ENC_KEY")

OFFLINE_GRANT_MAX_DAYS: int = _int("OFFLINE_GRANT_MAX_DAYS", 365)

SCHEDULER_TICK_SECONDS: int = _int("SCHEDULER_TICK_SECONDS", 30)
SCHEDULE_MAX_ACTIVE_JOBS_PER_USER: int = _int("SCHEDULE_MAX_ACTIVE_JOBS_PER_USER", 25)
SCHEDULE_MIN_INTERVAL_SECONDS: int = _int("SCHEDULE_MIN_INTERVAL_SECONDS", 60)

DREAMING_DEFAULT_CRON: str = os.getenv("DREAMING_DEFAULT_CRON", "0 3 * * *")

MEMORY_PROMOTION_MIN_RECALLS: int = _int("MEMORY_PROMOTION_MIN_RECALLS", 2)
