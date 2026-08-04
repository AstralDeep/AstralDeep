"""066 T032 parity pins: Windows renders honest refusal lines, not raw codes.

The web and Apple clients render a human sentence for every server refusal
reason; Windows used to surface the raw code (``worker_unavailable``) on the
capability-not-ready and session-create failure paths. These pins hold the
mapping and its unmapped-code fallback (verbatim code, never a generic line).
"""

from __future__ import annotations

from astral_client.voice import _REFUSAL_REASON_TEXT, _refusal_line


def test_known_refusal_reasons_render_human_lines() -> None:
    for code, line in _REFUSAL_REASON_TEXT.items():
        rendered = _refusal_line(code)
        assert rendered == line
        assert rendered != code
        assert " " in rendered  # a sentence, not a code


def test_fr033_refusal_classes_are_covered() -> None:
    # FR-033 names four refusal classes: worker, speech service, permission,
    # capacity. Permission is handled before the REST call on Windows; the
    # other three must map here.
    for code in (
        "worker_unavailable",
        "asr_unavailable",
        "tts_unavailable",
        "capacity_exhausted",
    ):
        assert code in _REFUSAL_REASON_TEXT


def test_unmapped_code_falls_back_to_the_verbatim_code() -> None:
    assert _refusal_line("stale_generation") == "stale_generation"
    assert _refusal_line("") == "Voice is temporarily unavailable. You can keep typing."
    assert _refusal_line(None) == "Voice is temporarily unavailable. You can keep typing."


def test_wording_matches_the_web_reference() -> None:
    # Cross-client copy parity with web's VOICE_REASON_TEXT (T032).
    assert (
        _refusal_line("capacity_exhausted")
        == "Voice is at capacity right now. Try again shortly."
    )
    assert (
        _refusal_line("feature_disabled")
        == "Voice is not enabled on this server. You can keep typing."
    )
