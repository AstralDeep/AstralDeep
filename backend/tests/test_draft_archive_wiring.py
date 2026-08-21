"""C-N4 (033) — evolutionary draft-archive *wiring* integration tests.

These drive the REAL agentic-creation lifecycle path (``_create_capability``
and the approval handler), not the pure ``draft_archive`` functions (those are
covered by ``test_draft_archive.py``). The assertions verify the behaviour the
wiring promises:

* flag ON + high-surrogate generated code  → the expensive behavioural
  self-test is SKIPPED (no draft-test chat turn happens) and the draft is
  archived as a future exemplar;
* flag OFF                                  → the self-test runs exactly as
  before (a draft-test chat turn happens) and nothing is archived;
* approval of a live draft archives it (flag ON).

The fake orchestrator writes a real ``mcp_tools.py`` to a temp agents dir so the
surrogate predictor (which reads that file from disk) has something to score —
matching how ``agent_lifecycle.generate_code`` writes the file in production.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import agentic_creation as ac  # noqa: E402
from orchestrator import draft_archive as da  # noqa: E402


# A draft that the surrogate rubric scores HIGH (>= 0.85): tool registry +
# docstring + dict return + try/except + reasonable length.
HIGH_SURROGATE_CODE = '''
"""A small, well-formed example agent for the archive wiring test."""

TOOL_REGISTRY = {}


def register_tool(name):
    """Register a tool by name into the module registry."""
    def deco(fn):
        TOOL_REGISTRY[name] = fn
        return fn
    return deco


@register_tool("do_thing")
def do_thing(params):
    """Do the thing and return a component dict (create_ui_response shape)."""
    try:
        value = int(params.get("x", 0)) + 1
        components = [{"type": "text", "content": str(value)}]
        return {"_ui_components": components, "_data": {"value": value}}
    except Exception:
        return {"_ui_components": [{"type": "text", "content": "error"}], "_data": {}}
'''


# ---------------------------------------------------------------------------
# Fakes (DB-free; write a real tools file so the surrogate has code to read)
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.drafts = {}
        self.ownership = {}
        self.users = {}

    def find_gap_draft(self, user_id, chat_id, fp):
        for d in self.drafts.values():
            if (d["user_id"], d.get("source_chat_id"), d.get("gap_fingerprint")) == \
                    (user_id, chat_id, fp) and d.get("status") != "live":
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

    def get_agent_ownership(self, agent_id):
        return self.ownership.get(agent_id)

    def get_user(self, user_id):
        return self.users.get(user_id)


class FakeLifecycle:
    """Writes a real ``<agents_dir>/<slug>/mcp_tools.py`` on generate."""

    def __init__(self, db, agents_dir, code=HIGH_SURROGATE_CODE):
        self.db = db
        self.draft_store = db
        self._agents_dir = agents_dir
        self._code = code
        self.calls = []
        self.approve_result = {"status": "live"}

    async def create_draft(self, user_id, agent_name, description, tools_spec=None,
                           skill_tags=None, packages=None, *, origin="manual",
                           source_chat_id=None, gap_fingerprint=None, **kwargs):
        draft_id = str(uuid.uuid4())
        slug = agent_name.lower().replace(" ", "_").replace("(", "").replace(")", "")
        row = {"id": draft_id, "user_id": user_id, "agent_name": agent_name,
               "draft_uuid": draft_id, "state_revision": 0,
               "agent_slug": slug, "description": description, "status": "pending"}
        row.update({"origin": origin, "source_chat_id": source_chat_id,
                    "gap_fingerprint": gap_fingerprint})
        self.db.drafts[draft_id] = row
        self.calls.append(("create_draft", agent_name))
        return dict(row)

    async def generate_code(self, draft_id, websocket=None):
        self.calls.append(("generate_code", draft_id))
        row = self.db.drafts[draft_id]
        agent_dir = os.path.join(self._agents_dir, row["agent_slug"])
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "mcp_tools.py"), "w", encoding="utf-8") as fh:
            fh.write(self._code)
        row["status"] = "generated"
        return dict(row)

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
        slug = agent_id[:-2].replace("-", "_") if agent_id.endswith("-1") else agent_id
        for d in self.db.drafts.values():
            if d["agent_slug"] == slug:
                return dict(d)
        return None


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
    def __init__(self, agents_dir, code=HIGH_SURROGATE_CODE):
        self.db = FakeDB()
        self.history = FakeHistory(self.db)
        self.lifecycle_manager = FakeLifecycle(self.db, agents_dir, code=code)
        self.sent = []
        self.chat_calls = []
        self.tool_permissions = types.SimpleNamespace(
            get_agent_scopes=lambda u, a: {"tools:read": True},
            set_agent_scopes=lambda u, a, s: None,
            get_tool_scope_map=lambda a: {},
        )
        self._ws_active_chat = {}

    async def send_ui_render(self, websocket, components, target="canvas"):
        self.sent.append((target, components))

    async def _send_or_replace_components(self, websocket, components, chat_id, user_id=None):
        self.sent.append(("replace", components))

    async def handle_chat_message(self, websocket, message, chat_id, display_message=None,
                                  user_id=None, draft_agent_id=None, selected_tools=None,
                                  attachments=None):
        # A real draft-test self-test turn — its presence is the signal that the
        # expensive self-test ran (so a skip means this list stays empty).
        self.chat_calls.append((message, chat_id, draft_agent_id))
        await websocket.send_json({"type": "chat_step",
                                   "step": {"kind": "tool_call", "name": "do_thing",
                                            "status": "completed"}})
        await websocket.send_json({"type": "ui_render", "components": [
            {"type": "card", "title": "Result",
             "content": [{"type": "text", "content": "it worked"}]}]})


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _isolate_archive():
    """Each test starts with an empty in-process archive."""
    da.reset_archive()
    yield
    da.reset_archive()


def _create_args():
    return {"agent_name": "Pdf Reader", "description": "Reads and extracts pdf tables daily",
            "tools_spec": [{"name": "read_pdf", "description": "extract pdf"}],
            "user_request": "read my pdf"}


# ---------------------------------------------------------------------------
# Flag OFF (default): self-test RUNS, nothing archived — unchanged behaviour
# ---------------------------------------------------------------------------

def test_flag_off_runs_self_test_and_does_not_archive(tmp_path, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    monkeypatch.delenv("FF_DRAFT_ARCHIVE", raising=False)
    assert da.archive_enabled() is False

    orch = FakeOrch(str(tmp_path))
    res = run(ac.handle_meta_tool(orch, "create_capability", _create_args(),
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "created"
    draft_id = res.result["draft_id"]

    # The behavioural self-test ran (a draft-test chat turn happened).
    assert orch.chat_calls, "flag OFF must run the real self-test (chat turn)"
    assert orch.chat_calls[0][2] == draft_id
    st = json.loads(orch.db.drafts[draft_id]["self_test"])
    assert st["status"] == "passed"
    assert not st.get("self_test_skipped")
    # Nothing archived while the flag is off.
    assert da.get_archive("u1") == []


# ---------------------------------------------------------------------------
# Flag ON + high surrogate score: self-test SKIPPED, draft archived
# ---------------------------------------------------------------------------

def test_flag_on_high_surrogate_skips_self_test_and_archives(tmp_path, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")
    assert da.archive_enabled() is True
    # Sanity: the generated code really does clear the skip threshold.
    assert da.surrogate_score(HIGH_SURROGATE_CODE) >= 0.85
    assert da.should_skip_self_test(HIGH_SURROGATE_CODE, min_score=0.85) is False

    orch = FakeOrch(str(tmp_path))
    res = run(ac.handle_meta_tool(orch, "create_capability", _create_args(),
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "created"
    draft_id = res.result["draft_id"]

    # THE SKIP ACTUALLY HAPPENED: no draft-test chat turn was executed.
    assert orch.chat_calls == [], "high-surrogate draft must skip the self-test"
    st = json.loads(orch.db.drafts[draft_id]["self_test"])
    assert st["status"] == "passed"
    assert st.get("self_test_skipped") is True
    assert st.get("surrogate_score", 0) >= 0.85

    # The passing draft was archived as a future exemplar.
    archive = da.get_archive("u1")
    assert len(archive) == 1
    rec = archive[0]
    assert rec.score > 0
    assert "do_thing" in rec.code
    # The archived fingerprint carries the capability's human terms (so the
    # Jaccard ranker can match a later, similar gap).
    assert "pdf" in rec.fingerprint.lower()


# A mid-range draft: registry present (+0.25) and length reward (+0.20) →
# ~0.45, which is above the cheap-reject floor (0.25) but below the
# high-confidence skip threshold (0.85), so it falls through to the real test.
MID_SURROGATE_CODE = "TOOL_REGISTRY = {}\ndef f(p):\n    return p\n" + "y = 2\n" * 30


def test_flag_on_mid_surrogate_still_runs_self_test(tmp_path, monkeypatch):
    """A mid-range draft scores between the cheap-reject floor and the
    high-confidence threshold, so NEITHER skip path fires even with the flag
    on — the real behavioural self-test runs."""
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")

    s = da.surrogate_score(MID_SURROGATE_CODE)
    assert 0.25 <= s < 0.85, f"fixture must be mid-range, got {s}"
    assert da.should_skip_self_test(MID_SURROGATE_CODE) is False
    orch = FakeOrch(str(tmp_path), code=MID_SURROGATE_CODE)
    res = run(ac.handle_meta_tool(orch, "create_capability", _create_args(),
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "created"
    # Self-test ran (chat turn happened) because the surrogate was mid-range.
    assert orch.chat_calls, "mid-surrogate draft must NOT skip the self-test"


def test_flag_on_very_weak_cheap_rejects_before_self_test(tmp_path, monkeypatch):
    """A draft the surrogate predicts will FAIL (below the reject floor) is
    cheap-rejected: the costly self-test is skipped with a FAILING verdict
    (no chat turn), which routes into the normal auto-refine loop."""
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "agentic_creation", True)
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")

    weak = "a = 1\nb = 2\nc = a + b\nprint(c)\nd = c * 3\ne = d - 1\nf = e + 7\n"
    assert da.should_skip_self_test(weak) is True  # below the reject floor
    orch = FakeOrch(str(tmp_path), code=weak)
    res = run(ac.handle_meta_tool(orch, "create_capability", _create_args(),
                                  user_id="u1", chat_id="c1"))
    assert res.result["status"] == "created"
    # NO behavioural self-test ran on any attempt (cheap-reject skipped it).
    assert orch.chat_calls == [], "very-weak draft must be cheap-rejected (no self-test)"
    st = json.loads(orch.db.drafts[res.result["draft_id"]]["self_test"])
    assert st["status"] == "failed"
    assert st.get("self_test_skipped") is True
    # A predicted-failure draft is NOT archived as an exemplar.
    assert da.get_archive("u1") == []


# ---------------------------------------------------------------------------
# Exemplar conditioning: an archived exemplar feeds the next codegen prompt
# ---------------------------------------------------------------------------

def test_archived_exemplar_conditions_next_codegen_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")
    # Seed the archive with a successful pdf-reader exemplar.
    draft_uuid = str(uuid.uuid4())
    da.record_archived_draft(
        "read pdf table extract",
        HIGH_SURROGATE_CODE,
        0.95,
        owner_user_id="u1",
        draft_uuid=draft_uuid,
        source_state_revision=3,
    )
    base = "GENERATE THE AGENT"
    out = da.exemplar_prompt_for(
        base,
        "read pdf table for a report",
        owner_user_id="u1",
        k=3,
    )
    assert out.startswith(base)
    assert "## Exemplars from past successful agents" in out
    assert "do_thing" in out  # the exemplar's code was embedded

    # Flag OFF → conditioning is inert (prompt unchanged).
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "false")
    assert da.exemplar_prompt_for(
        base,
        "read pdf table for a report",
        owner_user_id="u1",
    ) == base


def test_archive_is_owner_scoped_and_idempotent(monkeypatch):
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")
    draft_uuid = str(uuid.uuid4())
    arguments = {
        "owner_user_id": "owner-a",
        "draft_uuid": draft_uuid,
        "source_state_revision": 9,
    }

    first = da.record_archived_draft(
        "read pdf table",
        HIGH_SURROGATE_CODE,
        0.95,
        **arguments,
    )
    replay = da.record_archived_draft(
        "read pdf table",
        HIGH_SURROGATE_CODE,
        0.95,
        **arguments,
    )
    conflicting = da.record_archived_draft(
        "read pdf table",
        HIGH_SURROGATE_CODE + "\n# changed",
        0.95,
        **arguments,
    )

    assert first is not None
    assert replay is first
    assert conflicting is None
    assert da.get_archive("owner-a") == [first]
    assert da.get_archive("owner-b") == []
    assert "do_thing" in da.exemplar_prompt_for(
        "BASE",
        "read pdf",
        owner_user_id="owner-a",
    )
    assert da.exemplar_prompt_for(
        "BASE",
        "read pdf",
        owner_user_id="owner-b",
    ) == "BASE"


# ---------------------------------------------------------------------------
# Approval archives a live draft (flag ON)
# ---------------------------------------------------------------------------

def test_approval_archives_live_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("FF_DRAFT_ARCHIVE", "true")
    orch = FakeOrch(str(tmp_path))
    # Seed an already-generated, owned draft with code on disk.
    slug = "pdf_reader"
    agent_dir = tmp_path / slug
    agent_dir.mkdir()
    (agent_dir / "mcp_tools.py").write_text(HIGH_SURROGATE_CODE, encoding="utf-8")
    draft_uuid = str(uuid.uuid4())
    orch.db.drafts[draft_uuid] = {"id": draft_uuid, "draft_uuid": draft_uuid,
                            "state_revision": 7, "user_id": "u1", "agent_name": "Pdf Reader",
                            "agent_slug": slug, "description": "reads pdf tables",
                            "status": "testing", "gap_fingerprint": "read pdf",
                            "self_test": json.dumps({"status": "passed"})}

    monkeypatch.setenv("FF_REDTEAM_SELFTEST", "false")  # skip the red-team gate
    run(ac.HANDLERS["draft_approve"](
        orch, object(), "u1", [], {"draft_id": draft_uuid}
    ))

    assert ("approve", draft_uuid) in orch.lifecycle_manager.calls
    archive = da.get_archive("u1")
    assert len(archive) == 1
    assert "do_thing" in archive[0].code
