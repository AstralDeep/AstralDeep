"""Tests for the native artifact frame (orchestrator/artifact_frame.py, ui_designer.py,
workspace.py, orchestrator.py): designer framing, validation and repair, the deterministic
fallback, document frames, and per-client gating on every send path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import artifact_frame, ui_designer  # noqa: E402
from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.workspace import iter_layout_refs, prune_layout_refs  # noqa: E402
from rote.rote import ROTE  # noqa: E402
from shared.feature_flags import flags  # noqa: E402

ALLOWED = {
    "container", "text", "card", "table", "list", "alert", "metric", "grid", "tabs",
    "collapsible", "hero", "keyvalue", "badge", "divider", "bar_chart", "file_download",
}
NATIVE_TYPES = ["text", "card", "table", "metric", "grid", "container", "hero", "file_download",
                "artifact", "html_artifact"]


def _ref(cid):
    return {"type": "ref", "component_id": cid}


def _comp(cid, ctype="table", **extra):
    c = {"type": ctype, "component_id": cid, "title": f"T-{cid}",
         "_source_agent": "a", "_source_tool": "t"}
    c.update(extra)
    return c


def _components(n=2):
    return [_comp(f"wc_{i}") for i in range(n)]


def _scripted(replies):
    calls = []

    async def llm(messages):
        calls.append(messages)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return llm, calls


def _drive(llm, *, components=None, frame=True, max_rounds=1, current_layout=None,
           canvas_rows=(), request="compare the two quarters"):
    return asyncio.run(ui_designer.design_round(
        user_request=request,
        round_components=components if components is not None else _components(),
        canvas_rows=list(canvas_rows),
        chat_id="chat-1",
        layout_key="ly_test",
        allowed_types=ALLOWED,
        llm_call=llm,
        current_layout=current_layout,
        timeout_s=2,
        max_rounds=max_rounds,
        frame=frame,
    ))


@pytest.fixture(autouse=True)
def _designer_env(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER", "true")
    monkeypatch.setenv("FF_UI_DESIGNER_TASKMODEL", "false")
    monkeypatch.setitem(flags._flags, "artifact_export", True)


def _frame(layout):
    assert isinstance(layout, list) and len(layout) == 1
    node = layout[0]
    assert node["type"] == "artifact"
    return node


def _section_refs(frame):
    return [list(artifact_frame.iter_refs(s["children"])) for s in frame["sections"]]


def test_flag_defaults_off_and_reads_registry(monkeypatch):
    assert flags._read("FF_ARTIFACT_FRAME_UNSET_PROBE", False) is False
    monkeypatch.setitem(flags._flags, "artifact_frame", False)
    assert artifact_frame.enabled() is False
    monkeypatch.setitem(flags._flags, "artifact_frame", True)
    assert artifact_frame.enabled() is True


def test_flag_registered_with_false_default(monkeypatch):
    from shared.feature_flags import FeatureFlags
    monkeypatch.delenv("FF_ARTIFACT_FRAME", raising=False)
    assert FeatureFlags().is_enabled("artifact_frame") is False
    monkeypatch.setenv("FF_ARTIFACT_FRAME", "true")
    assert FeatureFlags().is_enabled("artifact_frame") is True


def test_llm_frame_is_validated_and_emitted():
    reply = json.dumps({
        "title": "Quarter Comparison", "kind": "dashboard",
        "subtitle": "Q1 against Q2", "summary": "Two tables side by side.",
        "sections": [
            {"heading": "Headline", "layout": "stack",
             "children": [{"type": "metric", "title": "Delta", "value": "4"}]},
            {"heading": "Detail", "layout": "grid-2",
             "children": [_ref("wc_0"), _ref("wc_1")]},
        ],
    })
    llm, calls = _scripted([reply])
    frame = _frame(_drive(llm))
    assert frame["id"] == artifact_frame.frame_id("chat-1", "ly_test")
    assert frame["id"].startswith("art_") and len(frame["id"]) == 20
    assert frame["title"] == "Quarter Comparison"
    assert frame["kind"] == "dashboard"
    assert frame["subtitle"] == "Q1 against Q2"
    assert frame["summary"] == "Two tables side by side."
    assert frame["version"] == 1
    assert frame["actions"] == [{"label": "Copy", "action": "copy"},
                                {"label": "Export", "action": "export"}]
    assert [s["id"] for s in frame["sections"]] == ["s1", "s2"]
    assert [s["layout"] for s in frame["sections"]] == ["stack", "grid-2"]
    assert _section_refs(frame) == [[], ["wc_0", "wc_1"]]
    garnish = frame["sections"][0]["children"][0]
    assert garnish["id"].startswith("dg_")
    prompt = calls[0][1]["content"]
    assert "ARTIFACT FRAME" in prompt and '"sections"' in prompt
    assert 'Output ONLY JSON: {"layout"' not in prompt


def test_leftover_refs_appended_as_also_section():
    reply = json.dumps({"title": "One Table", "kind": "report",
                        "sections": [{"heading": "Main", "children": [_ref("wc_0")]}]})
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm, components=_components(3)))
    assert [s["heading"] for s in frame["sections"]] == ["Main", "Also"]
    assert _section_refs(frame) == [["wc_0"], ["wc_1", "wc_2"]]


def test_unknown_duplicate_and_garnish_artifact_types_are_repaired():
    reply = json.dumps({"title": "x" * 200, "kind": "slideshow",
                        "sections": [
                            {"heading": "A" * 120, "layout": "carousel",
                             "children": [_ref("wc_0"), _ref("ghost"),
                                          {"type": "html_artifact", "html": "<script>"}]},
                            {"heading": "B", "children": [_ref("wc_0"), _ref("wc_1")]},
                            {"heading": "Empty", "children": [_ref("ghost")]},
                        ]})
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm, request="Revenue by region this year"))
    assert frame["kind"] == "report"
    assert frame["title"] == "Revenue by region this year"
    assert len(frame["sections"]) == 2
    assert len(frame["sections"][0]["heading"]) == artifact_frame.MAX_HEADING_CHARS
    assert frame["sections"][0]["layout"] == "stack"
    assert _section_refs(frame) == [["wc_0"], ["wc_1"]]
    assert frame["sections"][0]["children"][1]["type"] == "container"
    assert "html" in frame["sections"][0]["children"][1]


def test_title_fallback_chain_prefers_hero_then_heading_then_short_text():
    hero = [{"type": "text", "content": "short note", "variant": "body"},
            {"type": "hero", "title": "Masthead"}]
    assert artifact_frame.fallback_title(hero, "req") == "Masthead"
    heading = [{"type": "text", "content": "a body line"},
               {"type": "text", "content": "## Findings\nmore", "variant": "markdown"}]
    assert artifact_frame.fallback_title(heading, "req") == "Findings"
    assert artifact_frame.fallback_title(
        [{"type": "text", "content": "Section", "variant": "h2"}], "req") == "Section"
    short = [{"type": "text", "content": "x" * 80}, {"type": "text", "content": "Quick facts"}]
    assert artifact_frame.fallback_title(short, "req") == "Quick facts"
    assert artifact_frame.fallback_title([], "  " + "w" * 90) == "w" * 60
    assert artifact_frame.fallback_title([], "") == "Response"


def test_title_from_round_components_when_llm_omits_it():
    comps = [_comp("wc_0", "hero", title="Tool Hero"), _comp("wc_1")]
    reply = json.dumps({"sections": [{"heading": "All", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, _ = _scripted([reply])
    assert _frame(_drive(llm, components=comps))["title"] == "Tool Hero"


def test_plain_layout_reply_becomes_single_stack_section():
    llm, _ = _scripted(['{"layout": [{"type": "ref", "component_id": "wc_1"}]}'])
    frame = _frame(_drive(llm))
    assert [s["heading"] for s in frame["sections"]] == ["", "Also"]
    assert frame["sections"][0]["layout"] == "stack"
    assert _section_refs(frame) == [["wc_1"], ["wc_0"]]


def test_more_than_twelve_sections_merge_into_the_last():
    comps = _components(14)
    sections = [{"heading": f"S{i}", "children": [_ref(f"wc_{i}")]} for i in range(14)]
    llm, _ = _scripted([json.dumps({"title": "Many", "sections": sections})])
    frame = _frame(_drive(llm, components=comps))
    assert len(frame["sections"]) == artifact_frame.MAX_SECTIONS
    assert _section_refs(frame)[-1] == ["wc_11", "wc_12", "wc_13"]


def test_depth_beyond_three_is_pruned_and_refs_return_as_leftovers():
    deep = {"type": "card", "title": "L1", "content": [
        {"type": "grid", "children": [
            {"type": "card", "title": "L3", "content": [_ref("wc_1")]}]}]}
    reply = json.dumps({"title": "Deep", "sections": [{"heading": "D", "children": [_ref("wc_0"), deep]}]})
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm))
    assert _section_refs(frame) == [["wc_0"], ["wc_1"]]
    pruned = frame["sections"][0]["children"][1]["content"][0]["children"][0]
    assert pruned["content"] == []


def test_node_budget_overflow_uses_deterministic_fallback():
    garnish = [{"type": "badge", "label": str(i)} for i in range(70)]
    reply = json.dumps({"title": "Busy", "sections": [{"heading": "B", "children": garnish}]})
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm))
    assert frame["title"] == "compare the two quarters"
    assert len(frame["sections"]) == 1 and frame["sections"][0]["heading"] == ""
    assert _section_refs(frame) == [["wc_0", "wc_1"]]


@pytest.mark.parametrize("reply", [
    "not json at all", "ERROR: cannot improve", '{"sections": []}', "",
    asyncio.TimeoutError(), RuntimeError("provider down"),
])
def test_failed_designs_use_deterministic_fallback_frame(reply):
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm, request="Show the latest metrics please"))
    assert frame["kind"] == "report"
    assert frame["id"] == artifact_frame.frame_id("chat-1", "ly_test")
    assert _section_refs(frame) == [["wc_0", "wc_1"]]


def test_failed_design_without_frame_keeps_legacy_none():
    llm, _ = _scripted(["not json"])
    assert _drive(llm, frame=False) is None


def test_designer_off_still_frames_when_requested(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER", "false")
    llm, calls = _scripted(["{}"])
    assert ui_designer.should_design(_components(), frame=True) is True
    assert ui_designer.should_design(_components()) is False
    frame = _frame(_drive(llm))
    assert calls == []
    assert _section_refs(frame) == [["wc_0", "wc_1"]]


def test_single_component_rounds_stay_bare():
    assert ui_designer.should_design(_components(1), frame=True) is False
    llm, _ = _scripted(['{"layout": [{"type": "ref", "component_id": "wc_0"}]}'])
    layout = _drive(llm, components=_components(1))
    assert layout == [{"type": "ref", "component_id": "wc_0"}]


def test_artifact_components_are_never_designed_or_referenced():
    page = {"type": "html_artifact", "component_id": "art_0123456789abcdef", "html": "<p>x</p>"}
    assert ui_designer.should_design([page, _comp("wc_0")], frame=True) is False
    rows = [{"component_id": "art_fedcba9876543210", "component_type": "html_artifact"}]
    reply = json.dumps({"title": "Two", "sections": [{"heading": "x", "children": [
        _ref("wc_0"), _ref("art_fedcba9876543210"), _ref("wc_1")]}]})
    llm, calls = _scripted([reply])
    frame = _frame(_drive(llm, components=_components() + [page], canvas_rows=rows))
    assert _section_refs(frame) == [["wc_0", "wc_1"]]
    assert "art_0123456789abcdef" not in calls[0][1]["content"]
    assert artifact_frame.fallback_frame([page, _comp("wc_0")], chat_id="c", layout_key="k",
                                         user_request="r") is None


def test_validate_layout_never_accepts_artifact_garnish():
    clean, _ = ui_designer.validate_layout(
        [{"type": "artifact", "sections": []}, {"type": "html_artifact", "html": "x"}],
        set(), ALLOWED | {"artifact", "html_artifact"})
    assert [n["type"] for n in clean] == ["container", "container"]


def test_refine_round_keeps_frame_and_accepts_improvement():
    first = json.dumps({"title": "First", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    better = json.dumps({"title": "Better", "kind": "analysis", "sections": [
        {"heading": "Lead", "children": [{"type": "metric", "title": "M", "value": "1"}]},
        {"heading": "Data", "layout": "grid-2", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, calls = _scripted([first, better])
    frame = _frame(_drive(llm, max_rounds=2))
    assert frame["title"] == "Better" and frame["kind"] == "analysis"
    refine_prompt = calls[1][1]["content"]
    assert '"type": "artifact"' in refine_prompt
    assert 'section "A" layout=stack' in refine_prompt
    assert "ARTIFACT FRAME shape" in refine_prompt


@pytest.mark.parametrize("second", [
    "DONE",
    json.dumps({"title": "Lost", "sections": [{"heading": "A", "children": [_ref("wc_0")]}]}),
    json.dumps({"title": "Busy", "sections": [{"heading": "B", "children": [
        {"type": "badge", "label": str(i)} for i in range(70)]}]}),
    "garbage",
])
def test_refine_round_rejections_keep_first_frame(second):
    first = json.dumps({"title": "First", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, _ = _scripted([first, second])
    frame = _frame(_drive(llm, max_rounds=2))
    assert frame["title"] == "First"
    assert _section_refs(frame) == [["wc_0", "wc_1"]]


def test_refine_identical_reply_is_stable():
    first = json.dumps({"title": "First", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, calls = _scripted([first, first, first])
    _frame(_drive(llm, max_rounds=3))
    assert len(calls) == 2


def test_retry_prompt_names_the_frame_shape():
    good = json.dumps({"title": "Ok", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, calls = _scripted(["junk", good])
    frame = _frame(_drive(llm, max_rounds=2))
    assert frame["title"] == "Ok"
    assert "the ARTIFACT FRAME JSON object" in calls[1][-1]["content"]


def test_frame_meta_is_linted(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_LINT", "true")
    reply = json.dumps({"title": "Act now", "subtitle": "Only 3 left in stock today",
                        "sections": [{"heading": "Hurry", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, _ = _scripted([reply])
    frame = _frame(_drive(llm, request="Status board"))
    assert frame["title"] == "Status board"
    assert "Only 3 left" not in frame.get("subtitle", "")
    assert frame["sections"][0]["heading"] == ""


def test_conservative_keeps_stored_frame(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_CONSERVATIVE", "true")
    stored = [{"type": "artifact", "id": "art_stored", "title": "Stored", "kind": "report",
               "version": 1, "sections": [{"id": "s1", "heading": "", "layout": "grid-2",
                                           "children": [{"type": "grid", "columns": 2, "children": [
                                               _ref("wc_0"), _ref("wc_1")]}]}]}]
    reply = json.dumps({"title": "New", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    llm, _ = _scripted([reply])
    monkeypatch.setattr(ui_designer, "should_adopt", lambda *a, **k: False)
    assert _drive(llm, current_layout=stored) == stored
    plain = [_ref("wc_1"), _ref("wc_0")]
    kept = _drive(_scripted([reply])[0], current_layout=plain)
    assert [n["component_id"] for n in kept] == ["wc_1", "wc_0"]


def test_scorer_picks_best_frame_across_rounds(monkeypatch):
    monkeypatch.setenv("FF_UI_DESIGNER_SCORER", "true")
    first = json.dumps({"title": "Best", "sections": [{"heading": "A", "children": [_ref("wc_0"), _ref("wc_1")]}]})
    second = json.dumps({"title": "Worse", "sections": [{"heading": "B", "children": [_ref("wc_1"), _ref("wc_0")]}]})
    scores = iter([5.0, 1.0])
    monkeypatch.setattr(ui_designer, "score_arrangement", lambda *a, **k: next(scores))
    llm, _ = _scripted([first, second])
    assert _frame(_drive(llm, max_rounds=2))["title"] == "Best"


def test_too_many_components_disable_framing():
    comps = _components(artifact_frame.MAX_NODES + 1)
    llm, _ = _scripted(["junk"])
    assert _drive(llm, components=comps) is None


def test_materialize_and_workspace_walk_sections():
    frame = {"type": "artifact", "id": "art_x", "sections": [
        {"id": "s1", "heading": "H", "children": [_ref("wc_0"), {"type": "text", "content": "t"}]},
        {"id": "s2", "heading": "", "children": [_ref("wc_1")]}, "junk"]}
    by_id = {"wc_0": _comp("wc_0"), "wc_1": _comp("wc_1")}
    out = ui_designer.materialize([frame], by_id)[0]
    child = out["sections"][0]["children"][0]
    assert child["component_id"] == "wc_0" and child["attributes"]["data-component-id"] == "wc_0"
    assert list(iter_layout_refs([frame])) == ["wc_0", "wc_1"]
    pruned = prune_layout_refs([frame], {"wc_0"})[0]
    assert list(iter_layout_refs([pruned])) == ["wc_1"]
    assert pruned["sections"][2] == "junk"
    sketch = ui_designer._layout_sketch([frame], {"wc_0": "table — T"})
    assert any('section "H"' in line for line in sketch)


def test_inner_layout_and_split_sections_helpers():
    frame = [{"type": "artifact", "sections": [{"children": [_ref("a")]}, {"children": "x"}, 3]}]
    assert artifact_frame.inner_layout(frame) == [_ref("a")]
    assert artifact_frame.inner_layout([_ref("b")]) == [_ref("b")]
    assert artifact_frame.split_sections([]) is None
    assert artifact_frame.split_sections("x") is None
    assert artifact_frame.split_sections({"title": "t"}) is None
    meta, sections = artifact_frame.split_sections(
        {"title": "t", "sections": [[_ref("a")], 5, {"heading": "h"},
                                    {"title": "T", "layout": [_ref("b")]},
                                    {"heading": "c", "content": [_ref("c")]}]})
    assert meta["title"] == "t"
    assert [s["children"] for s in sections] == [[_ref("a")], [_ref("b")], [_ref("c")]]
    assert sections[1]["heading"] == "T" and sections[1]["layout"] is None
    assert artifact_frame.split_sections([_ref("z")]) == ({}, [{"children": [_ref("z")]}])
    with pytest.raises(ui_designer.DesignRejected):
        ui_designer.parse_frame_response('{"title": "no sections"}')


def test_repair_sections_respects_section_cap():
    sections = [{"heading": str(i), "layout": "stack", "children": [_ref(f"r{i}")]}
                for i in range(artifact_frame.MAX_SECTIONS)]
    repaired = artifact_frame.repair_sections(sections, [f"r{i}" for i in range(12)], ["r0", "extra"])
    assert len(repaired) == artifact_frame.MAX_SECTIONS
    assert repaired[-1]["children"][-1] == _ref("extra")


def test_substantial_text_thresholds():
    assert artifact_frame.substantial_text("x" * 600) is True
    assert artifact_frame.substantial_text("short answer") is False
    assert artifact_frame.substantial_text("# Title\n\nbody\n\n## Next\nmore") is True
    assert artifact_frame.substantial_text("- a\n- b\n- c") is False
    assert artifact_frame.substantial_text("- a\n- b\n\ntext\n\n1. one\n2. two") is True
    assert artifact_frame.substantial_text("```py\n# not a heading\n```\n- item") is True
    assert artifact_frame.substantial_text("```\n# inside\n## fence\n```") is False


def test_document_frame_shape():
    text = "Intro line\n\n## Plan\n- step one\n- step two"
    doc = artifact_frame.document_frame("chat-9", text, user_request="make a plan")
    assert doc["type"] == "artifact" and doc["kind"] == "document" and doc["version"] == 1
    assert doc["id"] == doc["component_id"] and doc["id"].startswith("art_")
    assert doc["title"] == "Plan"
    assert doc["sections"] == [{"id": "s1", "heading": "", "layout": "stack",
                                "children": [{"type": "text", "content": text, "variant": "markdown"}]}]
    assert artifact_frame.document_frame("chat-9", "Short opener\nbody")["title"] == "Short opener"
    assert artifact_frame.document_frame("c", "y" * 90, user_request="the ask")["title"] == "the ask"
    assert artifact_frame.document_frame("c", "z" * 90)["title"] == "Document"
    assert artifact_frame.document_frame("c", text)["id"] != doc["id"]
    assert artifact_frame.document_frame("chat-9", text)["id"] == doc["id"]


def test_accepted_types_prefers_supported_types():
    capable = types.SimpleNamespace(supported_types=frozenset({"text", "artifact", "html_artifact"}))
    partial = types.SimpleNamespace(supported_types=frozenset({"text", "artifact"}))
    legacy = types.SimpleNamespace(supported_types=frozenset({"text"}))
    unknown = types.SimpleNamespace(supported_types=None)
    assert artifact_frame.accepted_types(capable, []) == artifact_frame.ARTIFACT_TYPES
    assert artifact_frame.accepted_types(partial, []) == {"artifact"}
    assert artifact_frame.accepted_types(legacy, ["artifact_v1"]) == frozenset()
    assert artifact_frame.accepted_types(unknown, ["artifact_v1"]) == artifact_frame.ARTIFACT_TYPES
    assert artifact_frame.accepted_types(unknown, "artifact_v1") == frozenset()
    assert artifact_frame.accepted_types(None, None) == frozenset()


def _rote_socket(supported=None, device="macos"):
    rote = ROTE()
    ws = object()
    info = {"device_type": device}
    if supported is not None:
        info["supported_types"] = supported
    rote.register_device(ws, info)
    return rote, ws


def test_socket_capability_reads_profile_and_session():
    rote, ws = _rote_socket(NATIVE_TYPES)
    orch = types.SimpleNamespace(rote=rote, ui_sessions={})
    assert artifact_frame.artifact_capable(orch, ws) is True
    rote2, ws2 = _rote_socket(None, device="browser")
    orch2 = types.SimpleNamespace(rote=rote2, ui_sessions={ws2: {"_client_capabilities": ["artifact_v1"]}})
    assert artifact_frame.artifact_capable(orch2, ws2) is True
    orch2.ui_sessions = {}
    assert artifact_frame.artifact_capable(orch2, ws2) is False
    assert artifact_frame.socket_accepted_types(orch, None) == frozenset()
    assert artifact_frame.socket_accepted_types(types.SimpleNamespace(), object()) == frozenset()


def _sample_frame():
    return {"type": "artifact", "id": "art_aaaaaaaaaaaaaaaa", "title": "Frame", "kind": "report",
            "version": 1, "sections": [
                {"id": "s1", "heading": "Numbers", "layout": "grid-2",
                 "children": [_comp("wc_0"), "junk"]},
                {"id": "s2", "heading": "", "layout": "stack", "children": [_comp("wc_1", "metric", value="1")]},
                "junk"]}


def _page(cid="art_bbbbbbbbbbbbbbbb", size=10):
    return {"type": "html_artifact", "id": cid, "component_id": cid, "title": "Page Tool",
            "kind": "app", "version": 1, "html": "x" * size, "provenance": "estimated"}


def test_gate_flattens_for_non_capable_clients():
    out = artifact_frame.gate_components([_sample_frame(), {"type": "text", "content": "t"}], frozenset())
    assert [c["type"] for c in out] == ["text", "table", "metric", "text"]
    assert out[0]["content"] == "Numbers" and out[0]["variant"] == "h2"
    assert out[0]["id"].startswith("dg_")
    assert out[1]["component_id"] == "wc_0"


def test_gate_passes_frames_through_for_capable_clients_with_adapted_children():
    adapt = lambda kids: [dict(k, adapted=True) for k in kids if isinstance(k, dict)]  # noqa: E731
    out = artifact_frame.gate_components([_sample_frame()], artifact_frame.ARTIFACT_TYPES, adapt=adapt)
    assert out[0]["type"] == "artifact"
    assert [c.get("adapted") for c in out[0]["sections"][0]["children"]] == [True]
    assert out[0]["sections"][2] == "junk"
    same = [{"type": "text"}]
    assert artifact_frame.gate_components(same, frozenset()) is same
    assert artifact_frame.gate_components(None, frozenset()) is None


def test_gate_html_fallbacks_and_budget():
    link = lambda comp: f"/dl/{comp['component_id']}"  # noqa: E731
    down = artifact_frame.gate_components([_page()], frozenset(), download=link)[0]
    assert down == {"id": "art_bbbbbbbbbbbbbbbb", "component_id": "art_bbbbbbbbbbbbbbbb",
                    "provenance": "estimated", "type": "file_download",
                    "label": "Download Page Tool", "url": "/dl/art_bbbbbbbbbbbbbbbb",
                    "filename": "page-tool.html"}
    note = artifact_frame.gate_components([_page()], frozenset())[0]
    assert note["type"] == "text" and "Astral Mac app" in note["content"]
    assert "<" not in note["content"] and "html" not in note
    untitled = artifact_frame.html_fallback({"type": "html_artifact", "title": "!!!"},
                                            lambda c: "/x")
    assert untitled["filename"] == "page.html"
    older, newer = _page("art_1111111111111111", 600), _page("art_2222222222222222", 600)
    kept = artifact_frame.gate_components([older, newer], artifact_frame.ARTIFACT_TYPES,
                                          download=link, html_budget=1000)
    assert kept[0]["type"] == "file_download" and kept[1] is newer
    both = artifact_frame.gate_components([older, newer], artifact_frame.ARTIFACT_TYPES)
    assert both == [older, newer]
    big = _page("art_3333333333333333", 5000)
    assert artifact_frame.gate_components([big], {"html_artifact"}, html_budget=10)[0] is big


def test_gate_history_never_ships_html_and_export_flattens():
    hist = artifact_frame.gate_history([_page(), _sample_frame()], artifact_frame.ARTIFACT_TYPES)
    assert hist[0]["type"] == "text" and hist[1]["type"] == "artifact"
    plain = [{"type": "text"}]
    assert artifact_frame.gate_history(plain, frozenset()) is plain
    exported = artifact_frame.flatten_for_export([_sample_frame(), _page()])
    assert [c["type"] for c in exported] == ["text", "table", "metric", "text"]


class _FakeRote:
    def __init__(self, supported):
        self.rote, self.ws = _rote_socket(supported)

    def adapt(self, websocket, components):
        return self.rote.adapt(self.ws, components)

    def get_profile(self, websocket):
        return self.rote.get_profile(self.ws)


def _render_orch(supported):
    orch = types.SimpleNamespace(rote=_FakeRote(supported), sent=[], ui_sessions={},
                                 _ws_active_chat={}, ui_clients=[])

    async def _safe_send(websocket, data):
        orch.sent.append(json.loads(data))
        return True

    orch._safe_send = _safe_send
    orch._gate_artifacts = types.MethodType(Orchestrator._gate_artifacts, orch)
    orch.send_ui_render = types.MethodType(Orchestrator.send_ui_render, orch)
    orch.send_ui_upsert = types.MethodType(Orchestrator.send_ui_upsert, orch)
    orch._get_user_id = lambda ws: "owner"
    return orch


LEGACY_TYPES = ["text", "table", "metric", "card", "container", "file_download"]


def test_send_ui_render_flattens_for_legacy_native_client():
    orch = _render_orch(LEGACY_TYPES)
    ws = object()
    orch._ws_active_chat[id(ws)] = "chat-1"
    asyncio.run(orch.send_ui_render(ws, [_sample_frame(), _page()]))
    comps = orch.sent[0]["components"]
    assert [c["type"] for c in comps] == ["text", "table", "metric", "file_download"]
    assert comps[-1]["url"] == "/api/export/component/art_bbbbbbbbbbbbbbbb.html?chat_id=chat-1"
    assert "[unsupported component" not in (orch.sent[0]["html"] or "")


def test_send_ui_render_passes_artifacts_to_capable_client():
    orch = _render_orch(NATIVE_TYPES)
    asyncio.run(orch.send_ui_render(object(), [_sample_frame(), _page()]))
    comps = orch.sent[0]["components"]
    assert [c["type"] for c in comps] == ["artifact", "html_artifact"]
    assert comps[0]["sections"][0]["children"][0]["component_id"] == "wc_0"
    assert comps[1]["html"] == "x" * 10


def test_send_ui_upsert_gates_each_op():
    orch = _render_orch(LEGACY_TYPES)
    ws = object()
    ops = [{"op": "upsert", "component_id": "art_aaaaaaaaaaaaaaaa", "component": _sample_frame()},
           {"op": "upsert", "component_id": "art_bbbbbbbbbbbbbbbb", "component": _page()},
           {"op": "upsert", "component_id": "wc_9", "component": _comp("wc_9")},
           {"op": "remove", "component_id": "gone"}]
    asyncio.run(orch.send_ui_upsert(ws, "chat-1", "owner", ops))
    wire = orch.sent[0]["ops"]
    assert wire[0]["component"]["type"] == "container"
    assert [c["type"] for c in wire[0]["component"]["content"]] == ["text", "table", "metric"]
    assert wire[0]["component"]["component_id"] == "art_aaaaaaaaaaaaaaaa"
    assert wire[1]["component"]["type"] == "file_download"
    assert wire[2]["component"]["type"] == "table"
    assert wire[3] == {"op": "remove", "component_id": "gone"}
    capable = _render_orch(NATIVE_TYPES)
    asyncio.run(capable.send_ui_upsert(object(), "chat-1", "owner", ops[:2]))
    assert [o["component"]["type"] for o in capable.sent[0]["ops"]] == ["artifact", "html_artifact"]


def test_gate_artifacts_history_mode_uses_fallback():
    orch = _render_orch(NATIVE_TYPES)
    out = orch._gate_artifacts(object(), [_page()], chat_id="chat-2", history=True)
    assert out[0]["type"] == "file_download"
    assert out[0]["url"].endswith("chat_id=chat-2")


def _wanted_orch(capable=True, frame_flag=True):
    supported = NATIVE_TYPES if capable else ["text"]
    orch = types.SimpleNamespace(rote=_FakeRote(supported), ui_sessions={})
    orch._artifact_frame_wanted = types.MethodType(Orchestrator._artifact_frame_wanted, orch)
    orch._narrative_canvas_doc = types.MethodType(Orchestrator._narrative_canvas_doc, orch)
    orch._narrative_is_long = Orchestrator._narrative_is_long
    orch._narrative_doc_card = Orchestrator._narrative_doc_card
    return orch


def test_narrative_document_frame_only_for_capable_clients(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_frame", True)
    text = "Intro\n\n## Results\n- one\n- two"
    doc = _wanted_orch()._narrative_canvas_doc(object(), "chat-1", text, "request")
    assert doc["type"] == "artifact" and doc["kind"] == "document" and doc["title"] == "Results"
    legacy = _wanted_orch(capable=False)._narrative_canvas_doc(object(), "chat-1", text, "request")
    assert legacy["type"] == "card" and legacy["id"].startswith("doc_")
    assert _wanted_orch()._narrative_canvas_doc(object(), None, text) is None
    assert _wanted_orch()._narrative_canvas_doc(object(), "chat-1", "brief reply") is None
    monkeypatch.setitem(flags._flags, "artifact_frame", False)
    assert _wanted_orch()._narrative_canvas_doc(object(), "chat-1", text)["type"] == "card"


class _Workspace:
    def __init__(self, layouts=(), rows=()):
        self.layouts, self.rows = list(layouts), list(rows)

    def live_layouts(self, chat_id, user_id):
        return self.layouts

    def live_rows(self, chat_id, user_id):
        return self.rows


def test_run_designer_frames_for_capable_socket(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_frame", True)
    orch = _wanted_orch()
    orch.workspace = _Workspace(layouts=[{"layout_key": "other", "layout": []}])
    orch.sent = []

    async def _safe_send(ws, data):
        orch.sent.append(json.loads(data))

    async def _call_llm(ws, messages, **kwargs):
        return types.SimpleNamespace(content="not json"), None

    orch._safe_send = _safe_send
    orch._call_llm = _call_llm
    run = types.MethodType(Orchestrator._run_designer, orch)
    layout = asyncio.run(run(object(), _components(), "chat-1", "owner", "Show sales", "ly_k"))
    frame = _frame(layout)
    assert frame["id"] == artifact_frame.frame_id("chat-1", "ly_k")
    assert orch.sent and orch.sent[0]["message"].startswith("Designing your layout")
    monkeypatch.setitem(flags._flags, "artifact_frame", False)
    assert asyncio.run(run(object(), _components(), "chat-1", "owner", "Show sales", "ly_k")) is None


def test_post_done_designer_runs_for_capable_native_socket(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_frame", True)
    monkeypatch.setitem(flags._flags, "designer_all_devices", True)
    monkeypatch.setenv("FF_UI_DESIGNER", "false")
    orch = _wanted_orch()
    orch._ws_timeline_mode = {}
    orch.history = types.SimpleNamespace(get_latest_message_id=lambda chat, user_id=None: "m1")
    saved = []
    orch.workspace = types.SimpleNamespace(
        upsert_layout=lambda chat, user, key, layout: saved.append((key, layout)))
    orch._run_designer = AsyncMock(return_value=[{"type": "artifact", "sections": []}])
    post = types.MethodType(Orchestrator._design_turn_post_done, orch)
    marker = asyncio.run(post(object(), "chat-1", "owner", "req", _components(), turn_marker="m1"))
    assert marker == "m1" and saved and saved[0][1][0]["type"] == "artifact"
    legacy = _wanted_orch(capable=False)
    legacy._ws_timeline_mode = {}
    legacy._run_designer = AsyncMock()
    post2 = types.MethodType(Orchestrator._design_turn_post_done, legacy)
    assert asyncio.run(post2(object(), "chat-1", "owner", "req", _components())) is None
    legacy._run_designer.assert_not_awaited()


def test_refine_never_edits_artifacts():
    orch = types.SimpleNamespace()
    refine = types.MethodType(Orchestrator._refine_component_llm, orch)
    assert asyncio.run(refine(object(), _page(), "make it blue")) is None
    assert asyncio.run(refine(object(), _sample_frame(), "make it blue")) is None


def test_node_walkers_cover_tabs_sections_and_non_dicts():
    tree = ["junk", {"type": "tabs", "tabs": [{"label": "a", "content": [_ref("t1")]}, "x"]},
            {"type": "artifact", "sections": [{"children": [_ref("s1")]}, "y"]}]
    assert list(artifact_frame.iter_refs(tree)) == ["t1", "s1"]
    assert artifact_frame.count_nodes(tree) == 4
    pruned = artifact_frame._prune_depth(
        [{"type": "tabs", "tabs": [{"label": "a", "content": [_ref("t1")]}, "x"]}], 3)
    assert pruned[0]["tabs"] == [{"label": "a", "content": []}, "x"]
    assert artifact_frame._heading_of({"type": "text", "content": "plain first line\n# later"}) == ""
    assert artifact_frame._heading_of({"type": "text", "content": "\n\n"}) == ""
    assert artifact_frame._short_text_of({"type": "metric", "content": "x"}) == ""
    assert artifact_frame._short_text_of({"type": "text", "content": "two\nlines"}) == ""


def test_deliver_round_components_keeps_flat_path_for_single_component(monkeypatch):
    monkeypatch.setitem(flags._flags, "artifact_frame", True)
    rote, ws = _rote_socket(None, device="browser")
    orch = types.SimpleNamespace(rote=types.SimpleNamespace(get_profile=lambda w: rote.get_profile(ws)),
                                 ui_sessions={}, _ws_timeline_mode={})
    orch._send_or_replace_components = AsyncMock(return_value=["op"])
    deliver = types.MethodType(Orchestrator._deliver_round_components, orch)
    assert asyncio.run(deliver(object(), _components(1), "chat-1", "owner")) == ["op"]
    orch._send_or_replace_components.assert_awaited_once()


async def _register_with_flags(monkeypatch, enabled):
    from orchestrator import local_agents
    from shared import attachment_materializer, attachment_resolver
    monkeypatch.setattr(flags, "is_enabled", lambda name: name in enabled)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *_args: False)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *_args: None)
    plane = types.SimpleNamespace(runtime=object(), repositories=object(), blobs=object(),
                                  attachment_materializer=object())
    host = types.SimpleNamespace(runtime_composition=types.SimpleNamespace(plane=plane), local_agents={},
                                 register_agent=AsyncMock())
    return await local_agents.register_built_ins(host), host


@pytest.mark.parametrize("enabled,expected", [
    ({"artifact_frame", "inprocess_agents"}, ["artifacts-1"]),
    ({"artifact_frame"}, []),
    ({"inprocess_agents"}, []),
])
def test_artifacts_agent_registers_in_process_only_with_flags(monkeypatch, tmp_path, enabled, expected):
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "artifacts-key.pem"))
    registered, host = asyncio.run(_register_with_flags(monkeypatch, enabled))
    assert registered == expected
    if expected:
        agent = host.local_agents["artifacts-1"]
        assert agent.mcp_server._orchestrator is host
        assert host.register_agent.await_args.args[1].agent_card.agent_id == "artifacts-1"
