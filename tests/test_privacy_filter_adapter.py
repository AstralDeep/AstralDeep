import builtins
import logging
from unittest import mock

import pytest

# Import the module under test
from backend.shared import privacy_filter_adapter as pfa

# ---------------------------------------------------------------------------
# Helper classes to mimic the OpenAI privacy‑filter return objects
# ---------------------------------------------------------------------------
class DummyRedactionResult:
    def __init__(self, redacted_text: str, spans):
        self.redacted_text = redacted_text
        self.spans = spans

# ---------------------------------------------------------------------------
# Test that a successful model call returns the expected structure
# ---------------------------------------------------------------------------
def test_filter_text_success(monkeypatch):
    # Prepare a dummy result that redacts a name
    original = "Patient John Doe was admitted on 2023-01-15."
    redacted = "Patient [REDACTED:name] was admitted on 2023-01-15."
    spans = [(8, 16, "name", "[REDACTED:name]")]
    dummy_result = DummyRedactionResult(redacted, spans)

    # Mock the private _get_filter to return an object with a ``redact`` method
    mock_filter = mock.Mock()
    mock_filter.redact.return_value = dummy_result
    monkeypatch.setattr(pfa, "_get_filter", lambda: mock_filter)

    out_text, detections = pfa.filter_text(original)
    assert out_text == redacted
    assert detections == [{"start": 8, "end": 16, "category": "name", "replacement": "[REDACTED:name]"}]

    # Ensure the mock was called with the correct arguments
    mock_filter.redact.assert_called_once_with(original, max_tokens=2048)

# ---------------------------------------------------------------------------
# Test that any exception results in a safe fallback (original text, no detections)
# ---------------------------------------------------------------------------
def test_filter_text_failure(monkeypatch, caplog):
    # Force the private _get_filter to raise an exception
    def broken_get_filter():
        raise RuntimeError("model load failed")

    monkeypatch.setattr(pfa, "_get_filter", broken_get_filter)

    caplog.set_level(logging.WARNING)
    original = "Sensitive MRN: 12345678"
    out_text, detections = pfa.filter_text(original)

    # The function must return the original text unchanged and no detections
    assert out_text == original
    assert detections == []
    # A warning should have been logged
    assert any("Privacy filter failed" in rec.message for rec in caplog.records)

# ---------------------------------------------------------------------------
# Test the convenience helper ``redact_with_fallback``
# ---------------------------------------------------------------------------
def test_redact_with_fallback_uses_filter(monkeypatch):
    # Mock filter_text to return a known redacted string
    monkeypatch.setattr(pfa, "filter_text", lambda txt: ("[REDACTED]", []))
    assert pfa.redact_with_fallback("any text") == "[REDACTED]"

# ---------------------------------------------------------------------------
# Test that missing model assets raise a clear error
# ---------------------------------------------------------------------------
def test_missing_model_assets(monkeypatch, caplog):
    # Mock os.path.exists to return False for model files
    monkeypatch.setattr(pfa.os.path, "exists", lambda x: False)
    
    # Reset the filter instance to force reload
    pfa._filter_instance = None
    
    caplog.set_level(logging.WARNING)
    original = "Test text"
    out_text, detections = pfa.filter_text(original)
    
    assert out_text == original
    assert detections == []
    assert any("Privacy filter failed" in rec.message for rec in caplog.records)
