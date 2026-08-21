"""055 US4 (T034) — server-side provenance stamp (wire-contract §6, FR-026).

Every delivered/persisted component dict carries a top-level
``provenance: "grounded"|"estimated"|"generated"`` field, stamped by the
orchestrator from the ``_source_*`` subtree with the SAME derivation the web
footer uses (renderer._subtree_tool_source), AFTER agent/designer output is
final. Agents, the chat model, and the designer structurally cannot upgrade
trust: their supplied values are always overwritten (property-tested).
ROTE degrade/collapse rebuilds preserve the stamped field. With
FF_COMPONENT_REFINE off, no field is stamped (pre-055 wire bytes).
"""
from __future__ import annotations

import asyncio
import os
import random
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.orchestrator import (  # noqa: E402
    _derive_provenance,
    _stamp_canvas_provenance,
    _stamp_provenance,
    _tag_source,
)
from rote.adapter import ComponentAdapter  # noqa: E402
from rote.capabilities import DeviceProfile  # noqa: E402
from shared.feature_flags import flags  # noqa: E402
from webrender.renderer import provenance_of  # noqa: E402

AGENT = "weather-1"
TOOL = "get_forecast"


@pytest.fixture(autouse=True)
def refine_on(monkeypatch):
    monkeypatch.setitem(flags._flags, "component_refine", True)


def _table(**extra):
    return {"type": "table", "headers": ["h"], "rows": [["v"]], **extra}


# ── derivation (same logic as the web footer) ────────────────────────────


def test_tool_sourced_component_stamps_grounded():
    comp = _table()
    _tag_source(comp, AGENT, TOOL, tool_params={})
    assert comp["provenance"] == "grounded"


def test_no_tool_source_stamps_generated():
    comp = _table()
    _tag_source(comp, "", "")
    assert comp["provenance"] == "generated"


def test_nested_children_are_stamped_too():
    comp = {"type": "card", "title": "T", "content": [
        {"type": "text", "content": "inner"}]}
    _tag_source(comp, AGENT, TOOL)
    assert comp["provenance"] == "grounded"
    assert comp["content"][0]["provenance"] == "grounded"


def test_container_wrapping_tool_sourced_child_derives_grounded():
    # A wrapper without its own _source_tool reads grounded when its subtree
    # traces to a tool — exactly the footer's rule for designed garnish.
    wrap = {"type": "card", "content": [
        {"type": "text", "content": "x", "_source_tool": TOOL}]}
    assert _derive_provenance(wrap) == "grounded"
    assert _derive_provenance({"type": "card", "content": [
        {"type": "text", "content": "x"}]}) == "generated"


def test_estimated_is_server_only_and_invalid_kinds_fall_back():
    comp = _table(_source_tool=TOOL)
    _stamp_provenance(comp, kind="estimated")
    assert comp["provenance"] == "estimated"
    # Outside the vocabulary → derivation wins (no smuggling through kind).
    _stamp_provenance(comp, kind="verified")
    assert comp["provenance"] == "grounded"


def test_stamped_value_agrees_with_web_footer():
    shapes = [
        _table(),
        {"type": "card", "title": "T", "content": [{"type": "text", "content": "x"}]},
        {"type": "metric", "title": "M", "value": "1"},
    ]
    for tool in ("", TOOL):
        for shape in shapes:
            comp = {k: v for k, v in shape.items()}
            _tag_source(comp, AGENT if tool else "", tool)
            assert provenance_of(comp) == comp["provenance"]


# ── property: agent-supplied values are ALWAYS overwritten (FR-026) ──────


def test_property_agent_supplied_provenance_never_survives():
    rng = random.Random(55)
    pool = ["grounded", "estimated", "generated", "verified", "tool",
            "GROUNDED", " estimated ", "low_confidence", "", "junk",
            42, True, None, {"nested": "dict"}, ["grounded"]]
    for _ in range(300):
        supplied = rng.choice(pool)
        tool = rng.choice(["", TOOL])
        comp = _table()
        if supplied is not None:
            comp["provenance"] = supplied
        if rng.random() < 0.5:
            comp = {"type": "card", "title": "wrap", "content": [comp]}
            if rng.random() < 0.5:
                comp["provenance"] = rng.choice(["grounded", "estimated"])
        _tag_source(comp, AGENT if tool else "", tool)
        expected = "grounded" if tool else "generated"
        assert comp["provenance"] == expected, (
            f"supplied={supplied!r} tool={tool!r} -> {comp['provenance']!r}")
        # The footer must read back exactly what the server stamped.
        assert provenance_of(comp) == expected


def test_flag_off_stamps_nothing_and_strips_agent_values():
    flags._flags["component_refine"] = False
    absent = _table()
    _tag_source(absent, AGENT, TOOL, tool_params={})
    assert "provenance" not in absent
    # FR-026 has no flag carve-out: an agent-supplied value is STRIPPED, not
    # passed through — 055-era clients render badges from this field, so
    # pass-through would let agents mint their own trust marks.
    supplied = _table(provenance="estimated")
    _tag_source(supplied, AGENT, TOOL)
    assert "provenance" not in supplied
    _stamp_canvas_provenance([absent])
    assert "provenance" not in absent


