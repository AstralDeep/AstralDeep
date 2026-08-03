"""Feature-066 pins for markdown in the chat rail and in chat previews.

Symptom this fixes (observed live 2026-08-03): the LIVE narrative rendered
markdown correctly, but the moment a turn committed — and on every reload —
the rail was replaced by the words-only conversation snapshot, whose text
parts carry only the RAW markdown source. The user saw literal
``**4, 5, 6**``. The recent-chats preview leaked the same markup by a
different route.

The fix keeps the semantic value authoritative and adds the web rendition on
the SAME transport-only ``_presentation`` envelope the components path uses,
through the SAME escape-first pipeline.
"""

from __future__ import annotations

from orchestrator.history import augment_conversation_snapshot_for_target
from webrender.sanitize import block_md, plain_md


class TestPlainMd:
    """``plain_md`` is the plain-text inverse of the renderers."""

    def test_strips_bold_and_keeps_words(self) -> None:
        assert plain_md("Rolled **6d6** -> **4, 5** (total **23**)") == (
            "Rolled 6d6 -> 4, 5 (total 23)"
        )

    def test_strips_inline_code_links_headings(self) -> None:
        assert plain_md("# Title\ntext with `code` and [link](https://x.test)") == (
            "Title text with code and link"
        )

    def test_strips_list_markers(self) -> None:
        assert plain_md("- one\n- two\n1. three") == "one two three"

    def test_strips_emphasis_and_strike(self) -> None:
        assert plain_md("~~gone~~ and _em_ and __bold__") == "gone and em and bold"

    def test_drops_fenced_code_blocks_entirely(self) -> None:
        assert plain_md("```\nprint(1)\n```\nafter") == "after"

    def test_flattens_tables_and_drops_separator_rows(self) -> None:
        assert plain_md("| a | b |\n|---|---|\n| 1 | 2 |") == "a b 1 2"

    def test_drops_horizontal_rules_and_blockquote_markers(self) -> None:
        assert plain_md("---\n> quoted") == "quoted"

    def test_plain_text_passes_through(self) -> None:
        assert plain_md("just words here") == "just words here"

    def test_empty_and_none_are_safe(self) -> None:
        assert plain_md(None) == ""
        assert plain_md("") == ""

    def test_does_not_escape_the_consumer_still_does(self) -> None:
        # Escape-first is preserved end to end: this returns RAW text and
        # every consumer escapes at render time.
        assert plain_md("5 < 6 & 7 > 2") == "5 < 6 & 7 > 2"


class TestSnapshotTextPresentation:
    """Assistant rail text gains a web rendition; nothing else changes."""

    @staticmethod
    def _snapshot(role: str, text: str) -> dict:
        return {
            "transcript": [
                {"role": role, "parts": [{"type": "text", "text": text}]}
            ]
        }

    def _augment(self, role: str, text: str) -> dict:
        snap = augment_conversation_snapshot_for_target(
            self._snapshot(role, text), None, target="web"
        )
        return snap["transcript"][0]["parts"][0]

    def test_assistant_text_gains_rendered_markdown(self) -> None:
        part = self._augment("assistant", "Rolled **6d6** for **23** pips")
        assert part["text"] == "Rolled **6d6** for **23** pips"  # semantic intact
        env = part["_presentation"]
        assert set(env) == {"target", "html"}  # 2-key envelope, no workspace
        assert env["target"] == "web"
        assert "<strong" in env["html"]
        assert "**" not in env["html"]

    def test_user_text_is_left_inert(self) -> None:
        part = self._augment("user", "why are there **asterisks** here")
        assert "_presentation" not in part

    def test_rendition_is_escape_first(self) -> None:
        part = self._augment("assistant", "<script>alert(1)</script> **b**")
        html = part["_presentation"]["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "<strong" in html  # the legitimate markdown still renders

    def test_matches_the_live_render_pipeline(self) -> None:
        """Byte-identical body to the live path, in the same wrapper.

        The live narrative renders through ``render_text``'s markdown branch,
        which wraps ``block_md`` output in the ``astral-md prose`` div. The
        rail rendition must be the SAME markup, or a committed turn would
        look different from the turn the user just watched.
        """
        source = "# Heading\n\n- one\n- two\n\n`code` and **bold**"
        part = self._augment("assistant", source)
        html = part["_presentation"]["html"]
        assert block_md(source) in html
        assert html.startswith('<div class="astral-md prose')

    def test_malformed_non_dict_part_is_skipped_not_fatal(self) -> None:
        snap = augment_conversation_snapshot_for_target(
            {
                "transcript": [
                    {
                        "role": "assistant",
                        "parts": [
                            "not-a-dict",
                            {"type": "text", "text": "**still bold**"},
                        ],
                    }
                ]
            },
            None,
            target="web",
        )
        parts = snap["transcript"][0]["parts"]
        # The malformed entry is left untouched...
        assert parts[0] == "not-a-dict"
        # ...and the well-formed sibling still gains its rendition.
        assert "<strong" in parts[1]["_presentation"]["html"]

    def test_native_targets_never_receive_the_envelope(self) -> None:
        snap = augment_conversation_snapshot_for_target(
            self._snapshot("assistant", "**bold**"), None, target="android"
        )
        assert "_presentation" not in snap["transcript"][0]["parts"][0]

    def test_components_parts_still_augmented(self) -> None:
        snap = augment_conversation_snapshot_for_target(
            {
                "transcript": [
                    {
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "components",
                                "components": [
                                    {"type": "text", "content": "hi",
                                     "component_id": "c1"}
                                ],
                            }
                        ],
                    }
                ]
            },
            None,
            target="web",
        )
        comp = snap["transcript"][0]["parts"][0]["components"][0]
        assert comp["_presentation"]["target"] == "web"
        # The components envelope keeps its workspace key — unchanged contract.
        assert "workspace" in comp["_presentation"]
