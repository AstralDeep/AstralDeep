"""Checks native bearer voice guidance through real PostgreSQL admission and the chat wrapper.
The model boundary stops after verified catalog expansion; cookie and preaccepted cases provide comparisons.
"""

import logging

import pytest

from orchestrator import turn_guidance_authority
from persistent_agents.models import AssignmentError

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("dependency", [False, True])
async def test_guidance_refusal_diagnostics_never_log_exception_text(caplog, dependency):
    with caplog.at_level(logging.WARNING, logger=turn_guidance_authority.__name__):
        with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
            if dependency:
                try:
                    raise RuntimeError("synthetic-private-token-and-transcript")
                except RuntimeError as exc:
                    turn_guidance_authority._refuse(cause=exc)
            else:
                turn_guidance_authority._refuse()
    assert "synthetic-private-token-and-transcript" not in caplog.text
    assert "test_native_voice_guidance_authority.py:test_guidance_refusal_diagnostics_never_log_exception_text:" in caplog.text
    assert ("error=RuntimeError" if dependency else "error=none") in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


