"""Feature 055 US4 — component_refine + component_restore handlers (T038).

Exercises the real, unbound ``Orchestrator`` methods over a fake ``self``
plus a REAL Postgres-backed ``WorkspaceManager``/``HistoryManager`` (the
test_component_action.py harness pattern), per
specs/055-uniform-artifacts/contracts/wire-contract.md §3 and research D10.

Covers: the full gate sequence (FF_COMPONENT_REFINE off, watch carve-out,
timeline read-only, security flags, per-user permission on the source
agent/tool, the 054 per-user LLM gate for refine), same-type-validated
bounded LLM edit with provenance re-stamped 'estimated', archive-before-
overwrite into component_version, force-upsert onto the same identity with
ui_upsert fan-out, restore without any LLM, and the audit trail
(workspace.component_refined / component_restored / action_denied).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator import artifact_versions as av
from orchestrator.workspace import WorkspaceManager
from orchestrator.orchestrator import Orchestrator
from shared.feature_flags import flags
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)


class _FakeWS:
    """Hashable, identity-compared websocket stand-in (see
    test_component_action.py for why SimpleNamespace doesn't work)."""

    def __init__(self, label: str = ""):
        self.label = label


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def refine_flag_on(monkeypatch):
    """US4 default posture; flag-off tests override inside the test body."""
    monkeypatch.setitem(flags._flags, "component_refine", True)


@pytest.fixture
def chat_env(plane_runtime):
    history = history_manager(plane_runtime)
    user_id = f"test-user-{uuid.uuid4()}"
    chat_id = history.create_chat(user_id=user_id)
    yield history, user_id, chat_id
    history.delete_chat(chat_id, user_id=user_id)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("component_refine") as runtime:
        yield runtime


@pytest.fixture
def audit_events(monkeypatch):
    events = []

    async def _record(**kwargs):
        events.append(kwargs)

    import audit.hooks
    monkeypatch.setattr(audit.hooks, "record_workspace_event", _record)
    return events


def _make_fake(history, user_id, *, allowed=True, security_flags=None,
               llm_configured=True, llm_result=None):
    """Fake orchestrator ``self`` carrying only what the handlers touch, with
    the real 055 implementations bound onto it. ``llm_result`` is what the
    faked ``_call_llm_json`` seam returns to the REAL ``_refine_component_llm``."""
    from rote.rote import ROTE

    sent = []            # (ws, parsed-json) for every _safe_send
    renders = []         # (ws, components, target) for every send_ui_render
    llm_calls = []       # (messages, kwargs) for every _call_llm_json
    unconfig_calls = []  # kwargs of every _record_llm_unconfigured

    async def _safe_send(ws, payload):
        sent.append((ws, json.loads(payload)))

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((ws, components, target))

    async def _call_llm_json(ws, messages, **kwargs):
        llm_calls.append((messages, kwargs))
        return llm_result

    async def llm_configured_for(uid):
        return llm_configured

    async def _record_llm_unconfigured(recorder, **kwargs):
        unconfig_calls.append(kwargs)

    fake = types.SimpleNamespace(
        workspace=WorkspaceManager(
            history,
            plane_runtime=history.plane_runtime,
            plane_repositories=history.plane_repositories,
        ),
        history=history,
        plane_runtime=history.plane_runtime,
        plane_repositories=history.plane_repositories,
        runtime_composition=types.SimpleNamespace(
            plane=types.SimpleNamespace(
                runtime=history.plane_runtime,
                repositories=history.plane_repositories,
            )
        ),
        _ws_active_chat={},
        _ws_timeline_mode={},
        _workspace_locks={},
        ui_clients=[],
        ui_sessions={},
        rote=ROTE(),
        security_flags=security_flags or {},
        tool_permissions=types.SimpleNamespace(is_tool_allowed=lambda u, a, t: allowed),
        audit_recorder=None,
        _get_user_id=lambda ws: user_id,
        _llm_audit_principals=lambda ws: (user_id, user_id),
        _safe_send=_safe_send,
        send_ui_render=send_ui_render,
        _call_llm_json=_call_llm_json,
        llm_configured_for=llm_configured_for,
        _record_llm_unconfigured=_record_llm_unconfigured,
    )
    for name in ("_refine_restore_gate", "_handle_component_refine",
                 "_handle_component_restore", "_refine_component_llm",
                 "_validate_component_tree", "_component_action_allowed",
                 "_audit_workspace_denial", "send_ui_upsert"):
        setattr(fake, name, types.MethodType(getattr(Orchestrator, name), fake))
    fake._sent = sent
    fake._renders = renders
    fake._llm_calls = llm_calls
    fake._unconfig_calls = unconfig_calls
    return fake


def _seed_component(workspace, chat_id, user_id, *, agent="agent-x",
                    tool="list_patients", params=None, title="Patients",
                    rows=None):
    comp = {
        "type": "table",
        "title": title,
        "headers": ["Name"],
        "rows": rows if rows is not None else [["Alice"]],
        "_source_agent": agent,
        "_source_tool": tool,
        "_source_params": params if params is not None else {"page": 1},
    }
    ops = workspace.upsert(chat_id, user_id, [comp])
    assert len(ops) == 1
    return ops[0]["component_id"]


def _run(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result
    return asyncio.run(_wrapper())


def _alerts(fake, target="chat"):
    out = []
    for _, comps, tgt in fake._renders:
        if tgt == target:
            out.extend(c for c in comps if isinstance(c, dict) and c.get("type") == "alert")
    return out


def _refined_table(**extra):
    comp = {"type": "table", "title": "Patients", "headers": ["Name"],
            "rows": [["Alice"], ["TOTAL: 1"]]}
    comp.update(extra)
    return comp


# ---------------------------------------------------------------------------
# Refine happy path
# ---------------------------------------------------------------------------

def test_refine_happy_path(chat_env, audit_events):
    """FR-022/FR-024: the prior dict is archived BEFORE the overwrite, the
    same-type LLM edit lands on the SAME identity, provenance re-stamps
    'estimated', source attribution carries over, a ui_upsert fans out, and
    the mutation is audited as workspace.component_refined."""
    history, user_id, chat_id = chat_env
    # The model also tries to self-upgrade trust and mint attribution —
    # both must be stripped (FR-026).
    fake = _make_fake(history, user_id, llm_result=_refined_table(
        provenance="grounded", _source_agent="evil-agent", id="au_hijack"))
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("origin")

    _run(fake._handle_component_refine(ws, user_id, {
        "chat_id": chat_id, "component_id": cid,
        "instruction": "add a totals row",
    }))

    # Exactly one bounded LLM call carrying the instruction + original JSON.
    assert len(fake._llm_calls) == 1
    messages, kwargs = fake._llm_calls[0]
    assert kwargs.get("feature") == "component_refine"
    assert "add a totals row" in messages[-1]["content"]
    assert "Alice" in messages[-1]["content"]

    # v1 archived with the ORIGINAL content, reason 'refine'.
    v1 = av.get_version(history, chat_id, user_id, cid, 1)
    assert v1 is not None and v1["reason"] == "refine"
    assert v1["component"]["rows"] == [["Alice"]]

    # Live row updated in place under the same identity, no duplicate row.
    rows = fake.workspace.live_rows(chat_id, user_id)
    assert len(rows) == 1
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"], ["TOTAL: 1"]]
    assert data["component_id"] == cid
    # Trust + attribution are server-owned: estimated (no tool re-run),
    # original source kept, model-minted id gone.
    assert data["provenance"] == "estimated"
    assert data["_source_agent"] == "agent-x"
    assert data["_source_tool"] == "list_patients"
    assert data["_source_params"] == {"page": 1}
    assert data.get("id") != "au_hijack"

    # ui_upsert fan with the dual shape; final chat_status done.
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 1
    op = upserts[0]["ops"][0]
    assert op["component_id"] == cid
    assert op["component"]["rows"] == [["Alice"], ["TOTAL: 1"]]
    assert op["html"] and f'data-component-id="{cid}"' in op["html"]
    statuses = [m for _, m in fake._sent if m["type"] == "chat_status"]
    assert statuses and statuses[-1]["status"] == "done"

    # Snapshot with the refine cause; audit row present.
    snaps = [
        snapshot
        for snapshot in fake.workspace.list_snapshots(chat_id, user_id)
        if snapshot["cause"] == "component_refine"
    ]
    assert len(snaps) == 1
    refined_events = [e for e in audit_events if e.get("action") == "component_refined"]
    assert len(refined_events) == 1
    assert refined_events[0]["component_id"] == cid
    assert refined_events[0]["detail"]["archived_version"] == 1


def test_refine_sourceless_component_skips_agent_gates(chat_env, audit_events):
    """A model-authored artifact has no source agent/tool to gate — refine
    still works (the LLM gate is the operative one), even when the
    permission checker would deny everything."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False,
                      llm_result={"type": "card", "title": "Note", "body": "edited"})
    ops = fake.workspace.upsert(chat_id, user_id, [
        {"type": "card", "title": "Note", "body": "original"}])
    cid = ops[0]["component_id"]

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "edit it",
    }))

    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["body"] == "edited"
    assert data["provenance"] == "estimated"
    assert [e for e in audit_events if e.get("action") == "action_denied"] == []


# ---------------------------------------------------------------------------
# Refine refusal paths
# ---------------------------------------------------------------------------

def test_refine_flag_off_refused(chat_env, audit_events, monkeypatch):
    """D12: FF_COMPONENT_REFINE off ⇒ the action refuses honestly — no LLM
    call, no version row, no workspace change."""
    monkeypatch.setitem(flags._flags, "component_refine", False)
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "error"
    assert "not enabled" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "feature_disabled"
    assert av.list_versions(history, chat_id, user_id, cid) == []
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"]]


def test_restore_flag_off_refused(chat_env, audit_events, monkeypatch):
    monkeypatch.setitem(flags._flags, "component_refine", False)
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 1,
    }))

    alerts = _alerts(fake)
    assert alerts and "not enabled" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1 and denials[0]["detail"]["reason"] == "feature_disabled"


def test_refine_timeline_mode_refused(chat_env, audit_events):
    """FR-023: read-only timeline views refuse refine exactly like existing
    timeline mutations."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("timeline")
    fake._ws_timeline_mode[id(ws)] = True

    _run(fake._handle_component_refine(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and "past workspace state" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "timeline_readonly"


def test_refine_unconfigured_llm_refused_by_054_gate(chat_env, audit_events):
    """quickstart §US4.4: an unconfigured-LLM user's refine is refused by the
    054 gate — audited as llm_unconfigured, honest alert, nothing runs."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_configured=False,
                      llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    assert len(fake._unconfig_calls) == 1
    assert fake._unconfig_calls[0]["feature"] == "ui_event:component_refine"
    alerts = _alerts(fake)
    assert alerts and "Set up your AI provider" in alerts[0]["message"]
    assert av.list_versions(history, chat_id, user_id, cid) == []


def test_refine_permission_denied(chat_env, audit_events):
    """FR-023: the per-user permission gate on the source agent/tool applies
    to refine exactly as it does to component_action."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, allowed=False, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and "Action not permitted" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert "permissions" in denials[0]["detail"]["reason"]


def test_refine_security_flag_block(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(
        history, user_id,
        security_flags={"agent-x": {"list_patients": {"blocked": True}}},
        llm_result=_refined_table(),
    )
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and "blocked by a security review" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert "security review" in denials[0]["detail"]["reason"]


def test_refine_watch_profile_refused(chat_env, audit_events):
    """wire-contract §3: the watch renders no refine affordance and a raw
    frame from one is refused honestly."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)
    fake.rote = types.SimpleNamespace(
        get_profile=lambda ws: types.SimpleNamespace(device_type="watch"))

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and "watch" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "watch_unsupported"


def test_refine_missing_instruction_refused(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "   ",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and alerts[0]["variant"] == "warning"


def test_refine_unknown_component_refused(chat_env):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": "wc_deadbeefdeadbeef",
        "instruction": "x",
    }))

    assert fake._llm_calls == []
    alerts = _alerts(fake)
    assert alerts and "no longer available" in alerts[0]["message"]


def test_refine_type_change_rejected(chat_env):
    """D10: the edit is constrained to the SAME component type — a
    type-changing result leaves the component untouched (no archive, no
    overwrite) with an honest explanation."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id,
                      llm_result={"type": "card", "title": "Patients", "body": "nope"})
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "make it a card",
    }))

    assert len(fake._llm_calls) == 1
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"]]
    assert av.list_versions(history, chat_id, user_id, cid) == []
    alerts = _alerts(fake)
    assert alerts and "left unchanged" in alerts[0]["message"]


def test_refine_unusable_llm_output_rejected(chat_env):
    """A None/refusal from the LLM seam leaves the component untouched."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=None)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "x",
    }))

    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"]]
    assert av.list_versions(history, chat_id, user_id, cid) == []


# ---------------------------------------------------------------------------
# Version cycle: refine → list → restore → audit
# ---------------------------------------------------------------------------

def test_version_cycle_refine_list_restore_audit(chat_env, audit_events):
    """FR-024: refine archives v1; restore archives the refined state as v2
    (reason 'restore') and puts v1's content back under the same identity;
    both mutations are audited and fanned out."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_result=_refined_table())
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_refine(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "instruction": "add a totals row",
    }))
    listed = av.list_versions(history, chat_id, user_id, cid)
    assert [v["version_no"] for v in listed] == [1]
    assert listed[0]["reason"] == "refine"

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 1,
    }))

    # The refined state was archived as v2 before the restore overwrite.
    listed = av.list_versions(history, chat_id, user_id, cid)
    assert [v["version_no"] for v in listed] == [2, 1]
    assert listed[0]["reason"] == "restore"
    v2 = av.get_version(history, chat_id, user_id, cid, 2)
    assert v2["component"]["rows"] == [["Alice"], ["TOTAL: 1"]]

    # Live row is v1's content again, same identity, single row.
    rows = fake.workspace.live_rows(chat_id, user_id)
    assert len(rows) == 1
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"]]
    assert data["component_id"] == cid

    # Both verbs fanned a ui_upsert onto the same identity.
    upserts = [m for _, m in fake._sent if m["type"] == "ui_upsert"]
    assert len(upserts) == 2
    assert all(u["ops"][0]["component_id"] == cid for u in upserts)

    # Restore audit: workspace.component_restored with both version numbers.
    restored = [e for e in audit_events if e.get("action") == "component_restored"]
    assert len(restored) == 1
    assert restored[0]["component_id"] == cid
    assert restored[0]["detail"] == {"restored_version": 1, "archived_version": 2}
    # Restore snapshot cause recorded too.
    snaps = [
        snapshot
        for snapshot in fake.workspace.list_snapshots(chat_id, user_id)
        if snapshot["cause"] == "component_restore"
    ]
    assert len(snaps) == 1


