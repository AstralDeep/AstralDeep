"""DB-free tests for orchestrator/projection_surfaces/atlas.py: list, page, history, and
source views over a fake AtlasPageService, chrome_open navigation payloads, SDUI parity,
strict navigation-param denials, escaping, source state machine, and the Personalization
tab-bar entry points.
"""

import asyncio
import html as html_module
import json
import re
from types import SimpleNamespace

import pytest

from orchestrator.projection_surfaces import SURFACE_MODULES
from orchestrator.projection_surfaces import atlas as surf
from personalization.atlas_pages import GLOBAL_PROJECT
from personalization.atlas_service import (
    AtlasNotFound,
    AtlasUnavailable,
    PageBatch,
    PageScan,
    PageSummary,
)


_PAGE_COUNTER = 0


def _page(slug="kickoff", title="Kickoff", body="Project kickoff notes.\n\nSecond paragraph.",
          revision=2, sources=(), project="atlas-ui", page_id=None, created=1700000000000,
          updated=1700000050000):
    global _PAGE_COUNTER
    _PAGE_COUNTER += 1
    return PageSummary(
        page_id=page_id or f"00000000-0000-4000-8000-{_PAGE_COUNTER:012d}",
        owner_id="u1",
        project_id=None if project in (None, GLOBAL_PROJECT) else project,
        slug=slug,
        title=title,
        revision=revision,
        sources=tuple(sources),
        created_at=created,
        updated_at=updated,
        body=body,
    )


class FakeService:
    def __init__(self, pages=(), *, batch_cursor=None, list_error=None, page_error=None,
                 history_error=None, links_error=None, scan_error=None, facets=True):
        self.pages = list(pages)
        self.batch_cursor = batch_cursor
        self.list_error = list_error
        self.page_error = page_error
        self.history_error = history_error
        self.links_error = links_error
        self.scan_error = scan_error
        self.facets = facets
        self.list_calls = []
        self.page_calls = []
        self.history_calls = []
        self.links_calls = []
        self.scan_calls = []

    def list_page_batch(self, *, owner_id, project_id, limit, cursor=None):
        self.list_calls.append({"owner": owner_id, "project": project_id,
                                "limit": limit, "cursor": cursor})
        if self.list_error is not None:
            raise self.list_error
        return PageBatch(tuple(self.pages), self.batch_cursor)

    def get_page(self, *, owner_id, project_id, reference, revision=None):
        self.page_calls.append({"owner": owner_id, "project": project_id,
                                "reference": reference, "revision": revision})
        if self.page_error is not None:
            raise self.page_error
        for page in self.pages:
            if page.page_id == reference or page.slug == reference:
                if revision is not None and revision != page.revision:
                    raise AtlasNotFound("revision_not_found")
                return page
        raise AtlasNotFound()

    def page_history(self, *, owner_id, project_id, reference):
        self.history_calls.append({"owner": owner_id, "project": project_id,
                                   "reference": reference})
        if self.history_error is not None:
            raise self.history_error
        page = self.get_page(owner_id=owner_id, project_id=project_id, reference=reference)
        older = _page(slug=page.slug, title=page.title, body="old", revision=page.revision - 1,
                      project=page.project_id or GLOBAL_PROJECT,
                      page_id=page.page_id, created=page.created_at,
                      updated=page.updated_at - 1000, sources=page.sources)
        return (page, older)

    def page_links(self, *, owner_id, project_id, reference):
        self.links_calls.append({"owner": owner_id, "project": project_id,
                                 "reference": reference})
        if self.links_error is not None:
            raise self.links_error
        return tuple(page for page in self.pages if page.page_id != reference)

    def scan_pages(self, *, owner_id, project_id=None, all_projects=False, limit=500):
        self.scan_calls.append({"owner": owner_id, "all_projects": all_projects})
        if self.scan_error is not None:
            raise self.scan_error
        if not self.facets:
            return PageScan((), 0, True)
        return PageScan(tuple(self.pages), len(self.pages), True)


class FakeMemoryRepo:
    def __init__(self, records=None, error=None):
        self.records = records or {}
        self.error = error
        self.calls = []

    def get_memory(self, user_id, mem_id):
        self.calls.append((user_id, mem_id))
        if self.error is not None:
            raise self.error
        return self.records.get(mem_id)


