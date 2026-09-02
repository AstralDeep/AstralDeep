"""Feature 058 (T017/T018/T019) — the 5-phase guided authoring flow.

What is actually load-bearing here, and therefore what these tests pin:

1. **The phase machine advances only on an explicit act** and never skips.
2. **Clarify is a hard gate** — an unanswered question (or a Clarify that was
   never run) stops the session dead, with a plain-language reason.
3. **Analyze is a hard gate** — a constitution-violating design does not advance
   and generates NOTHING.
4. **Generation is STRUCTURALLY post-Analyze** — the refusal lives on the server
   (phase + stored pass + constitution version + a fingerprint of the artifacts
   Analyze saw), so a forged ``chrome_author_generate`` on a half-finished
   session is refused, not merely un-clickable.
5. **Flag-off is inert** — the surface refuses, every handler refuses, the menu
   item is absent (FR-009).
6. **No share/publish/transfer affordance exists** (FR-020, Constitution K).

The phase machine's DB accessors are synchronous, so every call to one from an
async test rides ``_t`` (asyncio.to_thread) — feature 052's event-loop-blocking
detector is CI-enforced with an empty allowlist.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import flags  # noqa: E402
from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from webrender.chrome.menu_model import build_menu_model  # noqa: E402
from orchestrator.projection_surfaces import authoring  # noqa: E402
from tests.helpers.draft_store_double import InMemoryDraftStore  # noqa: E402
from tests.helpers.user_agent_registry import make_user_agent_registry  # noqa: E402

BUNDLE = {"agent_main.py": "print('x')", "mcp_tools.py": "TOOL_REGISTRY = {}",
          "manifest.json": "{}"}

OWNER = "byoflow-owner"


async def _t(fn, *args, **kwargs):
    """Run a synchronous (DB-touching) helper off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.fixture(autouse=True)
def _byo_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "byo_agents", True)


@pytest.fixture()
def db():
    return InMemoryDraftStore()


def make_orch(db, llm=None):
    """A fake orchestrator with explicit typed draft and user-agent stores."""
    o = MagicMock()
    o.history.db = db
    draft_store = db
    o.lifecycle_manager.draft_store = draft_store
    o.user_agent_registry = make_user_agent_registry()

    async def _create_draft(user_id, agent_name, description, tools_spec=None, **kw):
        did = str(uuid.uuid4())

        def _insert():
            draft_store.create_draft_agent(
                draft_id=did,
                user_id=user_id,
                agent_name=agent_name,
                agent_slug="byoflow-" + did[:8],
                description=description,
                tools_spec=None,
                origin=kw.get("origin", "manual"),
                revises_agent_id=kw.get("revises_agent_id"),
            )
            return draft_store.get_draft_agent(did)

        return await asyncio.to_thread(_insert)

    o.lifecycle_manager.create_draft = AsyncMock(side_effect=_create_draft)
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "generated", "files": dict(BUNDLE)})
    o.lifecycle_manager.generated_agent_publication_service.load_published = (
        AsyncMock(return_value=None)
    )
    o.deliver_agent_bundle = AsyncMock(return_value=1)
    o.delete_user_agent = AsyncMock(return_value=True)
    o._call_llm_json = AsyncMock(return_value=llm)
    o.agents = {}
    o._tunnel_sockets = {}
    return o


async def _session(orch, user_id=OWNER, name="Inbox Sorter",
                   description="sorts my own inbox into folders each morning"):
    return await aa.start_session(orch, user_id=user_id, agent_name=name,
                                  description=description)


def _plan_fields(tools="sort_inbox | tools:read | reads my inbox and files messages"):
    return {"tools": tools, "scopes": "", "egress": ""}


async def _answer_clarify(db, draft_id, question="Which mailbox?", answer="my work mailbox"):
    await _t(db.update_draft_agent, draft_id,
             clarify_answers=json.dumps([{"question": question, "answer": answer}]))


async def _walk_to_analyze(orch, db, draft_id, tools=None):
    """Drive a session specify → clarify → plan → tasks → analyze the long way
    (through the real gates), so every test starts from an honestly-reached state."""
    ok, phase, msg = await _t(
        aa.advance, orch, OWNER, draft_id,
        {"agent_name": "Inbox Sorter", "specification": "sorts my own inbox each morning"})
    assert ok and phase == "clarify", msg
    await _answer_clarify(db, draft_id)
    ok, phase, msg = await _t(aa.advance, orch, OWNER, draft_id, {})
    assert ok and phase == "plan", msg
    ok, phase, msg = await _t(
        aa.advance, orch, OWNER, draft_id, _plan_fields(tools) if tools else _plan_fields())
    assert ok and phase == "tasks", msg
    ok, phase, msg = await _t(
        aa.advance, orch, OWNER, draft_id, {"tasks": "read the inbox\nfile the messages"})
    assert ok and phase == "analyze", msg


