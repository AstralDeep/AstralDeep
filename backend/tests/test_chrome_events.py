"""Feature 027 — T019/T027/T029: chrome dispatcher, convergence, admin gating.

DB-free: a fake orchestrator + a stub surface module exercise routing,
re-render-with-notice, error paths, admin DOM/action gating, and the
chat↔drafts-surface convergence (SC-007).
"""
import asyncio
import json
import sys
import types

import pytest

from orchestrator import chrome_events


class FakeWS:
    pass


class FakeOrch:

    # 056 US2: machine-turn classes derive their root authority at the
    # orchestrator's shared seam; a stand-in must model it. No durable consent
    # exists in these tests, so the honest answer is an AuthoritySkip (the turn
    # runs unbound, exactly as it does in dev posture today).
    async def derive_machine_authority(self, **kwargs):
        from orchestrator.chain_authority import AuthoritySkip
        return AuthoritySkip("missing_consent", "test double")

    def _bind_machine_turn(self, vws, authority):
        pass

    def _unbind_machine_turn(self, vws):
        pass
    def __init__(self, roles=("user",)):
        self.ws = FakeWS()
        self.ui_sessions = {self.ws: {"realm_access": {"roles": list(roles)}}}
        self.sent = []

    async def _safe_send(self, websocket, data):
        self.sent.append(json.loads(data))
        return True


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def stub_surface(monkeypatch):
    """Register a stub surface module under key 'stub' and reset the cache."""
    mod = types.ModuleType("tests.stub_surface")
    mod.TITLE = "Stub"

    async def render(orch, user_id, roles, params):
        return f'<div id="stub-body">params={json.dumps(params, sort_keys=True)}</div>'

    async def _save(orch, websocket, user_id, roles, payload):
        return ("stub", {"saved": True}, '<div class="astral-chrome-notice">ok</div>')

    async def _boom(orch, websocket, user_id, roles, payload):
        raise RuntimeError("kaboom")

    mod.render = render
    mod.HANDLERS = {"chrome_stub_save": _save, "chrome_stub_boom": _boom}
    sys.modules["tests.stub_surface"] = mod
    from orchestrator import projection_surfaces as reg
    monkeypatch.setitem(reg.SURFACE_MODULES, "stub", "tests.stub_surface")
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)
    yield mod
    monkeypatch.setattr(chrome_events, "_HANDLERS", None)


def _last_modal(orch):
    frames = [f for f in orch.sent if f.get("type") == "chrome_render"]
    assert frames, "no chrome_render frame pushed"
    assert frames[-1]["region"] == "modal"
    return frames[-1]["html"]


# ---------------------------------------------------------------------------
# T019 — dispatcher routing
# ---------------------------------------------------------------------------

def test_chrome_open_renders_surface_into_modal(stub_surface):
    orch = FakeOrch()
    handled = run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "stub", "params": {"x": 1}}, "u1"))
    assert handled is True
    html = _last_modal(orch)
    assert 'id="stub-body"' in html and '"x": 1' in html
    assert 'role="dialog"' in html and "Stub" in html  # modal shell + title


def test_chrome_open_accepts_json_string_params(stub_surface):
    orch = FakeOrch()
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "stub", "params": '{"y": 2}'}, "u1"))
    assert '"y": 2' in _last_modal(orch)


def test_chrome_close_clears_modal(stub_surface):
    orch = FakeOrch()
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_close", {}, "u1"))
    assert handled is True
    assert _last_modal(orch) == ""


def test_unknown_surface_is_not_silent(stub_surface):
    orch = FakeOrch()
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {"surface": "nope"}, "u1"))
    assert "Unknown settings surface" in _last_modal(orch)


def test_unknown_chrome_action_is_not_silent(stub_surface):
    orch = FakeOrch()
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_wat", {}, "u1"))
    assert handled is True
    assert "Unknown action" in _last_modal(orch)


def test_non_chrome_action_returns_false(stub_surface):
    orch = FakeOrch()
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chat_message", {}, "u1"))
    assert handled is False
    assert orch.sent == []


def test_handler_tuple_rerenders_with_notice(stub_surface):
    orch = FakeOrch()
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_stub_save", {}, "u1"))
    html = _last_modal(orch)
    assert "astral-chrome-notice" in html  # notice prepended
    assert '"saved": true' in html  # re-rendered with handler params


