"""Feature-066 T023 contract pins: why a caption may NOT carry its variant.

T023 proposed carrying the lifted text primitive's variant on the rail part
so a caption hydrates at caption weight. Adversarial verification during the
2026-08-04 close-out proved the premise unsafe: canonical transcript text
parts are EXACTLY ``{"type", "text"}`` — enforced by the server-side
``ConversationSnapshot._validate_part``, mirrored by Apple's exact-key
``ConversationPart`` decode, and pinned by the 060 ``native == original``
snapshot rule — so ANY additional part key breaks conversation continuity
the first time a real caption flows through. These pins hold that boundary:
the lift stays canonical for every variant, the canonical validator refuses
a variant-carrying part (documenting the constraint), and the web rendition
still arrives through the transport-only envelope.

Restoring caption weight on hydration requires a deliberate cross-client
contract extension (server validator + three native decoders + contract
doc), tracked as the reopened T023 in specs/066-canvas-first-uiux/tasks.md.
"""

from __future__ import annotations

import pytest

from orchestrator.history import (
    _rail_parts,
    augment_conversation_snapshot_for_target,
)
from shared.protocol import ConversationSnapshot, ProtocolValidationError


def _components_part(*components: dict) -> dict:
    return {"type": "components", "components": list(components)}


class TestLiftStaysCanonical:
    def test_caption_primitive_lifts_to_exact_canonical_shape(self) -> None:
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "caption", "content": "As of July"}
                )
            ]
        )
        assert parts == [{"type": "text", "text": "As of July"}]

    def test_every_variant_lifts_to_the_same_shape(self) -> None:
        for variant in ("caption", "h1", "h2", "h3", "body", "markdown", "odd"):
            parts = _rail_parts(
                [
                    _components_part(
                        {"type": "text", "variant": variant, "content": "words"}
                    )
                ]
            )
            assert parts == [{"type": "text", "text": "words"}], variant

    def test_wrapper_lift_keeps_caption_words_and_canonical_shape(self) -> None:
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
        # The caption's WORDS survive (words-only rail rule) even though its
        # weight cannot; no part may carry a variant key.
        assert parts == [
            {"type": "text", "text": "Body words"},
            {"type": "text", "text": "Source: sensor 4"},
        ]


class TestCanonicalContractRefusesVariant:
    """Documents WHY the carry is forbidden: the canonical validator."""

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

    def test_variant_carrying_part_is_refused(self) -> None:
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "text", "text": "words", "variant": "caption"}]
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

    def test_lifted_caption_words_render_via_the_markdown_pipeline(self) -> None:
        # Until the contract extension lands, a lifted caption renders like
        # every other rail text: markdown weight, escape-first, words intact.
        part = self._hydrated_part({"type": "text", "text": "As of July"})
        html = part["_presentation"]["html"]
        assert "astral-md" in html
        assert "As of July" in html

    def test_rendition_is_escape_first(self) -> None:
        part = self._hydrated_part(
            {"type": "text", "text": "<script>alert(1)</script>"}
        )
        html = part["_presentation"]["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_native_target_gets_no_presentation(self) -> None:
        part = self._hydrated_part(
            {"type": "text", "text": "As of July"}, target="windows"
        )
        assert part == {"type": "text", "text": "As of July"}
