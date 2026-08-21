"""Feature 027 — T025: agentic creation unit/behavior tests.

DB-free: a fake orchestrator/lifecycle exercises the meta-tool handlers,
dedup (FR-007), decision handlers (ownership + audit), and the revision
apply/rollback safety (FR-006) against a real temp filesystem.
"""
import asyncio
import json
import types


from orchestrator import agentic_creation as ac


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.drafts = {}
        self.ownership = {}
        self.users = {}

    def find_gap_draft(self, user_id, chat_id, fp):
        for d in self.drafts.values():
            if (d["user_id"], d.get("source_chat_id"), d.get("gap_fingerprint")) == (user_id, chat_id, fp) \
                    and d.get("status") != "live":
                return dict(d)
        return None

    def get_draft_agent(self, draft_id):
        d = self.drafts.get(draft_id)
        return dict(d) if d else None

    def get_owned_draft_agent(self, user_id, draft_id):
        draft = self.get_draft_agent(draft_id)
        if draft is None or draft.get("user_id") != user_id:
            return None
        return draft

    def update_draft_agent(self, draft_id, **kw):
        self.drafts.setdefault(draft_id, {}).update(kw)
        return True

    def get_agent_ownership(self, agent_id):
        return self.ownership.get(agent_id)

    def get_user(self, user_id):
        return self.users.get(user_id)

    def set_agent_visibility(self, agent_id, is_public):
        ownership = self.ownership.get(agent_id)
        if ownership is None:
            return False
        ownership["is_public"] = bool(is_public)
        return True

    def get_agent_is_safe(self, _agent_id):
        return False


class FakeLifecycle:
    def __init__(self, db, agents_dir):
        self.db = db
        self.draft_store = db
        self._agents_dir = agents_dir
        self.calls = []
        self.approve_result = {"status": "live"}

    async def create_draft(
        self,
        user_id,
        agent_name,
        description,
        tools_spec=None,
        skill_tags=None,
        packages=None,
        **metadata,
    ):
        draft_id = f"draft-{len(self.db.drafts) + 1}"
        slug = agent_name.lower().replace(" ", "_").replace("(", "").replace(")", "")
        row = {
            "id": draft_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "agent_slug": slug,
            "description": description,
            "status": "pending",
            "origin": metadata.get("origin", "manual"),
            "source_chat_id": metadata.get("source_chat_id"),
            "gap_fingerprint": metadata.get("gap_fingerprint"),
            "revises_agent_id": metadata.get("revises_agent_id"),
        }
        self.db.drafts[draft_id] = row
        self.calls.append(("create_draft", agent_name))
        return dict(row)

    async def generate_code(self, draft_id, websocket=None):
        self.calls.append(("generate_code", draft_id))
        self.db.drafts[draft_id]["status"] = "generated"
        return dict(self.db.drafts[draft_id])

    async def start_draft_agent(self, draft_id, websocket=None, align_scopes=True):
        self.calls.append(("start", draft_id))
        self.db.drafts[draft_id]["status"] = "testing"
        return dict(self.db.drafts[draft_id])

    async def stop_draft_agent(self, draft_id):
        self.calls.append(("stop", draft_id))

    async def refine_agent(self, draft_id, message, websocket=None):
        self.calls.append(("refine", draft_id, message))
        self.db.drafts[draft_id]["status"] = "generated"
        return dict(self.db.drafts[draft_id])

    async def approve_agent(self, draft_id, websocket=None):
        self.calls.append(("approve", draft_id))
        self.db.drafts[draft_id]["status"] = self.approve_result["status"]
        return {**self.db.drafts[draft_id], **self.approve_result}

    async def delete_draft(self, draft_id):
        self.calls.append(("delete", draft_id))
        self.db.drafts.pop(draft_id, None)
        return True

    def _get_draft_by_agent_id(self, agent_id):
        for d in self.drafts_by_agent().items():
            pass
        slug = agent_id[:-2].replace("-", "_") if agent_id.endswith("-1") else agent_id
        for d in self.db.drafts.values():
            if d["agent_slug"] == slug:
                return dict(d)
        return None

    def drafts_by_agent(self):
        return {}