def test_handler_exception_renders_error_block(stub_surface):
    orch = FakeOrch()
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_stub_boom", {}, "u1"))
    assert handled is True
    html = _last_modal(orch)
    assert "astral-chrome-error" in html


def _native(orch):
    """Give a FakeOrch a native (windows) ROTE profile so error notices take
    the chrome_surface path (feature 044 — surface_key assertions)."""
    from rote.capabilities import DeviceProfile
    profile = DeviceProfile.from_dict({"device_type": "windows"})
    orch.rote = types.SimpleNamespace(get_profile=lambda ws: profile)
    return orch


def test_handler_exception_notice_carries_acting_surface_key(stub_surface):
    orch = _native(FakeOrch())
    handled = run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_stub_boom", {}, "u1"))
    assert handled is True
    frames = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert frames, "no chrome_surface error notice pushed"
    assert frames[-1]["surface_key"] == "stub"


def test_admin_denied_action_notice_carries_owner_surface_key():
    chrome_events._HANDLERS = None
    orch = _native(FakeOrch(roles=("user",)))
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_admin_step_archive", {"step_id": 1}, "u1"))
    frames = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert frames, "no chrome_surface error notice pushed"
    assert frames[-1]["surface_key"] == "admin_tools"


def test_unknown_action_notice_defaults_to_error_surface_key(stub_surface):
    orch = _native(FakeOrch())
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_wat", {}, "u1"))
    frames = [f for f in orch.sent if f.get("type") == "chrome_surface"]
    assert frames, "no chrome_surface error notice pushed"
    assert frames[-1]["surface_key"] == "error"


def test_render_exception_renders_error_block(stub_surface, monkeypatch):
    async def bad_render(orch, user_id, roles, params):
        raise RuntimeError("render broke")
    monkeypatch.setattr(stub_surface, "render", bad_render)
    orch = FakeOrch()
    run(chrome_events.handle_chrome_event(orch, orch.ws, "chrome_open", {"surface": "stub"}, "u1"))
    html = _last_modal(orch)
    assert "astral-chrome-error" in html and "Retry" in html


def test_creation_actions_registered():
    chrome_events._HANDLERS = None
    handlers = chrome_events._handlers()
    for action in ("draft_approve", "draft_refine", "draft_discard",
                   "revision_apply", "revision_discard", "chrome_draft_create"):
        assert action in handlers, f"missing handler: {action}"


# ---------------------------------------------------------------------------
# T029 — admin gating (US4)
# ---------------------------------------------------------------------------

def test_admin_surface_denied_for_non_admin():
    chrome_events._HANDLERS = None
    orch = FakeOrch(roles=("user",))
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "admin_tools"}, "u1"))
    html = _last_modal(orch)
    assert "Not authorized" in html and "admin role" in html


def test_admin_surface_allowed_for_admin():
    chrome_events._HANDLERS = None
    orch = FakeOrch(roles=("admin", "user"))
    orch.onboarding_repo = types.SimpleNamespace(list_all_steps=lambda include_archived=True: [])
    orch.feedback_repo = types.SimpleNamespace(
        list_underperforming=lambda **kw: [],
        category_breakdown=lambda **kw: {},
        list_proposals=lambda **kw: [])
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_open", {"surface": "admin_tools"}, "admin1"))
    html = _last_modal(orch)
    assert "Not authorized" not in html
    assert "Admin tools" in html


def test_admin_action_denied_for_non_admin():
    chrome_events._HANDLERS = None
    orch = FakeOrch(roles=("user",))
    run(chrome_events.handle_chrome_event(
        orch, orch.ws, "chrome_admin_step_archive", {"step_id": 1}, "u1"))
    html = _last_modal(orch)
    assert "Not authorized" in html or "Admin role required" in html


def test_roles_extracted_from_resource_access_too():
    orch = FakeOrch(roles=())
    orch.ui_sessions[orch.ws] = {"resource_access": {"astral": {"roles": ["admin"]}}}
    assert "admin" in chrome_events._roles(orch, orch.ws)


# ---------------------------------------------------------------------------
# T027 — convergence: chat-created drafts are first-class in the drafts surface
# ---------------------------------------------------------------------------

