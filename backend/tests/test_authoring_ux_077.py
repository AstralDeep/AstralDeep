"""Feature 077 — create your own agents and skills, the easy path.

Pins, in order:

1. The express lane runs the SAME gated session end to end from one description
   (SC-001) and stops at Clarify when the assistant has questions (FR-002).
2. An Analyze refusal ends the run as ``failed`` and generates nothing (SC-002).
3. Desktop presence is honest for a first-time user (SC-003, FR-005).
4. User skills: store bounds/validation, the digest, /command expansion and
   ``/help`` (SC-004, FR-008..FR-010); flag-off is inert (FR-015).
5. The surface: home view (web + native) shows status, the express lane, runs,
   skills; handlers create/edit/toggle/delete; progress pushes re-render only
   the socket that is still looking (FR-011, FR-012).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import flags  # noqa: E402
from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import agent_quick_create as qc  # noqa: E402
from orchestrator import skill_packs, slash_commands  # noqa: E402
from orchestrator import user_skills as us  # noqa: E402
from orchestrator.projection_surfaces import authoring  # noqa: E402
from tests.test_byo_authoring_flow import OWNER, make_orch  # noqa: E402
from tests.helpers.draft_store_double import InMemoryDraftStore  # noqa: E402


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    monkeypatch.setitem(flags._flags, "user_skills", True)
    qc._RUNS.clear()


@pytest.fixture()
def db():
    return InMemoryDraftStore()


class _LLM:
    """The assistant's drafts, keyed by the phase the prompt asks for."""

    def __init__(self, questions=None):
        self.questions = list(questions or [])
        self.calls = []

    def __call__(self, websocket, messages, schema=None, schema_name=None, feature=None):
        text = messages[-1]["content"]
        self.calls.append(text)
        if "Key: specification" in text:
            return {"specification": "Sorts the owner's own inbox into folders every morning."}
        if "Key: questions" in text:
            return {"questions": list(self.questions)}
        if "Keys: tools, notes" in text:
            return {"tools": [{"name": "sort_inbox", "scope": "tools:read",
                               "description": "reads my inbox and files messages"}],
                    "notes": ""}
        if "Key: tasks" in text:
            return {"tasks": ["read the inbox", "file the messages"]}
        return None


def _orch(db, llm, tmp_path, host=True):
    orch = make_orch(db)
    orch._call_llm_json = AsyncMock(side_effect=llm)
    orch.knowledge_index = SimpleNamespace(knowledge_dir=str(tmp_path))
    orch.owner_host_sockets = MagicMock(return_value=[object()] if host else [])
    orch._personal_agent_host_sessions = {}
    orch.computer_hosts = None
    orch.ui_clients = set()
    orch._open_chrome_surface = {}
    return orch


async def _settle(run: qc.QuickRun, timeout=5.0):
    if run.task is not None:
        await asyncio.wait_for(asyncio.shield(run.task), timeout)


# ── 1. the express lane ──────────────────────────────────────────────────────

async def test_one_description_becomes_a_delivered_agent(db, tmp_path):
    llm = _LLM(questions=[])
    orch = _orch(db, llm, tmp_path)
    pushes = []

    async def refresh(o, ws, user, roles, run):
        pushes.append(run.current)

    run, message = await qc.start(orch, object(), OWNER, ["user"],
                                  description="sort my inbox into folders every morning",
                                  refresh=refresh)
    assert run is not None and "Creating" in message
    assert run.agent_name == "Sort Inbox Folders Morning"   # derived, never empty
    await _settle(run)
    assert run.state == qc.DONE, run.message
    assert all(run.steps[s] == "done" for s in qc.STEPS)
    assert run.agent_id and orch.deliver_agent_bundle.await_count == 1
    # every phase went through the real machine: the session sits at generate
    row = await asyncio.to_thread(aa.get_session, orch, OWNER, run.draft_id)
    assert aa.phase_of(row) == "generate" and aa.analyze_record(row)["passed"] is True
    # progress was pushed for every step, in order
    assert pushes[:4] == ["specify", "clarify", "plan", "tasks"]
    assert pushes[-1] == "deliver"
    # the run shows up on the home view, the session does not double as an
    # editor session
    from tests.test_byo_authoring_flow import _t
    ctx = await authoring._list_context(orch, OWNER)
    assert [r.draft_id for r in ctx["runs"]] == [run.draft_id]
    assert all(s["id"] != run.draft_id for s in ctx["sessions"])
    _ = _t


