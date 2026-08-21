"""Admin-side API contract tests for the onboarding subsystem (feature 005)."""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audit.recorder import Recorder, set_recorder
from audit.repository import AuditRepository
from onboarding.api import onboarding_admin_router
from onboarding.repository import OnboardingRepository
from orchestrator.auth import (
    get_current_user_payload,
    verify_admin,
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


def _build_app(database, *, is_admin: bool, user_id: str = "pytest-admin"):
    app = FastAPI()

    class _Orch:
        pass

    orch = _Orch()
    orch.onboarding_repo = _onboarding_repository(database)
    app.state.orchestrator = orch

    payload = {
        "sub": user_id,
        "preferred_username": user_id,
        "realm_access": {"roles": ["admin"] if is_admin else ["user"]},
    }

    if is_admin:
        async def _verify_admin_ok():
            payload["is_admin"] = True
            return payload
        app.dependency_overrides[verify_admin] = _verify_admin_ok
    else:
        # Real verify_admin will see no admin role and raise 403.
        async def _no_admin():
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Not authorized (Requires 'admin' role)")
        app.dependency_overrides[verify_admin] = _no_admin

    async def _payload():
        return payload

    app.dependency_overrides[get_current_user_payload] = _payload
    app.include_router(onboarding_admin_router)
    return app


@pytest.fixture
def wire_audit(database):
    rec = Recorder(_audit_repository(database))
    set_recorder(rec)
    yield rec
    set_recorder(None)


def _slug(name: str) -> str:
    return f"pytest-{name}-{uuid.uuid4().hex[:8]}"


def test_non_admin_blocked(database, wire_audit):
    app = _build_app(database, is_admin=False)
    client = TestClient(app)
    r = client.get("/api/admin/tutorial/steps")
    assert r.status_code == 403
    r = client.post("/api/admin/tutorial/steps", json={
        "slug": _slug("blocked"), "audience": "user", "display_order": 1,
        "target_kind": "none", "target_key": None, "title": "x", "body": "y",
    })
    assert r.status_code == 403


def test_create_step_201(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("create")
    r = client.post("/api/admin/tutorial/steps", json={
        "slug": slug, "audience": "user", "display_order": 100,
        "target_kind": "none", "target_key": None,
        "title": "Hello", "body": "World",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == slug
    assert body["title"] == "Hello"
    # Audit row should now exist
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT count(*) AS count FROM audit_events "
            "WHERE event_class = 'tutorial_step_edited' "
            "AND inputs_meta->>'step_slug' = %s",
            (slug,),
        )
    assert row["count"] == 1


def test_duplicate_slug_409(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("dup")
    payload = {
        "slug": slug, "audience": "user", "display_order": 110,
        "target_kind": "none", "target_key": None,
        "title": "T", "body": "B",
    }
    r1 = client.post("/api/admin/tutorial/steps", json=payload)
    assert r1.status_code == 201
    r2 = client.post("/api/admin/tutorial/steps", json=payload)
    assert r2.status_code == 409


def test_target_consistency_validation(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    # target_kind='none' with non-null target_key
    r = client.post("/api/admin/tutorial/steps", json={
        "slug": _slug("bad-target"), "audience": "user", "display_order": 120,
        "target_kind": "none", "target_key": "should-be-null",
        "title": "T", "body": "B",
    })
    assert r.status_code == 422  # pydantic model_validator catches it
    # target_kind='static' with empty target_key
    r2 = client.post("/api/admin/tutorial/steps", json={
        "slug": _slug("bad-target-2"), "audience": "user", "display_order": 121,
        "target_kind": "static", "target_key": "",
        "title": "T", "body": "B",
    })
    assert r2.status_code == 422


def test_update_step_changed_fields_minimal(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("upd")
    r = client.post("/api/admin/tutorial/steps", json={
        "slug": slug, "audience": "user", "display_order": 130,
        "target_kind": "none", "target_key": None,
        "title": "Original", "body": "Body",
    })
    step_id = r.json()["id"]
    # Update only title; body sent matching existing value -> not in changed_fields
    r2 = client.put(f"/api/admin/tutorial/steps/{step_id}", json={
        "title": "Renamed", "body": "Body",
    })
    assert r2.status_code == 200
    assert r2.json()["title"] == "Renamed"
    # Audit row's changed_fields should contain title but NOT body
    with database.transaction() as transaction:
        row = transaction.fetch_one(
            "SELECT inputs_meta FROM audit_events "
            "WHERE event_class = 'tutorial_step_edited' "
            "AND inputs_meta->>'change_kind' = 'update' "
            "AND inputs_meta->>'step_id' = %s "
            "ORDER BY recorded_at DESC LIMIT 1",
            (str(step_id),),
        )
    assert row is not None
    changed = row["inputs_meta"]["changed_fields"]
    assert "title" in changed
    assert "body" not in changed


def test_archive_then_restore_round_trip(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("ar")
    step_id = client.post("/api/admin/tutorial/steps", json={
        "slug": slug, "audience": "user", "display_order": 140,
        "target_kind": "none", "target_key": None,
        "title": "T", "body": "B",
    }).json()["id"]
    r = client.post(f"/api/admin/tutorial/steps/{step_id}/archive")
    assert r.status_code == 200
    assert r.json()["archived_at"] is not None
    r2 = client.post(f"/api/admin/tutorial/steps/{step_id}/restore")
    assert r2.status_code == 200
    assert r2.json()["archived_at"] is None


def test_update_step_404_for_unknown_id(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    r = client.put("/api/admin/tutorial/steps/999999999", json={"title": "x"})
    assert r.status_code == 404


def test_revisions_endpoint(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("rev")
    step_id = client.post("/api/admin/tutorial/steps", json={
        "slug": slug, "audience": "user", "display_order": 150,
        "target_kind": "none", "target_key": None,
        "title": "T", "body": "B",
    }).json()["id"]
    client.put(f"/api/admin/tutorial/steps/{step_id}", json={"title": "T2"})
    r = client.get(f"/api/admin/tutorial/steps/{step_id}/revisions")
    assert r.status_code == 200
    revs = r.json()["revisions"]
    # At least the create + update revisions
    assert len(revs) >= 2
    # Newest first
    assert revs[0]["change_kind"] in ("update", "create")


def test_list_admin_includes_archived(database, wire_audit):
    app = _build_app(database, is_admin=True)
    client = TestClient(app)
    slug = _slug("la")
    step_id = client.post("/api/admin/tutorial/steps", json={
        "slug": slug, "audience": "user", "display_order": 160,
        "target_kind": "none", "target_key": None,
        "title": "T", "body": "B",
    }).json()["id"]
    client.post(f"/api/admin/tutorial/steps/{step_id}/archive")
    # Default include_archived=true
    r = client.get("/api/admin/tutorial/steps")
    assert r.status_code == 200
    slugs = [s["slug"] for s in r.json()["steps"]]
    assert slug in slugs
    # Explicit false excludes
    r2 = client.get("/api/admin/tutorial/steps?include_archived=false")
    slugs2 = [s["slug"] for s in r2.json()["steps"]]
    assert slug not in slugs2
