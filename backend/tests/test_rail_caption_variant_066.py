"""Feature-066 T023 pins: a caption survives the rail as a caption.

``_rail_parts`` used to discard the lifted text primitive's variant and
``augment_text`` hardcoded ``markdown``, so a caption hydrated at full
narrative weight. These pins hold the carry: non-default variants (caption,
h1-h3) ride the lifted part; default lifts stay byte-identical; the web
rendition uses the carried variant through the same escape-first pipeline;
natives still never receive the ``_presentation`` envelope.
"""

from __future__ import annotations

from orchestrator.history import (
    _rail_parts,
    augment_conversation_snapshot_for_target,
)


def _components_part(*components: dict) -> dict:
    return {"type": "components", "components": list(components)}


class TestRailLiftCarriesVariant:
    def test_caption_primitive_lift_carries_variant(self) -> None:
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

    def test_default_text_lift_stays_byte_identical(self) -> None:
        for shape in (
            {"type": "text", "content": "plain words"},
            {"type": "text", "variant": "body", "content": "plain words"},
            {"type": "text", "variant": "markdown", "content": "plain words"},
        ):
            parts = _rail_parts([_components_part(dict(shape))])
            assert parts == [{"type": "text", "text": "plain words"}]

    def test_heading_variants_are_carried(self) -> None:
        for variant in ("h1", "h2", "h3"):
            parts = _rail_parts(
                [
                    _components_part(
                        {"type": "text", "variant": variant, "content": "Title"}
                    )
                ]
            )
            assert parts == [{"type": "text", "text": "Title", "variant": variant}]

    def test_wrapper_lift_preserves_child_caption(self) -> None:
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

    def test_unknown_variant_is_not_carried(self) -> None:
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "sparkly", "content": "words"}
                )
            ]
        )
        assert parts == [{"type": "text", "text": "words"}]


def _hydrated_part(part: dict, target: str = "web") -> dict:
    snap = augment_conversation_snapshot_for_target(
        {
            "transcript": [
                {"role": "assistant", "parts": [dict(part)]}
            ]
        },
        None,
        target=target,
    )
    return snap["transcript"][0]["parts"][0]


class TestHydrationRendition:
    def test_caption_part_renders_at_caption_weight(self) -> None:
        part = _hydrated_part(
            {"type": "text", "text": "As of July", "variant": "caption"}
        )
        html = part["_presentation"]["html"]
        assert "text-xs" in html and "text-astral-muted" in html
        assert "astral-md" not in html and "prose" not in html
        assert "As of July" in html

    def test_variantless_part_still_renders_markdown(self) -> None:
        part = _hydrated_part({"type": "text", "text": "Rolled **6d6**"})
        html = part["_presentation"]["html"]
        assert "astral-md" in html
        assert "<strong" in html and ">6d6</strong>" in html

    def test_caption_rendition_is_escape_first(self) -> None:
        part = _hydrated_part(
            {
                "type": "text",
                "text": "<script>alert(1)</script>",
                "variant": "caption",
            }
        )
        html = part["_presentation"]["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_heading_variant_renders_heading_tag(self) -> None:
        part = _hydrated_part(
            {"type": "text", "text": "Results", "variant": "h2"}
        )
        html = part["_presentation"]["html"]
        assert html.startswith("<h2")

    def test_native_target_still_gets_no_presentation(self) -> None:
        part = _hydrated_part(
            {"type": "text", "text": "As of July", "variant": "caption"},
            target="windows",
        )
        assert "_presentation" not in part
        assert part["text"] == "As of July"