async def test_the_express_lane_stops_for_the_assistants_questions(db, tmp_path):
    llm = _LLM(questions=["Which mailbox?", "What counts as junk?"])
    orch = _orch(db, llm, tmp_path)
    run, _ = await qc.start(orch, object(), OWNER, ["user"],
                            description="sort my inbox into folders every morning")
    await _settle(run)
    assert run.state == qc.NEEDS_ANSWERS
    assert [q["question"] for q in run.questions] == ["Which mailbox?", "What counts as junk?"]
    assert run.steps["clarify"] == "waiting" and run.steps["plan"] == "pending"
    orch.deliver_agent_bundle.assert_not_awaited()
    # an incomplete answer set is refused by the same hard gate the editor uses
    ok, message = await qc.resume_with_answers(orch, object(), OWNER, ["user"], run.draft_id,
                                               {"q0": "work"})
    assert ok is False and "still need an answer" in message
    assert run.state == qc.NEEDS_ANSWERS
    # complete answers resume the pipeline to the end
    ok, _ = await qc.resume_with_answers(orch, object(), OWNER, ["user"], run.draft_id,
                                         {"q0": "work", "q1": "newsletters"})
    assert ok is True
    await _settle(run)
    assert run.state == qc.DONE and orch.deliver_agent_bundle.await_count == 1
    row = await asyncio.to_thread(aa.get_session, orch, OWNER, run.draft_id)
    assert [i["answer"] for i in aa.clarify_items(row)] == ["work", "newsletters"]


async def test_an_analyze_refusal_generates_nothing(db, tmp_path):
    class _Bad(_LLM):
        def __call__(self, websocket, messages, **kw):
            text = messages[-1]["content"]
            if "Keys: tools, notes" in text:
                return {"tools": [{"name": "share_agent", "scope": "tools:write",
                                   "description": "shares the agent with others"}],
                        "notes": ""}
            return super().__call__(websocket, messages, **kw)
    orch = _orch(db, _Bad(), tmp_path)
    run, _ = await qc.start(orch, object(), OWNER, ["user"],
                            description="sort my inbox and share the agent with my team")
    await _settle(run)
    assert run.state == qc.FAILED and run.steps["analyze"] == "failed"
    assert run.outcome.get("violations"), run.message
    orch.lifecycle_manager.generate_code.assert_not_awaited()
    orch.deliver_agent_bundle.assert_not_awaited()
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "Fix in the editor" in html and "nothing was generated" in html


async def test_without_a_desktop_the_run_waits_and_resend_delivers_later(db, tmp_path, monkeypatch):
    orch = _orch(db, _LLM(), tmp_path, host=False)
    orch.deliver_agent_bundle = AsyncMock(return_value=0)   # nobody to send it to
    run, _ = await qc.start(orch, object(), OWNER, ["user"],
                            description="sort my inbox into folders every morning")
    await _settle(run)
    assert run.state == qc.WAITING_FOR_DESKTOP and run.steps["deliver"] == "waiting"
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "Resend to my desktop" in html and "No desktop client connected" in html
    # Resend re-enters generate_from_session, which (pinned in test_byo_authoring)
    # reopens the exact immutable publication without a model call; here only
    # the run's bookkeeping is under test.
    calls = []

    async def _resend(o, user, draft_id, websocket=None, **kw):
        calls.append(draft_id)
        return {"status": "delivered", "agent_id": run.agent_id}
    monkeypatch.setattr(aa, "generate_from_session", _resend)
    result = await authoring.HANDLERS["chrome_author_quick_resend"](
        orch, object(), OWNER, ["user"], {"draft_id": run.draft_id})
    assert calls == [run.draft_id]
    assert "Delivered" in result[2] and run.state == qc.DONE and run.steps["deliver"] == "done"
    orch.lifecycle_manager.generate_code.assert_awaited_once()   # the run's single model call


async def test_refusals_and_bounds(db, tmp_path, monkeypatch):
    orch = _orch(db, _LLM(), tmp_path)
    run, message = await qc.start(orch, object(), OWNER, ["user"], description="short")
    assert run is None and "10+" in message
    monkeypatch.setitem(flags._flags, "byo_agents", False)
    run, message = await qc.start(orch, object(), OWNER, ["user"],
                                  description="a perfectly good description")
    assert run is None and "not enabled" in message
    assert qc.derive_agent_name("") == "My agent"
    assert qc.derive_agent_name("please make an agent that will") == "My agent"