def test_restore_needs_no_llm(chat_env, audit_events):
    """wire-contract §3: restore runs the same gates MINUS the LLM gate —
    an unconfigured-LLM user can still restore."""
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id, llm_configured=False)
    cid = _seed_component(fake.workspace, chat_id, user_id)
    # Seed an archived version directly (sync, off-loop).
    current = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    old = dict(current)
    old["rows"] = [["Old Bob"]]
    av.archive(history, chat_id, user_id, cid, old, "refine")

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 1,
    }))

    assert fake._unconfig_calls == []
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Old Bob"]]
    restored = [e for e in audit_events if e.get("action") == "component_restored"]
    assert len(restored) == 1


def test_restore_unknown_version_refused(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)

    _run(fake._handle_component_restore(_FakeWS(), user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 7,
    }))

    alerts = _alerts(fake)
    assert alerts and "version is no longer available" in alerts[0]["message"]
    data = fake.workspace.get_by_component_id(chat_id, user_id, cid)["component_data"]
    assert data["rows"] == [["Alice"]]
    assert [e for e in audit_events if e.get("action") == "component_restored"] == []


def test_restore_timeline_mode_refused(chat_env, audit_events):
    history, user_id, chat_id = chat_env
    fake = _make_fake(history, user_id)
    cid = _seed_component(fake.workspace, chat_id, user_id)
    ws = _FakeWS("timeline")
    fake._ws_timeline_mode[id(ws)] = True

    _run(fake._handle_component_restore(ws, user_id, {
        "chat_id": chat_id, "component_id": cid, "version_no": 1,
    }))

    alerts = _alerts(fake)
    assert alerts and "past workspace state" in alerts[0]["message"]
    denials = [e for e in audit_events if e.get("action") == "action_denied"]
    assert len(denials) == 1
    assert denials[0]["detail"]["reason"] == "timeline_readonly"
