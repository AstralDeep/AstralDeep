"""Tests for orchestrator/ui_designer.py: the invocation predicate, response parsing,
the validate-dedupe-repair pipeline, deterministic garnish ID stamping,
materialization onto webrender, and the fail-open design_round driver.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import ui_designer  # noqa: E402
from orchestrator.ui_designer import (  # noqa: E402
    DesignRejected,
    build_design_messages,
    design_round,
    materialize,
    parse_design_response,
    repair_layout,
    should_design,
    stamp_garnish_ids,
    validate_layout,
)

ALLOWED = {
    "container", "text", "button", "input", "param_picker", "card", "table",
    "list", "alert", "progress", "metric", "code", "image", "grid", "tabs",
    "divider", "collapsible", "bar_chart", "line_chart", "pie_chart",
    "plotly_chart", "color_picker", "theme_apply", "file_upload",
    "file_download", "audio",
}


def _ref(cid):
    return {"type": "ref", "component_id": cid}


def _comp(cid, **extra):
    c = {"type": "table", "component_id": cid, "title": f"T-{cid}",
         "_source_agent": "a", "_source_tool": "t"}
    c.update(extra)
    return c


def test_designer_enabled_default_on(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    assert ui_designer.designer_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_designer_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("FF_UI_DESIGNER", value)
    assert ui_designer.designer_enabled() is False


def test_timeout_default_and_override(monkeypatch):
    monkeypatch.delenv("UI_DESIGNER_TIMEOUT_SECONDS", raising=False)
    assert ui_designer.designer_timeout_seconds() == ui_designer.DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setenv("UI_DESIGNER_TIMEOUT_SECONDS", "3.5")
    assert ui_designer.designer_timeout_seconds() == 3.5
    monkeypatch.setenv("UI_DESIGNER_TIMEOUT_SECONDS", "garbage")
    assert ui_designer.designer_timeout_seconds() == ui_designer.DEFAULT_TIMEOUT_SECONDS
    monkeypatch.setenv("UI_DESIGNER_TIMEOUT_SECONDS", "-1")
    assert ui_designer.designer_timeout_seconds() == ui_designer.DEFAULT_TIMEOUT_SECONDS


def test_should_design_thresholds(monkeypatch):
    monkeypatch.delenv("FF_UI_DESIGNER", raising=False)
    two = [_comp("wc_a"), _comp("wc_b")]
    assert should_design(two) is True
    assert should_design([_comp("wc_a")]) is False, "single-component rounds skip the designer"
    assert should_design([]) is False
    assert should_design(two, timeline_mode=True) is False, "timeline views are read-only"
    monkeypatch.setenv("FF_UI_DESIGNER", "false")
    assert should_design(two) is False, "flag off restores legacy behavior"


def test_parse_plain_layout_object():
    layout = parse_design_response('{"layout": [{"type": "ref", "component_id": "wc_a"}]}')
    assert layout == [{"type": "ref", "component_id": "wc_a"}]


def test_parse_bare_array_accepted():
    layout = parse_design_response('[{"type": "text", "content": "hi"}]')
    assert layout[0]["type"] == "text"


def test_parse_fenced_json():
    raw = "```json\n{\"layout\": [{\"type\": \"divider\"}]}\n```"
    assert parse_design_response(raw) == [{"type": "divider"}]


def test_parse_json_embedded_in_prose():
    raw = 'Here is the design:\n{"layout": [{"type": "divider"}]}'
    assert parse_design_response(raw) == [{"type": "divider"}]


def test_parse_error_refusal():
    with pytest.raises(DesignRejected) as exc:
        parse_design_response("ERROR: components are unrelated")
    assert exc.value.reason == "refusal"


@pytest.mark.parametrize("raw,reason", [
    ("", "empty"),
    ("not json at all", "parse"),
    ('{"layout": []}', "invalid"),
    ('{"nope": 1}', "invalid"),
])
def test_parse_failures_carry_reasons(raw, reason):
    with pytest.raises(DesignRejected) as exc:
        parse_design_response(raw)
    assert exc.value.reason == reason


def test_validate_drops_unknown_ref():
    clean, refs = validate_layout([_ref("wc_known"), _ref("wc_ghost")], {"wc_known"}, ALLOWED)
    assert refs == ["wc_known"]
    assert clean == [{"type": "ref", "component_id": "wc_known"}]


def test_validate_duplicate_ref_first_wins():
    layout = [_ref("wc_a"), {"type": "card", "title": "x", "content": [_ref("wc_a")]}]
    clean, refs = validate_layout(layout, {"wc_a"}, ALLOWED)
    assert refs == ["wc_a"]
    assert clean[1]["content"] == [], "second occurrence dropped from the card"


def test_validate_unknown_type_becomes_container_keeping_children():
    layout = [{"type": "hologram", "children": [_ref("wc_a")]}]
    clean, refs = validate_layout(layout, {"wc_a"}, ALLOWED)
    assert clean[0]["type"] == "container"
    assert refs == ["wc_a"]


def test_validate_chart_alias_maps_to_plotly():
    clean, _ = validate_layout([{"type": "chart", "data": []}], set(), ALLOWED)
    assert clean[0]["type"] == "plotly_chart"


def test_validate_coerces_bare_strings_to_text():
    clean, _ = validate_layout([{"type": "card", "content": ["plain words"]}], set(), ALLOWED)
    assert clean[0]["content"][0] == {"type": "text", "content": "plain words", "variant": "body"}


def test_validate_walks_tabs_content():
    layout = [{"type": "tabs", "tabs": [{"label": "A", "content": [_ref("wc_a"), _ref("wc_nope")]}]}]
    clean, refs = validate_layout(layout, {"wc_a"}, ALLOWED)
    assert refs == ["wc_a"]
    assert clean[0]["tabs"][0]["content"] == [{"type": "ref", "component_id": "wc_a"}]


def test_repair_appends_missing_components_in_dispatch_order():
    layout = [_ref("wc_b")]
    repaired = repair_layout(layout, ["wc_b"], ["wc_a", "wc_b", "wc_c"])
    assert [n["component_id"] for n in repaired] == ["wc_b", "wc_a", "wc_c"]


def test_repair_noop_when_complete():
    layout = [_ref("wc_a"), _ref("wc_b")]
    assert repair_layout(layout, ["wc_a", "wc_b"], ["wc_a", "wc_b"]) == layout


def test_garnish_ids_deterministic_and_namespaced():
    layout = [{"type": "metric", "title": "Headline", "value": "42"}, _ref("wc_a")]
    once = stamp_garnish_ids(layout, "chat-1", "ly_abc")
    twice = stamp_garnish_ids(layout, "chat-1", "ly_abc")
    assert once[0]["id"] == twice[0]["id"]
    assert once[0]["id"].startswith("dg_")
    assert once[0]["attributes"]["data-component-id"] == once[0]["id"]
    assert "id" not in once[1], "ref nodes are never stamped"
    other_round = stamp_garnish_ids(layout, "chat-1", "ly_zzz")
    assert other_round[0]["id"] != once[0]["id"], "different rounds, different garnish ids"


def test_materialize_substitutes_and_anchors_nested_refs():
    by_id = {"wc_a": _comp("wc_a"), "wc_b": _comp("wc_b")}
    layout = [
        _ref("wc_a"),
        {"type": "grid", "columns": 2, "children": [_ref("wc_b"), {"type": "divider"}]},
    ]
    out = materialize(layout, by_id)
    assert out[0]["component_id"] == "wc_a"
    assert "attributes" not in out[0], "top-level refs use the render_component_fragment wrapper"
    nested = out[1]["children"][0]
    assert nested["component_id"] == "wc_b"
    assert nested["attributes"]["data-component-id"] == "wc_b", "nested refs carry their own morph anchor"


def test_materialize_drops_vanished_refs():
    out = materialize([_ref("wc_gone"), {"type": "divider"}], {})
    assert out == [{"type": "divider"}]


def test_materialize_deepcopies_components():
    comp = _comp("wc_a", rows=[["x"]])
    out = materialize([{"type": "card", "content": [_ref("wc_a")]}], {"wc_a": comp})
    out[0]["content"][0]["rows"].append(["mutated"])
    assert comp["rows"] == [["x"]], "materialization must never mutate workspace state"


def test_materialize_walks_tabs():
    by_id = {"wc_a": _comp("wc_a")}
    out = materialize([{"type": "tabs", "tabs": [{"label": "L", "content": [_ref("wc_a")]}]}], by_id)
    assert out[0]["tabs"][0]["content"][0]["component_id"] == "wc_a"


def _round_components():
    return [_comp("wc_a"), _comp("wc_b")]


def _drive(llm_call, timeout_s=None, max_rounds=None):
    return asyncio.run(design_round(
        user_request="compare things",
        round_components=_round_components(),
        canvas_rows=[],
        chat_id="chat-1",
        layout_key="ly_test",
        allowed_types=ALLOWED,
        llm_call=llm_call,
        timeout_s=timeout_s,
        max_rounds=max_rounds,
    ))


def test_design_round_success_with_repair_and_garnish():
    async def llm(messages):
        return ('{"layout": [{"type": "metric", "title": "Headline", "value": "1"},'
                ' {"type": "ref", "component_id": "wc_a"}]}')

    layout = _drive(llm)
    assert layout is not None
    refs = list(ui_designer.iter_refs(layout))
    assert set(refs) == {"wc_a", "wc_b"}, "omission repair guarantees completeness"
    assert layout[0]["id"].startswith("dg_")


def test_design_round_timeout_falls_back():
    async def llm(messages):
        await asyncio.sleep(0.5)
        return "{}"

    assert _drive(llm, timeout_s=0.05) is None


def test_design_round_llm_exception_falls_back():
    async def llm(messages):
        raise RuntimeError("upstream 502")

    assert _drive(llm) is None


@pytest.mark.parametrize("content", [None, "", "ERROR: no", "not json", '{"layout": []}'])
def test_design_round_bad_outputs_fall_back(content):
    async def llm(messages):
        return content

    assert _drive(llm) is None


def test_design_round_all_refs_invalid_falls_back():
    async def llm(messages):
        return '{"layout": [{"type": "ref", "component_id": "wc_ghost"}]}'

    layout = _drive(llm)
    assert layout is not None
    assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}


def test_renderer_emits_nested_morph_anchor():
    from webrender import render_one

    comp = {"type": "table", "headers": ["A"], "rows": [["1"]],
            "attributes": {"data-component-id": "wc_anchor1",
                           "onclick": "alert(1)", "DATA-OK": "yes"}}
    html_out = render_one(comp)
    assert 'data-component-id="wc_anchor1"' in html_out
    assert "onclick" not in html_out, "non-data attributes must never render"
    assert 'data-ok="yes"' in html_out, "data-* keys are case-normalized and allowed"
    plain = render_one({"type": "table", "headers": ["A"], "rows": [["1"]]})
    assert "data-component-id" not in plain


def test_prompt_carries_ids_palette_and_rules():
    messages = build_design_messages(
        "x" * 5000, _round_components(),
        [{"component_id": "wc_old", "title": "Old", "component_type": "card"}],
        ALLOWED,
    )
    prompt = messages[1]["content"]
    assert "wc_a" in prompt and "wc_b" in prompt
    assert "wc_old" in prompt, "live canvas context rides along"
    assert "plotly_chart" in prompt, "full palette is offered (FR-020)"
    assert "exactly once" in prompt
    assert len(prompt) < 20000, "prompt stays bounded on huge inputs"


_DRAFT = '{"layout": [{"type": "ref", "component_id": "wc_a"}, {"type": "ref", "component_id": "wc_b"}]}'
_IMPROVED = ('{"layout": [{"type": "metric", "title": "Headline", "value": "2"},'
             ' {"type": "grid", "columns": 2, "children": ['
             '{"type": "ref", "component_id": "wc_a"}, {"type": "ref", "component_id": "wc_b"}]}]}')


def _scripted_llm(replies):
    calls = []

    async def llm(messages):
        calls.append(messages)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return llm, calls


def test_designer_max_rounds_env(monkeypatch):
    monkeypatch.delenv("UI_DESIGNER_MAX_ROUNDS", raising=False)
    assert ui_designer.designer_max_rounds() == ui_designer.DEFAULT_MAX_ROUNDS
    monkeypatch.setenv("UI_DESIGNER_MAX_ROUNDS", "5")
    assert ui_designer.designer_max_rounds() == 5
    monkeypatch.setenv("UI_DESIGNER_MAX_ROUNDS", "0")
    assert ui_designer.designer_max_rounds() == ui_designer.DEFAULT_MAX_ROUNDS
    monkeypatch.setenv("UI_DESIGNER_MAX_ROUNDS", "junk")
    assert ui_designer.designer_max_rounds() == ui_designer.DEFAULT_MAX_ROUNDS


def test_refine_pass_improves_the_draft():
    llm, calls = _scripted_llm([_DRAFT, _IMPROVED, "DONE"])
    layout = _drive(llm, max_rounds=3)
    assert layout is not None
    assert len(calls) == 3, "draft + refine + DONE"
    assert layout[0]["type"] == "metric", "refined arrangement won"
    assert layout[0]["id"].startswith("dg_"), "garnish stamped on the final layout"
    assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}
    refine_prompt = calls[1][1]["content"]
    assert "CURRENT ARRANGEMENT" in refine_prompt
    assert "wc_a" in refine_prompt and "DONE" in refine_prompt


def test_done_reply_keeps_the_draft():
    llm, calls = _scripted_llm([_DRAFT, "DONE"])
    layout = _drive(llm, max_rounds=3)
    assert layout is not None
    assert len(calls) == 2
    assert [n["type"] for n in layout] == ["ref", "ref"]


def test_stable_refinement_converges():
    llm, calls = _scripted_llm([_DRAFT, _DRAFT, _DRAFT])
    layout = _drive(llm, max_rounds=3)
    assert layout is not None
    assert len(calls) == 2, "identical refinement ends the loop"


def test_failed_refinement_keeps_best_arrangement():
    for bad_second in ("not json", "ERROR: meh", "", RuntimeError("boom")):
        llm, calls = _scripted_llm([_IMPROVED, bad_second])
        layout = _drive(llm, max_rounds=3)
        assert layout is not None, f"second-pass {bad_second!r} must not lose the draft"
        assert layout[0]["type"] == "metric"
        assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}


def test_degenerate_refinement_keeps_best_arrangement():
    for degen in (
        '{"layout": ["The arrangement is already well-designed"]}',
        '{"layout": [""]}',
        '{"layout": [{"type": "ref", "component_id": "wc_a"}]}',
    ):
        llm, calls = _scripted_llm([_IMPROVED, degen])
        layout = _drive(llm, max_rounds=3)
        assert layout is not None
        assert layout[0]["type"] == "metric", f"draft lost to degenerate refinement {degen!r}"
        assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}


def test_refinement_must_keep_canvas_refs():
    draft = ('{"layout": [{"type": "ref", "component_id": "wc_a"},'
             ' {"type": "ref", "component_id": "wc_b"},'
             ' {"type": "ref", "component_id": "wc_old"}]}')
    llm, calls = _scripted_llm([draft, _IMPROVED])
    layout = asyncio.run(design_round(
        user_request="rearrange everything into one dashboard",
        round_components=_round_components(),
        canvas_rows=[{"component_id": "wc_old", "title": "Old", "component_type": "card"}],
        chat_id="chat-1",
        layout_key="ly_test",
        allowed_types=ALLOWED,
        llm_call=llm,
        max_rounds=3,
    ))
    assert layout is not None
    assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b", "wc_old"}, \
        "a refinement that loses a placed canvas ref is rejected"
    refine_prompt = calls[1][1]["content"]
    assert "(canvas)" in refine_prompt
    assert "unknown" not in refine_prompt


def test_fenced_done_reply_ends_loop():
    llm, calls = _scripted_llm([_DRAFT, "```json\nDONE\n```"])
    layout = _drive(llm, max_rounds=3)
    assert layout is not None
    assert len(calls) == 2


def test_leaf_garnish_cannot_swallow_refs():
    layout = [
        {"type": "metric", "title": "T", "value": "1", "content": [_ref("wc_a")]},
        _ref("wc_b"),
    ]
    clean, referenced = validate_layout(layout, {"wc_a", "wc_b"}, ALLOWED)
    assert referenced == ["wc_b"], "refs nested in leaf garnish are never claimed"
    assert "content" not in clean[0], "leaf garnish loses its invisible children"
    repaired = repair_layout(clean, referenced, ["wc_a", "wc_b"])
    assert set(ui_designer.iter_refs(repaired)) == {"wc_a", "wc_b"}


def test_repair_dedupes_duplicate_round_ids():
    repaired = repair_layout([], [], ["wc_a", "wc_a", "wc_b"])
    assert [n["component_id"] for n in repaired] == ["wc_a", "wc_b"]


def test_refine_timeout_keeps_best_arrangement():
    calls = []

    async def llm(messages):
        calls.append(messages)
        if len(calls) > 1:
            await asyncio.sleep(0.5)
        return _IMPROVED

    layout = _drive(llm, timeout_s=0.2, max_rounds=3)
    assert layout is not None
    assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}


def test_format_retry_recovers_unusable_draft():
    llm, calls = _scripted_llm(["not json at all", _DRAFT, "DONE"])
    layout = _drive(llm, max_rounds=3)
    assert layout is not None
    assert set(ui_designer.iter_refs(layout)) == {"wc_a", "wc_b"}
    retry_messages = calls[1]
    assert retry_messages[-1]["role"] == "user"
    assert "could not be used" in retry_messages[-1]["content"]


def test_max_rounds_one_is_single_pass(monkeypatch):
    # A host .env can re-leak this flag past conftest's strip
    monkeypatch.delenv("FF_UI_DESIGNER_TASKMODEL", raising=False)
    llm, calls = _scripted_llm([_DRAFT])
    layout = asyncio.run(design_round(
        user_request="compare things",
        round_components=_round_components(),
        canvas_rows=[],
        chat_id="chat-1",
        layout_key="ly_test",
        allowed_types=ALLOWED,
        llm_call=llm,
        max_rounds=1,
    ))
    assert layout is not None
    assert len(calls) == 1, "max_rounds=1 restores the legacy single pass"


def test_first_pass_refusal_never_retries():
    llm, calls = _scripted_llm(["ERROR: cannot improve", _DRAFT])
    assert _drive(llm) is None
    assert len(calls) == 1, "a refusal is respected, not retried"