class FakeHistory:
    def __init__(self, db):
        self.db = db
        self.chats = []

    def create_chat(self, user_id="legacy", **kw):
        cid = f"chat-{len(self.chats) + 1}"
        self.chats.append(cid)
        return cid


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
    def __init__(self, agents_dir):
        self.db = FakeDB()
        self.history = FakeHistory(self.db)
        self.lifecycle_manager = FakeLifecycle(self.db, agents_dir)
        self.sent = []
        self.chat_calls = []
        self.tool_permissions = types.SimpleNamespace(
            get_agent_scopes=lambda u, a: {"tools:read": True},
            set_agent_scopes=lambda u, a, s: None,
        )

    async def send_ui_render(self, websocket, components, target="canvas"):
        self.sent.append((target, components))

    async def handle_chat_message(self, websocket, message, chat_id, display_message=None,
                                  user_id=None, draft_agent_id=None, selected_tools=None,
                                  attachments=None):
        self.chat_calls.append((message, chat_id, draft_agent_id))
        # Simulate a successful draft-test turn: one tool step + one card.
        await websocket.send_json({"type": "chat_step",
                                   "step": {"kind": "tool_call", "name": "new_tool", "status": "completed"}})
        await websocket.send_json({"type": "ui_render", "components": [
            {"type": "card", "title": "Result", "content": [{"type": "text", "content": "it worked"}]}]})


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fingerprint + injection guards
# ---------------------------------------------------------------------------

def test_fingerprint_stable_and_order_insensitive():
    a = ac.gap_fingerprint("Stock Tracker", [{"name": "track"}, {"name": "report"}])
    b = ac.gap_fingerprint("stock tracker", [{"name": "report"}, {"name": "track"}])
    assert a == b
    assert a != ac.gap_fingerprint("Stock Tracker", [{"name": "other"}])


