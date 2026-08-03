"""Feature 063 — the chat rail is TEXT ONLY; UI components live on the canvas.

`_rail_parts` post-processes a transcript message's parts: a `components` part
is reduced to the plain text of `text` primitives — top-level ones, plus those
nested inside TEXT-ONLY wrapper chrome (card/container/collapsible with only
words inside, the shape `_chat_narrative` persists for multi-paragraph
answers) — so no assistant words are lost. Every rich component (tables,
metrics, charts) and every wrapper carrying one is dropped to the canvas;
other part kinds pass through, and a message whose parts all drop is omitted
by the caller. Supersedes the feature-062 rule that kept text-like components
whole in the rail. Pure functions.
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

    def test_text_only_card_lifts_its_words(self):
        # Regression (bug A, 2026-08-03): ``_chat_narrative`` persists a
        # multi-paragraph answer as Card(title="Response", content=[Text]).
        # That card is chat-rail narrative — it NEVER enters the workspace —
        # so the pre-fix rule (drop every card) erased the assistant's answer
        # from the committed snapshot and every client rendered the turn
        # answer-less. A text-only wrapper now has its words lifted.
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

    def test_nested_text_only_wrappers_lift_depth_first(self):
        inner = {"type": "collapsible", "title": "More", "content": [_text("inner")]}
        outer = {"type": "container", "children": [_text("outer"), inner]}
        parts = [{"type": "components", "components": [outer]}]
        assert _rail_parts(parts) == [
            {"type": "text", "text": "outer"},
            {"type": "text", "text": "inner"},
        ]

    def test_card_with_rich_child_still_drops_whole_to_canvas(self):
        # A wrapper carrying any rich component is canvas state (the
        # workspace re-hydrates it) — lifting its text would duplicate the
        # canvas card's words in the rail (the feature-062 regression).
        doc = {"type": "card", "title": "Stats", "content": [_text("lead"), _metric()]}
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == []

    def test_workspace_anchored_doc_card_still_drops_to_canvas(self):
        # ``_narrative_doc_card`` persists Card(id="doc_…", content=[Text])
        # in the transcript AND upserts it into the workspace; its rail twin
        # is the concise lead. Lifting its words would duplicate the whole
        # write-up beside the canvas doc, so an author-identified wrapper
        # keeps the canvas-only rule.
        doc = {
            "type": "card",
            "component_id": "doc_2b7c9d5e1a44",
            "title": "Specific Aims",
            "content": [_text("Aim 1: …\n\nAim 2: …")],
        }
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == []

    def test_synthesized_cc_identity_still_lifts(self):
        # ``_canonical_component`` stamps identity-less components with a
        # ``cc_`` fingerprint before the rail reduction runs — that
        # synthesized identity must not be mistaken for a workspace anchor.
        doc = {
            "type": "card",
            "component_id": "cc_0123456789abcdef01234567",
            "title": "Response",
            "content": [_text("the answer")],
        }
        parts = [{"type": "components", "components": [doc]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "the answer"}]

    def test_non_mapping_component_entries_are_skipped(self):
        # A malformed transcript row (a stray string beside real components) must
        # not abort the rail reduction — the entry is skipped, the rest survives.
        parts = [{"type": "components", "components": ["stray string", _text("kept")]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "kept"}]

    def test_text_primitive_serialized_under_the_text_key_is_lifted(self):
        # astralprims Text serializes as `content`; older transcript rows carry
        # `text`. Both are the assistant's words, so both are lifted to the rail.
        parts = [{"type": "components", "components": [{"type": "text", "text": "older row"}]}]
        assert _rail_parts(parts) == [{"type": "text", "text": "older row"}]

    def test_rich_dropped_and_text_lifted_keeping_order(self):
        words = {"type": "text", "text": "before"}
        rich = {"type": "components", "components": [_metric()]}
        after = {"type": "components", "components": [_text("after")]}
        assert _rail_parts([words, rich, after]) == [words, {"type": "text", "text": "after"}]
