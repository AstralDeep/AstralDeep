"""Feature-065 deterministic recap and sensitive-output tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.voice_recap import (
    MAX_RECAP_WORDS,
    RESULT_READY_NOTICE,
    SENSITIVE_NOTICE,
    CommittedVisibleTextExtractor,
    SensitiveResultConsent,
    SpokenControlContext,
    VoiceRecapError,
    apply_sensitivity_policy,
    build_spoken_recap,
    resolve_spoken_control,
    sanitize_speakable_text,
)


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)
RESULT = "00000000-0000-4000-8000-000000000001"


def _visible_components() -> list[dict]:
    return [
        {
            "type": "card",
            "title": "Analysis complete",
            "content": "The deployment is healthy.",
            "hidden_reasoning": "Secret chain of thought",
            "children": [
                {"type": "alert", "warning": "However, TURN still needs validation."},
                {"type": "text", "next_action": "Next, run the mobile network test."},
                {"type": "text", "text": "The deployment is healthy."},
                {"type": "text", "text": "Never visible", "hidden": True},
                {"type": "html", "content": "<b>raw hidden HTML</b>"},
            ],
            "tool_calls": [{"content": "private tool payload"}],
            "trace": "private trace",
        }
    ]


def test_authoritative_summary_always_wins_and_preserves_caveat_and_action() -> None:
    summary = (
        "The request completed successfully. "
        + "Detail " * 90
        + "However, one compatibility caveat remains. Next, validate on Android."
    )
    recap = build_spoken_recap(
        authoritative_summary=summary,
        committed_components=[{"text": "Contradictory fallback"}],
        detected_language="en-US",
    )
    assert recap.source == "authoritative_summary"
    assert "Contradictory" not in recap.text
    assert "caveat" in recap.text
    assert "Android" in recap.text
    assert len(recap.text.split()) <= MAX_RECAP_WORDS


def test_fallback_uses_only_committed_visible_semantics_and_deduplicates() -> None:
    recap = build_spoken_recap(
        authoritative_summary=None,
        committed_components=_visible_components(),
        detected_language="en",
    )
    assert recap.source == "committed_visible_fallback"
    assert "Analysis complete" in recap.text
    assert recap.text.count("deployment is healthy") == 1
    assert "TURN still needs validation" in recap.text
    assert "mobile network test" in recap.text
    for forbidden in (
        "Secret chain",
        "Never visible",
        "raw hidden HTML",
        "private tool",
        "private trace",
    ):
        assert forbidden not in recap.text


def test_fallback_handles_visible_rows_but_excludes_secret_fields() -> None:
    text = CommittedVisibleTextExtractor().extract(
        [
            {
                "type": "table",
                "title": "Checks",
                "rows": [
                    {"Client": "Web", "Status": "Passed", "api_key": "never"},
                    {"Client": "Android", "Status": "Pending"},
                ],
            }
        ]
    )
    assert "Client: Web" in text
    assert "Status: Passed" in text
    assert "Android" in text
    assert "never" not in text


def test_unavailable_visible_text_gets_honest_terminal_notice() -> None:
    recap = build_spoken_recap(
        authoritative_summary="",
        committed_components=[{"type": "image", "alt": "not traversed"}],
        detected_language="en-GB",
    )
    assert recap.text == RESULT_READY_NOTICE
    assert recap.source == "terminal_status"
    assert recap.output_reason == "visible_text_unavailable"


@pytest.mark.parametrize("language", ["es", "fr-FR", "und", "", "EN-us-x"])
def test_non_english_result_preserves_text_but_speaks_only_safe_notice(language) -> None:
    recap = build_spoken_recap(
        authoritative_summary="Detailed private result",
        committed_components=_visible_components(),
        detected_language=language,
    )
    # Canonical English tags are case-normalized; EN-us-x is still en-*.
    if language == "EN-us-x":
        assert recap.source == "authoritative_summary"
    else:
        assert recap.text == RESULT_READY_NOTICE
        assert recap.source == "terminal_status"
        assert recap.output_policy == "english_lifecycle_only"


def test_sanitization_strips_markup_urls_controls_and_drops_credentials() -> None:
    assert sanitize_speakable_text(
        "<b>Done</b> [details](https://example.test)\x00 `now`"
    ) == "Done details now"
    assert sanitize_speakable_text("Bearer abcdefghijklmnopqrstuvwxyz") == ""
    assert sanitize_speakable_text("api_key=supersecretvalue") == ""


def test_fallback_bounds_nodes_depth_and_words() -> None:
    nested: dict = {"type": "text", "text": "start"}
    for index in range(30):
        nested = {"type": "card", "title": f"level {index}", "children": [nested]}
    components = [nested] + [{"text": "word " * 200}]
    recap = build_spoken_recap(
        authoritative_summary=None,
        committed_components=components,
        detected_language="en",
    )
    assert len(recap.text.split()) <= MAX_RECAP_WORDS


def test_sensitive_policy_fails_closed_for_unknown_phi_and_detector_error() -> None:
    recap = build_spoken_recap(
        authoritative_summary="Patient result is complete",
        committed_components=None,
        detected_language="en",
    )
    unknown = apply_sensitivity_policy(
        recap,
        confidentiality="unknown",
        contains_phi=lambda _text: False,
    )
    detected = apply_sensitivity_policy(
        recap,
        confidentiality="non_sensitive",
        contains_phi=lambda _text: True,
    )

    def broken(_text: str) -> bool:
        raise RuntimeError("detector failed")

    failed = apply_sensitivity_policy(
        recap,
        confidentiality="non_sensitive",
        contains_phi=broken,
    )
    for decision in (unknown, detected, failed):
        assert decision.text == SENSITIVE_NOTICE
        assert decision.source == "sensitive_notice"
        assert decision.sensitivity == "sensitive"


def test_non_sensitive_or_consented_sensitive_recap_keeps_selected_source() -> None:
    recap = build_spoken_recap(
        authoritative_summary="The result is ready.",
        committed_components=None,
        detected_language="en",
    )
    clean = apply_sensitivity_policy(
        recap,
        confidentiality="non_sensitive",
        contains_phi=lambda _text: False,
    )
    consented = apply_sensitivity_policy(
        recap,
        confidentiality="sensitive",
        contains_phi=lambda _text: True,
        consent_granted=True,
    )
    assert clean.text == recap.text and clean.sensitivity == "non_sensitive"
    assert consented.text == recap.text and consented.sensitivity == "sensitive"
    assert consented.source == "authoritative_summary"


def test_sensitive_consent_is_fresh_owner_result_bound_and_one_time() -> None:
    consent = SensitiveResultConsent.issue(
        user_id="user-a",
        result_id=RESULT,
        method="strict_spoken_control",
        now=NOW,
    )
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="user-b", result_id=RESULT, now=NOW)
    with pytest.raises(VoiceRecapError, match="scope_mismatch"):
        consent.consume(user_id="user-a", result_id="other", now=NOW)
    with pytest.raises(VoiceRecapError, match="expired"):
        consent.consume(
            user_id="user-a",
            result_id=RESULT,
            now=NOW + timedelta(minutes=2),
        )
    consumed = consent.consume(
        user_id="user-a",
        result_id=RESULT,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(VoiceRecapError, match="already_consumed"):
        consumed.consume(
            user_id="user-a",
            result_id=RESULT,
            now=NOW + timedelta(seconds=2),
        )


def test_strict_spoken_control_resolves_only_when_exact_context_exists() -> None:
    context = SpokenControlContext(
        pending_sensitive_result_id=RESULT,
        speech_active=True,
        voice_active=True,
        foreground_task_id="task-1",
    )
    assert resolve_spoken_control(" Read it! ", context).action == "read_sensitive_result"
    assert resolve_spoken_control("stop speaking", context).action == "stop_speech"
    assert resolve_spoken_control("mute voice", context).action == "mute_voice"
    assert resolve_spoken_control("cancel my request", context).target_id == "task-1"
    for ordinary in (
        "read the latest result",
        "please read it",
        "mute the television",
        "cancel all requests",
        "Ｒｅａｄ it",
    ):
        assert resolve_spoken_control(ordinary, context) is None
    assert resolve_spoken_control("read it", SpokenControlContext()) is None
    assert resolve_spoken_control("stop speaking", SpokenControlContext()) is None


def test_invalid_component_and_consent_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="sequence"):
        CommittedVisibleTextExtractor().extract("not components")
    with pytest.raises(ValueError, match="object"):
        CommittedVisibleTextExtractor().extract(["not an object"])
    with pytest.raises(VoiceRecapError, match="invalid_consent_lifetime"):
        SensitiveResultConsent.issue(
            user_id="user-a",
            result_id=RESULT,
            method="tap",
            now=NOW,
            lifetime=timedelta(minutes=3),
        )
