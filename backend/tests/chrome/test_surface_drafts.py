"""Feature 027 — T026: drafts surface structure + manual-create handler."""
import asyncio
import json
import types

from orchestrator.projection_surfaces import drafts


class FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetch_all(self, sql, params=()):
        return list(self.rows)

    def get_draft_agent(self, draft_id):
        for r in self.rows:
            if r["id"] == draft_id:
                return dict(r)
        return None

    def get_owned_draft_agent(self, user_id, draft_id):
        draft = self.get_draft_agent(draft_id)
        if draft is None or draft.get("user_id") != user_id:
            return None
        return draft

    def get_decidable_drafts(self, user_id):
        return [
            dict(row)
            for row in self.rows
            if row.get("user_id") == user_id and row.get("status") != "live"
        ]


def orch_with(rows):
    store = FakeDB(rows)
    return types.SimpleNamespace(
        history=types.SimpleNamespace(db=store),
        lifecycle_manager=types.SimpleNamespace(draft_store=store),
    )


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _draft(**kw):
    base = {"id": "d1", "user_id": "u1", "agent_name": "Tracker", "agent_slug": "tracker",
            "description": "tracks things", "status": "testing", "origin": "auto_chat",
            "self_test": json.dumps({"status": "passed", "summary": "1 tool, 1 component"})}
    base.update(kw)
    return base


def test_list_shows_origin_badges_and_self_test():
    html = run(drafts.render(orch_with([
        _draft(),
        _draft(id="d2", agent_name="Rev", origin="revision", revises_agent_id="x-1",
               self_test=None),
        _draft(id="d3", agent_name="Manual", origin="manual"),
    ]), "u1", [], {}))
    assert "from chat" in html and "revision" in html and "manual" in html
    assert "self-test passed" in html
    assert "not self-tested yet" in html


def test_empty_list_hints_at_both_entry_points():
    html = run(drafts.render(orch_with([]), "u1", [], {}))
    assert "No drafts yet" in html
    assert "Create a new agent" in html  # form always present


def test_detail_decisions_for_normal_draft():
    html = run(drafts.render(orch_with([_draft()]), "u1", [], {"draft_id": "d1"}))
    assert 'data-ui-action="draft_approve"' in html
    assert 'data-ui-action="draft_discard"' in html
    assert "draft_refine" not in html  # refine form only with refine param


def test_detail_refine_form_collects_message():
    html = run(drafts.render(orch_with([_draft()]), "u1", [], {"draft_id": "d1", "refine": True}))
    assert 'data-ui-action="draft_refine"' in html
    assert 'data-ui-collect="true"' in html and 'name="message"' in html


def test_detail_revision_uses_revision_actions():
    html = run(drafts.render(orch_with([
        _draft(origin="revision", revises_agent_id="stock-1")]), "u1", [], {"draft_id": "d1"}))
    assert 'data-ui-action="revision_apply"' in html
    assert 'data-ui-action="revision_discard"' in html


def test_detail_owner_scoped():
    html = run(drafts.render(orch_with([_draft(user_id="someone_else")]), "u1", [],
                             {"draft_id": "d1"}))
    assert "Draft not found" in html


def test_rejected_drafts_remain_listed():
    """012 FR-010a: rejected drafts stay editable, so they must be visible."""
    html = run(drafts.render(orch_with([_draft(status="rejected")]), "u1", [], {}))
    assert "Tracker" in html


def test_create_handler_validates_fields():
    out = run(drafts.HANDLERS["chrome_draft_create"](
        orch_with([]), object(), "u1", [], {"fields": {"agent_name": "", "description": "x"}}))
    surface, params, notice = out
    assert surface == "drafts" and "required" in notice


def test_create_handler_runs_shared_pipeline(monkeypatch):
    calls = {}

    async def fake_meta(orch, tool, args, *, user_id, chat_id, websocket=None):
        calls["tool"] = tool
        calls["args"] = args
        return types.SimpleNamespace(error=None, result={"status": "created", "draft_id": "dX"})

    from orchestrator import agentic_creation
    monkeypatch.setattr(agentic_creation, "handle_meta_tool", fake_meta)
    out = run(drafts.HANDLERS["chrome_draft_create"](
        orch_with([]), object(), "u1", [],
        {"fields": {"agent_name": "CSV Bot", "description": "exports things to CSV files",
                    "tools": "export_csv: writes csv"}}))
    surface, params, notice = out
    assert calls["tool"] == "create_capability"
    assert calls["args"]["tools_spec"][0]["name"] == "export_csv"
    assert params == {"draft_id": "dX"} and "Staged" in notice
