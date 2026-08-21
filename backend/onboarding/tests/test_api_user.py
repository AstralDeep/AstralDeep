"""User-side API contract tests for the onboarding subsystem (feature 005)."""
from __future__ import annotations


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.recorder import Recorder, set_recorder
from audit.repository import AuditRepository
from onboarding.api import onboarding_user_router
from onboarding.repository import OnboardingRepository
from orchestrator.auth import (
    get_current_user_payload,
    require_user_id,
)


def _onboarding_repository(database):
    return OnboardingRepository(
        None,
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


def _audit_repository(database):
    return AuditRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


def _build_app(database, *, user_id: str, roles=("user",)):
    """Build a FastAPI app wired to the user router with overridden auth."""
    app = FastAPI()

    class _Orch:
        pass

    orch = _Orch()
    orch.onboarding_repo = _onboarding_repository(database)
    app.state.orchestrator = orch

    payload = {
        "sub": user_id,
        "preferred_username": user_id,
        "realm_access": {"roles": list(roles)},
    }

    async def _fake_user_id():
        return user_id

    async def _fake_payload():
        return payload

    app.dependency_overrides[require_user_id] = _fake_user_id
    app.dependency_overrides[get_current_user_payload] = _fake_payload
    app.include_router(onboarding_user_router)
    return app, orch


@pytest.fixture
def wire_audit(database):
    """Wire the audit recorder so endpoint calls don't silently drop events."""
    rec = Recorder(_audit_repository(database))
    set_recorder(rec)
    yield rec
    set_recorder(None)


# ---------------------------------------------------------------------------
# GET /api/onboarding/state
# ---------------------------------------------------------------------------

def test_get_state_default_not_started(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.get("/api/onboarding/state")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "not_started"
    assert body["last_step_id"] is None
    assert body["completed_at"] is None


def test_get_state_rejects_user_id_query_param(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.get("/api/onboarding/state?user_id=other")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/onboarding/state
# ---------------------------------------------------------------------------

def test_put_state_in_progress_records_started(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.put(
        "/api/onboarding/state",
        json={"status": "in_progress", "last_step_id": None},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    # Verify audit row
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT count(*) AS count FROM audit_events "
            "WHERE actor_user_id = %s "
            "AND event_class = 'onboarding_started'",
            (unique_user,),
        )
    assert row["count"] == 1


def test_put_state_completed_sets_completed_at(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"status": "in_progress"})
    r = client.put("/api/onboarding/state", json={"status": "completed"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None


def test_put_state_skipped_sets_skipped_at(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"status": "in_progress"})
    r = client.put("/api/onboarding/state", json={"status": "skipped"})
    body = r.json()
    assert body["status"] == "skipped"
    assert body["skipped_at"] is not None


def test_put_state_terminal_to_in_progress_returns_409(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"status": "in_progress"})
    client.put("/api/onboarding/state", json={"status": "completed"})
    r = client.put("/api/onboarding/state", json={"status": "in_progress"})
    assert r.status_code == 409


def test_put_state_rejects_user_id_param(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.put(
        "/api/onboarding/state?actor_user_id=evil",
        json={"status": "in_progress"},
    )
    assert r.status_code == 400


def test_put_state_rejects_not_started(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.put("/api/onboarding/state", json={"status": "not_started"})
    assert r.status_code == 422  # pydantic validation


def test_put_state_rejects_admin_step_for_non_admin(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user, roles=("user",))
    client = TestClient(app)
    repo = _onboarding_repository(database)
    admin_step = repo.create_step(
        editor_user_id=unique_user,
        slug=f"pytest-{unique_user}-admin",
        audience="admin",
        display_order=200,
        target_kind="none",
        target_key=None,
        title="t",
        body="b",
    )
    r = client.put(
        "/api/onboarding/state",
        json={"status": "in_progress", "last_step_id": admin_step.id},
    )
    assert r.status_code == 400


def test_put_state_rejects_unknown_step_id(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.put(
        "/api/onboarding/state",
        json={"status": "in_progress", "last_step_id": 99999999},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/onboarding/replay
# ---------------------------------------------------------------------------

def test_replay_records_event_without_mutating_state(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    client.put("/api/onboarding/state", json={"status": "in_progress"})
    client.put("/api/onboarding/state", json={"status": "completed"})
    pre = client.get("/api/onboarding/state").json()

    r = client.post("/api/onboarding/replay")
    assert r.status_code == 204

    post = client.get("/api/onboarding/state").json()
    # Replay does NOT mutate the persisted state
    assert post["status"] == pre["status"] == "completed"
    assert post["completed_at"] == pre["completed_at"]

    # Verify audit row was written with prior_status='completed'
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT inputs_meta FROM audit_events "
            "WHERE actor_user_id = %s "
            "AND event_class = 'onboarding_replayed' "
            "ORDER BY recorded_at DESC LIMIT 1",
            (unique_user,),
        )
    assert row is not None
    assert row["inputs_meta"]["prior_status"] == "completed"


def test_replay_works_for_user_with_no_row(database, unique_user, wire_audit):
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.post("/api/onboarding/replay")
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# GET /api/tutorial/steps
# ---------------------------------------------------------------------------

def test_steps_user_excludes_admin(database, unique_user, wire_audit):
    repo = _onboarding_repository(database)
    repo.create_step(
        editor_user_id=unique_user, slug=f"pytest-{unique_user}-u",
        audience="user", display_order=100, target_kind="none",
        target_key=None, title="t", body="b",
    )
    repo.create_step(
        editor_user_id=unique_user, slug=f"pytest-{unique_user}-a",
        audience="admin", display_order=200, target_kind="none",
        target_key=None, title="t", body="b",
    )
    app, _ = _build_app(database, user_id=unique_user, roles=("user",))
    client = TestClient(app)
    r = client.get("/api/tutorial/steps")
    assert r.status_code == 200
    slugs = [s["slug"] for s in r.json()["steps"]]
    assert f"pytest-{unique_user}-u" in slugs
    assert f"pytest-{unique_user}-a" not in slugs


def test_steps_admin_includes_both_audiences(database, unique_user, wire_audit):
    repo = _onboarding_repository(database)
    repo.create_step(
        editor_user_id=unique_user, slug=f"pytest-{unique_user}-u2",
        audience="user", display_order=300, target_kind="none",
        target_key=None, title="t", body="b",
    )
    repo.create_step(
        editor_user_id=unique_user, slug=f"pytest-{unique_user}-a2",
        audience="admin", display_order=400, target_kind="none",
        target_key=None, title="t", body="b",
    )
    app, _ = _build_app(database, user_id=unique_user, roles=("user", "admin"))
    client = TestClient(app)
    r = client.get("/api/tutorial/steps")
    slugs = [s["slug"] for s in r.json()["steps"]]
    assert f"pytest-{unique_user}-u2" in slugs
    assert f"pytest-{unique_user}-a2" in slugs


def test_steps_user_view_omits_admin_only_fields(database, unique_user, wire_audit):
    repo = _onboarding_repository(database)
    repo.create_step(
        editor_user_id=unique_user, slug=f"pytest-{unique_user}-vw",
        audience="user", display_order=500, target_kind="none",
        target_key=None, title="t", body="b",
    )
    app, _ = _build_app(database, user_id=unique_user)
    client = TestClient(app)
    r = client.get("/api/tutorial/steps")
    items = r.json()["steps"]
    for item in items:
        # archived_at and updated_at must be hidden from the user view
        assert "archived_at" not in item
        assert "updated_at" not in item
