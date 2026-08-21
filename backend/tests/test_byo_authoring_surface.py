"""Feature 058 — the BYO authoring/management SURFACE
(``orchestrator.projection_surfaces.authoring``).

``test_byo_authoring_flow`` pins the phase machine and ``test_byo_lifecycle``
drives the lifecycle handlers against a live ``Orchestrator``. This file pins the
SURFACE itself with the flag ON: ``render()``/``components()`` across every phase
with POPULATED artifacts, and each ``chrome_author_*`` handler's success and error
branches. Every assertion checks observable behaviour (the rendered artifact, the
handler's returned surface/params/notice, the persisted row) — never mere line
execution.

Sync (DB-touching) helpers ride ``_t`` (asyncio.to_thread) — feature 052's
event-loop-blocking detector is CI-enforced with an empty allowlist.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.feature_flags import flags  # noqa: E402
from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import agent_analyze  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.projection_surfaces import authoring  # noqa: E402
from webrender.chrome.surfaces import _sdui  # noqa: E402
from tests.helpers.draft_store_double import InMemoryDraftStore  # noqa: E402
from tests.helpers.user_agent_registry import make_user_agent_registry  # noqa: E402

OWNER = "byosurf-owner"

BUNDLE = {"agent_main.py": "print('x')", "mcp_tools.py": "TOOL_REGISTRY = {}",
          "manifest.json": "{}"}


async def _t(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.fixture(autouse=True)
def _byo_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "byo_agents", True)


@pytest.fixture()
def db():
    return InMemoryDraftStore()


def make_orch(db, llm=None):
    """Fake orchestrator with typed stores and mocked LLM/codegen seams."""
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
                agent_slug="byosurf-" + did[:8],
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


async def _session(orch, name="Inbox Sorter",
                   description="sorts my own inbox into folders each morning"):
    return await aa.start_session(orch, user_id=OWNER, agent_name=name,
                                  description=description)


async def _walk_to_analyze(orch, db, draft_id, tools=None):
    """Drive specify → analyze the long way through the real gates."""
    tools = tools or "sort_inbox | tools:read | reads my inbox and files messages"
    ok, phase, _ = await _t(aa.advance, orch, OWNER, draft_id,
                            {"agent_name": "Inbox Sorter",
                             "specification": "sorts my own inbox each morning"})
    assert ok and phase == "clarify"
    await _t(db.update_draft_agent, draft_id,
             clarify_answers=json.dumps([{"question": "Which mailbox?", "answer": "work"}]))
    assert (await _t(aa.advance, orch, OWNER, draft_id, {}))[0]
    assert (await _t(aa.advance, orch, OWNER, draft_id,
                     {"tools": tools, "scopes": "", "egress": ""}))[0]
    assert (await _t(aa.advance, orch, OWNER, draft_id,
                     {"tasks": "read the inbox\nfile the messages"}))[0]


# ── chrome_author_start ──────────────────────────────────────────────────────

async def test_h_start_opens_a_session_and_returns_its_id(db):
    orch = make_orch(db, llm={"specification": "a drafted specification for the agent"})
    surface, params, notice = await authoring._h_start(
        orch, None, OWNER, ["user"],
        {"fields": {"agent_name": "Mailer", "description": "sorts my mail each day"}})
    assert surface == "agent_authoring"
    draft_id = params["draft_id"]
    row = await _t(aa.get_session, orch, OWNER, draft_id)
    assert row is not None and row["agent_name"] == "Mailer"
    assert aa.phase_of(row) == "specify"
    assert "info" in notice or "Drafted" in notice or "specification" in notice


async def test_h_start_rejects_a_too_short_request(db):
    orch = make_orch(db)
    _s, params, notice = await authoring._h_start(
        orch, None, OWNER, ["user"], {"fields": {"agent_name": "X", "description": "no"}})
    assert params == {}
    assert "at least 10 characters" in notice
    orch.lifecycle_manager.create_draft.assert_not_awaited()   # no session created


# ── chrome_author_draft ──────────────────────────────────────────────────────

async def test_h_draft_redrafts_the_current_artifact(db):
    orch = make_orch(db, llm={"specification": "freshly redrafted specification text"})
    row = await _session(orch)
    _s, params, notice = await authoring._h_draft(
        orch, None, OWNER, ["user"], {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert fresh["description"] == "freshly redrafted specification text"
    assert "edit it" in notice


async def test_h_draft_reports_a_fail_open_message_without_an_llm(db):
    orch = make_orch(db, llm=None)         # LLM unavailable
    row = await _session(orch)
    _s, _p, notice = await authoring._h_draft(orch, None, OWNER, ["user"],
                                              {"draft_id": row["id"]})
    assert "write one yourself" in notice   # honest fail-open, never a dead end


# ── chrome_author_edit ───────────────────────────────────────────────────────

async def test_h_edit_persists_and_drops_non_scalar_fields(db):
    """The human's edit is saved verbatim; ``_fields`` filters out the nested
    dict/list values the client should never send, so a stray object can't corrupt
    the artifact."""
    orch = make_orch(db)
    row = await _session(orch)
    _s, params, notice = await authoring._h_edit(orch, None, OWNER, ["user"], {
        "draft_id": row["id"],
        "fields": {"draft_id": row["id"], "agent_name": "Renamed",
                   "specification": "a completely rewritten specification here",
                   "junk_obj": {"a": 1}, "junk_list": [1, 2]}})
    assert params["draft_id"] == row["id"]
    assert "success" in notice or "aved" in notice
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert fresh["agent_name"] == "Renamed"
    assert fresh["description"] == "a completely rewritten specification here"


# ── chrome_author_advance (+ _autodraft) ─────────────────────────────────────

async def test_h_advance_saves_advances_and_autodrafts_the_next_phase(db):
    orch = make_orch(db, llm={"questions": ["Which mailbox should it read?"]})
    row = await _session(orch)
    _s, params, notice = await authoring._h_advance(orch, None, OWNER, ["user"], {
        "draft_id": row["id"],
        "fields": {"draft_id": row["id"],
                   "specification": "sorts my own inbox each morning into folders"}})
    assert params["draft_id"] == row["id"]
    fresh = await _t(aa.get_session, orch, OWNER, row["id"])
    assert aa.phase_of(fresh) == "clarify"                    # advanced one phase
    # …and the next phase's artifact was auto-drafted for the human to react to.
    assert [i["question"] for i in aa.clarify_items(fresh)] == \
        ["Which mailbox should it read?"]
    assert "complete" in notice and "Clarify" in notice


async def test_h_advance_blocked_by_the_clarify_gate_reports_the_reason(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _t(aa.advance, orch, OWNER, row["id"],
             {"specification": "sorts my own inbox each morning"})
    await _t(db.update_draft_agent, row["id"], clarify_answers=json.dumps(
        [{"question": "Which mailbox?", "answer": ""}]))
    _s, params, notice = await authoring._h_advance(orch, None, OWNER, ["user"],
                                                    {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "Which mailbox?" in notice          # cites the blocking question in plain language
    assert "red-500" in notice                 # rendered as an error notice
    assert aa.phase_of(await _t(aa.get_session, orch, OWNER, row["id"])) == "clarify"


async def test_autodraft_is_a_noop_when_the_artifact_already_exists(db):
    """``_autodraft`` only drafts an EMPTY artifact — landing on a phase whose
    artifact is already present must not overwrite the human's work, and returns
    an empty message."""
    orch = make_orch(db, llm={"questions": ["should never be drafted"]})
    row = await _session(orch)
    # clarify already answered → no redraft
    await _t(db.update_draft_agent, row["id"], phase="clarify",
             clarify_answers=json.dumps([{"question": "Mine?", "answer": "yes"}]))
    assert await authoring._autodraft(orch, None, OWNER, row["id"]) == ""
    # plan already has tools → no redraft
    await _t(db.update_draft_agent, row["id"], phase="plan",
             plan_json=json.dumps({"tools_used": ["t"], "tools": [{"name": "t"}]}))
    assert await authoring._autodraft(orch, None, OWNER, row["id"]) == ""
    # tasks already present → no redraft
    await _t(db.update_draft_agent, row["id"], phase="tasks",
             plan_json=json.dumps({"tasks": ["do it"]}))
    assert await authoring._autodraft(orch, None, OWNER, row["id"]) == ""
    # analyze/generate/specify are never auto-drafted
    await _t(db.update_draft_agent, row["id"], phase="analyze")
    assert await authoring._autodraft(orch, None, OWNER, row["id"]) == ""
    orch._call_llm_json.assert_not_awaited()
    # a missing session is a silent no-op
    assert await authoring._autodraft(orch, None, OWNER, "no-such-id") == ""


# ── chrome_author_analyze (the Analyze HARD GATE) ────────────────────────────

async def test_h_analyze_passes_and_opens_generate(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    _s, params, notice = await authoring._h_analyze(orch, None, OWNER, ["user"],
                                                    {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "passed" in notice
    assert aa.phase_of(await _t(aa.get_session, orch, OWNER, row["id"])) == "generate"


async def test_h_analyze_reports_violations_and_generates_nothing(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="share_agent | tools:write | shares the agent with others")
    _s, params, notice = await authoring._h_analyze(orch, None, OWNER, ["user"],
                                                    {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "problem" in notice and "nothing was generated" in notice
    assert aa.phase_of(await _t(aa.get_session, orch, OWNER, row["id"])) == "analyze"


async def test_h_analyze_too_early_before_the_plan_is_done(db):
    orch = make_orch(db)
    row = await _session(orch)                     # still at specify
    _s, _p, notice = await authoring._h_analyze(orch, None, OWNER, ["user"],
                                                {"draft_id": row["id"]})
    assert "Finish the earlier steps" in notice


async def test_h_analyze_unavailable_for_an_unknown_session(db):
    orch = make_orch(db)
    _s, params, notice = await authoring._h_analyze(orch, None, OWNER, ["user"],
                                                    {"draft_id": "no-such-id"})
    assert params == {} and "not available" in notice


# ── chrome_author_generate ───────────────────────────────────────────────────

async def _passed_session(orch, db, name="Inbox Sorter"):
    row = await _session(orch, name=name)
    await _walk_to_analyze(orch, db, row["id"])
    assert (await _t(aa.run_analyze, orch, OWNER, row["id"]))["status"] == "passed"
    return row


async def test_h_generate_delivers_after_a_pass(db):
    orch = make_orch(db)
    row = await _passed_session(orch, db)
    _s, params, notice = await authoring._h_generate(orch, None, OWNER, ["user"],
                                                     {"draft_id": row["id"]})
    assert params == {}
    assert "Sent to your desktop host" in notice
    orch.deliver_agent_bundle.assert_awaited_once()


async def test_h_generate_reports_no_host_when_nothing_is_connected(db):
    orch = make_orch(db)
    orch.deliver_agent_bundle = AsyncMock(return_value=0)      # no desktop host online
    row = await _passed_session(orch, db, name="No Host Agent")
    _s, params, notice = await authoring._h_generate(orch, None, OWNER, ["user"],
                                                     {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "no desktop client is connected" in notice
    assert "Generate again" in notice
    assert "same verified bundle" in notice


async def test_h_generate_refused_before_analyze(db):
    orch = make_orch(db)
    row = await _session(orch)                                 # never analyzed
    _s, params, notice = await authoring._h_generate(orch, None, OWNER, ["user"],
                                                     {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "Analyze" in notice
    orch.lifecycle_manager.generate_code.assert_not_awaited()


async def test_h_generate_reports_a_codegen_failure(db):
    orch = make_orch(db)
    orch.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "error", "error_message": "codegen boom"})
    row = await _passed_session(orch, db, name="Boom Agent")
    _s, params, notice = await authoring._h_generate(orch, None, OWNER, ["user"],
                                                     {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "Code generation failed" in notice and "boom" in notice
    orch.deliver_agent_bundle.assert_not_awaited()


async def test_h_generate_reports_terminal_host_activation_failure(
    db,
    monkeypatch,
):
    orch = make_orch(db)
    row = await _passed_session(orch, db, name="Host Activation Failure")
    monkeypatch.setattr(
        aa,
        "generate_from_session",
        AsyncMock(
            return_value={
                "status": "delivery_failed",
                "failure_code": "child_start_failed",
            }
        ),
    )

    _surface, params, notice = await authoring._h_generate(
        orch,
        None,
        OWNER,
        ["user"],
        {"draft_id": row["id"]},
    )

    assert params["draft_id"] == row["id"]
    assert "could not activate this revision" in notice
    assert "new revision" in notice


async def test_h_generate_reports_pending_activation_recovery(db, monkeypatch):
    orch = make_orch(db)
    row = await _passed_session(orch, db, name="Activation Recovery")
    monkeypatch.setattr(
        aa,
        "generate_from_session",
        AsyncMock(
            return_value={
                "status": "delivery_pending",
                "failure_code": "revision_promotion_recovery_pending",
            }
        ),
    )

    _surface, params, notice = await authoring._h_generate(
        orch,
        None,
        OWNER,
        ["user"],
        {"draft_id": row["id"]},
    )

    assert params["draft_id"] == row["id"]
    assert "still needs recovery" in notice
    assert "run Generate again" in notice
    assert "reuse the exact verified revision" in notice


async def test_h_generate_fails_closed_if_the_constitution_moves_after_the_pass(db,
                                                                               monkeypatch):
    """If the world moves under a passed session (a late constitution violation),
    generation refuses and the session is pushed back to Analyze — nothing ships."""
    orch = make_orch(db)
    row = await _passed_session(orch, db, name="Moved Agent")

    from orchestrator.agent_analyze import AnalyzeResult, Violation
    fail = AnalyzeResult(passed=False, constitution_version="x", violations=[
        Violation(principle="K", title="No sharing", plain_language="cannot share",
                  offending_field="declared_tools")])
    monkeypatch.setattr(agent_analyze, "check", lambda *a, **k: fail)

    _s, params, notice = await authoring._h_generate(orch, None, OWNER, ["user"],
                                                     {"draft_id": row["id"]})
    assert params["draft_id"] == row["id"]
    assert "refused this design" in notice
    orch.lifecycle_manager.generate_code.assert_not_awaited()
    assert aa.phase_of(await _t(aa.get_session, orch, OWNER, row["id"])) == "analyze"


# ── chrome_author_list ───────────────────────────────────────────────────────

async def test_h_list_returns_to_the_empty_list_view(db):
    orch = make_orch(db)
    surface, params, notice = await authoring._h_list(orch, None, OWNER, ["user"], {})
    assert surface == "agent_authoring" and params == {} and notice == ""


@pytest.mark.parametrize(
    "delivery_result",
    [
        {"status": "no_host"},
        {
            "status": "delivery_pending",
            "failure_code": "revision_delivery_capacity_pending",
        },
    ],
    ids=["no-host", "delivery-pending"],
)
async def test_generate_ready_session_remains_listed_until_revision_is_live(
    db,
    monkeypatch,
    delivery_result,
):
    """A durable authoring row is not a delivered/live revision by itself."""

    orch = make_orch(db)
    row = await _passed_session(orch, db, name="Retryable Delivery")
    generate_ready = await _t(aa.get_session, orch, OWNER, row["id"])
    assert generate_ready is not None
    assert aa.phase_of(generate_ready) == "generate"

    agent_id = generate_ready["target_agent_id"]
    await _t(
        ua.create_user_agent,
        orch.user_agent_registry,
        agent_id=agent_id,
        owner_user_id=OWNER,
        display_name="Retryable Delivery",
        draft_id=row["id"],
        declared_tools=["sort_inbox"],
        declared_scopes=["tools:read"],
    )
    await _t(
        ua.mark_validated,
        orch.user_agent_registry,
        agent_id,
        "test-constitution",
    )
    persisted_agent = await _t(
        ua.get_user_agent,
        orch.user_agent_registry,
        agent_id,
    )
    assert persisted_agent["status"] == "validated"
    assert persisted_agent["active_revision_id"] is None

    monkeypatch.setattr(
        aa,
        "generate_from_session",
        AsyncMock(return_value=dict(delivery_result)),
    )
    _surface, params, _notice = await authoring._h_generate(
        orch,
        None,
        OWNER,
        ["user"],
        {"draft_id": row["id"]},
    )
    assert params["draft_id"] == row["id"]

    context = await authoring._list_context(orch, OWNER)
    visible = [item for item in context["sessions"] if item["id"] == row["id"]]
    assert len(visible) == 1
    assert aa.phase_of(visible[0]) == "generate"


# ── render(): the guided-session web view with populated artifacts ───────────

async def test_render_session_shows_the_clarify_questions(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _t(db.update_draft_agent, row["id"], phase="clarify", clarify_answers=json.dumps(
        [{"question": "Which mailbox should it read?", "answer": "my work mailbox"}]))
    html = await authoring.render(orch, OWNER, ["user"], {"draft_id": row["id"]})
    assert "Which mailbox should it read?" in html
    assert "my work mailbox" in html


async def test_render_session_shows_analyze_violations_in_plain_language(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="share_agent | tools:write | shares the agent with others")
    result = await _t(aa.run_analyze, orch, OWNER, row["id"])
    assert result["status"] == "analyze_failed"
    html = await authoring.render(orch, OWNER, ["user"], {"draft_id": row["id"]})
    assert "cannot be built" in html               # the _violations_block header
    # each violation is cited with its rule + offending field
    v = result["violations"][0]
    assert v["plain_language"] in html
    assert v["offending_field"] in html


async def test_render_list_shows_an_in_progress_session_row(db):
    orch = make_orch(db)
    await _session(orch, name="Half Done")
    html = await authoring.render(orch, OWNER, ["user"], {})
    assert "Half Done" in html
    assert "chrome_open" in html                    # the session-row button opens it
    assert aa.PHASE_LABELS["specify"] in html


async def test_render_session_unavailable_for_a_bad_draft_id(db):
    orch = make_orch(db)
    html = await authoring.render(orch, OWNER, ["user"], {"draft_id": "no-such-id"})
    assert "not available" in html


# ── components(): the native SDUI view across phases ─────────────────────────

async def test_components_specify_renders_an_editable_form(db):
    orch = make_orch(db)
    row = await _session(orch, name="Native Spec")
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    kinds = [c["type"] for c in comps]
    assert "param_picker" in kinds
    form = next(c for c in comps if c["type"] == "param_picker")
    field_names = [f["name"] for f in form["fields"]]
    assert "agent_name" in field_names and "specification" in field_names


async def test_components_clarify_and_plan_and_tasks_render_their_fields(db):
    orch = make_orch(db)
    row = await _session(orch)
    # clarify
    await _t(db.update_draft_agent, row["id"], phase="clarify", clarify_answers=json.dumps(
        [{"question": "Which mailbox?", "answer": "work"}]))
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    form = next(c for c in comps if c["type"] == "param_picker")
    assert form["fields"][0]["label"] == "Which mailbox?"
    # plan
    await _t(db.update_draft_agent, row["id"], phase="plan", plan_json=json.dumps(
        {"tools": [{"name": "sort_inbox", "scope": "tools:read", "description": "d"}],
         "declared_scopes": ["tools:read"], "declared_egress": ["mail.example.com"]}))
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    form = next(c for c in comps if c["type"] == "param_picker")
    names = [f["name"] for f in form["fields"]]
    assert names == ["tools", "scopes", "egress"]
    assert "sort_inbox" in form["fields"][0]["default"]
    # tasks
    await _t(db.update_draft_agent, row["id"], phase="tasks", plan_json=json.dumps(
        {"tasks": ["read the inbox", "file the messages"]}))
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    form = next(c for c in comps if c["type"] == "param_picker")
    assert form["fields"][0]["name"] == "tasks"
    assert "read the inbox" in form["fields"][0]["default"]


async def test_components_analyze_and_generate_phases(db):
    orch = make_orch(db)
    # analyze — violations become error alerts
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"],
                           tools="share_agent | tools:write | shares the agent with others")
    await _t(aa.run_analyze, orch, OWNER, row["id"])
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    alerts = [c for c in comps if c["type"] == "alert" and c.get("variant") == "error"]
    assert alerts and any("field:" in a["message"] for a in alerts)   # cites the field
    assert any(c.get("action") == "chrome_author_analyze" for c in comps)

    # generate — a passing session offers Generate + Re-run Analyze
    row2 = await _session(orch, name="Native Gen")
    await _walk_to_analyze(orch, db, row2["id"])
    await _t(aa.run_analyze, orch, OWNER, row2["id"])
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row2["id"]})
    actions = [c.get("action") for c in comps if c.get("action")]
    assert "chrome_author_generate" in actions
    assert "chrome_author_analyze" in actions          # re-run
    assert any(c["type"] == "alert" and c.get("variant") == "success" for c in comps)


async def test_components_analyze_passed_shows_a_success_alert(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    await _t(aa.run_analyze, orch, OWNER, row["id"])
    # force the phase back to analyze (passed record present) to hit that branch
    await _t(db.update_draft_agent, row["id"], phase="analyze")
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    assert any(c["type"] == "alert" and c.get("variant") == "success"
               and "Analyze passed" in c["message"] for c in comps)


async def test_components_session_unavailable_for_a_bad_draft_id(db):
    orch = make_orch(db)
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": "no-such-id"})
    assert len(comps) == 1 and comps[0]["type"] == "alert"
    assert "not available" in comps[0]["message"]


async def test_components_list_renders_agent_cards_and_a_revalidation_warning(db):
    orch = make_orch(db)
    aid = "ua-native-byosurfo"
    await _t(ua.create_user_agent, orch.user_agent_registry,
             agent_id=aid, owner_user_id=OWNER,
             display_name="Native Card", declared_tools=["t"], declared_scopes=["tools:read"])
    await _t(ua.mark_validated, orch.user_agent_registry, aid, "0.1.0")
    await _t(
        ua.mark_revalidation_required,
        orch.user_agent_registry,
        aid,
        True,
    )
    comps = await authoring.components(orch, OWNER, ["user"], {})
    card = next(c for c in comps if c["type"] == "card" and c.get("title") == "Native Card")
    body = card.get("content") or card.get("children") or []
    kinds = [c.get("type") for c in body]
    assert "badge" in kinds                                 # derived offline/running badge
    # the revalidation warning is inserted at the top of the card
    assert any(c.get("type") == "alert" and "rules changed" in (c.get("message") or "")
               for c in body)
    actions = [c.get("action") for c in body if c.get("action")]
    assert "chrome_author_revise" in actions and "chrome_author_delete" in actions


async def test_components_list_empty_state_and_session_button(db):
    orch = make_orch(db)
    await _session(orch, name="Open Session")           # an in-progress session, no agents
    comps = await authoring.components(orch, OWNER, ["user"], {})
    texts = [c.get("content") for c in comps if c["type"] == "text"]
    assert any("No agents yet" in (t or "") for t in texts)
    # the in-progress session is a button that re-opens the flow
    buttons = [c for c in comps if c["type"] == "button"
               and c.get("action") == "chrome_open"]
    assert any("Open Session" in (b.get("label") or "") for b in buttons)


def test_sdui_phase_fields_has_no_editable_fields_for_analyze_or_generate():
    """Analyze and Generate are decision phases, not editing phases — they expose
    actions, not a form. (Guards the ``return []`` fallthrough.)"""
    assert authoring._sdui_phase_fields({}, "analyze", _sdui) == []
    assert authoring._sdui_phase_fields({}, "generate", _sdui) == []


# ── a few more render/components branches + the _draft_id form fallback ───────

async def test_render_session_analyze_passed_shows_the_success_notice(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    await _t(aa.run_analyze, orch, OWNER, row["id"])            # passes → phase=generate
    await _t(db.update_draft_agent, row["id"], phase="analyze")  # look at the analyze card
    html = await authoring.render(orch, OWNER, ["user"], {"draft_id": row["id"]})
    assert "Analyze passed" in html and "you can generate" in html


async def test_components_analyze_not_checked_yet(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _t(db.update_draft_agent, row["id"], phase="analyze")  # reached but never run
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    texts = [c.get("content") for c in comps if c["type"] == "text"]
    assert any("Not checked yet" in (t or "") for t in texts)


async def test_components_phase_with_no_drafted_fields_prompts_the_assistant(db):
    orch = make_orch(db)
    row = await _session(orch)
    await _t(db.update_draft_agent, row["id"], phase="clarify")  # no clarify_answers yet
    comps = await authoring.components(orch, OWNER, ["user"], {"draft_id": row["id"]})
    texts = [c.get("content") for c in comps if c["type"] == "text"]
    assert any("Nothing drafted yet" in (t or "") for t in texts)
    # …and the "ask the assistant" affordance is present
    assert any(c.get("action") == "chrome_author_draft" for c in comps)


async def test_handler_reads_the_draft_id_from_collected_form_fields(db):
    """A collecting button posts the id in ``fields`` (no top-level ``draft_id``);
    ``_draft_id`` must fall back to it or the action addresses no session."""
    orch = make_orch(db)
    row = await _session(orch)
    await _walk_to_analyze(orch, db, row["id"])
    # payload carries the id ONLY under fields — the form-collect shape
    _s, params, notice = await authoring._h_analyze(
        orch, None, OWNER, ["user"], {"fields": {"draft_id": row["id"]}})
    assert params["draft_id"] == row["id"]
    assert "passed" in notice