# ── materialized-canvas stamp (designer garnish, legacy rows) ────────────


def test_garnish_forged_value_is_rederived():
    garnish = {"type": "text", "id": "dg_abc123", "content": "note",
               "provenance": "grounded"}
    _stamp_canvas_provenance([garnish])
    assert garnish["provenance"] == "generated"


def test_garnish_wrapping_generated_component_cannot_upgrade():
    # The wrapped (materialized ref) component is model-authored/"generated";
    # a forged "grounded" on the dg_ container must re-derive to generated.
    wrap = {"type": "card", "id": "dg_wrap01", "provenance": "grounded",
            "content": [{"type": "text", "content": "model prose",
                         "component_id": "wc_x", "provenance": "generated"}]}
    _stamp_canvas_provenance([wrap])
    assert wrap["provenance"] == "generated"
    assert wrap["content"][0]["provenance"] == "generated"


def test_garnish_wrapping_tool_sourced_component_reads_grounded():
    wrap = {"type": "card", "id": "dg_wrap02", "content": [
        _table(component_id="wc_y", _source_tool=TOOL, provenance="grounded")]}
    _stamp_canvas_provenance([wrap])
    assert wrap["provenance"] == "grounded"


def test_persisted_server_stamp_is_preserved_but_invalid_values_rederive():
    # "estimated" is unreachable via derivation, so a persisted estimated can
    # only be a server re-stamp (refine, D10) — the canvas pass keeps it.
    refined = _table(component_id="wc_z", _source_tool=TOOL, provenance="estimated")
    legacy = _table(component_id="wc_l", _source_tool=TOOL)
    forged = _table(component_id="wc_f", provenance="verified")
    _stamp_canvas_provenance([refined, legacy, forged])
    assert refined["provenance"] == "estimated"
    assert legacy["provenance"] == "grounded"
    assert forged["provenance"] == "generated"


# ── ROTE: provenance is a preserved field ────────────────────────────────

_WATCH_TYPES = ["alert", "badge", "card", "container", "divider",
                "keyvalue", "list", "metric", "progress", "text"]


def _watch(advertise: bool = True) -> DeviceProfile:
    payload = {"device_type": "watch"}
    if advertise:
        payload["supported_types"] = _WATCH_TYPES
    return DeviceProfile.from_dict(payload)


def test_rote_degrade_keeps_provenance_on_watch():
    hero = {"type": "hero", "id": "wc_h", "component_id": "wc_h",
            "title": "W", "subtitle": "s", "provenance": "grounded"}
    out = ComponentAdapter.adapt([hero], _watch())
    assert out[0]["type"] != "hero"
    assert out[0]["provenance"] == "grounded"


def test_rote_grid_collapse_keeps_provenance():
    grid = {"type": "grid", "id": "wc_g", "columns": 2, "provenance": "estimated",
            "children": [{"type": "card", "title": "A", "content": []}]}
    out = ComponentAdapter.adapt([grid], _watch())
    assert out[0]["type"] == "container"
    assert out[0]["provenance"] == "estimated"


def test_rote_chart_to_metric_rebuild_keeps_provenance():
    chart = {"type": "line_chart", "component_id": "wc_c", "provenance": "grounded",
             "labels": ["a"], "datasets": [{"label": "s", "data": [1]}]}
    out = ComponentAdapter.adapt([chart], _watch(advertise=False))
    assert out[0]["type"] == "metric"
    assert out[0]["provenance"] == "grounded"


def test_rote_voice_collapse_keeps_provenance():
    comp = {"type": "metric", "title": "M", "value": "1", "provenance": "grounded"}
    out = ComponentAdapter.adapt([comp], DeviceProfile.from_dict({"device_type": "voice"}))
    assert out[0]["type"] == "text"
    assert out[0]["provenance"] == "grounded"


def test_rote_unstamped_components_gain_no_field():
    out = ComponentAdapter.adapt(
        [{"type": "hero", "title": "T", "subtitle": "s"}], _watch())
    assert "provenance" not in out[0]


# ── persistence integration (real Orchestrator + DB) ─────────────────────


@pytest.fixture
async def env():
    from orchestrator.orchestrator import Orchestrator
    try:
        orch = await asyncio.to_thread(Orchestrator)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"orchestrator/database unavailable: {exc}")
    user_id = f"prov-stamp-{uuid.uuid4().hex[:8]}"
    try:
        chat_id = await asyncio.to_thread(
            orch.history.create_chat,
            user_id=user_id,
        )
        try:
            yield orch, chat_id, user_id
        finally:
            try:
                await asyncio.to_thread(
                    orch.history.delete_chat,
                    chat_id,
                    user_id=user_id,
                )
            except Exception:
                pass
    finally:
        await orch._close_started_services()


