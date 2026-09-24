"""Tests for backend/orchestrator/history.py's _rail_parts: rich components (tables,
charts, metrics) drop to the canvas, text-only wrappers lift their words to the chat
rail, and malformed entries are skipped without losing siblings.
"""

import pytest

from orchestrator.history import _is_rail_text_only, _rail_parts


def _text(content: str) -> dict:
    return {"type": "text", "content": content}


def _metric() -> dict:
    return {"type": "metric", "label": "TOP MATCH", "value": "59.2%"}


class TestIsRailTextOnly:
    def test_text_primitive_is_text_only(self):
        assert _is_rail_text_only([_text("hello")])

    def test_rich_primitive_is_not(self):
        assert not _is_rail_text_only([_metric()])

    def test_table_and_chart_are_not(self):
        assert not _is_rail_text_only([{"type": "table", "headers": [], "rows": []}])
        assert not _is_rail_text_only([{"type": "bar_chart", "series": []}])

    def test_card_of_text_is_text_only(self):
        card = {"type": "card", "title": "Response", "content": [_text("words")]}
        assert _is_rail_text_only([card])

    def test_card_with_nested_rich_child_is_not(self):
        card = {"type": "card", "title": "Stats", "content": [_metric()]}
        assert not _is_rail_text_only([card])

    def test_container_children_key_is_checked(self):
        container = {"type": "container", "children": [_metric()]}
        assert not _is_rail_text_only([container])

    def test_non_mapping_entries_are_ignored(self):
        assert _is_rail_text_only(["stray string", _text("ok")])

    def test_alert_and_list_and_divider_are_text_only(self):
        assert _is_rail_text_only(
            [
                {"type": "alert", "message": "heads up", "variant": "info"},
                {"type": "list", "items": ["a", "b"]},
                {"type": "divider"},
            ]
        )


class TestRailParts:
    @pytest.mark.parametrize("identity", ["wc_source", "au_source", "doc_source"])
    def test_canvas_source_text_is_not_duplicated_in_transcript(self, identity):
        source = {"type": "text", "component_id": identity,
                  "variant": "markdown", "content": "Source: [Dog grooming](https://en.wikipedia.org/wiki/Dog_grooming)"}
        parts = [{"type": "components", "components": [source, _text("Summary.")]}]
        assert _rail_parts(parts, canvas_component_ids=frozenset({identity})) == [
            {"type": "text", "text": "Summary."}
        ]
        assert parts[0]["components"][0] == source

    @pytest.mark.parametrize("identity", [None, "", "cc_narrative", "authored_answer"])
    def test_unanchored_text_still_reaches_transcript(self, identity):
        text = {**_text("The answer."), "component_id": identity}
        assert _rail_parts(
            [{"type": "components", "components": [text]}],
            canvas_component_ids=frozenset({"wc_source"}),
        ) == [
            {"type": "text", "text": "The answer."}
        ]

    def test_nested_anchored_text_and_wrapper_are_not_lifted(self):
        wrapper = {"type": "card", "content": [
            _text("Summary."),
            {**_text("Source."), "component_id": "wc_source"},
            {"type": "container", "component_id": "wc_detail",
             "children": [_text("Canvas detail.")]},
        ]}
        assert _rail_parts(
            [{"type": "components", "components": [wrapper]}],
            canvas_component_ids=frozenset({"wc_source", "wc_detail"}),
        ) == [
            {"type": "text", "text": "Summary."}
        ]

    def test_text_part_passes_through(self):
        parts = [{"type": "text", "text": "hello"}]
        assert _rail_parts(parts) == parts

    def test_structured_and_recovery_parts_pass_through(self):
        parts = [
            {"type": "structured", "value": {"a": 1}, "plain_text": "a=1"},
            {"type": "recovery", "code": "bad_content", "message": "unreadable"},
        ]
        assert _rail_parts(parts) == parts

    def test_pure_tool_components_part_drops_entirely(self):
        parts = [{"type": "components", "components": [_metric(), _metric()]}]
        assert _rail_parts(parts) == []

    def test_components_part_lifts_text_and_drops_rich(self):
        parts = [{"type": "components", "components": [_metric(), _text("the answer")]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "the answer"}]

    def test_text_only_card_lifts_its_words(self):
        doc = {"type": "card", "title": "Response", "content": [_text("summary")]}
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "summary"}]

    def test_text_only_card_words_keep_paragraph_order(self):
        doc = {
            "type": "card",
            "title": "Response",
            "content": [_text("first paragraph"), _text("second paragraph")],
        }
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == [
            {"type": "text", "text": "first paragraph"},
            {"type": "text", "text": "second paragraph"},
        ]

    def test_nested_text_only_wrappers_keep_collapsible_boundaries(self):
        inner = {"type": "collapsible", "title": "More", "content": [_text("inner")]}
        outer = {"type": "container", "children": [_text("outer"), inner]}
        parts = [{"type": "components", "components": [outer]}]
        assert _rail_parts(parts) == [
            {"type": "text", "text": "outer"},
            {"type": "components", "components": [inner]},
        ]

    def test_card_with_rich_child_still_drops_whole_to_canvas(self):
        doc = {"type": "card", "title": "Stats", "content": [_text("lead"), _metric()]}
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == []

    def test_workspace_anchored_doc_card_still_drops_to_canvas(self):
        doc = {
            "type": "card",
            "component_id": "doc_2b7c9d5e1a44",
            "title": "Specific Aims",
            "content": [_text("Aim 1: …\n\nAim 2: …")],
        }
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == []

    def test_synthesized_cc_identity_still_lifts(self):
        doc = {
            "type": "card",
            "component_id": "cc_0123456789abcdef01234567",
            "title": "Response",
            "content": [_text("the answer")],
        }
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "the answer"}]

    def test_non_mapping_component_entries_are_skipped(self):
        parts = [{"type": "components", "components": ["stray string", _text("kept")]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "kept"}]

    def test_text_primitive_serialized_under_the_text_key_is_lifted(self):
        parts = [{"type": "components", "components": [{"type": "text", "text": "older row"}]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "older row"}]

    def test_rich_dropped_and_text_lifted_keeping_order(self):
        words = {"type": "text", "text": "before"}
        rich = {"type": "components", "components": [_metric()]}
        after = {"type": "components", "components": [_text("after")]}
        assert _rail_parts([words, rich, after]) == [words, {"type": "text", "text": "after"}]
