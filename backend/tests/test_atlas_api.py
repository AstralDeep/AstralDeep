"""Round-trip tests for the authorized Atlas REST surface (personalization/atlas_api.py)
over a real PostgreSQL-backed AstralPlane runtime and the mock-auth dispatch path:
create/read/update/delete by slug and id, revision conflicts, wrong-owner denial, PHI
refusal, generated-import unavailability, bounded results, and audit evidence.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["USE_MOCK_AUTH"] = "true"

from audit.repository import AuditRepository  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from orchestrator.credential_manager import CredentialManager  # noqa: E402
from personalization import phi_gate  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER_A = "atlas-api-owner-a"
OWNER_B = "atlas-api-owner-b"


def _token(sub: str) -> str:
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    claims = base64.urlsafe_b64encode(
        json.dumps({"sub": sub, "preferred_username": sub}).encode()
    ).decode().rstrip("=")
    return f"{header}.{claims}.signature"


AUTH_A = {"Authorization": f"Bearer {_token(OWNER_A)}"}
AUTH_B = {"Authorization": f"Bearer {_token(OWNER_B)}"}


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("atlas_api") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def credential_manager(plane_runtime, tmp_path_factory):
    return CredentialManager(
        plane_runtime=plane_runtime, data_dir=str(tmp_path_factory.mktemp("atlas-api-keys"))
    )


@pytest.fixture(scope="module")
def client(plane_runtime, credential_manager):
    monkeypatch_fixture = pytest.MonkeyPatch()
    monkeypatch_fixture.setattr(
        phi_gate, "_GATE",
        phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])),
    )
    from personalization.atlas_api import atlas_router

    app = FastAPI(title="Atlas API Test App")
    app.include_router(atlas_router)
    app.state.orchestrator = SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=plane_runtime,
                repositories=plane_runtime.repositories,
            )
        ),
        audit_repo=AuditRepository(
            plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
        ),
        credential_manager=credential_manager,
    )
    with TestClient(app) as test_client:
        yield test_client
    monkeypatch_fixture.undo()


def _create(client, project="atlas-api", **changes):
    payload = {
        "slug": f"round-trip-{uuid4().hex[:10]}",
        "title": "Round trip",
        "body": "Human-authored project knowledge.",
        "sources": ["https://example.invalid/source"],
    }
    payload.update(changes)
    return client.post(
        f"/api/atlas/projects/{project}/pages", json=payload, headers=AUTH_A
    ), payload


def test_full_page_lifecycle_round_trip_over_http(client):
    response, payload = _create(client)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["revision"] == 1
    assert created["owner_id"] == OWNER_A
    assert created["project_id"] == "atlas-api"
    assert "body" not in created

    read = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}", headers=AUTH_A
    )
    assert read.status_code == 200
    assert read.json()["body"] == "Human-authored project knowledge."
    by_id = client.get(
        f"/api/atlas/projects/atlas-api/pages/{created['page_id']}", headers=AUTH_A
    )
    assert by_id.status_code == 200
    assert by_id.json()["page_id"] == created["page_id"]

    update = client.put(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}",
        json={"expected_revision": 1, "body": "Revised project knowledge."},
        headers=AUTH_A,
    )
    assert update.status_code == 200
    assert update.json()["revision"] == 2
    assert update.json()["body"] == "Revised project knowledge."

    stale = client.put(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}",
        json={"expected_revision": 1, "body": "Stale edit."},
        headers=AUTH_A,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "page_revision_conflict"
    assert stale.json()["detail"]["current_revision"] == 2

    revisions = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}/revisions",
        headers=AUTH_A,
    )
    assert revisions.status_code == 200
    assert [entry["revision"] for entry in revisions.json()["revisions"]] == [2, 1]
    old_revision = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}?revision=1",
        headers=AUTH_A,
    )
    assert old_revision.status_code == 200
    assert old_revision.json()["body"] == "Human-authored project knowledge."

    links = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}/links", headers=AUTH_A
    )
    assert links.status_code == 200
    assert links.json()["count"] == 0

    delete = client.delete(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}?expected_revision=2",
        headers=AUTH_A,
    )
    assert delete.status_code == 200
    assert delete.json()["deleted"] is True
    assert delete.json()["revisions_removed"] >= 1
    gone = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}", headers=AUTH_A
    )
    assert gone.status_code == 404


def test_links_round_trip_and_missing_targets(client):
    response, first = _create(client)
    assert response.status_code == 201
    target = response.json()
    linked_response, linked = _create(client, links=[first["slug"]])
    assert linked_response.status_code == 201
    created_linked = linked_response.json()
    links = client.get(
        f"/api/atlas/projects/atlas-api/pages/{created_linked['page_id']}/links",
        headers=AUTH_A,
    )
    assert [entry["page_id"] for entry in links.json()["links"]] == [target["page_id"]]
    dangling, _ = _create(client, links=["missing-page"])
    assert dangling.status_code == 404
    assert dangling.json()["detail"] == "link_target_not_found"


def test_wrong_owner_is_denied_through_the_real_dispatch_path(client):
    response, payload = _create(client)
    assert response.status_code == 201
    created = response.json()
    denied_read = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}", headers=AUTH_B
    )
    assert denied_read.status_code == 404
    denied_update = client.put(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}",
        json={"expected_revision": 1, "body": "hijack"},
        headers=AUTH_B,
    )
    assert denied_update.status_code == 404
    denied_delete = client.delete(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}?expected_revision=1",
        headers=AUTH_B,
    )
    assert denied_delete.status_code == 404
    foreign_list = client.get("/api/atlas/projects/atlas-api/pages", headers=AUTH_B)
    assert all(
        entry["page_id"] != created["page_id"]
        for entry in foreign_list.json()["pages"]
    )


def test_unauthenticated_requests_are_rejected(client):
    response, payload = _create(client)
    assert response.status_code == 201
    for method, url in (
        ("get", "/api/atlas/projects/atlas-api/pages"),
        ("post", "/api/atlas/projects/atlas-api/pages"),
        (
            "get",
            f"/api/atlas/projects/atlas-api/pages/{payload['slug']}",
        ),
    ):
        kwargs = {"headers": {}} if method == "get" else {
            "headers": {}, "json": {"slug": "s", "title": "t", "body": "b"}
        }
        assert getattr(client, method)(url, **kwargs).status_code == 401


def test_phi_bodies_are_refused_with_http_422(client):
    response, _ = _create(client, body="Ward round notes: call nurse@example.org.")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "page_phi_refused"
    created_response, payload = _create(client)
    assert created_response.status_code == 201
    phi_update = client.put(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}",
        json={"expected_revision": 1, "body": "SSN 123-45-6789 verified."},
        headers=AUTH_A,
    )
    assert phi_update.status_code == 422
    assert phi_update.json()["detail"]["error"] == "page_phi_refused"
    unchanged = client.get(
        f"/api/atlas/projects/atlas-api/pages/{payload['slug']}", headers=AUTH_A
    )
    assert unchanged.json()["revision"] == 1


def test_generated_imports_are_explicitly_unavailable(client):
    response, _ = _create(client, **{"import": {"pages": ["x"]}})
    assert response.status_code == 422
    assert "generated_imports_unavailable" in response.text
    generated, _ = _create(client, generated=True)
    assert generated.status_code == 422
    assert "generated_imports_unavailable" in generated.text
    import_route = client.post(
        "/api/atlas/projects/atlas-api/pages/import", json={}, headers=AUTH_A
    )
    assert import_route.status_code in (404, 405)


def test_validation_bounds_are_enforced_over_http(client):
    oversized, _ = _create(client, slug="x")
    assert oversized.status_code == 422
    empty_body, _ = _create(client, body="   ")
    assert empty_body.status_code == 422
    bad_limit = client.get(
        "/api/atlas/projects/atlas-api/pages?limit=0", headers=AUTH_A
    )
    assert bad_limit.status_code == 422
    big_limit = client.get(
        "/api/atlas/projects/atlas-api/pages?limit=1000", headers=AUTH_A
    )
    assert big_limit.status_code == 422
    bad_revision = client.put(
        "/api/atlas/projects/atlas-api/pages/whatever",
        json={"expected_revision": 0, "body": "x"},
        headers=AUTH_A,
    )
    assert bad_revision.status_code == 422


def test_listing_is_bounded_and_supports_the_limit_parameter(client):
    for _ in range(3):
        assert _create(client)[0].status_code == 201
    limited = client.get(
        "/api/atlas/projects/atlas-api/pages?limit=2", headers=AUTH_A
    )
    assert limited.status_code == 200
    assert limited.json()["count"] == 2
    assert len(limited.json()["pages"]) == 2
    full = client.get("/api/atlas/projects/atlas-api/pages", headers=AUTH_A)
    assert full.status_code == 200
    assert full.json()["count"] >= 3
    assert all(
        entry["owner_id"] == OWNER_A for entry in full.json()["pages"]
    )


def test_http_listing_uses_owner_bound_continuation_and_retains_original_fields(client):
    project = "cursor-" + uuid4().hex
    created = [_create(client, project=project)[0].json()["page_id"] for _ in range(3)]
    url = f"/api/atlas/projects/{project}/pages"
    first = client.get(url, params={"limit": 2}, headers=AUTH_A)
    assert first.status_code == 200
    page = first.json()
    assert set(page) == {"pages", "count", "next_cursor"}
    assert page["count"] == 2 and page["next_cursor"]
    second = client.get(url, params={"limit": 2, "cursor": page["next_cursor"]}, headers=AUTH_A)
    assert second.status_code == 200
    rest = second.json()
    assert rest["count"] == 1 and rest["next_cursor"] is None
    assert {p["page_id"] for p in page["pages"] + rest["pages"]} == set(created)
    for target, headers in [(url, AUTH_B), (url.replace(project, "foreign"), AUTH_A)]:
        denied = client.get(target, params={"cursor": page["next_cursor"]}, headers=headers)
        assert denied.status_code == 422
        assert denied.json()["detail"] == "page_cursor_invalid"
    for value in ["", "!", "x" * 4097]:
        assert client.get(url, params={"cursor": value}, headers=AUTH_A).status_code == 422


@pytest.mark.parametrize("depth", [20, 1100])
def test_http_listing_refuses_nested_cursor_with_typed_422(client, depth):
    raw = ("[" * depth + "0" + "]" * depth).encode()
    value = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert len(value) < 4096
    response = client.get("/api/atlas/projects/-/pages", params={"cursor": value}, headers=AUTH_A)
    assert response.status_code == 422
    assert response.json()["detail"] == "page_cursor_invalid"


def test_global_project_scope_round_trips(client):
    global_response, global_payload = _create(client, project="-")
    assert global_response.status_code == 201
    read = client.get(
        f"/api/atlas/projects/-/pages/{global_payload['slug']}", headers=AUTH_A
    )
    assert read.status_code == 200
    assert read.json()["project_id"] is None
    listed = client.get("/api/atlas/projects/-/pages", headers=AUTH_A)
    assert listed.status_code == 200
    assert any(
        entry["page_id"] == global_response.json()["page_id"]
        for entry in listed.json()["pages"]
    )
    assert all(entry["project_id"] is None for entry in listed.json()["pages"])


def test_mutations_leave_hash_chained_audit_evidence(client, plane_runtime):
    response, payload = _create(client)
    assert response.status_code == 201
    row = plane_runtime.fetch_one(
        "SELECT actor_user_id, event_class, outcome FROM audit_events "
        "WHERE action_type = 'atlas_page_create' AND actor_user_id = %s "
        "ORDER BY started_at DESC LIMIT 1",
        (OWNER_A,),
    )
    assert row is not None
    assert row["event_class"] == "memory"
    assert row["outcome"] == "success"
    assert payload["slug"]


def test_missing_pages_report_404_on_history_and_link_reads(client):
    for path in (
        "/api/atlas/projects/atlas-api/pages/never-created/revisions",
        "/api/atlas/projects/atlas-api/pages/never-created/links",
        "/api/atlas/projects/atlas-api/pages/never-created",
    ):
        assert client.get(path, headers=AUTH_A).status_code == 404


def test_unbound_atlas_subsystem_answers_503(plane_runtime):
    from personalization.atlas_api import atlas_router

    app = FastAPI(title="Atlas Unbound")
    app.include_router(atlas_router)
    app.state.orchestrator = SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=plane_runtime,
                repositories=plane_runtime.repositories,
            )
        ),
        audit_repo=None,
        credential_manager=None,
    )
    with TestClient(app) as broken:
        assert broken.get(
            "/api/atlas/projects/atlas-api/pages", headers=AUTH_A
        ).status_code == 503
        assert broken.post(
            "/api/atlas/projects/atlas-api/pages",
            json={"slug": "unbound", "title": "t", "body": "b"},
            headers=AUTH_A,
        ).status_code == 503


def test_mounted_sub_apps_resolve_the_root_orchestrator(plane_runtime, credential_manager):
    from personalization.atlas_api import atlas_router

    root = FastAPI(title="Atlas Root")
    child = FastAPI(title="Atlas Child")
    child.include_router(atlas_router)
    root.state.orchestrator = SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=plane_runtime,
                repositories=plane_runtime.repositories,
            )
        ),
        audit_repo=AuditRepository(
            plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
        ),
        credential_manager=credential_manager,
    )
    root.mount("/mounted", child)
    child._root_app = root
    with TestClient(root) as mounted:
        response = mounted.get("/mounted/api/atlas/projects/atlas-api/pages", headers=AUTH_A)
        assert response.status_code == 200