class _ConvDB:
    def __init__(self):
        self.drafts = {}

    def find_gap_draft(self, user_id, chat_id, fp):
        for d in self.drafts.values():
            if d.get("gap_fingerprint") == fp and d.get("status") != "live":
                return dict(d)
        return None

    def get_draft_agent(self, draft_id):
        d = self.drafts.get(draft_id)
        return dict(d) if d else None

    def get_owned_draft_agent(self, user_id, draft_id):
        draft = self.get_draft_agent(draft_id)
        return draft if draft and draft.get("user_id") == user_id else None

    def get_decidable_drafts(self, user_id):
        return [
            dict(draft)
            for draft in self.drafts.values()
            if draft.get("user_id") == user_id and draft.get("status") != "live"
        ]

    def update_draft_agent(self, draft_id, **kw):
        self.drafts.setdefault(draft_id, {}).update(kw)
        return True

    def fetch_all(self, sql, params=()):
        return [dict(d) for d in self.drafts.values() if d.get("status") != "live"]


def test_chat_created_draft_appears_in_drafts_surface_and_discards(monkeypatch):
    from orchestrator import agentic_creation as ac
    from shared.feature_flags import flags
    from orchestrator.projection_surfaces import drafts as drafts_surface
    monkeypatch.setitem(flags._flags, "agentic_creation", True)

    db = _ConvDB()
    lifecycle_calls = []

    class _LC:
        def __init__(self):
            self.draft_store = db

        async def create_draft(self, user_id, agent_name, description, **kw):
            row = {"id": "d-conv", "user_id": user_id, "agent_name": agent_name,
                   "agent_slug": "conv", "description": description, "status": "pending",
                   "origin": kw.get("origin"),
                   "source_chat_id": kw.get("source_chat_id"),
                   "gap_fingerprint": kw.get("gap_fingerprint")}
            db.drafts["d-conv"] = row
            return dict(row)

        async def generate_code(self, draft_id, websocket=None):
            db.drafts[draft_id]["status"] = "generated"
            return dict(db.drafts[draft_id])

        async def start_draft_agent(self, draft_id, websocket=None):
            db.drafts[draft_id]["status"] = "testing"
            return dict(db.drafts[draft_id])

        async def delete_draft(self, draft_id):
            lifecycle_calls.append(("delete", draft_id))
            db.drafts.pop(draft_id, None)
            return True

    orch = types.SimpleNamespace(
        history=types.SimpleNamespace(
            db=db, create_chat=lambda user_id=None, **kw: "test-chat"),
        lifecycle_manager=_LC(),
        sent=[],
    )

    async def send_ui_render(websocket, components, target="canvas"):
        orch.sent.append((target, components))
    orch.send_ui_render = send_ui_render

    async def handle_chat_message(websocket, message, chat_id, display_message=None,
                                  user_id=None, draft_agent_id=None, selected_tools=None,
                                  attachments=None):
        await websocket.send_json({"type": "ui_render", "components": [
            {"type": "card", "title": "ok", "content": [{"type": "text", "content": "fine"}]}]})
    orch.handle_chat_message = handle_chat_message

    # 056 US2: the self-test is a machine turn — model the authority seam.
    async def derive_machine_authority(**kwargs):
        from orchestrator.chain_authority import AuthoritySkip
        return AuthoritySkip("missing_consent", "test double")
    orch.derive_machine_authority = derive_machine_authority
    orch._bind_machine_turn = lambda vws, authority: None
    orch._unbind_machine_turn = lambda vws: None

    # 1. Created from chat
    res = run(ac.handle_meta_tool(
        orch, "create_capability",
        {"agent_name": "Conv Agent", "description": "does convergence things",
         "tools_spec": [{"name": "conv_tool", "description": "x"}],
         "user_request": "do the thing"},
        user_id="u1", chat_id="c1"))
    assert res.result["status"] == "created"

    # 2. Appears in the drafts surface with its origin badge + decisions
    html = run(drafts_surface.render(orch, "u1", [], {}))
    assert "Conv Agent" in html and "from chat" in html
    detail = run(drafts_surface.render(orch, "u1", [], {"draft_id": "d-conv"}))
    assert 'data-ui-action="draft_approve"' in detail
    assert 'data-ui-action="draft_discard"' in detail

    # 3. Discardable from the surface via the SAME handler chat uses (SC-007)
    run(ac.HANDLERS["draft_discard"](orch, FakeWS(), "u1", [], {"draft_id": "d-conv"}))
    assert ("delete", "d-conv") in lifecycle_calls
    html_after = run(drafts_surface.render(orch, "u1", [], {}))
    assert "Conv Agent" not in html_after