def test_should_inject_respects_flag_and_draft_session(monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    assert ac.should_inject(None) is True
    assert ac.should_inject("draft-123") is False  # draft-test exclusion
    monkeypatch.setitem(flags._flags, "agentic_creation", False)
    assert ac.should_inject(None) is False


def test_meta_tool_definitions_shape():
    defs = ac.meta_tool_definitions()
    names = [d["function"]["name"] for d in defs]
    assert names == ["create_capability", "extend_agent"]
    cc = defs[0]["function"]["parameters"]
    assert set(cc["required"]) == {"agent_name", "description", "tools_spec", "user_request"}


# ---------------------------------------------------------------------------
# Self-test summarizer
# ---------------------------------------------------------------------------

def test_summarize_outputs_pass_and_fail():
    ok = ac._summarize_outputs([
        {"type": "chat_step", "step": {"kind": "tool_call", "name": "t1"}},
        {"type": "ui_render", "components": [{"type": "card", "title": "x",
                                              "content": [{"type": "text", "content": "data"}]}]},
    ])
    assert ok["status"] == "passed" and ok["tools_called"] == ["t1"]
    assert "data" in ok["evidence"]

    bad = ac._summarize_outputs([
        {"type": "ui_render", "components": [{"type": "alert", "variant": "error", "message": "boom"}]},
    ])
    assert bad["status"] == "failed" and bad["errors"]


# ---------------------------------------------------------------------------
# create_capability flow
# ---------------------------------------------------------------------------

def test_create_capability_happy_path(tmp_path, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    orch = FakeOrch(str(tmp_path))
    res = run(ac.handle_meta_tool(
        orch, "create_capability",
        {"agent_name": "Stock Tracker", "description": "Tracks favorite stocks daily",
         "tools_spec": [{"name": "track_stocks", "description": "track"}],
         "user_request": "track my stocks"},
        user_id="u1", chat_id="c1"))
    assert res.error is None
    assert res.result["status"] == "created"
    draft_id = res.result["draft_id"]
    # lifecycle path: create -> generate -> start -> (self-test via chat) once
    steps = [c[0] for c in orch.lifecycle_manager.calls]
    assert steps[:3] == ["create_draft", "generate_code", "start"]
    assert orch.chat_calls and orch.chat_calls[0][2] == draft_id  # draft-test self-test
    # card carries the three decisions
    card = res.ui_components[0]
    actions = [c.get("action") for c in card["content"] if c.get("type") == "button"]
    assert actions == ["draft_approve", "draft_refine", "draft_discard"]
    # provenance persisted
    row = orch.db.drafts[draft_id]
    assert row["origin"] == "auto_chat" and row["gap_fingerprint"]
    assert json.loads(row["self_test"])["status"] == "passed"


def test_create_capability_dedups_per_gap(tmp_path, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    orch = FakeOrch(str(tmp_path))
    args = {"agent_name": "Stock Tracker", "description": "Tracks favorite stocks daily",
            "tools_spec": [{"name": "track_stocks", "description": "track"}],
            "user_request": "track my stocks"}
    first = run(ac.handle_meta_tool(orch, "create_capability", dict(args), user_id="u1", chat_id="c1"))
    second = run(ac.handle_meta_tool(orch, "create_capability", dict(args), user_id="u1", chat_id="c1"))
    assert first.result["status"] == "created"
    assert second.result["status"] == "duplicate"
    assert second.result["draft_id"] == first.result["draft_id"]
    assert [c[0] for c in orch.lifecycle_manager.calls].count("create_draft") == 1


def test_create_capability_rejects_bad_args(tmp_path):
    orch = FakeOrch(str(tmp_path))
    res = run(ac.handle_meta_tool(orch, "create_capability", {"agent_name": "x"},
                                  user_id="u1", chat_id="c1"))
    assert res.error is not None


# ---------------------------------------------------------------------------
# extend_agent ownership gate
# ---------------------------------------------------------------------------

def test_extend_agent_requires_ownership(tmp_path):
    orch = FakeOrch(str(tmp_path))
    orch.db.ownership["weather-1"] = {"owner_email": "someone-else@x", "is_public": True}
    res = run(ac.handle_meta_tool(orch, "extend_agent",
                                  {"agent_id": "weather-1", "instruction": "add csv export"},
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "not_owned"
    assert res.ui_components and res.ui_components[0]["type"] == "alert"


def test_extend_agent_requires_lifecycle_managed_live_agent(tmp_path):
    orch = FakeOrch(str(tmp_path))
    orch.db.ownership["weather-1"] = {"owner_email": "u1", "is_public": False}
    res = run(ac.handle_meta_tool(orch, "extend_agent",
                                  {"agent_id": "weather-1", "instruction": "add csv export"},
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "not_revisable"


# ---------------------------------------------------------------------------
# Decision handlers
# ---------------------------------------------------------------------------

def _seed_draft(orch, user_id="u1"):
    orch.db.drafts["d1"] = {"id": "d1", "user_id": user_id, "agent_name": "T",
                            "agent_slug": "t", "description": "d", "status": "testing"}
    return orch.db.drafts["d1"]


def test_draft_approve_owner_only(tmp_path):
    orch = FakeOrch(str(tmp_path))
    _seed_draft(orch, user_id="someone_else")
    run(ac.HANDLERS["draft_approve"](orch, object(), "u1", [], {"draft_id": "d1"}))
    target, comps = orch.sent[-1]
    assert comps[0]["type"] == "alert"  # not found / not yours
    assert ("approve", "d1") not in orch.lifecycle_manager.calls


def test_draft_approve_promotes_and_confirms(tmp_path):
    orch = FakeOrch(str(tmp_path))
    _seed_draft(orch)
    run(ac.HANDLERS["draft_approve"](orch, object(), "u1", [], {"draft_id": "d1"}))
    assert ("approve", "d1") in orch.lifecycle_manager.calls
    target, comps = orch.sent[-1]
    assert target == "chat" and "is live" in comps[0]["title"]


def test_draft_approve_rejection_keeps_draft_editable(tmp_path):
    orch = FakeOrch(str(tmp_path))
    _seed_draft(orch)
    orch.lifecycle_manager.approve_result = {"status": "rejected", "error_message": "critical issue"}
    run(ac.HANDLERS["draft_approve"](orch, object(), "u1", [], {"draft_id": "d1"}))
    target, comps = orch.sent[-1]
    assert "not promoted" in comps[0]["title"]
    actions = [c.get("action") for c in comps[0]["content"] if c.get("type") == "button"]
    assert "draft_refine" in actions and "draft_discard" in actions


def test_draft_discard_deletes(tmp_path):
    orch = FakeOrch(str(tmp_path))
    _seed_draft(orch)
    run(ac.HANDLERS["draft_discard"](orch, object(), "u1", [], {"draft_id": "d1"}))
    assert ("delete", "d1") in orch.lifecycle_manager.calls
    assert "d1" not in orch.db.drafts


# ---------------------------------------------------------------------------
# apply_revision — gate + swap + rollback (FR-006)
# ---------------------------------------------------------------------------

class _Sev:
    def __init__(self, name):
        self.name = name


class _Report:
    def __init__(self, sev):
        self.max_severity = _Sev(sev)

    def to_dict(self):
        return {"max_severity": self.max_severity.name}


class _Validation:
    def __init__(self, passed):
        self.passed = passed
        self.tools_passed = 1 if passed else 0
        self.tools_tested = 1

    def to_dict(self):
        return {"passed": self.passed}


def _revision_fixture(tmp_path, gate_pass=True, start_raises=False):
    orch = FakeOrch(str(tmp_path))
    lc = orch.lifecycle_manager
    # live agent on disk
    live_dir = tmp_path / "stock_tracker"
    live_dir.mkdir()
    (live_dir / "mcp_tools.py").write_text("OLD = 1\n", encoding="utf-8")
    orch.db.drafts["live1"] = {"id": "live1", "user_id": "u1", "agent_name": "Stock Tracker",
                               "agent_slug": "stock_tracker", "description": "d", "status": "live"}
    # staged revision on disk
    rev_dir = tmp_path / "stock_tracker_revision"
    rev_dir.mkdir()
    (rev_dir / "mcp_tools.py").write_text("NEW = 2\n", encoding="utf-8")
    orch.db.drafts["rev1"] = {"id": "rev1", "user_id": "u1", "agent_name": "Stock Tracker (revision)",
                              "agent_slug": "stock_tracker_revision", "description": "rev",
                              "status": "generated", "revises_agent_id": "stock-tracker-1"}
    lc.security = types.SimpleNamespace(analyze=lambda code, filename=None: _Report("LOW"))
    lc.validator = types.SimpleNamespace(
        validate=lambda code, slug, agents_dir: _Validation(gate_pass))
    if start_raises:
        async def boom(draft_id, websocket=None, align_scopes=True):
            lc.calls.append(("start", draft_id))
            if draft_id == "live1" and ("start", "live1") not in lc.calls[:-1]:
                raise RuntimeError("restart failed")
            return dict(orch.db.drafts[draft_id])
        lc.start_draft_agent = boom
    return orch


def test_apply_revision_success_swaps_and_cleans_up(tmp_path):
    orch = _revision_fixture(tmp_path, gate_pass=True)
    out = run(ac.apply_revision(orch, orch.db.drafts["rev1"], "u1"))
    assert out["applied"] is True
    live_code = (tmp_path / "stock_tracker" / "mcp_tools.py").read_text(encoding="utf-8")
    assert "NEW = 2" in live_code
    assert not (tmp_path / "stock_tracker" / "mcp_tools.py.bak027").exists()
    assert "rev1" not in orch.db.drafts  # staged row cleaned up


def test_apply_revision_gate_failure_leaves_live_untouched(tmp_path):
    orch = _revision_fixture(tmp_path, gate_pass=False)
    out = run(ac.apply_revision(orch, orch.db.drafts["rev1"], "u1"))
    assert out["applied"] is False
    live_code = (tmp_path / "stock_tracker" / "mcp_tools.py").read_text(encoding="utf-8")
    assert "OLD = 1" in live_code  # FR-006: unchanged on gate failure
    assert orch.db.drafts["rev1"]["status"] == "rejected"  # stays editable


def test_apply_revision_swap_failure_rolls_back(tmp_path):
    orch = _revision_fixture(tmp_path, gate_pass=True, start_raises=True)
    out = run(ac.apply_revision(orch, orch.db.drafts["rev1"], "u1"))
    assert out["applied"] is False
    live_code = (tmp_path / "stock_tracker" / "mcp_tools.py").read_text(encoding="utf-8")
    assert "OLD = 1" in live_code  # backup restored