# ── 2. desktop presence ──────────────────────────────────────────────────────

def test_host_presence_counts_a_signed_in_desktop_before_any_tunnel():
    orch = SimpleNamespace(_tunnel_sockets={}, owner_host_sockets=lambda o: [],
                           computer_hosts=None, _personal_agent_host_sessions={})
    assert aa.host_presence(orch, OWNER) == {"online": False, "label": "your desktop client",
                                             "hosts": 0, "tunnels": False}
    sock = object()
    orch.owner_host_sockets = lambda o: [sock]
    orch._personal_agent_host_sessions = {id(sock): SimpleNamespace(platform="windows",
                                                                   client_version="0.5.0")}
    presence = aa.host_presence(orch, OWNER)
    assert presence["online"] is True and presence["label"] == "windows desktop client v0.5.0"
    orch.computer_hosts = SimpleNamespace(online_for_owner=lambda o: [SimpleNamespace(name="RyzenRoll")])
    assert aa.host_presence(orch, OWNER)["label"] == "RyzenRoll"
    orch.owner_host_sockets = lambda o: []
    orch._tunnel_sockets = {(OWNER, "ua-1"): object()}
    assert aa.host_online(orch, OWNER) is True


# ── 3. skills ────────────────────────────────────────────────────────────────

def test_skill_store_saves_lists_toggles_and_deletes(tmp_path):
    store = us.UserSkillStore(str(tmp_path))
    skill = store.save(OWNER, name="Weekly status", instructions="Three bullets, then risks.",
                       applies_to="", command="/status", reserved_commands=["help"])
    assert skill.slug == "weekly-status" and skill.command == "status" and skill.always
    assert store.list(OWNER)[0] == skill
    assert (tmp_path / "user_skills").is_dir()
    path = next((tmp_path / "user_skills").rglob("weekly-status.md"))
    text = path.read_text(encoding="utf-8")
    assert "type: user_skill" in text and "applies_to: [always]" in text
    # a different owner sees nothing; the owner's directory name is a hash
    assert store.list("someone-else") == [] and OWNER not in str(path)
    scoped = store.save(OWNER, name="Summarizer voice", instructions="Be terse, cite sources.",
                        applies_to="summarizer-1, web-research-1")
    assert scoped.applies_to == ("summarizer-1", "web-research-1") and not scoped.always
    off = store.set_enabled(OWNER, "weekly-status", False)
    assert off is not None and off.enabled is False and store.command_map(OWNER) == {}
    assert store.set_enabled(OWNER, "weekly-status", True).command == "status"
    assert set(store.command_map(OWNER)) == {"status"}
    edited = store.save(OWNER, name="Weekly status", instructions="Four bullets.",
                        applies_to="", command="stat", slug="weekly-status")
    assert edited.instructions == "Four bullets." and set(store.command_map(OWNER)) == {"stat"}
    assert store.delete(OWNER, "weekly-status") is True and store.delete(OWNER, "weekly-status") is False
    assert [s.slug for s in store.list(OWNER)] == ["summarizer-voice"]


def test_skill_store_validation(tmp_path):
    store = us.UserSkillStore(str(tmp_path))
    with pytest.raises(us.SkillValidationError, match="name"):
        store.save(OWNER, name="x", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="instructions"):
        store.save(OWNER, name="Fine", instructions="short", applies_to="")
    with pytest.raises(us.SkillValidationError, match="built-in"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
                   command="help", reserved_commands=slash_commands.reserved_names())
    with pytest.raises(us.SkillValidationError, match="command is"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
                   command="Bad Name!")
    with pytest.raises(us.SkillValidationError, match="not an agent id"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.",
                   applies_to="../etc")
    store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
               command="go")
    with pytest.raises(us.SkillValidationError, match="already have"):
        store.save(OWNER, name="fine", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="already used"):
        store.save(OWNER, name="Other", instructions="Long enough instructions.", applies_to="",
                   command="go")
    for i in range(us.MAX_SKILLS - 1):
        store.save(OWNER, name=f"Skill {i}", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="up to"):
        store.save(OWNER, name="One too many", instructions="Long enough instructions.", applies_to="")