async def test_model_forged_value_overwritten_before_persist(env):
    # Parsed model components enter through _send_or_replace_components
    # without passing _tag_source — the stamp there is what stops a
    # model-authored card from persisting as "grounded".
    orch, chat_id, user_id = env
    comp = {"type": "card", "title": "Made up", "provenance": "grounded",
            "content": [{"type": "text", "content": "model prose"}]}
    ops = await orch._send_or_replace_components(None, [comp], chat_id, user_id)
    assert ops
    row = await orch.workspace.aget_by_component_id(
        chat_id, user_id, ops[0]["component_id"])
    assert row["component_data"]["provenance"] == "generated"


async def test_tool_component_persists_grounded(env):
    orch, chat_id, user_id = env
    comp = _table()
    _tag_source(comp, AGENT, TOOL, tool_params={})
    ops = await orch._send_or_replace_components(None, [comp], chat_id, user_id)
    row = await orch.workspace.aget_by_component_id(
        chat_id, user_id, ops[0]["component_id"])
    assert row["component_data"]["provenance"] == "grounded"


async def test_materialized_canvas_delivers_stamped_garnish(env):
    orch, chat_id, user_id = env
    comp = {"type": "card", "title": "Prose", "provenance": "grounded",
            "content": [{"type": "text", "content": "model prose"}]}
    ops = await orch._send_or_replace_components(None, [comp], chat_id, user_id)
    cid = ops[0]["component_id"]
    layout = [
        {"type": "card", "id": "dg_e2e00000001", "title": "Wrap",
         "provenance": "grounded",
         "content": [{"type": "ref", "component_id": cid}]},
        {"type": "text", "id": "dg_e2e00000002", "content": "garnish note",
         "provenance": "grounded"},
    ]
    assert await asyncio.to_thread(
        orch.workspace.upsert_layout, chat_id, user_id, "lay-prov-1", layout)
    canvas = await asyncio.to_thread(orch._canvas_components, chat_id, user_id)
    by_id = {c.get("id") or c.get("component_id"): c for c in canvas}
    # Designer-forged trust on garnish never survives materialization.
    assert by_id["dg_e2e00000001"]["provenance"] == "generated"
    assert by_id["dg_e2e00000002"]["provenance"] == "generated"
    # The wrapped persisted component keeps its server stamp.
    wrapped = by_id["dg_e2e00000001"]["content"][0]
    assert wrapped["component_id"] == cid
    assert wrapped["provenance"] == "generated"


async def test_legacy_rows_gain_field_on_rehydrate(env):
    # Rows persisted before the stamp (or while the flag was off) carry no
    # field; the canvas pass derives it in place on delivery.
    orch, chat_id, user_id = env
    flags._flags["component_refine"] = False
    comp = _table()
    _tag_source(comp, AGENT, TOOL, tool_params={})
    assert "provenance" not in comp
    ops = await orch._send_or_replace_components(None, [comp], chat_id, user_id)
    flags._flags["component_refine"] = True
    canvas = await asyncio.to_thread(orch._canvas_components, chat_id, user_id)
    stamped = [c for c in canvas if c.get("component_id") == ops[0]["component_id"]]
    assert stamped and stamped[0]["provenance"] == "grounded"


async def test_flag_off_canvas_is_field_free(env):
    orch, chat_id, user_id = env
    flags._flags["component_refine"] = False
    comp = _table()
    _tag_source(comp, AGENT, TOOL, tool_params={})
    await orch._send_or_replace_components(None, [comp], chat_id, user_id)
    canvas = await asyncio.to_thread(orch._canvas_components, chat_id, user_id)
    assert canvas and all("provenance" not in c for c in canvas)


class TestFlagOffStripsForgedTrust:
    """FR-026 has no flag carve-out: with FF_COMPONENT_REFINE off the server
    must STRIP agent-supplied provenance, not pass it through — 055-era
    native badge renderers would otherwise mint the agent's own trust mark."""

    def test_stamp_strips_agent_supplied_value_flag_off(self):
        from orchestrator.orchestrator import _stamp_provenance
        flags._flags["component_refine"] = False
        comp = {"type": "metric", "title": "M", "provenance": "grounded"}
        _stamp_provenance(comp)
        assert "provenance" not in comp

    def test_canvas_pass_strips_flag_off(self):
        from orchestrator.orchestrator import _stamp_canvas_provenance
        flags._flags["component_refine"] = False
        comps = [{"type": "card", "provenance": "verified"},
                 {"type": "text", "content": "x"}]
        _stamp_canvas_provenance(comps)
        assert all("provenance" not in c for c in comps)

    def test_tag_source_strips_nested_flag_off(self):
        from orchestrator.orchestrator import _tag_source
        flags._flags["component_refine"] = False
        comp = {"type": "card", "provenance": "grounded",
                "content": [{"type": "metric", "provenance": "grounded"}]}
        _tag_source(comp, "agent-1", "tool_x")
        assert "provenance" not in comp
        assert "provenance" not in comp["content"][0]