# ── 1. the phase machine ─────────────────────────────────────────────────────

async def test_session_starts_at_specify_and_is_byo_origin(db):
    orch = make_orch(db)
    row = await _session(orch)
    assert aa.phase_of(row) == "specify"
    # origin is stamped from the start — it is what keeps this draft off every
    # server-side execution path (SC-002).
    stored = await _t(db.get_draft_agent, row["id"])
    assert stored["origin"] == "byo_client"


async def test_phases_advance_one_step_at_a_time(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert aa.phase_of(await _t(aa.get_session, orch, OWNER, row["id"])) == "analyze"
    # analyze does NOT advance by "continue" — only run_analyze can move it on.
    ok, phase, msg = await _t(aa.advance, orch, OWNER, row["id"], {})
    assert not ok and phase == "analyze" and "Analyze" in msg


async def test_artifacts_are_human_editable_without_advancing(db):
    orch = make_orch(db)
    row = await _session(orch)
    ok, msg = await _t(aa.save_artifact, orch, OWNER, row["id"], {
        "agent_name": "Renamed", "specification": "a completely rewritten specification"})
    assert ok, msg
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert fresh["agent_name"] == "Renamed"
    assert fresh["description"] == "a completely rewritten specification"
    assert aa.phase_of(fresh) == "specify"        # saving never advances


# ── 2. the CLARIFY hard gate ─────────────────────────────────────────────────

async def test_clarify_blocks_while_a_question_is_unanswered(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _t(aa.advance, orch, OWNER, row["id"],
             {"specification": "sorts my own inbox each morning"})
    await _t(db.update_draft_agent, row["id"], clarify_answers=json.dumps([
        {"question": "Which mailbox should it read?", "answer": ""},
        {"question": "How often?", "answer": "daily"},
    ]))
    ok, phase, msg = await _t(aa.advance, orch, OWNER, row["id"], {})
    assert not ok
    assert phase == "clarify"                       # did NOT advance
    assert "Which mailbox should it read?" in msg   # plain-language, cites the question
    # answering it unblocks
    ok, phase, _ = await _t(aa.advance, orch, OWNER, row["id"], {"q0": "my work mailbox"})
    assert ok and phase == "plan"


async def test_clarify_that_never_ran_cannot_be_walked_past(db):
    """The gate is not "answer the questions you were shown" — it is "the
    questions must have been ASKED". An empty submission must not conjure an
    empty (= nothing-ambiguous) question list."""
    orch = make_orch(db)
    row = await _session(orch)
    await _t(aa.advance, orch, OWNER, row["id"],
             {"specification": "sorts my own inbox each morning"})
    assert (await _t(aa.get_session, orch, OWNER, row["id"]))["clarify_answers"] is None
    ok, phase, msg = await _t(aa.advance, orch, OWNER, row["id"], {})
    assert not ok and phase == "clarify" and "Clarify" in msg
    ok, _msg = await _t(aa.save_artifact, orch, OWNER, row["id"], {})
    assert not ok                                    # and it cannot be saved into existence
    assert (await _t(aa.get_session, orch, OWNER, row["id"]))["clarify_answers"] is None


async def test_clarify_draft_failure_is_fail_closed(db):
    """A drafting call that returns nothing must not be recorded as "no open
    questions" — that assertion is what lets a session past the gate."""
    orch = make_orch(db, llm=None)   # LLM unavailable
    row = await _session(orch)
    await _t(aa.advance, orch, OWNER, row["id"],
             {"specification": "sorts my own inbox each morning"})
    ok, msg = await aa.draft_phase(orch, None, OWNER, row["id"])
    assert not ok and "try again" in msg
    assert (await _t(aa.get_session, orch, OWNER, row["id"]))["clarify_answers"] is None
    advanced, phase, _ = await _t(aa.advance, orch, OWNER, row["id"], {})
    assert not advanced and phase == "clarify"


async def test_clarify_draft_persists_questions_for_the_human(db):
    orch = make_orch(db, llm={"questions": ["Which mailbox?", "How often?"]})
    row = await _session(orch)
    await _t(aa.advance, orch, OWNER, row["id"],
             {"specification": "sorts my own inbox each morning"})
    ok, _msg = await aa.draft_phase(orch, None, OWNER, row["id"])
    assert ok
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert [i["question"] for i in aa.clarify_items(fresh)] == ["Which mailbox?", "How often?"]
    assert aa.unresolved_clarifications(fresh) == ["Which mailbox?", "How often?"]


# ── 3. the ANALYZE hard gate ─────────────────────────────────────────────────

async def test_analyze_violation_does_not_advance_and_generates_nothing(db):
    orch = make_orch(db)
    row = await _session(orch)
    # A share/publish capability — Constitution K.
    await _walk_to_analyze(orch, db, row["id"],
                           tools="share_agent | tools:write | shares the agent with others")
    result = await _t(aa.run_analyze, orch, OWNER, row["id"])
    assert result["status"] == "analyze_failed"
    principles = {v["principle"] for v in result["violations"]}
    assert "K" in principles
    assert all(v["offending_field"] for v in result["violations"])   # cites the field
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert aa.phase_of(fresh) == "analyze"                           # no advance
    orch.lifecycle_manager.generate_code.assert_not_awaited()        # no code


async def test_analyze_pass_stamps_the_constitution_and_opens_generate(db):
    from orchestrator.agent_constitution import AGENT_CONSTITUTION_VERSION
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    result = await _t(aa.run_analyze, orch, OWNER, row["id"])
    assert result["status"] == "passed"
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert aa.phase_of(fresh) == "generate"
    assert fresh["constitution_version"] == AGENT_CONSTITUTION_VERSION


# ── 4. generation is STRUCTURALLY post-Analyze ───────────────────────────────

async def test_generate_refused_before_analyze(db):
    orch = make_orch(db)
    row = await _session(orch)              # still at 'specify'
    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "gate_blocked"
    orch.lifecycle_manager.generate_code.assert_not_awaited()
    orch.deliver_agent_bundle.assert_not_awaited()


async def test_generate_refused_when_analyze_failed(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="share_agent | tools:write | shares the agent with others")
    await _t(aa.run_analyze, orch, OWNER, row["id"])
    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "gate_blocked"
    orch.lifecycle_manager.generate_code.assert_not_awaited()


async def test_generate_refused_when_the_design_changed_after_the_pass(db):
    """A pass certifies THOSE artifacts. Editing the plan afterwards must not ride
    the stale approval into codegen."""
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"
    # sneak a new, unanalyzed tool onto the plan
    plan = aa.plan_artifact(await _t(aa.get_session, orch, OWNER, row["id"]))
    plan["tools_used"].append("exfiltrate")
    plan["tool_scopes"]["exfiltrate"] = "tools:system"
    await _t(db.update_draft_agent, row["id"], plan_json=json.dumps(plan))

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "gate_blocked"
    assert "run analyze again" in result["reason"].lower()
    orch.lifecycle_manager.generate_code.assert_not_awaited()


async def test_generate_refused_when_the_constitution_moved(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"
    record = aa.analyze_record(await _t(aa.get_session, orch, OWNER, row["id"]))
    record["constitution_version"] = "0.0.1-ancient"
    await _t(db.update_draft_agent, row["id"], analyze_result=json.dumps(record))

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "gate_blocked"
    orch.lifecycle_manager.generate_code.assert_not_awaited()


async def test_generate_after_a_pass_delivers_the_bundle_and_never_popens(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "delivered"
    kw = orch.lifecycle_manager.generate_code.await_args.kwargs
    assert kw["target"] == "byo"                     # the self-contained bundle
    assert kw["agent_id"] == result["agent_id"]      # owner-namespaced identity
    files = orch.deliver_agent_bundle.await_args.args[2]
    assert set(files) == set(BUNDLE)
    agent = await _t(ua.get_user_agent, orch.user_agent_registry, result["agent_id"])
    assert agent["status"] == "validated" and agent["owner_user_id"] == OWNER
    assert agent["is_public"] is False


async def test_the_approved_tool_set_is_handed_to_codegen(db):
    """The Analyze-approved Plan must reach the generator. It never did: codegen
    reads ``draft_agents.tools_spec``, which was left NULL, so the card's skills
    were whatever the LLM invented from the free-text description — the gate
    approved one agent and the owner ran another."""
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="sort_inbox | tools:read | files my messages")
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"

    assert (await aa.generate_from_session(orch, OWNER, row["id"]))["status"] == "delivered"
    stored = json.loads((await _t(db.get_draft_agent, row["id"]))["tools_spec"])
    assert [t["name"] for t in stored] == ["sort_inbox"]
    assert stored[0]["scope"] == "tools:read"


async def test_a_generated_tool_that_was_never_approved_is_not_delivered(db):
    """Fail-closed conformance: the bundle's TOOL_REGISTRY must be a subset of
    what Analyze approved, at the scopes it approved."""
    orch = make_orch(db)
    orch.lifecycle_manager.generate_code = AsyncMock(return_value={
        "status": "generated",
        "files": {**BUNDLE, "mcp_tools.py":
                  "TOOL_REGISTRY = {'sort_inbox': {'function': a, 'scope': 'tools:read'},\n"
                  "                 'exfiltrate': {'function': b, 'scope': 'tools:system'}}"},
    })
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="sort_inbox | tools:read | files my messages")
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "generation_failed"
    assert "exfiltrate" in result["error"]
    orch.deliver_agent_bundle.assert_not_awaited()
    agent = await _t(ua.get_user_agent, orch.user_agent_registry, result["agent_id"])
    assert agent["status"] != "validated"      # 'validated' still means Analyze passed


async def test_a_widened_scope_is_not_delivered(db):
    orch = make_orch(db)
    orch.lifecycle_manager.generate_code = AsyncMock(return_value={
        "status": "generated",
        "files": {**BUNDLE, "mcp_tools.py":
                  "TOOL_REGISTRY = {'sort_inbox': {'function': a, 'scope': 'tools:system'}}"},
    })
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="sort_inbox | tools:read | files my messages")
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "generation_failed"
    assert "tools:system" in result["error"] and "tools:read" in result["error"]
    orch.deliver_agent_bundle.assert_not_awaited()


async def test_a_bundle_that_failed_spec_validation_is_not_delivered(db):
    """generate_code reports GENERATED even when spec validation failed (the user
    may still refine). That is NOT a validated bundle: it must not be marked
    'validated' nor pushed to the host."""
    orch = make_orch(db)
    orch.lifecycle_manager.generate_code = AsyncMock(return_value={
        "status": "generated", "files": dict(BUNDLE),
        "validation_report": json.dumps({
            "passed": False, "tools_tested": 1, "tools_passed": 0,
            "findings": [{"severity": "error", "category": "RETURN_FORMAT",
                          "message": "tool never returns _ui_components",
                          "tool_name": "sort_inbox"}],
            "tools": []}),
    })
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"

    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "generation_failed"
    assert "_ui_components" in result["error"]
    orch.deliver_agent_bundle.assert_not_awaited()
    agent = await _t(ua.get_user_agent, orch.user_agent_registry, result["agent_id"])
    assert agent["status"] != "validated"


# ── 5. flag-off is inert (FR-009) ────────────────────────────────────────────

def _flag_off(monkeypatch):
    monkeypatch.setitem(flags._flags, "byo_agents", False)


async def test_surface_refuses_when_flag_off(db, monkeypatch):
    """FF_BYO_AGENTS off: no personal-agent affordance at all. Feature 077: the
    skills section (its own flag, no desktop needed) is still there; with BOTH
    flags off the surface is exactly the old single notice."""
    _flag_off(monkeypatch)
    orch = make_orch(db)
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "not enabled" in html
    assert "chrome_author_start" not in html          # no affordance at all
    assert "chrome_author_quick_create" not in html
    assert "chrome_user_skill_save" in html                 # skills need no host
    comps = await authoring.components(orch, OWNER, ["user"], {})
    assert comps[0]["type"] == "alert" and "not enabled" in comps[0]["message"]
    assert not any(str(c.get("submit_action") or "").startswith("chrome_author") for c in comps)
    from orchestrator import user_skills
    monkeypatch.setattr(user_skills, "enabled", lambda: False)
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "not enabled" in html and "chrome_user_skill_save" not in html
    comps = await authoring.components(orch, OWNER, ["user"], {})
    assert len(comps) == 1 and comps[0]["type"] == "alert"


async def test_every_handler_refuses_when_flag_off(db, monkeypatch):
    _flag_off(monkeypatch)
    orch = make_orch(db)
    for action, fn in authoring.HANDLERS.items():
        if action.startswith("chrome_user_skill_") or action == "chrome_author_list":
            continue  # skills are not gated by FF_BYO_AGENTS; list is the shared home
        result = await fn(orch, None, OWNER, ["user"], {"draft_id": "x", "agent_id": "y"})
        assert result is not None, action
        _surface, _params, notice = result
        assert "not enabled" in notice, action
    from orchestrator import user_skills
    monkeypatch.setattr(user_skills, "enabled", lambda: False)
    for action in ("chrome_author_list", "chrome_user_skill_save", "chrome_user_skill_edit",
                   "chrome_user_skill_toggle", "chrome_user_skill_delete"):
        result = await authoring.HANDLERS[action](orch, None, OWNER, ["user"], {"slug": "x"})
        assert result is not None and "not enabled" in result[2], action
    # nothing was generated, delivered, or deleted on ANY of those paths
    orch.lifecycle_manager.generate_code.assert_not_awaited()
    orch.deliver_agent_bundle.assert_not_awaited()
    orch.delete_user_agent.assert_not_awaited()


async def test_generate_refuses_when_flag_off_even_from_a_passed_session(db, monkeypatch):
    """The flag is checked at the entry point, not only at render: a session that
    passed Analyze while the flag was on cannot generate once it is off."""
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"
    _flag_off(monkeypatch)
    result = await aa.generate_from_session(orch, OWNER, row["id"])
    assert result["status"] == "disabled"
    orch.lifecycle_manager.generate_code.assert_not_awaited()


def test_handlers_are_reachable_through_the_chrome_dispatcher():
    """The surface is only wired if ``chrome_events`` can route to it: the
    ``chrome_`` prefix puts these in the chrome namespace, and ``collect_handlers``
    aggregates them from SURFACE_MODULES."""
    from orchestrator.chrome_events import _is_chrome_action
    from orchestrator.projection_surfaces import SURFACE_MODULES, collect_handlers, get_surface

    assert SURFACE_MODULES["agent_authoring"] == "orchestrator.projection_surfaces.authoring"
    assert get_surface("agent_authoring") is authoring
    handlers = collect_handlers()
    for action in authoring.HANDLERS:
        assert _is_chrome_action(action), action
        assert handlers.get(action) == ("agent_authoring", authoring.HANDLERS[action]), action


def test_menu_item_absent_when_flag_off_present_when_on():
    off = build_menu_model(["user"], pulse_enabled=False, byo_enabled=False)
    assert all(i.surface != "agent_authoring" for g in off.menu for i in g.items)
    on = build_menu_model(["user"], pulse_enabled=False, byo_enabled=True)
    items = [i for g in on.menu for i in g.items if i.surface == "agent_authoring"]
    assert len(items) == 1 and items[0].label == "My agents & skills"


# ── 5b. every rendered action can actually address its session ───────────────

_BUTTON = re.compile(r"<button\b[^>]*>")


async def test_every_wizard_action_carries_the_session_id(db):
    """A ``chrome_author_*`` button that reaches the server with no ``draft_id``
    addresses nothing and dies in a "session is not available" notice. Web
    payloads come from EITHER ``data-ui-payload`` OR the collected form fields —
    so each button must have one of the two."""
    orch = make_orch(db)
    row = await _session(orch)
    draft_id = row["id"]
    for phase in aa.PHASES:
        await _t(db.update_draft_agent, draft_id, phase=phase)
        html = await authoring.render(orch, OWNER, ["user"], {"draft_id": draft_id})
        assert f'name="draft_id" value="{draft_id}"' in html, phase
        for tag in _BUTTON.findall(html):
            if "chrome_author_" not in tag or 'data-ui-action="chrome_author_list"' in tag:
                continue
            has_payload = draft_id in tag
            collects = 'data-ui-collect="true"' in tag   # picks up the hidden input
            assert has_payload or collects, f"{phase}: action cannot address the session: {tag}"


# ── 6. no share / publish / transfer anywhere (FR-020, Constitution K) ───────

_FORBIDDEN = ("share", "publish", "transfer", "make_public")


def test_no_share_publish_or_transfer_handler_exists():
    for action in authoring.HANDLERS:
        assert not any(word in action for word in _FORBIDDEN), action


async def test_no_share_affordance_in_web_or_native_render(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    await _t(aa.run_analyze, orch, OWNER, row["id"])
    await aa.generate_from_session(orch, OWNER, row["id"])

    html = await authoring.render(orch, OWNER, ["user"], {})
    # scan ACTIONS, not prose: the constitution's own plain-language text
    # legitimately contains the word "share".
    actions = [seg.split('"')[0] for seg in html.split('data-ui-action="')[1:]]
    assert actions, "the list view has actions"
    for action in actions:
        assert not any(word in action for word in _FORBIDDEN), action

    comps = await authoring.components(orch, OWNER, ["user"], {})
    native_actions = []

    def walk(c):
        if not isinstance(c, dict):
            return
        for key in ("action", "submit_action"):
            if c.get(key):
                native_actions.append(c[key])
        for a in (c.get("actions") or []):
            if isinstance(a, dict) and a.get("action"):
                native_actions.append(a["action"])
        for child in (c.get("children") or c.get("content") or []):
            walk(child)

    for c in comps:
        walk(c)
    assert native_actions
    for action in native_actions:
        assert not any(word in action for word in _FORBIDDEN), action
