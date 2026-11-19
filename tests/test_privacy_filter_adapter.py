import builtins
import logging
import os
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
# Test the convenience helper ``redact_with_fallback`` success path
# ---------------------------------------------------------------------------
def test_redact_with_fallback_uses_filter(monkeypatch):
    # Mock filter_text to return a known redacted string
    monkeypatch.setattr(pfa, "filter_text", lambda txt: ("[REDACTED]", []))
    assert pfa.redact_with_fallback("any text") == "[REDACTED]"

# ---------------------------------------------------------------------------
# Test the convenience helper ``redact_with_fallback`` failure path
# ---------------------------------------------------------------------------
def test_redact_with_fallback_failure(monkeypatch):
    # Mock filter_text to return original text on failure
    original = "Sensitive Data"
    monkeypatch.setattr(pfa, "filter_text", lambda txt: (original, []))
    assert pfa.redact_with_fallback(original) == original

# ---------------------------------------------------------------------------
# Test _load_filter when OPF is not available
# ---------------------------------------------------------------------------
def test_load_filter_no_opf(monkeypatch):
    monkeypatch.setattr(pfa, "_HAS_OPF", False)
    with pytest.raises(RuntimeError, match="OpenAI privacy‑filter package not installed"):
        pfa._load_filter()

# ---------------------------------------------------------------------------
# Test _load_filter when assets are missing
# ---------------------------------------------------------------------------
def test_load_filter_missing_assets(monkeypatch):
    monkeypatch.setattr(pfa, "_HAS_OPF", True)
    # Mock os.path.exists to return False
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    with pytest.raises(RuntimeError, match="Privacy filter model assets not found"):
        pfa._load_filter()

# ---------------------------------------------------------------------------
# Test _load_filter success path
# ---------------------------------------------------------------------------
def test_load_filter_success(monkeypatch):
    monkeypatch.setattr(pfa, "_HAS_OPF", True)
    # Mock os.path.exists to return True
    monkeypatch.setattr(os.path, "exists", lambda path: True)
    
    # Mock the PrivacyFilter class
    mock_privacy_filter = mock.Mock()
    monkeypatch.setattr(pfa, "PrivacyFilter", mock_privacy_filter)
    
    pfa._load_filter()
    
    mock_privacy_filter.assert_called_once_with(
        model_path=pfa.MODEL_CHECKPOINT,
        tokenizer_path=pfa.TOKENIZER_PATH,
        device="cpu",
    )

# ---------------------------------------------------------------------------
# Test _get_filter caching behavior
# ---------------------------------------------------------------------------
def test_get_filter_caching(monkeypatch):
    # Reset the global instance
    pfa._filter_instance = None
    
    mock_filter = mock.Mock()
    mock_load = mock.Mock(return_value=mock_filter)
    monkeypatch.setattr(pfa, "_load_filter", mock_load)
    
    # First call should load
    result1 = pfa._get_filter()
    assert result1 is mock_filter
    assert mock_load.call_count == 1
    
    # Second call should use cache
    result2 = pfa._get_filter()
    assert result2 is mock_filter
    assert mock_load.call_count == 1
