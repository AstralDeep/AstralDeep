"""Feature-066 T023 contract pins: the bounded caption carry.

T023 CLOSED (2026-08-05) as a deliberate cross-client contract extension:
canonical transcript text parts are ``{"type", "text"}`` plus an OPTIONAL
``variant`` drawn from the closed set ``CANONICAL_TEXT_PART_VARIANTS``
(exactly ``{"caption"}``). The server validator, the web client
(``validateSnapshotShape``), Windows (``astral_client/protocol.py``),
Android (``Wire.kt``) and Apple (``ConversationContinuity.swift``) all
accept the same bounded shape — see
specs/060-runtime-reliability-hardening/contracts/conversation-continuity.md.

These pins hold the NEW boundary from both sides: a lifted caption keeps
its weight through commit and hydration, every other authoring variant
still normalizes away, and the canonical validator refuses anything
outside the closed set — the extension must never become an open door for
arbitrary part keys.
"""

from __future__ import annotations

import pytest

from orchestrator.history import (
    _rail_parts,
    augment_conversation_snapshot_for_target,
)
from shared.protocol import (
    CANONICAL_TEXT_PART_VARIANTS,
    ConversationSnapshot,
    ProtocolValidationError,
)


def _components_part(*components: dict) -> dict:
    return {"type": "components", "components": list(components)}


def test_bounded_alphabet_is_exactly_caption() -> None:
    # Drift pin: widening the closed set is a NEW cross-client contract
    # change (five validators + the 060 contract doc), never a casual edit.
    assert CANONICAL_TEXT_PART_VARIANTS == frozenset({"caption"})


class TestLiftCarriesCaption:
    def test_caption_primitive_lifts_with_its_variant(self) -> None:
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "caption", "content": "As of July"}
                )
            ]
        )
        assert parts == [
            {"type": "text", "text": "As of July", "variant": "caption"}
        ]

    def test_every_other_variant_lifts_to_the_canonical_shape(self) -> None:
        for variant in ("h1", "h2", "h3", "body", "markdown", "odd", None):
            parts = _rail_parts(
                [
                    _components_part(
                        {"type": "text", "variant": variant, "content": "words"}
                    )
                ]
            )
            assert parts == [{"type": "text", "text": "words"}], variant

    def test_wrapper_lift_keeps_caption_weight_and_body_shape(self) -> None:
        parts = _rail_parts(
            [
                _components_part(
                    {
                        "type": "card",
                        "content": [
                            {"type": "text", "content": "Body words"},
                            {
                                "type": "text",
                                "variant": "caption",
                                "content": "Source: sensor 4",
                            },
                        ],
                    }
                )
            ]
        )
        assert parts == [
            {"type": "text", "text": "Body words"},
            {"type": "text", "text": "Source: sensor 4", "variant": "caption"},
        ]

    def test_stored_text_parts_normalize_but_keep_bounded_variant(self) -> None:
        # R-9 rail fix pins still hold: STORED narrative parts carrying
        # authoring fields (variant/content) normalize to the canonical
        # shape — except the bounded caption carve-out, which must survive
        # re-derivation or a committed caption would lose its weight on the
        # next hydration.
        parts = _rail_parts(
            [
                {"type": "text", "variant": "markdown", "text": "Narrative words"},
                {"type": "text", "variant": "markdown", "content": "From content"},
                {"type": "text", "variant": "caption", "text": "As of July"},
                {"type": "text", "text": "Already canonical"},
            ]
        )
        assert parts == [
            {"type": "text", "text": "Narrative words"},
            {"type": "text", "text": "From content"},
            {"type": "text", "text": "As of July", "variant": "caption"},
            {"type": "text", "text": "Already canonical"},
        ]

    def test_blank_stored_text_parts_are_dropped(self) -> None:
        parts = _rail_parts(
            [
                {"type": "text", "variant": "markdown", "text": "   "},
                {"type": "text", "text": "kept"},
            ]
        )
        assert parts == [{"type": "text", "text": "kept"}]

    def test_structured_and_recovery_parts_still_pass_through(self) -> None:
        structured = {"type": "structured", "value": {"a": 1}, "plain_text": "a=1"}
        recovery = {"type": "recovery", "code": "x_y", "message": "went wrong"}
        assert _rail_parts([dict(structured), dict(recovery)]) == [
            structured,
            recovery,
        ]


class TestCanonicalContractBoundsVariant:
    """The validator accepts the closed set and refuses everything else."""

    @staticmethod
    def _snapshot(parts: list[dict]) -> ConversationSnapshot:
        return ConversationSnapshot(
            snapshot_id="00000000-0000-4000-8000-000000000010",
            chat_id="00000000-0000-4000-8000-000000000001",
            connection_generation="00000000-0000-4000-8000-000000000002",
            request_generation="00000000-0000-4000-8000-000000000003",
            snapshot_purpose="hydration",
            render_revision=1,
            committed_at="2026-08-04T12:00:00Z",
            transcript=[
                {
                    "message_id": "m1",
                    "role": "assistant",
                    "created_at": "2026-08-04T12:00:00Z",
                    "parts": parts,
                    "attachments": [],
                }
            ],
            canvas={"target": "canvas", "components": []},
        )

    def test_canonical_part_shape_is_accepted(self) -> None:
        self._snapshot([{"type": "text", "text": "words"}]).validate()

    def test_caption_carrying_part_is_accepted(self) -> None:
        self._snapshot(
            [{"type": "text", "text": "words", "variant": "caption"}]
        ).validate()

    @pytest.mark.parametrize("variant", ["h1", "body", "markdown", "odd", "", 3, None])
    def test_unbounded_variant_is_refused(self, variant) -> None:
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "text", "text": "words", "variant": variant}]
            ).validate()

    def test_any_other_extra_key_is_still_refused(self) -> None:
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "text", "text": "words", "weight": "caption"}]
            ).validate()


class TestHydrationRendition:
    @staticmethod
    def _hydrated_part(part: dict, target: str = "web") -> dict:
        snap = augment_conversation_snapshot_for_target(
            {"transcript": [{"role": "assistant", "parts": [dict(part)]}]},
            None,
            target=target,
        )
        return snap["transcript"][0]["parts"][0]

    def test_plain_rail_text_renders_via_the_markdown_pipeline(self) -> None:
        part = self._hydrated_part({"type": "text", "text": "As of July"})
        html = part["_presentation"]["html"]
        assert "astral-md" in html
        assert "As of July" in html

    def test_caption_part_renders_at_caption_weight(self) -> None:
        part = self._hydrated_part(
            {"type": "text", "text": "As of July", "variant": "caption"}
        )
        html = part["_presentation"]["html"]
        assert "text-astral-muted" in html
        assert "astral-md" not in html
        assert "As of July" in html

    def test_caption_rendition_is_escape_first(self) -> None:
        part = self._hydrated_part(
            {
                "type": "text",
                "text": "<script>alert(1)</script>",
                "variant": "caption",
            }
        )
        html = part["_presentation"]["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_rendition_is_escape_first(self) -> None:
        part = self._hydrated_part(
            {"type": "text", "text": "<script>alert(1)</script>"}
        )
        html = part["_presentation"]["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_native_target_keeps_the_variant_key_untouched(self) -> None:
        # The 060 native == original rule: the semantic snapshot passes
        # through byte-identical, caption carry included.
        part = self._hydrated_part(
            {"type": "text", "text": "As of July", "variant": "caption"},
            target="windows",
        )
        assert part == {"type": "text", "text": "As of July", "variant": "caption"}
