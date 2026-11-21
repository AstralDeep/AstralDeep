"""Unit tests for the window-capture source descriptor: strict field, kind, length,
timestamp, and boolean validation with bounded refusal reasons for the audit trail.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.attachments.capture_source import (
    MAX_ACCESSIBILITY_TEXT_CHARS,
    MAX_APP_BUNDLE_ID_CHARS,
    MAX_WINDOW_TITLE_CHARS,
    CaptureSourceError,
    accessibility_text_from_turn_entry,
    capture_source_from_turn_entry,
    parse_capture_source,
    validate_capture_source_payload,
)


def _descriptor(**overrides):
    payload = {
        "kind": "window_capture",
        "app_bundle_id": "com.apple.TextEdit",
        "window_title": "Untitled — TextEdit",
        "captured_at": "2026-10-27T05:20:04Z",
        "accessibility_text_truncated": False,
    }
    payload.update(overrides)
    return payload


def test_valid_descriptor_parses_and_keeps_all_fields():
    source = parse_capture_source(json.dumps(_descriptor()))
    assert source is not None
    assert source.kind == "window_capture"
    assert source.app_bundle_id == "com.apple.TextEdit"
    assert source.window_title == "Untitled — TextEdit"
    assert source.captured_at == "2026-10-27T05:20:04Z"
    assert source.accessibility_text_truncated is False
    assert source.captured_at_datetime() is not None


def test_absent_raw_descriptor_parses_to_none():
    assert parse_capture_source(None) is None


def test_capture_time_accepts_offset_form():
    source = validate_capture_source_payload(
        _descriptor(captured_at="2026-10-27T05:20:04+02:00"))
    assert source.captured_at_datetime() is not None


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not json", "{", "5", '"a string"', "[]", "null"],
)
def test_malformed_json_strings_are_refused_with_reason(raw):
    with pytest.raises(CaptureSourceError) as exc_info:
        parse_capture_source(raw)
    assert exc_info.value.reason
    assert len(exc_info.value.reason) <= 200


def test_non_string_raw_descriptor_is_refused():
    with pytest.raises(CaptureSourceError) as exc_info:
        parse_capture_source({"kind": "window_capture"})
    assert exc_info.value.reason == "descriptor:not_a_json_string"


def test_unknown_kind_is_refused():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(kind="screen_recording"))
    assert "unknown_kind" in exc_info.value.reason


def test_unknown_extra_field_is_refused_by_name():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(pixel_scale=2))
    assert "unknown_field:pixel_scale" in exc_info.value.reason


def test_oversized_fields_are_refused_by_name():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(
            _descriptor(app_bundle_id="a" * (MAX_APP_BUNDLE_ID_CHARS + 1)))
    assert "field_too_long:app_bundle_id" in exc_info.value.reason

    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(
            _descriptor(window_title="w" * (MAX_WINDOW_TITLE_CHARS + 1)))
    assert "field_too_long:window_title" in exc_info.value.reason

    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(
            _descriptor(captured_at="2" * 65))
    assert "field_too_long:captured_at" in exc_info.value.reason


def test_missing_fields_are_refused_by_name():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload({"kind": "window_capture"})
    reason = exc_info.value.reason
    assert "missing_field:app_bundle_id" in reason
    assert "missing_field:window_title" in reason
    assert "missing_field:captured_at" in reason
    assert "missing_field:accessibility_text_truncated" in reason


def test_unparseable_capture_time_is_refused():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(captured_at="yesterday"))
    assert "invalid_timestamp" in exc_info.value.reason


def test_non_bool_truncation_flag_is_refused():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(accessibility_text_truncated="no"))
    assert "invalid_flag" in exc_info.value.reason


def test_control_characters_in_text_fields_are_refused():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(window_title="bad\x00title"))
    assert "control_characters" in exc_info.value.reason


def test_refusal_reasons_stay_bounded_for_audit():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(**{("z" * 500): 1}))
    assert len(exc_info.value.reason) <= 200


def test_turn_entry_helpers_drop_invalid_provenance_and_text():
    assert capture_source_from_turn_entry(None) is None
    assert capture_source_from_turn_entry(_descriptor()) is not None
    assert capture_source_from_turn_entry({"kind": "other"}) is None
    assert capture_source_from_turn_entry("window_capture") is None

    assert accessibility_text_from_turn_entry(None) is None
    assert accessibility_text_from_turn_entry("captured line") == "captured line"
    assert accessibility_text_from_turn_entry("") is None
    assert accessibility_text_from_turn_entry(7) is None
    assert accessibility_text_from_turn_entry("x" * (MAX_ACCESSIBILITY_TEXT_CHARS + 1)) is None


def test_captured_at_datetime_tolerates_unparseable_value():
    from orchestrator.attachments.capture_source import WindowCaptureSource

    source = WindowCaptureSource.model_construct(
        kind="window_capture",
        app_bundle_id="a",
        window_title="t",
        captured_at="not-a-timestamp",
        accessibility_text_truncated=False,
    )
    assert source.captured_at_datetime() is None


def test_unmapped_validation_error_kinds_map_to_invalid_field():
    with pytest.raises(CaptureSourceError) as exc_info:
        validate_capture_source_payload(_descriptor(app_bundle_id=123))
    assert "invalid_field:app_bundle_id" in exc_info.value.reason


def test_reason_fallback_when_no_error_entries():
    from types import SimpleNamespace

    from orchestrator.attachments.capture_source import _reason_from_validation_error

    assert _reason_from_validation_error(
        SimpleNamespace(errors=lambda: [])) == "descriptor:invalid"


def test_model_itself_forbids_unknown_fields():
    import pytest as _pytest
    from pydantic import ValidationError as _ValidationError

    from orchestrator.attachments.capture_source import WindowCaptureSource

    with _pytest.raises(_ValidationError) as exc_info:
        WindowCaptureSource.model_validate(_descriptor(unexpected="x"))
    assert "extra_forbidden" in str(exc_info.value)
    with _pytest.raises(_ValidationError):
        WindowCaptureSource.model_validate(_descriptor(kind="other_kind"))


def test_reason_mapper_reads_pydantic_extra_forbidden_and_kind_errors():
    from pydantic import ValidationError

    from orchestrator.attachments.capture_source import (
        _reason_from_validation_error,
        WindowCaptureSource,
    )

    try:
        WindowCaptureSource.model_validate(_descriptor(unexpected="x", kind="nope"))
    except ValidationError as exc:
        reason = _reason_from_validation_error(exc)
    assert "unknown_field:unexpected" in reason
    assert "unknown_kind" in reason
