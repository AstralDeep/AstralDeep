"""Tests for the personal-agent authoring UX across agent_authoring.py,
agent_quick_create.py, user_skills.py, and slash_commands.py: the express-creation
lane, desktop presence, skill-store bounds, and scoped progress pushes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

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
from tests.test_work_control_authority_088 import (  # noqa: E402
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, incoming,
)


@pytest.fixture(autouse=True)
def _flags_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "byo_agents", True)
    monkeypatch.setitem(flags._flags, "user_skills", True)
    qc._RUNS.clear()


@pytest.fixture()
def db():
    return InMemoryDraftStore()


@pytest.fixture
def skill_authority(bound, fixture):
    from orchestrator.human_request_authority import (
        HumanRequestBoundary, authenticate_current_human_request, bind_human_caller,
    )

    @asynccontextmanager
    async def select(orch, *, method="POST", other_owner=None):
        original = bound[0].orch
        orch.runtime_composition = original.runtime_composition
        orch.audit_repo = original.audit_repo
        orch.web_sessions = original.web_sessions
        boundary = HumanRequestBoundary(orch)
        orch.human_request_boundary = boundary
        credentials = fixture
        if other_owner is not None:
            credentials = (*fixture[:3], lambda: fixture[3](sub=other_owner), fixture[4])
        request = incoming(bound, credentials, method=method, cookie=other_owner is None)
        request.scope["app"] = SimpleNamespace(state=SimpleNamespace(orchestrator=orch))
        try:
            caller = await authenticate_current_human_request(request, boundary=boundary)
            with bind_human_caller(caller):
                yield caller
        finally:
            boundary.close()

    return SimpleNamespace(owner=fixture[1], select=select)


class _LLM:
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


async def test_one_description_becomes_a_delivered_agent(db, tmp_path, skill_authority):
    llm = _LLM(questions=[])
    orch = _orch(db, llm, tmp_path)
    owner = skill_authority.owner
    async with skill_authority.select(orch):
        pushes = []

        async def refresh(o, ws, user, roles, run):
            pushes.append(run.current)

        run, message = await qc.start(orch, object(), owner, ["user"],
                                      description="sort my inbox into folders every morning",
                                      refresh=refresh)
        assert run is not None and "Creating" in message
        assert run.agent_name == "Sort Inbox Folders Morning"
        await _settle(run)
        assert run.state == qc.DONE, run.message
        assert all(run.steps[s] == "done" for s in qc.STEPS)
        assert run.agent_id and orch.deliver_agent_bundle.await_count == 1
        row = await asyncio.to_thread(aa.get_session, orch, owner, run.draft_id)
        assert aa.phase_of(row) == "generate" and aa.analyze_record(row)["passed"] is True
        assert pushes[:4] == ["specify", "clarify", "plan", "tasks"]
        assert pushes[-1] == "deliver"
        from tests.test_byo_authoring_flow import _t
        ctx = await authoring._list_context(orch, owner)
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
    ok, message = await qc.resume_with_answers(orch, object(), OWNER, ["user"], run.draft_id,
                                               {"q0": "work"})
    assert ok is False and "still need an answer" in message
    assert run.state == qc.NEEDS_ANSWERS
    ok, _ = await qc.resume_with_answers(orch, object(), OWNER, ["user"], run.draft_id,
                                         {"q0": "work", "q1": "newsletters"})
    assert ok is True
    await _settle(run)
    assert run.state == qc.DONE and orch.deliver_agent_bundle.await_count == 1
    row = await asyncio.to_thread(aa.get_session, orch, OWNER, run.draft_id)
    assert [i["answer"] for i in aa.clarify_items(row)] == ["work", "newsletters"]


async def test_an_analyze_refusal_generates_nothing(db, tmp_path, skill_authority):
    class _Bad(_LLM):
        def __call__(self, websocket, messages, **kw):
            text = messages[-1]["content"]
            if "Keys: tools, notes" in text:
                return {"tools": [{"name": "share_agent", "scope": "tools:write",
                                   "description": "shares the agent with others"}],
                        "notes": ""}
            return super().__call__(websocket, messages, **kw)
    orch = _orch(db, _Bad(), tmp_path)
    owner = skill_authority.owner
    async with skill_authority.select(orch):
        run, _ = await qc.start(orch, object(), owner, ["user"],
                                description="sort my inbox and share the agent with my team")
        await _settle(run)
        assert run.state == qc.FAILED and run.steps["analyze"] == "failed"
        assert run.outcome.get("violations"), run.message
        orch.lifecycle_manager.generate_code.assert_not_awaited()
        orch.deliver_agent_bundle.assert_not_awaited()
        html = await authoring.render(orch, owner, ["user"], {})
        assert "Fix in the editor" in html and "nothing was generated" in html


async def test_without_a_desktop_the_run_waits_and_resend_delivers_later(db, tmp_path, monkeypatch, skill_authority):
    orch = _orch(db, _LLM(), tmp_path, host=False)
    owner = skill_authority.owner
    async with skill_authority.select(orch):
        orch.deliver_agent_bundle = AsyncMock(return_value=0)
        run, _ = await qc.start(orch, object(), owner, ["user"],
                                description="sort my inbox into folders every morning")
        await _settle(run)
        assert run.state == qc.WAITING_FOR_DESKTOP and run.steps["deliver"] == "waiting"
        html = await authoring.render(orch, owner, ["user"], {})
        assert "Resend to my desktop" in html and "No desktop client connected" in html
        calls = []

        async def _resend(o, user, draft_id, websocket=None, **kw):
            calls.append(draft_id)
            return {"status": "delivered", "agent_id": run.agent_id}
        monkeypatch.setattr(aa, "generate_from_session", _resend)
        result = await authoring.HANDLERS["chrome_author_quick_resend"](
            orch, object(), owner, ["user"], {"draft_id": run.draft_id})
        assert calls == [run.draft_id]
        assert "Delivered" in result[2] and run.state == qc.DONE and run.steps["deliver"] == "done"
        orch.lifecycle_manager.generate_code.assert_awaited_once()


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


async def test_skills_reach_the_digest_and_the_slash_expansion(tmp_path, monkeypatch, skill_authority):
    orch = SimpleNamespace(knowledge_index=SimpleNamespace(knowledge_dir=str(tmp_path)))
    index = SimpleNamespace(get_techniques_for_agent=lambda aid: "")
    async with skill_authority.select(orch) as caller:
        store = us.store_for(orch)
        for name, instructions, applies_to, command in (
            ("House style", "Always answer in British English.", "", ""),
            ("Research depth", "Cite at least three sources.", "web-research-1", ""),
            ("Standup", "Yesterday / today / blockers, one line each.", "", "standup"),
        ):
            await store.save(caller=caller, skill_id=str(uuid4()), command_id=str(uuid4()),
                             expected_revision=0, name=name, instructions=instructions,
                             applies_to=applies_to, command=command)
        skills = await store.list(caller=caller)
        digest = skill_packs.build_skill_digest(index, ["summarizer-1"], user_skills=skills)
        assert "Your skill: House style" in digest and "Your skill: Standup" in digest
        assert "Research depth" not in digest
        digest = skill_packs.build_skill_digest(index, ["web-research-1"], user_skills=skills)
        assert "Research depth" in digest
        assert skill_packs.build_skill_digest(index, ["summarizer-1"]) == ""

        commands = {skill.command: skill for skill in skills if skill.enabled and skill.command}
        expanded = slash_commands.expand_message("/standup fixed the build", commands)
        assert "Standup" in expanded and "one line each" in expanded and "fixed the build" in expanded
        assert slash_commands.expand_message("/standup", commands).endswith("asking for any input it needs.")
        assert slash_commands.expand_message("/help", commands).count("/standup") == 1
        assert "/standup" in slash_commands.expand_message("/nope", commands)
        assert slash_commands.expand_message("/weather Lexington", commands).startswith("What's the current weather")
        assert slash_commands.expand_message("/usr/local/bin", commands) == "/usr/local/bin"
        assert slash_commands.expand_message("/standup x") != "/standup x"
        assert slash_commands.expand_message("/standup x", None).startswith("The user typed an unrecognized")

        house = next(skill for skill in skills if skill.slug == "house-style")
        await store.set_enabled(caller=caller, skill_id=house.skill_id, command_id=str(uuid4()),
                                expected_revision=house.revision, enabled=False)
        current = await store.list(caller=caller)
        assert "House style" not in skill_packs.build_skill_digest(index, ["summarizer-1"], user_skills=current)
    async with skill_authority.select(orch, method="GET", other_owner=str(uuid4())) as caller:
        assert await store.list(caller=caller) == ()
        assert skill_packs.build_skill_digest(index, ["summarizer-1"],
                                             user_skills=await store.list(caller=caller)) == ""
    monkeypatch.setitem(flags._flags, "user_skills", False)
    assert us.store_for(orch) is None
    assert skill_packs.build_skill_digest(index, ["summarizer-1"]) == ""


async def test_home_view_web_and_native(db, tmp_path, skill_authority):
    from persistent_agents.models import AssignmentError

    orch = _orch(db, _LLM(), tmp_path)
    owner = skill_authority.owner
    async with skill_authority.select(orch) as caller:
        html = await authoring.render(orch, owner, ["user"], {})
        assert "Desktop host connected" in html
        assert "chrome_author_quick_create" in html and "Create</button>" in html
        assert "Advanced: build it step by step" in html and "chrome_author_start" in html
        assert "Your skills" in html and "chrome_user_skill_save" in html
        assert 'data-astral-commands="[]"' in html
        assert "share" not in html.lower().replace("shared", "")
        comps = await authoring.components(orch, owner, ["user"], {})
        kinds = [(c["type"], c.get("submit_action")) for c in comps]
        assert ("alert", None) == kinds[0]
        submits = [k[1] for k in kinds if k[1]]
        assert submits == ["chrome_author_quick_create", "chrome_author_start", "chrome_user_skill_save"]
        form = next(c for c in comps if c.get("submit_action") == "chrome_user_skill_save")
        identity = form["submit_payload"]
        assert UUID(identity["skill_id"]).version == UUID(identity["command_id"]).version == 4
        assert identity["expected_revision"] == 0 and identity["skill_enabled"] == "true"
        fields = {"skill_name": "Standup", "skill_command": "standup", "skill_applies": "",
                  "skill_instructions": "Yesterday / today / blockers."}
        result = await authoring.HANDLERS["chrome_user_skill_save"](
            orch, object(), owner, ["user"], {**identity, "fields": fields})
        assert "Skill saved." in result[2] and "/standup" not in result[2]
        skills = await us.store_for(orch).list(caller=caller)
        assert len(skills) == 1 and skills[0].skill_id == identity["skill_id"] and skills[0].revision == 1
        html = await authoring.render(orch, owner, ["user"], {})
        assert "/standup" in html and 'data-astral-commands="[{' in html
        comps = await authoring.components(orch, owner, ["user"], {})
        add = next(c for c in comps if c.get("submit_action") == "chrome_user_skill_save")
        with pytest.raises(AssignmentError, match="skill_invalid") as invalid:
            await authoring.HANDLERS["chrome_user_skill_save"](
                orch, object(), owner, ["user"],
                {**add["submit_payload"], "fields": {**fields, "skill_command": "help"}})
        assert invalid.value.status_code == 422
        assert await us.store_for(orch).list(caller=caller) == skills
        result = await authoring.HANDLERS["chrome_user_skill_edit"](
            orch, object(), owner, ["user"], {"slug": "standup"})
        assert result[1] == {"skill_slug": "standup"}
        html = await authoring.render(orch, owner, ["user"], result[1])
        assert "Edit skill" in html and 'name="skill_slug" value="standup"' in html
        comps = await authoring.components(orch, owner, ["user"], result[1])
        form = next(c for c in comps if c.get("submit_action") == "chrome_user_skill_save")
        edit = form["submit_payload"]
        assert edit == {"skill_slug": "standup", "skill_id": identity["skill_id"],
                        "command_id": edit["command_id"], "expected_revision": 1, "skill_enabled": "true"}
        assert UUID(edit["command_id"]).version == 4 and edit["command_id"] != identity["command_id"]
        cards = [c for c in comps if c.get("type") == "card" and c.get("title") == "Standup"]
        toggle = next(c for c in cards[0]["content"] if c.get("action") == "chrome_user_skill_toggle")
        result = await authoring.HANDLERS["chrome_user_skill_toggle"](
            orch, object(), owner, ["user"], toggle["payload"])
        assert "Skill setting saved." in result[2]
        current = await us.store_for(orch).list(caller=caller)
        assert len(current) == 1 and current[0].revision == 2 and not current[0].enabled
        html = await authoring.render(orch, owner, ["user"], {})
        assert 'data-astral-commands="[]"' in html
        comps = await authoring.components(orch, owner, ["user"], {})
        card = next(c for c in comps if c.get("type") == "card" and c.get("title") == "Standup")
        delete = next(c for c in card["content"] if c.get("action") == "chrome_user_skill_delete")
        result = await authoring.HANDLERS["chrome_user_skill_delete"](
            orch, object(), owner, ["user"], delete["payload"])
        assert "deleted" in result[2] and await us.store_for(orch).list(caller=caller) == ()


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
    assert rendered == []
    chrome_events._note_open_surface(orch, ws, authoring.SURFACE_KEY)
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]
    chrome_events._note_open_surface(orch, ws, "agents")
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]
    chrome_events._note_open_surface(orch, ws, authoring.SURFACE_KEY)
    orch.ui_clients = set()
    await authoring._refresh_home(orch, ws, OWNER, ["user"], run)
    assert rendered == [authoring.SURFACE_KEY]


async def test_quick_create_handler_and_dismiss(db, tmp_path, skill_authority):
    orch = _orch(db, _LLM(), tmp_path)
    owner = skill_authority.owner
    async with skill_authority.select(orch):
        result = await authoring.HANDLERS["chrome_author_quick_create"](
            orch, object(), owner, ["user"], {"fields": {"description": "sort my inbox every morning"}})
        assert "Creating" in result[2]
        run = qc.runs_for(owner)[0]
        await _settle(run)
        assert run.state == qc.DONE
        html = await authoring.render(orch, owner, ["user"], {})
        assert "Running on" in html and "Dismiss" in html
        await authoring.HANDLERS["chrome_author_quick_dismiss"](orch, object(), owner, ["user"],
                                                                {"draft_id": run.draft_id})
        assert qc.runs_for(owner) == []


def test_step_editor_copy_and_stale_pass_warning(db):
    from tests.test_byo_authoring_flow import _plan_fields  # noqa: F401
    orch = make_orch(db)
    row = {"phase": "clarify", "clarify_answers": None, "agent_name": "x", "state_revision": 1}
    assert "Find open questions" in authoring._phase_body(row, "clarify")
    assert "Find open questions" in authoring._phase_actions("d", "clarify", 1)
    assert "Ask the assistant" in authoring._phase_actions("d", "plan", 1)
    stale = authoring._phase_actions("d", "generate", 1, stale=True)
    assert "chrome_author_generate" not in stale and "Re-run Analyze" in stale
    fresh = authoring._phase_actions("d", "generate", 1, stale=False)
    assert "chrome_author_generate" in fresh
    passed = {"phase": "generate", "analyze_result": json.dumps({"passed": True, "constitution_version": "v"}),
              "plan_json": "{}", "agent_name": "x", "description": "d"}
    assert "Re-run Analyze before generating" in authoring._phase_body(passed, "generate", orch)


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


def test_agent_status_sees_a_fenced_v3_runtime_as_running():
    from shared.local_transport import FencedTunnelSocket
    fence = SimpleNamespace(agent_id="ua-1", host_session_id="hs-1",
                            runtime_instance_id="ri-1")
    route = FencedTunnelSocket(object(), OWNER, fence, AsyncMock())
    orch = SimpleNamespace(agents={"ua-1": route},
                           _tunnel_sockets={},
                           _personal_agent_runtime_sockets={"ri-1": route})
    assert aa.agent_status(orch, OWNER, "ua-1") == "running"
    assert aa.agent_status(orch, "someone-else", "ua-1") == "offline"
    assert aa.agent_status(orch, OWNER, "ua-2") == "offline"
    orch._personal_agent_runtime_sockets["ri-1"] = object()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
    orch._personal_agent_runtime_sockets.clear()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
    v2 = object()
    orch = SimpleNamespace(agents={"ua-1": v2}, _tunnel_sockets={(OWNER, "ua-1"): v2},
                           _personal_agent_runtime_sockets={})
    assert aa.agent_status(orch, OWNER, "ua-1") == "running"
    orch.agents["ua-1"] = object()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
