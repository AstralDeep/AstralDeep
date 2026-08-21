"""Shipped-client compatibility gate: a rail part must never render blank.

WHY THIS EXISTS. The canonical ``structured`` transcript part carries a
``plain_text`` rendition, and the server validator only ever type-checked it
(``isinstance(part.get("plain_text"), str)``). Every Apple client — the
shipped ``apple-v1.2`` build and the 1.3 tree alike — is stricter:

  * ``ConversationContinuity.swift``'s ``case "structured"`` branch guards
    ``continuityNonBlank(plainText)``, which TRIMS whitespace before testing
    emptiness. A blank rendition returns nil for that part.
  * Decoding is all-or-nothing on both sides of the message boundary:
    ``ConversationMessage`` guards ``parts.count == rawParts.count`` and
    ``Frames.swift`` guards ``messages.count == transcript.count``. So ONE
    blank part discards the ENTIRE ``conversation_snapshot`` and that chat's
    rail never hydrates.

The server was the permissive end: ``_content_parts`` renders each element of
a stored JSON array through ``_plain_text``, which returns "" for an empty
string and for a nested array that reduces to one. A tool result carrying a
blank element therefore committed a part no Apple client could render. The
blank *text* part twin was already handled — ``_rail_parts`` drops a text
part that fails ``text.strip()`` — but ``structured`` parts pass through it
untouched, which is why this hole stayed open.

Web, Windows and Android accept a blank ``plain_text``, so this was silently
Apple-only. The fix moves the SERVER to the strictest client's rule rather
than relaxing that client: emission gets strict, acceptance stays as shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.history import (
    _content_parts,
    _rail_parts,
)
from shared.protocol import ConversationSnapshot, ProtocolValidationError


ROOT = Path(__file__).resolve().parents[2]
APPLE_CONTINUITY = (
    ROOT
    / "components"
    / "AstralProjection"
    / "apple-clients"
    / "AstralCore"
    / "Sources"
    / "AstralCore"
    / "Protocol"
    / "ConversationContinuity.swift"
)


def apple_renders(part: dict) -> bool:
    """The shipped Apple decoder's verdict for one canonical part.

    A faithful twin of ``ConversationPart.init?(json:)`` for the two blank-
    sensitive branches: ``continuityNonBlank`` trims whitespace and newlines
    before testing emptiness, so "   " is as fatal as "".
    """
    if part.get("type") == "structured":
        return bool(str(part.get("plain_text", "")).strip())
    if part.get("type") == "text":
        return bool(str(part.get("text", "")).strip())
    return True


def committed_parts(stored: str) -> list[dict]:
    """The rail parts one stored message contributes to a snapshot."""
    return _rail_parts(_content_parts(stored))


class TestAppleIsTheStrictestValidator:
    """Drift pin: the guard this server rule exists to satisfy."""

    def test_apple_structured_branch_requires_a_non_blank_rendition(self) -> None:
        if not APPLE_CONTINUITY.exists():  # pragma: no cover - source layout
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        structured = source[source.index('case "structured":') :]
        structured = structured[: structured.index('case "recovery":')]
        assert "continuityNonBlank(plainText)" in structured

    def test_apple_blankness_test_trims_whitespace(self) -> None:
        # Load-bearing for this whole gate: if Apple only tested `isEmpty`,
        # a whitespace-only rendition would be fine and the server rule could
        # be a plain truthiness check.
        if not APPLE_CONTINUITY.exists():  # pragma: no cover - source layout
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        body = source[source.index("func continuityNonBlank") :][:200]
        assert "trimmingCharacters" in body
        assert "whitespacesAndNewlines" in body

    def test_apple_discards_the_whole_snapshot_on_one_bad_part(self) -> None:
        # The all-or-nothing guards are why a single part is fatal rather
        # than merely lossy — the cost of a blank part is the entire rail.
        if not APPLE_CONTINUITY.exists():  # pragma: no cover - source layout
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        assert "parts.count == rawParts.count" in source


class TestBlankRenditionsAreNeverCommitted:
    """The reproduction: no stored shape may commit an unrenderable part."""

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param(json.dumps(["Result:", ""]), id="empty-string-element"),
            pytest.param(json.dumps(["", ""]), id="all-empty-elements"),
            pytest.param(json.dumps(["Result:", [""]]), id="nested-empty-string"),
            pytest.param(json.dumps(["Result:", ["   "]]), id="nested-whitespace"),
            pytest.param(json.dumps([["   "]]), id="only-nested-whitespace"),
        ],
    )
    def test_every_committed_part_renders_on_apple(self, stored: str) -> None:
        parts = committed_parts(stored)
        unrenderable = [part for part in parts if not apple_renders(part)]
        assert not unrenderable, (
            f"stored {stored} commits a part no Apple client can render: "
            f"{unrenderable}"
        )

    def test_a_blank_element_degrades_to_an_honest_recovery(self) -> None:
        # Honest degradation, not silent omission: the turn says a saved
        # response could not be displayed rather than pretending it was fine.
        parts = committed_parts(json.dumps(["Result:", ""]))
        assert parts[0] == {"type": "text", "text": "Result:"}
        assert {"type", "code", "message"} == set(parts[1])
        assert parts[1]["code"] == "saved_content_unrenderable"

    def test_the_surviving_elements_are_preserved(self) -> None:
        # One bad element must not cost the rest of the array.
        parts = committed_parts(json.dumps(["before", "", "after"]))
        texts = [part["text"] for part in parts if part["type"] == "text"]
        assert texts == ["before", "after"]


class TestRenderableContentIsUnaffected:
    """The fix must not widen into content that already renders."""

    def test_a_populated_structured_element_still_commits(self) -> None:
        parts = committed_parts(json.dumps(["Result:", {"a": 1}]))
        assert parts == [
            {"type": "text", "text": "Result:"},
            {"type": "structured", "value": {"a": 1}, "plain_text": "a: 1"},
        ]

    def test_an_empty_array_still_commits_its_bracket_rendition(self) -> None:
        # "[]" is a real, visible rendition of an empty result — not blank.
        assert _content_parts(json.dumps([])) == [
            {"type": "structured", "value": [], "plain_text": "[]"}
        ]

    def test_typed_repository_string_is_not_decoded_twice(self) -> None:
        assert _content_parts("[]", already_decoded=True) == [
            {"type": "text", "text": "[]"}
        ]
        assert _content_parts("null", already_decoded=True) == [
            {"type": "text", "text": "null"}
        ]

    def test_an_empty_object_still_commits_its_brace_rendition(self) -> None:
        assert _content_parts(json.dumps({})) == [
            {"type": "structured", "value": {}, "plain_text": "{}"}
        ]

    def test_falsy_scalars_still_commit(self) -> None:
        for stored, plain in ((json.dumps([0]), "0"), (json.dumps([False]), "false")):
            parts = committed_parts(stored)
            assert parts[0]["plain_text"] == plain, stored


class TestCanonicalContractRefusesBlankRenditions:
    """The declared contract now matches the strictest client."""

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

    def test_a_populated_structured_part_is_accepted(self) -> None:
        self._snapshot(
            [{"type": "structured", "value": {"a": 1}, "plain_text": "a: 1"}]
        ).validate()

    @pytest.mark.parametrize("plain_text", ["", "   ", "\n", "\t\n "])
    def test_a_blank_structured_part_is_refused(self, plain_text: str) -> None:
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "structured", "value": [""], "plain_text": plain_text}]
            ).validate()

    def test_a_non_string_rendition_is_still_refused(self) -> None:
        with pytest.raises(ProtocolValidationError):
            self._snapshot(
                [{"type": "structured", "value": [1], "plain_text": None}]
            ).validate()
