"""Rejects standalone extraction authority even when callers supply legacy provider credentials.
Host-owned extraction uses the ordinary governed dispatcher instead of MCP credential arguments.
"""

import json

import pytest

from agents.fhir import extraction
from agents.fhir.tests.test_extraction import (
    NOTE,
    NOTE_ID,
    golden_payload,
    make_fake_openai,
)


@pytest.mark.parametrize(
    "credentials",
    [
        {"_session_llm_credentials": {"OPENAI_API_KEY": "synthetic-legacy-key"}},
        {"_credentials": {"OPENAI_API_KEY": "synthetic-legacy-key"}},
    ],
)
def test_standalone_credentials_cannot_authorize_a_provider_effect(
    monkeypatch, credentials
):
    monkeypatch.setitem(extraction.flags._flags, "clinical_note_extract", True)
    fake, calls = make_fake_openai([json.dumps(golden_payload())])
    monkeypatch.setattr(extraction, "OpenAI", fake, raising=False)
    result = extraction.extract_note_fields(
        note_text=NOTE, note_id=NOTE_ID, **credentials
    )
    assert result.get("_error", {}).get("code") == "EXTRACT_HOST_UNAVAILABLE"
    assert calls == []
