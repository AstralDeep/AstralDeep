"""Shipped-client compatibility gate for the 066 T023 caption carry.

WHY THIS EXISTS. T023 (commit ea59f84, 2026-08-05) extended the canonical
transcript text part with an OPTIONAL bounded ``variant`` key. That landed
AFTER the ``apple-v1.2`` tag and after the Android ``versionCode 4`` bundle,
and every already-shipped client validates a text part with EXACT key-set
equality:

  * Android ``Wire.kt`` — ``hasExactKeys("type", "text")``, and
    ``conversation_snapshot`` decodes to ``Inbound.Unknown`` on any miss, so
    the WHOLE snapshot is discarded and the client then loops on
    "Conversation restore timed out; retrying…".
  * Apple ``ConversationContinuity.swift`` — ``Set(object.keys) ==
    ["type", "text"]``, and ``Frames.swift`` guards
    ``messages.count == transcript.count``, so one caption part nils the
    entire snapshot.

Caption text is emitted routinely (weather, general, medical, connectors,
remote_observe), so a HEAD server talking to a store-installed v1.2 client
silently stops committing the chat rail.

So EMISSION is now gated (``FF_RAIL_CAPTION_VARIANT``, default OFF) while
ACCEPTANCE stays exactly as T023 specified on all five validators. That
asymmetry is the point: a v1.3 client keeps working either way, the operator
can flip emission on once store adoption is up, and no client needs a second
contract change to get there.

These pins hold the gate from both sides and pin that it is an emission gate,
NOT a retraction of the T023 contract.
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
    """Set the emission gate for one test, restoring it afterwards."""

    def _set(enabled: bool):
        monkeypatch.setitem(flags._flags, "rail_caption_variant", enabled)

    return _set


class TestFlagIsRegisteredAndDefaultsOff:
    def test_gate_is_a_registered_flag(self) -> None:
        # Registered, not merely absent: an unknown flag also reads False, so
        # without this pin the gate could silently never exist.
        assert "rail_caption_variant" in flags._flags

    def test_committed_default_is_off_regardless_of_ambient_env(
        self, monkeypatch
    ) -> None:
        # Pin the COMMITTED default, not the ambient process env — asserting
        # flags.is_enabled() directly would go red the moment an operator
        # performs the documented FF_RAIL_CAPTION_VARIANT rollout.
        monkeypatch.delenv("FF_RAIL_CAPTION_VARIANT", raising=False)
        assert FeatureFlags().is_enabled("rail_caption_variant") is False

    @pytest.mark.parametrize(
        "value,expected",
        [("true", True), ("1", True), ("yes", True), ("false", False), ("", False)],
    )
    def test_env_var_name_is_actually_wired(
        self, monkeypatch, value, expected
    ) -> None:
        # Traverses FeatureFlags._read, so a typo'd or renamed env string
        # cannot leave the suite green with the rollout silently a no-op.
        monkeypatch.setenv("FF_RAIL_CAPTION_VARIANT", value)
        assert FeatureFlags().is_enabled("rail_caption_variant") is expected


class TestGateOffEmitsTheShippedClientShape:
    """With the gate OFF every rail text part is exactly {type, text}."""

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
        # A caption committed while the gate was ON must not resurrect the
        # variant on the next hydration once the gate is OFF, or a single
        # historical turn would keep breaking a v1.2 client forever.
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
        # The property that actually protects the shipped clients: whatever
        # the authoring side used, nothing on the rail has a third key.
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
        # `variant` arrives from stored agent output. An agent emitting plain
        # dicts rather than typed astralprims classes (an LLM-authored parser,
        # a BYO agent, an agentic_creation draft) can persist a non-string, and
        # `x in frozenset` raises TypeError on an unhashable value. The gate is
        # read FIRST so the default path never evaluates the membership test.
        caption_emission(False)
        parts = _rail_parts(
            [{"type": "text", "variant": ["caption"], "text": "words"}]
        )
        assert parts == [{"type": "text", "text": "words"}]

    def test_native_hydration_frame_carries_no_third_key(
        self, caption_emission
    ) -> None:
        # The property that actually protects store clients, asserted where it
        # ships — after the per-target augmentation — not only at the private
        # helper. Guards against a future downstream transform re-stamping a
        # weight hint onto a text part.
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
        # The ACCEPTED TRADE-OFF, pinned so it is a deliberate state rather
        # than a bug someone "fixes" by re-deriving weight downstream: while
        # the gate is off a caption renders at markdown weight everywhere,
        # including on web and Windows, which decode the shape correctly.
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
    """The gate governs EMISSION only — the T023 contract still stands."""

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