def test_skills_reach_the_digest_and_the_slash_expansion(tmp_path, monkeypatch):
    orch = SimpleNamespace(knowledge_index=SimpleNamespace(knowledge_dir=str(tmp_path)))
    store = us.store_for(orch)
    store.save(OWNER, name="House style", instructions="Always answer in British English.",
               applies_to="")
    store.save(OWNER, name="Research depth", instructions="Cite at least three sources.",
               applies_to="web-research-1")
    store.save(OWNER, name="Standup", instructions="Yesterday / today / blockers, one line each.",
               applies_to="", command="standup")

    index = SimpleNamespace(get_techniques_for_agent=lambda aid: "")
    digest = skill_packs.build_skill_digest(index, ["summarizer-1"], orch=orch, owner=OWNER)
    assert "Your skill: House style" in digest and "Your skill: Standup" in digest
    assert "Research depth" not in digest                      # scoped to another agent
    digest = skill_packs.build_skill_digest(index, ["web-research-1"], orch=orch, owner=OWNER)
    assert "Research depth" in digest
    assert skill_packs.build_skill_digest(index, ["summarizer-1"]) == ""   # no owner: as before
    assert skill_packs.build_skill_digest(index, ["summarizer-1"], orch=orch, owner="nobody") == ""

    commands = store.command_map(OWNER)
    expanded = slash_commands.expand_message("/standup fixed the build", commands)
    assert "Standup" in expanded and "one line each" in expanded and "fixed the build" in expanded
    assert slash_commands.expand_message("/standup", commands).endswith("asking for any input it needs.")
    assert slash_commands.expand_message("/help", commands).count("/standup") == 1
    assert "/standup" in slash_commands.expand_message("/nope", commands)
    assert slash_commands.expand_message("/weather Lexington", commands).startswith("What's the current weather")
    assert slash_commands.expand_message("/usr/local/bin", commands) == "/usr/local/bin"
    assert slash_commands.expand_message("/standup x") != "/standup x"   # unchanged without skills? no:
    assert slash_commands.expand_message("/standup x", None).startswith("The user typed an unrecognized")

    store.set_enabled(OWNER, "house-style", False)
    assert "House style" not in skill_packs.build_skill_digest(index, ["summarizer-1"], orch=orch, owner=OWNER)
    monkeypatch.setitem(flags._flags, "user_skills", False)
    assert us.store_for(orch) is None
    assert skill_packs.build_skill_digest(index, ["summarizer-1"], orch=orch, owner=OWNER) == ""


# ── 4. the surface ───────────────────────────────────────────────────────────

async def test_home_view_web_and_native(db, tmp_path):
    orch = _orch(db, _LLM(), tmp_path)
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "Desktop client connected" in html
    assert "chrome_author_quick_create" in html and "Create</button>" in html
    assert "Advanced: build it step by step" in html and "chrome_author_start" in html
    assert "Your skills" in html and "chrome_skill_save" in html
    assert 'data-astral-commands="[]"' in html
    assert "share" not in html.lower().replace("shared", "")   # no share/publish affordance
    comps = await authoring.components(orch, OWNER, ["user"], {})
    kinds = [(c["type"], c.get("submit_action")) for c in comps]
    assert ("alert", None) == kinds[0]
    submits = [k[1] for k in kinds if k[1]]
    assert submits == ["chrome_author_quick_create", "chrome_author_start", "chrome_skill_save"]
    # skills through the handlers
    result = await authoring.HANDLERS["chrome_skill_save"](orch, object(), OWNER, ["user"], {
        "fields": {"skill_name": "Standup", "skill_command": "standup", "skill_applies": "",
                   "skill_instructions": "Yesterday / today / blockers."}})
    assert "Saved" in result[2] and "/standup" in result[2]
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "/standup" in html and 'data-astral-commands="[{' in html
    result = await authoring.HANDLERS["chrome_skill_save"](orch, object(), OWNER, ["user"], {
        "fields": {"skill_name": "Standup", "skill_command": "help", "skill_applies": "",
                   "skill_instructions": "Yesterday / today / blockers."}})
    assert "built-in" in result[2]
    result = await authoring.HANDLERS["chrome_skill_edit"](orch, object(), OWNER, ["user"],
                                                          {"slug": "standup"})
    assert result[1] == {"skill_slug": "standup"}
    html = await authoring.render(orch, OWNER, ["user"], result[1])
    assert "Edit skill" in html and 'name="skill_slug" value="standup"' in html
    comps = await authoring.components(orch, OWNER, ["user"], result[1])
    form = [c for c in comps if c.get("submit_action") == "chrome_skill_save"][0]
    assert form["submit_payload"] == {"skill_slug": "standup"}
    result = await authoring.HANDLERS["chrome_skill_toggle"](orch, object(), OWNER, ["user"],
                                                            {"slug": "standup", "enabled": False})
    assert "now off" in result[2]
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert 'data-astral-commands="[]"' in html                     # disabled ⇒ not advertised
    result = await authoring.HANDLERS["chrome_skill_delete"](orch, object(), OWNER, ["user"],
                                                            {"slug": "standup"})
    assert "deleted" in result[2] and us.store_for(orch).list(OWNER) == []


