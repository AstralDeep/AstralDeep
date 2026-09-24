"""Tests for the authenticated schedule-action API (backend/scheduler/api.py): auth
enforcement, owner isolation, feature-flag refusal, and the atomic cancellation seam
behind pause/delete/resume.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.auth import get_current_user_payload, require_user_id
from scheduler import api as schedule_api
from shared.feature_flags import flags


@dataclass(frozen=True)
class _RunNowResult:
    occurrence_id: uuid.UUID
    job_id: uuid.UUID
    owner_user_id: str
    scheduled_for: datetime
    state: str
    created: bool


class _FakeScheduleStore:
    def __init__(self) -> None:
        self.jobs: dict[tuple[str, str], dict[str, Any]] = {}
        self.run_now_calls: list[dict[str, Any]] = []
        self.atomic_status_calls: list[dict[str, Any]] = []
        self.legacy_status_calls: list[tuple[str, str, str]] = []
        self._run_now: dict[tuple[str, uuid.UUID], _RunNowResult] = {}

    def add_job(self, user_id: str, job_id: uuid.UUID, *, status: str = "active") -> None:
        self.jobs[(user_id, str(job_id))] = {
            "id": str(job_id),
            "user_id": user_id,
            "status": status,
            "agent_id": None,
            "consented_scopes": [],
        }

    def get_job(self, user_id: str, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get((user_id, str(job_id)))
        return dict(job) if job is not None else None

    def materialize_run_now(
        self,
        *,
        user_id: str,
        job_id: str,
        submission_id: uuid.UUID,
        eligibility,
    ) -> _RunNowResult:
        self.run_now_calls.append(
            {
                "user_id": user_id,
                "job_id": str(job_id),
                "submission_id": submission_id,
                "eligibility": eligibility,
            }
        )
        key = (user_id, submission_id)
        existing = self._run_now.get(key)
        if existing is not None:
            if str(existing.job_id) != str(job_id):
                raise RuntimeError("idempotency_conflict")
            return _RunNowResult(**{**existing.__dict__, "created": False})
        result = _RunNowResult(
            occurrence_id=uuid.uuid4(),
            job_id=uuid.UUID(str(job_id)),
            owner_user_id=user_id,
            scheduled_for=datetime(2026, 7, 16, 15, 30, 0, 123456, tzinfo=UTC),
            state="pending",
            created=True,
        )
        self._run_now[key] = result
        return result

    def set_status_and_cancel_unstarted(
        self,
        *,
        user_id: str,
        job_id: str,
        status: str,
        terminal_code: str,
    ) -> bool:
        self.atomic_status_calls.append(
            {
                "user_id": user_id,
                "job_id": str(job_id),
                "status": status,
                "terminal_code": terminal_code,
            }
        )
        job = self.jobs.get((user_id, str(job_id)))
        if job is None:
            return False
        job["status"] = status
        return True

    def set_status(self, user_id: str, job_id: str, status: str) -> bool:
        self.legacy_status_calls.append((user_id, str(job_id), status))
        job = self.jobs.get((user_id, str(job_id)))
        if job is None:
            return False
        job["status"] = status
        return True


def _client(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeScheduleStore,
    *,
    user_id: str | None,
    execution_enabled: bool,
) -> TestClient:
    app = FastAPI()
    runner = SimpleNamespace(assess_job=lambda _job: SimpleNamespace(eligible=True))
    app.state.orchestrator = SimpleNamespace(
        history=SimpleNamespace(db=object()),
        work_admission=object(),
        _scheduler_loop=(SimpleNamespace(runner=runner) if execution_enabled else None),
        _save_user_profile=lambda _payload: None,
    )
    app.include_router(schedule_api.schedule_router)
    if user_id is not None:
        claims = {"sub": user_id, "realm_access": {"roles": ["user"]}}
        app.dependency_overrides[require_user_id] = lambda: user_id
        app.dependency_overrides[get_current_user_payload] = lambda: claims
    monkeypatch.setattr(schedule_api, "_store", lambda _request: store)
    monkeypatch.setitem(flags._flags, "scheduler_execution", execution_enabled)

    async def record_generic(**_kwargs):
        return None

    monkeypatch.setattr(schedule_api, "record_generic", record_generic)
    return TestClient(app)


def _safe_error_code(response) -> str:
    body = response.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code") or detail.get("error") or "")
    return str(body.get("code") or body.get("error") or detail or "")


def test_run_now_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id=None,
        execution_enabled=True,
    )

    response = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": str(uuid.uuid4())},
    )

    assert response.status_code == 401
    assert store.run_now_calls == []


def test_run_now_returns_safe_no_store_projection_and_reconciles_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    submission_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id="owner-a",
        execution_enabled=True,
    )

    first = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": str(submission_id)},
    )
    replay = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": str(submission_id)},
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.headers["cache-control"] == "no-store"
    assert replay.headers["cache-control"] == "no-store"
    expected_fields = {
        "submission_id",
        "occurrence_id",
        "job_id",
        "scheduled_for",
        "state",
        "duplicate",
    }
    assert set(first.json()) == expected_fields
    assert set(replay.json()) == expected_fields
    assert first.json()["submission_id"] == str(submission_id)
    assert first.json()["job_id"] == str(job_id)
    assert first.json()["state"] == "pending"
    assert first.json()["scheduled_for"].endswith("Z")
    assert first.json()["duplicate"] is False
    assert replay.json()["duplicate"] is True
    assert replay.json()["occurrence_id"] == first.json()["occurrence_id"]
    assert [call["submission_id"] for call in store.run_now_calls] == [
        submission_id,
        submission_id,
    ]


def test_run_now_owner_isolation_uses_same_not_found_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id="owner-b",
        execution_enabled=True,
    )

    response = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": str(uuid.uuid4())},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}
    assert store.run_now_calls == []
    assert "owner-a" not in response.text
    assert "owner-b" not in response.text


def test_run_now_flag_off_refuses_without_any_store_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id="owner-a",
        execution_enabled=False,
    )

    response = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": str(uuid.uuid4())},
    )

    assert response.status_code == 409
    assert _safe_error_code(response) == "scheduler_execution_disabled"
    assert store.run_now_calls == []


def test_run_now_rejects_missing_or_invalid_submission_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id="owner-a",
        execution_enabled=True,
    )

    missing = client.post(f"/api/schedule/{job_id}/run-now", json={})
    invalid = client.post(
        f"/api/schedule/{job_id}/run-now",
        json={"submission_id": "not-a-uuid"},
    )

    assert missing.status_code == 422
    assert invalid.status_code == 422
    assert store.run_now_calls == []


@pytest.mark.parametrize(
    ("method", "suffix", "expected_status", "status", "terminal_code"),
    (
        ("post", "/pause", 200, "paused", "cancelled_job_paused"),
        ("delete", "", 204, "disabled", "cancelled_job_deleted"),
    ),
)
def test_pause_and_delete_use_atomic_unstarted_cancellation_seam(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    suffix: str,
    expected_status: int,
    status: str,
    terminal_code: str,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id)
    client = _client(
        monkeypatch,
        store,
        user_id="owner-a",
        execution_enabled=False,
    )

    response = getattr(client, method)(f"/api/schedule/{job_id}{suffix}")

    assert response.status_code == expected_status
    assert store.atomic_status_calls == [
        {
            "user_id": "owner-a",
            "job_id": str(job_id),
            "status": status,
            "terminal_code": terminal_code,
        }
    ]
    assert store.legacy_status_calls == []


def test_resume_preserves_definition_management_without_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeScheduleStore()
    job_id = uuid.uuid4()
    store.add_job("owner-a", job_id, status="paused")
    client = _client(
        monkeypatch,
        store,
        user_id="owner-a",
        execution_enabled=False,
    )

    response = client.post(f"/api/schedule/{job_id}/resume")

    assert response.status_code == 200
    assert response.json() == {"job_id": str(job_id), "status": "active"}
    assert store.atomic_status_calls == []
    assert store.legacy_status_calls == [("owner-a", str(job_id), "active")]
