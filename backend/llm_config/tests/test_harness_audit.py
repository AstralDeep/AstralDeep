"""Selected call audit includes only the immutable public execution identity.
Private configuration binding and encryption material never enter event metadata.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.harness_capture import HarnessCapture
from llm_config.audit_events import record_llm_call
from llm_config.types import CredentialSource, ResolvedConfig


@pytest.mark.asyncio
async def test_only_public_harness_identity_enters_audit_metadata():
    recorder = SimpleNamespace(record=AsyncMock())
    captured = HarnessCapture("owner", 1, HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
        "b" * 64, "local-page-selection/v1", "private_key_id", "c" * 64)
    await record_llm_call(recorder, actor_user_id="owner", auth_principal="owner", feature="synthetic",
        credential_source=CredentialSource.USER, resolved=ResolvedConfig("http://127.0.0.1:11434/v1", "synthetic"),
        total_tokens=100, outcome="success", harness_capture=captured)
    event = recorder.record.call_args.args[0]
    assert event.inputs_meta["harness"] == {"profile": "standard_local", "version": "1.0.0",
        "artifact_digest": "a" * 64, "research_profile": "local-page-selection/v1"}
    serialized = json.dumps(event.inputs_meta)
    assert "private_key_id" not in serialized and "c" * 64 not in serialized and "b" * 64 not in serialized


@pytest.mark.asyncio
async def test_arbitrary_harness_metadata_never_enters_audit():
    recorder = SimpleNamespace(record=AsyncMock())
    with pytest.raises(ValueError):
        await record_llm_call(recorder, actor_user_id="owner", auth_principal="owner", feature="synthetic",
            credential_source=CredentialSource.USER, resolved=ResolvedConfig("https://example.org/v1", "synthetic"),
            total_tokens=None, outcome="failure", harness_capture={"qualified": True})
    recorder.record.assert_not_called()