async def test_progress_pushes_only_while_the_person_is_looking(db, tmp_path, monkeypatch):
    from orchestrator import chrome_events
    orch = _orch(db, _LLM(), tmp_path)
    ws = object()
    orch.ui_clients = {ws}
    rendered = []

    async def _render(o, websocket, user_id, roles, key, params, notice):
        rendered.append(key)
    monkeypatch.setattr(chrome_events, "_render_surface", _render)
    run = qc.QuickRun(owner=OWNER, draft_id="d", agent_name="x")
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == []                                          # nothing open
    chrome_events._note_open_surface(orch, ws, authoring.SURFACE_KEY)
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]
    chrome_events._note_open_surface(orch, ws, "agents")            # navigated away
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]
    chrome_events._note_open_surface(orch, ws, authoring.SURFACE_KEY)
    orch.ui_clients = set()                                          # disconnected
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]


async def test_quick_create_handler_and_dismiss(db, tmp_path):
    orch = _orch(db, _LLM(), tmp_path)
    result = await authoring.HANDLERS["chrome_author_quick_create"](
        orch, object(), OWNER, ["user"], {"fields": {"description": "sort my inbox every morning"}})
    assert "Creating" in result[2]
    run = qc.runs_for(OWNER)[0]
    await _settle(run)
    assert run.state == qc.DONE
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "Running on" in html and "Dismiss" in html
    await authoring.HANDLERS["chrome_author_quick_dismiss"](orch, object(), OWNER, ["user"],
                                                            {"draft_id": run.draft_id})
    assert qc.runs_for(OWNER) == []


def test_step_editor_copy_and_stale_pass_warning(db):
    from tests.test_byo_authoring_flow import _plan_fields  # noqa: F401 — shared helpers
    orch = make_orch(db)
    row = {"phase": "clarify", "clarify_answers": None, "agent_name": "x", "state_revision": 1}
    assert "Find open questions" in authoring._phase_body(row, "clarify")
    assert "Find open questions" in authoring._phase_actions("d", "clarify", 1)
    assert "Ask the assistant" in authoring._phase_actions("d", "plan", 1)
    # a passed session whose artifacts changed afterwards: Generate is replaced
    stale = authoring._phase_actions("d", "generate", 1, stale=True)
    assert "chrome_author_generate" not in stale and "Re-run Analyze" in stale
    fresh = authoring._phase_actions("d", "generate", 1, stale=False)
    assert "chrome_author_generate" in fresh
    passed = {"phase": "generate", "analyze_result": json.dumps({"passed": True, "constitution_version": "v"}),
              "plan_json": "{}", "agent_name": "x", "description": "d"}
    assert "Re-run Analyze before generating" in authoring._phase_body(passed, "generate", orch)


# ── 5. the static code gate names builtins, not every method called compile ──

def test_code_gate_flags_builtins_but_not_library_methods_of_the_same_name():
    from orchestrator.code_security import CodeSecurityAnalyzer, Severity, blocks_execution
    analyzer = CodeSecurityAnalyzer()
    ok = analyzer.analyze(
        "import re\n"
        "PATTERN = re.compile(r'(\\\\d+)d(\\\\d+)')\n"
        "def roll(expr):\n"
        "    m = PATTERN.match(expr)\n"
        "    return m.groups() if m else None\n",
        filename="ok/mcp_tools.py")
    assert not blocks_execution(ok), [f.message for f in ok.findings]
    bad = analyzer.analyze("def run(src):\n    return compile(src, 'x', 'exec')\n", filename="bad.py")
    assert blocks_execution(bad) and bad.max_severity == Severity.CRITICAL
    sneaky = analyzer.analyze("import builtins\n"
                              "def run(src):\n    return builtins.eval(src)\n", filename="sneaky.py")
    assert blocks_execution(sneaky)
    dunder = analyzer.analyze("def run(src):\n    return __builtins__.exec(src)\n", filename="dunder.py")
    assert blocks_execution(dunder)
    system = analyzer.analyze("import os\ndef run(c):\n    return os.system(c)\n", filename="os.py")
    assert blocks_execution(system)
