"""A schedule proposed in chat must be able to run, or the model is told so it can correct it."""

from types import SimpleNamespace

import pytest

from orchestrator import scheduling_chat


def test_a_proposal_with_no_upcoming_run_is_refused(monkeypatch):
    monkeypatch.setattr(scheduling_chat, "_scheduler_store",
                        lambda _orch: SimpleNamespace(count_active=lambda _user: 0))

    with pytest.raises(ValueError, match="no upcoming run"):
        scheduling_chat._validate_proposal(SimpleNamespace(agent_cards={}), "owner", {
            "name": "Past", "instruction": "Roll a die",
            "schedule_kind": "one_shot", "schedule_expr": "2020-01-01T09:00:00Z",
        })
