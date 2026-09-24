"""Tests that backend/orchestrator/history.py's structured-part plain_text rendition is
never blank, matching the shipped Apple client's stricter all-or-nothing continuity
parsing (backend/shared/protocol.py).
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
    if part.get("type") == "structured":
        return bool(str(part.get("plain_text", "")).strip())
    if part.get("type") == "text":
        return bool(str(part.get("text", "")).strip())
    return True


def committed_parts(stored: str) -> list[dict]:
    return _rail_parts(_content_parts(stored))


class TestAppleIsTheStrictestValidator:
    def test_apple_structured_branch_requires_a_non_blank_rendition(self) -> None:
        if not APPLE_CONTINUITY.exists():  # pragma: no cover
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        structured = source[source.index('case "structured":') :]
        structured = structured[: structured.index('case "recovery":')]
        assert "continuityNonBlank(plainText)" in structured

    def test_apple_blankness_test_trims_whitespace(self) -> None:
        # Apple trims first; a plain truthiness check wouldn't match
        if not APPLE_CONTINUITY.exists():  # pragma: no cover
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        body = source[source.index("func continuityNonBlank") :][:200]
        assert "trimmingCharacters" in body
        assert "whitespacesAndNewlines" in body

    def test_apple_discards_the_whole_snapshot_on_one_bad_part(self) -> None:
        if not APPLE_CONTINUITY.exists():  # pragma: no cover
            pytest.skip("Apple client source is unavailable")
        source = APPLE_CONTINUITY.read_text(encoding="utf-8")
        assert "parts.count == rawParts.count" in source


class TestBlankRenditionsAreNeverCommitted:
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
        parts = committed_parts(json.dumps(["Result:", ""]))
        assert parts[0] == {"type": "text", "text": "Result:"}
        assert {"type", "code", "message"} == set(parts[1])
        assert parts[1]["code"] == "saved_content_unrenderable"

    def test_the_surviving_elements_are_preserved(self) -> None:
        parts = committed_parts(json.dumps(["before", "", "after"]))
        texts = [part["text"] for part in parts if part["type"] == "text"]
        assert texts == ["before", "after"]


class TestRenderableContentIsUnaffected:
    def test_a_populated_structured_element_still_commits(self) -> None:
        parts = committed_parts(json.dumps(["Result:", {"a": 1}]))
        assert parts == [
            {"type": "text", "text": "Result:"},
            {"type": "structured", "value": {"a": 1}, "plain_text": "a: 1"},
        ]

    def test_an_empty_array_still_commits_its_bracket_rendition(self) -> None:
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
