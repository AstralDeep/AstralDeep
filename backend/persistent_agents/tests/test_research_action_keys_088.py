"""Tests for persistent_agents/research_episode.py: key domains and integer pairs do not
alias or follow claims, and an invalid generation cannot select an action.
"""

from types import SimpleNamespace

import pytest

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.research_episode import research_action_keys


def test_key_domains_and_integer_pairs_do_not_alias_or_follow_claims():
    records = [SimpleNamespace(instruction_revision=r, control_epoch=e)
               for r, e in ((1, 23), (12, 3), (1, 24), (2, 23))]
    keys = [key for record in records for key in research_action_keys(record)]
    assert len(set(keys)) == len(keys)
    assert all(len(key) == 64 and all(c in "0123456789abcdef" for c in key) for key in keys)
    record = records[0]
    before = research_action_keys(record)
    record.claim_generation, record.state_version = 900, 1200
    record.consecutive_failures = 2
    assert research_action_keys(record) == before


@pytest.mark.parametrize("field", ["instruction_revision", "control_epoch"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None, 2**53])
def test_invalid_generation_cannot_select_an_action(field, value):
    values = {"instruction_revision": 1, "control_epoch": 1, field: value}
    with pytest.raises(DispatchDenied, match="assignment_operation_profile_unavailable"):
        research_action_keys(SimpleNamespace(**values))
