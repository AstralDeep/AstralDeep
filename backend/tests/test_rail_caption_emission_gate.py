"""Tests for the rail-caption-variant emission flag (backend/orchestrator/history.py,
shared/feature_flags.py, protocol.py): with the gate off every rail text part is
exactly {type, text}; turning it on restores the bounded variant carry.
"""

from __future__ import annotations

import pytest

from orchestrator.history import (
    _rail_parts,
    augment_conversation_snapshot_for_target,
)
from shared.feature_flags import FeatureFlags, flags
from shared.protocol import ConversationSnapshot


def _components_part(*components: dict) -> dict:
    return {"type": "components", "components": list(components)}


@pytest.fixture
def caption_emission(monkeypatch):
    def _set(enabled: bool):
        monkeypatch.setitem(flags._flags, "rail_caption_variant", enabled)

    return _set


class TestFlagIsRegisteredAndDefaultsOff:
    def test_gate_is_a_registered_flag(self) -> None:
        assert "rail_caption_variant" in flags._flags

    def test_committed_default_is_off_regardless_of_ambient_env(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("FF_RAIL_CAPTION_VARIANT", raising=False)
        assert FeatureFlags().is_enabled("rail_caption_variant") is False

    @pytest.mark.parametrize(
        "value,expected",
        [("true", True), ("1", True), ("yes", True), ("false", False), ("", False)],
    )
    def test_env_var_name_is_actually_wired(
        self, monkeypatch, value, expected
    ) -> None:
        monkeypatch.setenv("FF_RAIL_CAPTION_VARIANT", value)
        assert FeatureFlags().is_enabled("rail_caption_variant") is expected


class TestGateOffEmitsTheShippedClientShape:
    def test_caption_primitive_lifts_without_its_variant(
        self, caption_emission
    ) -> None:
        caption_emission(False)
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "caption", "content": "As of July"}
                )
            ]
        )
        assert parts == [{"type": "text", "text": "As of July"}]

    def test_wrapper_lift_drops_caption_weight(self, caption_emission) -> None:
        caption_emission(False)
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
            {"type": "text", "text": "Source: sensor 4"},
        ]

    def test_stored_caption_part_renormalizes_to_the_canonical_shape(
        self, caption_emission
    ) -> None:
        caption_emission(False)
        parts = _rail_parts(
            [
                {"type": "text", "variant": "caption", "text": "As of July"},
                {"type": "text", "text": "Already canonical"},
            ]
        )
        assert parts == [
            {"type": "text", "text": "As of July"},
            {"type": "text", "text": "Already canonical"},
        ]

    def test_no_rail_part_carries_any_variant_key(self, caption_emission) -> None:
        caption_emission(False)
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": v, "content": f"words {v}"}
                )
                for v in ("caption", "h1", "h2", "body", "markdown", "odd")
            ]
        )
        assert parts
        for part in parts:
            assert set(part) == {"type", "text"}, part


    def test_unhashable_stored_variant_does_not_raise(
        self, caption_emission
    ) -> None:
        # Gate checked before frozenset lookup — avoids TypeError here
        caption_emission(False)
        parts = _rail_parts(
            [{"type": "text", "variant": ["caption"], "text": "words"}]
        )
        assert parts == [{"type": "text", "text": "words"}]

    def test_native_hydration_frame_carries_no_third_key(
        self, caption_emission
    ) -> None:
        caption_emission(False)
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "caption", "content": "As of July"}
                )
            ]
        )
        snap = augment_conversation_snapshot_for_target(
            {"transcript": [{"role": "assistant", "parts": parts}]},
            None,
            target="windows",
        )
        for part in snap["transcript"][0]["parts"]:
            assert set(part) == {"type", "text"}, part

    def test_caption_hydrates_at_markdown_weight_with_the_gate_off(
        self, caption_emission
    ) -> None:
        caption_emission(False)
        parts = _rail_parts(
            [
                _components_part(
                    {"type": "text", "variant": "caption", "content": "As of July"}
                )
            ]
        )
        snap = augment_conversation_snapshot_for_target(
            {"transcript": [{"role": "assistant", "parts": parts}]},
            None,
            target="web",
        )
        html = snap["transcript"][0]["parts"][0]["_presentation"]["html"]
        assert "astral-md" in html
        assert "text-astral-muted" not in html


class TestGateOnRestoresTheT023Carry:
    def test_caption_primitive_lifts_with_its_variant(
        self, caption_emission
    ) -> None:
        caption_emission(True)
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

    def test_non_caption_variants_still_normalize_away(
        self, caption_emission
    ) -> None:
        caption_emission(True)
        for variant in ("h1", "h2", "h3", "body", "markdown", "odd", None):
            parts = _rail_parts(
                [
                    _components_part(
                        {"type": "text", "variant": variant, "content": "words"}
                    )
                ]
            )
            assert parts == [{"type": "text", "text": "words"}], variant


class TestAcceptanceIsUnchangedByTheGate:
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

    @pytest.mark.parametrize("enabled", [False, True])
    def test_validator_accepts_a_caption_part_either_way(
        self, caption_emission, enabled
    ) -> None:
        caption_emission(enabled)
        self._snapshot(
            [{"type": "text", "text": "words", "variant": "caption"}]
        ).validate()

    @pytest.mark.parametrize("enabled", [False, True])
    def test_validator_still_refuses_an_unbounded_variant(
        self, caption_emission, enabled
    ) -> None:
        from shared.protocol import ProtocolValidationError

        caption_emission(enabled)
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "text", "text": "words", "variant": "h1"}]
            ).validate()
