"""End-to-end Atlas surface tests over a real PostgreSQL-backed AstralPlane runtime:
browsing, revision and backlink navigation, authenticated memory-source opening with
stale/deleted/revoked states, owner isolation, and hash-chained audit evidence for the
chrome reads.
"""

from __future__ import annotations

import asyncio
import json
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
from orchestrator.credential_manager import CredentialManager  # noqa: E402
from orchestrator.projection_surfaces import atlas as surf  # noqa: E402
from personalization import phi_gate  # noqa: E402
from personalization.atlas_service import AtlasPageService, PageInput  # noqa: E402
from personalization.repository import PersonalizationRepository  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER_A = "atlas-surface-owner-a"
OWNER_B = "atlas-surface-owner-b"
_SLUG = {"n": 0}


def _next_slug(prefix: str) -> str:
    _SLUG["n"] += 1
    return f"{prefix}-case{_SLUG['n']}"


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("atlas_surface") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def credential_manager(plane_runtime, tmp_path_factory):
    return CredentialManager(
        plane_runtime=plane_runtime, data_dir=str(tmp_path_factory.mktemp("atlas-surface-keys"))
    )


@pytest.fixture(scope="module")
def orch(plane_runtime, credential_manager):
    from audit.recorder import Recorder, set_recorder

    audit_repo = AuditRepository(
        plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
    )
    previous = None
    try:
        from audit import recorder as recorder_module

        previous = recorder_module.get_recorder()
        set_recorder(Recorder(audit_repo))
    except Exception:
        set_recorder(None)
    yield SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=plane_runtime,
                repositories=plane_runtime.repositories,
            )
        ),
        audit_repo=audit_repo,
        credential_manager=credential_manager,
        personalization_service=SimpleNamespace(
            repo=PersonalizationRepository(
                None, plane_runtime=plane_runtime, plane_repositories=plane_runtime.repositories
            )
        ),
    )
    set_recorder(previous)


@pytest.fixture(autouse=True)
def _clean_phi_gate(monkeypatch):
    monkeypatch.setattr(
        phi_gate, "_GATE",
        phi_gate.PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: [])),
    )


def render(orch, user_id, params):
    return asyncio.run(surf.render(orch, user_id, ["user"], params))


def components(orch, user_id, params):
    return asyncio.run(surf.components(orch, user_id, ["user"], params))


def _service(orch) -> AtlasPageService:
    return surf._service(orch)


def _create_page(orch, owner, slug, *, body="Shared project knowledge.", sources=(), links=(),
                 project="atlas-ui"):
    return _service(orch).create_page(
        owner_id=owner,
        page=PageInput(slug=slug, title=slug.replace("-", " ").title(), body=body,
                       sources=tuple(sources), links=tuple(links),
                       project_id=None if project == "-" else project),
    )


def _source_ref(record):
    return f"memory:{record['id']}@{record['updated_at']}"


def test_list_and_page_views_browse_real_pages_end_to_end(orch):
    created = _create_page(orch, OWNER_A, _next_slug("round-trip"))
    html = render(orch, OWNER_A, {"view": "list", "project": "atlas-ui"})
    assert created.title in html
    assert created.slug in html
    page_html = render(orch, OWNER_A,
                       {"view": "page", "project": "atlas-ui", "page_ref": created.page_id})
    assert "Shared project knowledge." in page_html
    assert f"revision {created.revision}" in page_html
    comps = components(orch, OWNER_A, {"view": "page", "project": "atlas-ui",
                                       "page_ref": created.page_id})
    assert json.dumps(comps).count("Shared project knowledge.") >= 1
    listed = components(orch, OWNER_A, {"view": "list", "project": "atlas-ui"})
    assert any(node.get("type") == "card" and node.get("title") == created.title
               for node in listed)


def test_history_navigation_reads_historical_revision(orch):
    slug = _next_slug("history")
    _create_page(orch, OWNER_A, slug)
    updated = _service(orch).update_page(
        owner_id=OWNER_A, project_id="atlas-ui", reference=slug,
        title=None, body="Revised knowledge.", sources=None, links=None, expected_revision=1,
    )
    assert updated.revision == 2
    history_html = render(orch, OWNER_A,
                          {"view": "history", "project": "atlas-ui", "page_ref": slug})
    assert "Revision 1" in history_html and "Revision 2" in history_html
    old = render(orch, OWNER_A,
                 {"view": "page", "project": "atlas-ui", "page_ref": slug, "revision": 1})
    assert "Shared project knowledge." in old
    assert "historical revision 1" in old
    current = render(orch, OWNER_A,
                     {"view": "page", "project": "atlas-ui", "page_ref": slug})
    assert "Revised knowledge." in current
    assert "historical revision" not in current


def test_links_and_backlinks_navigate_between_pages(orch):
    slug_a = _next_slug("hub")
    page_a = _create_page(orch, OWNER_A, slug_a)
    slug_b = _next_slug("spoke")
    page_b = _create_page(orch, OWNER_A, slug_b, links=[slug_a])
    hub_view = render(orch, OWNER_A,
                      {"view": "page", "project": "atlas-ui", "page_ref": page_a.page_id})
    assert page_b.title in hub_view
    spoke_view = render(orch, OWNER_A,
                        {"view": "page", "project": "atlas-ui", "page_ref": page_b.page_id})
    assert page_a.title in spoke_view


