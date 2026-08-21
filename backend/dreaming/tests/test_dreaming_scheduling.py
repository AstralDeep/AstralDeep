"""030 — per-user recurring dreaming job registration (US4 / T028-T029)."""
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _scheduled_job_store(runtime):
    from scheduler.store import ScheduledJobStore

    return ScheduledJobStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


@pytest.fixture
def db():
    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("dreaming_scheduling") as runtime:
        yield runtime, f"pytest-dreaming-{uuid.uuid4().hex[:8]}"


def test_ensure_creates_then_is_idempotent(db):
    from dreaming.scheduling import DREAMING_AGENT_ID, ensure_dreaming_job
    runtime, user = db
    source = type("PlaneSource", (), {
        "plane_runtime": runtime,
        "plane_repositories": runtime.repositories,
    })()
    store = _scheduled_job_store(runtime)

    job = ensure_dreaming_job(source, user)
    assert job["agent_id"] == DREAMING_AGENT_ID
    assert job["schedule_kind"] == "cron"
    # idempotent: a second call returns the same active job (no duplicate)
    again = ensure_dreaming_job(source, user)
    assert again["id"] == job["id"]
    actives = [j for j in store.list_jobs(user)
               if j["agent_id"] == DREAMING_AGENT_ID and j["status"] == "active"]
    assert len(actives) == 1


def test_remove_then_resume(db):
    from dreaming.scheduling import (DREAMING_AGENT_ID, ensure_dreaming_job,
                                     remove_dreaming_job)
    runtime, user = db
    source = type("PlaneSource", (), {
        "plane_runtime": runtime,
        "plane_repositories": runtime.repositories,
    })()
    store = _scheduled_job_store(runtime)

    created = ensure_dreaming_job(source, user)
    assert remove_dreaming_job(source, user) == 1
    actives = [j for j in store.list_jobs(user)
               if j["agent_id"] == DREAMING_AGENT_ID and j["status"] == "active"]
    assert actives == []
    # re-enable reactivates the SAME job rather than creating a duplicate
    resumed = ensure_dreaming_job(source, user)
    assert resumed["id"] == created["id"]
    assert resumed["status"] == "active"


def test_set_offline_grant(db):
    import time

    runtime, user = db
    # A real grant must exist — scheduled_job.offline_grant_id is FK-constrained.
    # Insert a minimal user_offline_grant row directly (avoids crypto/env setup).
    grant_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    with runtime.transaction() as transaction:
        transaction.execute(
            """INSERT INTO user_offline_grant
                   (id, user_id, agent_id, refresh_token_enc, issued_at, expires_at,
                    revoked_at, created_at, updated_at)
               VALUES (%s, %s, NULL, %s, %s, %s, NULL, %s, %s)""",
            (grant_id, user, b"x", now, now + 10_000_000, now, now),
        )

    store = _scheduled_job_store(runtime)
    job = store.create_job(
        user, name="t", instruction="i", schedule_kind="interval", schedule_expr="1d",
        timezone="UTC", consented_scopes=[], agent_id=None, target_chat_id=None,
        next_run_at=None, offline_grant_id=None)
    assert job["offline_grant_id"] is None
    assert store.set_offline_grant(user, job["id"], grant_id) is True
    assert str(store.get_job(user, job["id"])["offline_grant_id"]) == grant_id
