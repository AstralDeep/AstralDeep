"""Feature 063 — the chat rail is TEXT ONLY; UI components live on the canvas.

`_rail_parts` post-processes a transcript message's parts: a `components` part is
reduced to the plain text of any top-level `text` primitives (lifted to `text`
parts, so no assistant words are lost) and every other component (cards, tables,
lists, alerts, metrics) is dropped to the canvas; other part kinds pass through,
and a message whose parts all drop is omitted by the caller. Supersedes the
feature-062 rule that kept text-like components in the rail. Pure functions.
"""

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
        # feature 063: a bare text primitive is lifted to a text part; the metric drops.
        parts = [{"type": "components", "components": [_metric(), _text("the answer")]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "the answer"}]

    def test_text_only_card_is_dropped_to_canvas(self):
        # feature 063: a card is a UI component — canvas only, never in the chat rail.
        doc = {"type": "card", "title": "Response", "content": [_text("summary")]}
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == []

    def test_rich_dropped_and_text_lifted_keeping_order(self):
        words = {"type": "text", "text": "before"}
        rich = {"type": "components", "components": [_metric()]}
        after = {"type": "components", "components": [_text("after")]}
        assert _rail_parts([words, rich, after]) == [words, {"type": "text", "text": "after"}]