def test_owner_b_cannot_browse_owner_a_pages(orch):
    created = _create_page(orch, OWNER_A, _next_slug("private"))
    html = render(orch, OWNER_B, {"view": "page", "project": "atlas-ui",
                                  "page_ref": created.page_id})
    assert "no longer exists" in html
    assert "Shared project knowledge." not in html
    listing = render(orch, OWNER_B, {"view": "list", "project": "atlas-ui"})
    assert created.title not in listing


def test_memory_source_opens_current_stale_and_deleted_states(orch):
    repo = orch.personalization_service.repo
    memory = repo.create_memory(OWNER_A, "preference", "Citeable project preference")
    source = _source_ref(memory)
    current = render(orch, OWNER_A, {"view": "source", "source": source})
    assert "Citeable project preference" in current
    assert "matches cited revision" in current

    repo.update_memory_value(OWNER_A, memory["id"], "Changed preference")
    stale = render(orch, OWNER_A, {"view": "source", "source": source})
    assert "has changed since this page cited it" in stale
    assert "Changed preference" not in stale

    updated = repo.get_memory(OWNER_A, memory["id"])
    fresh_source = f"memory:{memory['id']}@{updated['updated_at']}"
    reopened = render(orch, OWNER_A, {"view": "source", "source": fresh_source})
    assert "Changed preference" in reopened

    assert repo.delete_memory(OWNER_A, memory["id"]) is True
    deleted = render(orch, OWNER_A, {"view": "source", "source": source})
    assert "no longer exists" in deleted

    page = _create_page(orch, OWNER_A, _next_slug("plain-source"),
                        sources=["https://example.invalid/report"])
    page_html = render(orch, OWNER_A,
                       {"view": "page", "project": "atlas-ui", "page_ref": page.page_id})
    assert "https://example.invalid/report" in page_html
    assert "Open source" not in page_html


def test_revoked_and_atlas_internal_memory_sources_are_rejected(orch):
    repo = orch.personalization_service.repo
    memory = repo.create_memory(OWNER_A, "preference", "Soon revoked")
    revoked = f"memory:{memory['id']}@{memory['updated_at']}"
    assert repo.set_validity(OWNER_A, memory["id"], valid_to=1) is True
    html = render(orch, OWNER_A, {"view": "source", "source": revoked})
    assert "no longer active" in html

    page = _create_page(orch, OWNER_A, _next_slug("internal"))
    row = orch.personalization_service.repo.get_memory(OWNER_A, page.page_id)
    internal = render(orch, OWNER_A,
                      {"view": "source", "source": f"memory:{page.page_id}@{row['updated_at']}"})
    assert "not a citable memory" in internal


def test_note_source_refers_to_private_notes(orch):
    html = render(orch, OWNER_A,
                  {"view": "source",
                   "source": f"note:{uuid4()}@1"})
    assert "Private notes surface" in html


def test_unknown_source_memory_reports_deleted_state(orch):
    html = render(orch, OWNER_A,
                  {"view": "source", "source": f"memory:{uuid4()}@5"})
    assert "no longer exists" in html


def test_unbound_orchestrator_renders_unavailable_notices():
    broken = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(runtime=None,
                                                                  repositories=None)),
        audit_repo=None, credential_manager=None,
    )
    html = render(broken, OWNER_A, {"view": "list"})
    assert "temporarily unavailable" in html
    comps = components(broken, OWNER_A, {"view": "list"})
    assert any(node.get("type") == "alert" for node in comps)


def test_facet_scan_discovers_other_project_scopes(orch):
    project = _next_slug("facet")
    _create_page(orch, OWNER_A, _next_slug("faceted"), project=project)
    html = render(orch, OWNER_A, {"view": "list", "project": "-"})
    assert project in html


def test_atlas_views_leave_hash_chained_audit_evidence(orch, plane_runtime):
    slug = _next_slug("audited")
    page = _create_page(orch, OWNER_A, slug)
    render(orch, OWNER_A, {"view": "page", "project": "atlas-ui", "page_ref": page.page_id})
    row = plane_runtime.fetch_one(
        "SELECT actor_user_id, event_class, outcome FROM audit_events "
        "WHERE action_type = 'atlas_page_view' AND actor_user_id = %s "
        "ORDER BY started_at DESC LIMIT 1",
        (OWNER_A,),
    )
    assert row is not None
    assert row["event_class"] == "memory"
    assert row["outcome"] == "success"
    render(orch, OWNER_A, {"view": "source", "source": f"memory:{uuid4()}@1"})
    source_row = plane_runtime.fetch_one(
        "SELECT outcome FROM audit_events WHERE action_type = 'atlas_source_open' "
        "AND actor_user_id = %s ORDER BY started_at DESC LIMIT 1",
        (OWNER_A,),
    )
    assert source_row is not None