def make_orch(service=None, repo=None):
    return SimpleNamespace(
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(runtime=object(), repositories=object())),
        audit_repo=object(),
        credential_manager=SimpleNamespace(_fernet=object()),
        personalization_service=SimpleNamespace(repo=repo) if repo is not None else None,
    )


@pytest.fixture
def fake_service(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(surf, "_service", lambda orch: service)
    return service


def render(orch, params=None):
    return asyncio.run(surf.render(orch, "u1", ["user"], params or {}))


def components(orch, params=None):
    return asyncio.run(surf.components(orch, "u1", ["user"], params or {}))


def _html_action_payloads(markup, action):
    encoded = re.findall(
        rf'data-ui-action="{re.escape(action)}"[^>]*data-ui-payload=\'([^\']*)\'',
        markup,
    )
    return [json.loads(html_module.unescape(value)) for value in encoded]


def _sdui_action_payloads(value, action):
    found = []

    def visit(node):
        if isinstance(node, dict):
            if node.get("action") == action:
                found.append(node.get("payload") or {})
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def test_surface_is_registered_with_closed_handler_set():
    assert SURFACE_MODULES["atlas"] == "orchestrator.projection_surfaces.atlas"
    assert surf.TITLE == "Atlas"
    assert surf.HANDLERS == {}


def test_list_view_renders_pages_with_chrome_open_navigation(fake_service):
    fake_service.pages = [_page()]
    html = render(make_orch(), {"view": "list"})
    assert "Kickoff" in html
    assert "rev 2" in html
    opens = _html_action_payloads(html, "chrome_open")
    page_opens = [payload for payload in opens
                  if payload["surface"] == "atlas" and payload["params"].get("view") == "page"]
    assert page_opens and page_opens[0]["params"]["page_ref"].startswith("00000000")
    assert any(payload["params"].get("view") == "history" for payload in opens)
    assert fake_service.list_calls[0]["owner"] == "u1"
    assert fake_service.list_calls[0]["limit"] == surf._LIST_LIMIT


def test_list_view_carries_project_scope_and_pagination_cursor(fake_service):
    fake_service.pages = [_page()]
    fake_service.batch_cursor = "abc123cursor"
    html = render(make_orch(), {"view": "list", "project": "atlas-ui"})
    assert fake_service.list_calls[0]["project"] == "atlas-ui"
    older = [payload for payload in _html_action_payloads(html, "chrome_open")
             if payload["params"].get("cursor") == "abc123cursor"]
    assert older and older[0]["params"]["project"] == "atlas-ui"


def test_global_scope_uses_global_marker_in_navigation(fake_service):
    fake_service.pages = [_page(project=GLOBAL_PROJECT)]
    html = render(make_orch(), {"view": "list"})
    opens = _html_action_payloads(html, "chrome_open")
    assert any(payload["params"].get("project") == GLOBAL_PROJECT for payload in opens)


def test_list_view_offers_other_discovered_scopes(fake_service):
    global_page = _page(project=GLOBAL_PROJECT, slug="global-page")
    other = _page(project="deep-space", slug="deep-page")
    fake_service.pages = [global_page, other]
    html = render(make_orch(), {"view": "list", "project": GLOBAL_PROJECT})
    scopes = [payload["params"].get("project")
              for payload in _html_action_payloads(html, "chrome_open")]
    assert "deep-space" in scopes
    html_other = render(make_orch(), {"view": "list", "project": "deep-space"})
    scopes_other = [payload["params"].get("project")
                    for payload in _html_action_payloads(html_other, "chrome_open")]
    assert GLOBAL_PROJECT in scopes_other


def test_empty_list_renders_empty_state_without_pagination(fake_service):
    fake_service.pages = []
    html = render(make_orch(), {"view": "list"})
    assert "No Atlas pages here yet." in html
    assert "Older pages" not in html


def test_page_view_renders_body_sources_links_and_actions(fake_service):
    target = _page(slug="target", title="Target")
    source_page = _page(
        slug="kickoff",
        sources=["memory:123e4567-e89b-42d3-a456-426614174000@3",
                 "https://example.invalid/report", "note:123e4567-e89b-42d3-a456-426614174000@1"],
    )
    fake_service.pages = [source_page, target]
    html = render(make_orch(), {"view": "page", "page_ref": "kickoff", "project": "atlas-ui"})
    assert "Kickoff" in html
    assert "Project kickoff notes." in html
    assert "Second paragraph." in html
    opens = _html_action_payloads(html, "chrome_open")
    source_opens = [payload for payload in opens
                    if payload["params"].get("view") == "source"]
    assert any(payload["params"].get("source", "").startswith("memory:") for payload in source_opens)
    assert any(payload["params"].get("source", "").startswith("note:") for payload in source_opens)
    link_opens = [payload for payload in opens if payload["params"].get("page_ref") == target.page_id]
    assert link_opens
    assert "Linked pages (links and backlinks)" in html
    assert "https://example.invalid/report" in html
    assert "cited revision 3" in html
    assert "Revision history" in html


def test_page_view_renders_historical_revision_notice(fake_service):
    page = _page()
    fake_service.pages = [page]
    html = render(make_orch(), {"view": "page", "page_ref": page.page_id, "revision": page.revision})
    assert fake_service.page_calls[0]["revision"] == page.revision
    assert f"historical revision {page.revision}" in html
    assert "Open current version" in html


def test_page_view_escapes_hostile_content(fake_service):
    hostile = _page(title="<script>alert(1)</script>",
                    body="Body <img src=x onerror=alert(2)> text")
    fake_service.pages = [hostile]
    html = render(make_orch(), {"view": "page", "page_ref": hostile.slug})
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_missing_page_falls_back_to_list_with_notice(fake_service):
    fake_service.page_error = AtlasNotFound()
    html = render(make_orch(), {"view": "page", "page_ref": "ghost"})
    assert "no longer exists" in html
    assert "Browsing Atlas pages" in html
    assert fake_service.list_calls == []


def test_history_view_lists_revisions_with_current_badge(fake_service):
    page = _page()
    fake_service.pages = [page]
    html = render(make_orch(), {"view": "history", "page_ref": page.slug})
    assert "Revision history" in html
    assert "current" in html
    opens = [payload for payload in _html_action_payloads(html, "chrome_open")
             if payload["params"].get("view") == "page" and "revision" in payload["params"]]
    revisions = sorted(payload["params"]["revision"] for payload in opens)
    assert revisions == [1, 2]


def test_source_view_shows_current_memory_content(fake_service):
    repo = FakeMemoryRepo({
        "123e4567-e89b-42d3-a456-426614174000": {
            "id": "123e4567-e89b-42d3-a456-426614174000", "category": "preference",
            "value": "Prefers tables over prose", "updated_at": 3,
            "superseded_at": None, "superseded_by": None,
            "valid_from": None, "valid_to": None,
        },
    })
    page = _page(sources=["memory:123E4567-E89B-42D3-A456-426614174000@3"])
    fake_service.pages = [page]
    html = render(make_orch(repo=repo),
                  {"view": "source", "page_ref": page.page_id, "project": "atlas-ui",
                   "source": "memory:123e4567-e89b-42d3-a456-426614174000@3"})
    assert repo.calls == [("u1", "123e4567-e89b-42d3-a456-426614174000")]
    assert "Prefers tables over prose" in html
    assert "matches cited revision 3" in html
    assert "category: preference" in html
    assert "Back to page" in html


def test_source_view_states_hide_content(fake_service):
    cited = "123e4567-e89b-42d3-a456-426614174000"
    cases = {
        "deleted": None,
        "revoked": {"updated_at": 3, "superseded_at": 9, "superseded_by": "other",
                    "value": "secret", "valid_from": None, "valid_to": None},
        "stale": {"updated_at": 4, "superseded_at": None, "superseded_by": None,
                  "value": "changed content", "valid_from": None, "valid_to": None},
        "expired": {"updated_at": 3, "superseded_at": None, "superseded_by": None,
                    "value": "expired content", "valid_from": None, "valid_to": 1},
        "invalid": {"updated_at": 3, "superseded_at": None, "superseded_by": None,
                    "valid_from": None, "valid_to": None,
                    "value": json.dumps({"body": "sealed", "format": "astral.atlas-page/v1",
                                         "kind": "page", "page_id": cited,
                                         "owner_id": "u1", "revision": 1})},
    }
    for state, record in cases.items():
        repo = FakeMemoryRepo({cited: record})
        html = render(make_orch(repo=repo),
                      {"view": "source", "source": f"memory:{cited}@3"})
        assert "secret" not in html and "changed content" not in html
        assert "expired content" not in html
        if state == "deleted":
            assert "no longer exists" in html
        elif state == "stale":
            assert "has changed since this page cited it" in html
        elif state == "revoked":
            assert "no longer active" in html
        elif state == "invalid":
            assert "not a citable memory" in html


def test_note_source_refers_to_private_notes_without_content(fake_service):
    html = render(make_orch(),
                  {"view": "source", "source": "note:123e4567-e89b-42d3-a456-426614174000@1"})
    assert "Private notes surface" in html
    opens = _html_action_payloads(html, "chrome_open")
    assert any(payload["surface"] == "guidance" for payload in opens)
    assert not any(payload.get("surface") == "atlas" and payload["params"].get("view") == "page"
                   for payload in opens if payload["surface"] == "atlas") or "Back to page" not in html


def test_source_view_without_memory_subsystem_reports_unbound(fake_service):
    html = render(make_orch(),
                  {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@3"})
    assert "memory subsystem is unavailable" in html


def test_source_view_repo_failure_reports_unbound(fake_service):
    repo = FakeMemoryRepo(error=RuntimeError("db down"))
    html = render(make_orch(repo=repo),
                  {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@3"})
    assert "memory subsystem is unavailable" in html


@pytest.mark.parametrize("params", [
    {"view": "time-travel"},
    {"view": "list", "cursor": "not a cursor!"},
    {"view": "page", "page_ref": "x" * 200},
    {"view": "page"},
    {"view": "history"},
    {"view": "source"},
    {"view": "source", "source": "memory:not-a-uuid@3"},
    {"view": "source", "source": "drive:123e4567-e89b-42d3-a456-426614174000@3"},
    {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@99999999999999999999"},
    {"view": "page", "page_ref": "ok", "revision": 0},
    {"view": "list", "project": "x\x00y"},
])
def test_invalid_navigation_params_fall_back_to_list_with_notice(fake_service, params):
    html = render(make_orch(), params)
    assert "No Atlas pages here yet." in html or "Browsing Atlas pages" in html
    if params.get("view") not in (None, "list"):
        assert "invalid" in html or "Choose" in html or "unavailable" in html
    if params.get("view") == "page":
        assert fake_service.page_calls == []
    if params.get("view") == "source":
        assert fake_service.list_calls and fake_service.page_calls == []


def test_revision_and_cursor_bounds_are_enforced(fake_service):
    assert render(make_orch(), {"view": "page", "page_ref": "p", "revision": 2**53 - 2})
    assert render(make_orch(), {"view": "list", "cursor": "a" * 4096})


def test_unavailable_service_renders_temporary_notice(monkeypatch):
    def broken(orch):
        raise AtlasUnavailable("atlas_plane_unbound")

    monkeypatch.setattr(surf, "_service", broken)
    html = render(make_orch(), {"view": "list"})
    assert "temporarily unavailable" in html


def test_sdui_components_mirror_the_html_views(fake_service):
    from webrender.renderer import allowed_primitive_types

    allowed = set(allowed_primitive_types())
    target = _page(
        slug="kickoff",
        sources=["memory:123e4567-e89b-42d3-a456-426614174000@3"],
    )
    fake_service.pages = [target, _page(slug="other", title="Other")]

    comps = components(make_orch(), {"view": "list"})
    for node in comps:
        assert node["type"] in allowed
    opens = _sdui_action_payloads(comps, "chrome_open")
    assert any(payload["params"].get("view") == "page" for payload in opens)
    assert any(payload["params"].get("view") == "history" for payload in opens)

    page_comps = components(make_orch(), {"view": "page", "page_ref": "kickoff"})
    for node in page_comps:
        assert node["type"] in allowed
    page_opens = _sdui_action_payloads(page_comps, "chrome_open")
    assert any(payload["params"].get("source", "").startswith("memory:") for payload in page_opens)
    texts = json.dumps(page_comps)
    assert "Project kickoff notes." in texts

    history_comps = components(make_orch(), {"view": "history", "page_ref": "kickoff"})
    assert any(node["type"] == "card" for node in history_comps)

    source_comps = components(
        make_orch(repo=FakeMemoryRepo({
            "123e4567-e89b-42d3-a456-426614174000": {
                "id": "123e4567-e89b-42d3-a456-426614174000", "category": "preference",
                "value": "Prefers tables", "updated_at": 3, "superseded_at": None,
                "superseded_by": None, "valid_from": None, "valid_to": None,
            },
        })),
        {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@3"},
    )
    source_texts = json.dumps(source_comps)
    assert "Prefers tables" in source_texts
    note_comps = components(
        make_orch(),
        {"view": "source", "source": "note:123e4567-e89b-42d3-a456-426614174000@1"},
    )
    note_opens = _sdui_action_payloads(note_comps, "chrome_open")
    assert any(payload["surface"] == "guidance" for payload in note_opens)


def test_sdui_drops_explanatory_captions_and_keeps_state(fake_service):
    page = _page(slug="kickoff", sources=[])
    fake_service.pages = [page]
    listing = json.dumps(components(make_orch(), {"view": "list"}))
    assert "Scope: the global project" in listing
    assert "Browsing Atlas pages" not in listing and "Pages are created and" not in listing
    detail = json.dumps(components(make_orch(), {"view": "page", "page_ref": "kickoff"}))
    assert "records no sources" not in detail and "No linked pages yet" not in detail
    old = json.dumps(components(make_orch(), {"view": "page", "page_ref": "kickoff",
                                              "revision": page.revision}))
    assert "historical revision" in old
    assert "Open the current version for the live content" not in old


def test_sdui_components_escape_hostile_page_content(fake_service):
    from webrender.renderer import render_one

    fake_service.pages = [_page(title="<script>x</script>", body="body <b>bold</b>")]
    comps = components(make_orch(), {"view": "list"})
    rendered = "".join(render_one(node) for node in comps)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_sdui_missing_page_falls_back_to_list_alert(fake_service):
    fake_service.page_error = AtlasNotFound()
    comps = components(make_orch(), {"view": "page", "page_ref": "ghost"})
    assert any(node["type"] == "alert" for node in comps)


def test_sdui_unavailable_service_alerts(monkeypatch):
    def broken(orch):
        raise AtlasUnavailable("atlas_plane_unbound")

    monkeypatch.setattr(surf, "_service", broken)
    comps = components(make_orch(), {"view": "list"})
    assert any(node["type"] == "alert" and "temporarily unavailable" in node.get("message", "")
               for node in comps)


def test_non_string_navigation_values_are_denied(fake_service):
    assert render(make_orch(), {"view": "list", "project": 5})
    assert render(make_orch(), {"view": "page", "page_ref": 5})
    assert render(make_orch(), {"view": "page", "page_ref": "ok", "revision": "soon"})
    assert render(make_orch(), {"view": "list", "cursor": 5})
    assert render(make_orch(), {"view": "source", "source": 5})
    assert components(make_orch(), {"view": "list", "project": 5})


def test_invalid_timestamps_render_placeholder(fake_service):
    fake_service.pages = [_page(created="x", updated=None)]
    html = render(make_orch(), {"view": "list"})
    assert "—" in html


def test_empty_body_renders_placeholder(fake_service):
    fake_service.pages = [_page(body="   ")]
    html = render(make_orch(), {"view": "page", "page_ref": fake_service.pages[0].slug})
    assert "no content yet" in html
    comps = components(make_orch(), {"view": "page", "page_ref": fake_service.pages[0].slug})
    assert not any("no content yet" in json.dumps(node) for node in comps)


def test_validation_and_conflict_failures_map_to_notices(fake_service):
    from personalization.atlas_pages import AtlasPageError
    from personalization.atlas_service import AtlasConflict

    fake_service.list_error = AtlasPageError("limit_out_of_bounds")
    html = render(make_orch(), {"view": "list"})
    assert "limit_out_of_bounds" in html
    fake_service.list_error = AtlasConflict("page_revision_conflict")
    assert "refused" in render(make_orch(), {"view": "list"})


def test_memory_valid_from_future_is_revoked(fake_service):
    repo = FakeMemoryRepo({
        "123e4567-e89b-42d3-a456-426614174000": {
            "updated_at": 3, "superseded_at": None, "superseded_by": None,
            "value": "future content", "valid_from": 2**62, "valid_to": None,
        },
    })
    html = render(make_orch(repo=repo),
                  {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@3"})
    assert "no longer active" in html
    assert "future content" not in html


def test_sdui_covers_error_paths_and_pagination(fake_service):
    fake_service.pages = [_page()]
    fake_service.batch_cursor = "next-cursor"
    comps = components(make_orch(), {"view": "list"})
    assert any(payload.get("params", {}).get("cursor") == "next-cursor"
               for payload in _sdui_action_payloads(comps, "chrome_open"))

    fake_service.pages = []
    fake_service.batch_cursor = None
    empty = components(make_orch(), {"view": "list"})
    assert not any("No Atlas pages" in json.dumps(node) for node in empty)

    fake_service.page_error = AtlasNotFound()
    missing = components(make_orch(), {"view": "page", "page_ref": "ghost"})
    assert any(node.get("type") == "alert" and "no longer exists" in node.get("message", "")
               for node in missing)

    history_error = components(make_orch(), {"view": "history", "page_ref": "ghost"})
    assert any(node.get("type") == "alert" for node in history_error)


def test_sdui_renders_historical_revision_and_plain_sources(fake_service):
    page = _page(slug="kickoff", sources=["https://example.invalid/report"])
    fake_service.pages = [page]
    comps = components(make_orch(), {"view": "page", "page_ref": page.slug,
                                     "revision": page.revision})
    flat = json.dumps(comps)
    assert "historical revision" in flat
    assert "https://example.invalid/report" in flat


def test_sdui_renders_deleted_source_state(fake_service):
    comps = components(make_orch(repo=FakeMemoryRepo({})),
                       {"view": "source", "source": "memory:123e4567-e89b-42d3-a456-426614174000@3",
                        "page_ref": "kickoff"})
    flat = json.dumps(comps)
    assert "no longer exists" in flat
    assert any(payload.get("params", {}).get("view") == "page"
               for payload in _sdui_action_payloads(comps, "chrome_open"))


def test_dispatch_delivers_atlas_surface_over_the_real_chrome_path(fake_service, monkeypatch):
    from orchestrator import chrome_events
    from rote.capabilities import DeviceProfile

    class DispatchOrch:
        def __init__(self, device):
            self.ws = object()
            self.ui_sessions = {self.ws: {"sub": "u1",
                                          "realm_access": {"roles": ["user"]}}}
            self.sent = []
            self.rote = SimpleNamespace(
                get_profile=lambda ws: (DeviceProfile.default() if device == "browser"
                                        else DeviceProfile.from_dict({"device_type": device})))
            self._open_chrome_surface = {}
            self._ordinary_chrome_requests = {}
            self._ws_active_chat = {}

        async def _safe_send(self, websocket, data):
            self.sent.append(json.loads(data))
            return True

        async def derive_machine_authority(self, **kwargs):
            from orchestrator.chain_authority import AuthoritySkip
            return AuthoritySkip("missing_consent", "dispatch test")

        def _bind_machine_turn(self, vws, authority):
            pass

        def _unbind_machine_turn(self, vws):
            pass

    fake_service.pages = [_page()]

    web_orch = DispatchOrch("browser")
    handled = asyncio.run(chrome_events.handle_chrome_event(
        web_orch, web_orch.ws, "chrome_open",
        {"surface": "atlas", "params": {"view": "list"}}, "u1"))
    assert handled is True
    render_frames = [f for f in web_orch.sent if f.get("type") == "chrome_render"]
    assert render_frames and "Browsing Atlas pages" in render_frames[-1]["html"]
    assert fake_service.list_calls[-1]["owner"] == "u1"

    native_orch = DispatchOrch("windows")
    asyncio.run(chrome_events.handle_chrome_event(
        native_orch, native_orch.ws, "chrome_open",
        {"surface": "atlas", "params": {"view": "list"}}, "u1"))
    surfaces = [f for f in native_orch.sent if f.get("type") == "chrome_surface"]
    assert surfaces and surfaces[-1]["surface_key"] == "atlas"
    assert any(node.get("type") == "card" for node in surfaces[-1]["components"])


def test_personalization_tab_bar_offers_atlas_on_web_and_native(monkeypatch):
    from orchestrator.projection_surfaces import personalization as personalization_surf

    monkeypatch.setattr(surf, "_service", lambda orch: FakeService())
    html = personalization_surf._tab_bar("soul")
    atlas_opens = [payload for payload in _html_action_payloads(html, "chrome_open")
                   if payload.get("surface") == "atlas"]
    assert atlas_opens and atlas_opens[0]["params"] == {}

    orch = SimpleNamespace(
        personalization_service=None, tool_permissions=None, scheduled_job_store=None,
        work_admission=None,
    )
    comps = asyncio.run(personalization_surf.components(orch, "u1", ["user"], {}))
    native_opens = [payload for payload in _sdui_action_payloads(comps, "chrome_open")
                    if payload.get("surface") == "atlas"]
    assert native_opens and native_opens[0]["params"] == {}
