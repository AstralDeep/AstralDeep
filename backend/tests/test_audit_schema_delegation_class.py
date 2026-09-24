"""Tests for audit/schemas.py: the delegation event class (hop mint/enforce) is
registered in EVENT_CLASSES and validates, while unknown classes are still rejected.
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from audit.schemas import EVENT_CLASSES, AuditEventCreate


def _event(**overrides):
    base = dict(
        actor_user_id="user-1",
        auth_principal="agent:web-research-1",
        agent_id="summarizer-1",
        event_class="delegation",
        action_type="delegation.hop.mint",
        description="child delegation minted for chained hop",
        correlation_id="corr-1",
        outcome="in_progress",
        started_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return AuditEventCreate(**base)


def test_delegation_in_event_classes():
    assert "delegation" in EVENT_CLASSES


def test_delegation_event_validates():
    ev = _event()
    assert ev.event_class == "delegation"
    assert ev.action_type == "delegation.hop.mint"


def test_delegation_enforce_pair_validates():
    ev = _event(
        action_type="delegation.hop.enforce",
        outcome="failure",
        outcome_detail="empty_intersection",
        inputs_meta={
            "parent_actor": "agent:web-research-1",
            "delegation_depth": 1,
            "requested_scopes": ["tool:summarize_text"],
            "granted_scopes": [],
        },
    )
    assert ev.outcome_detail == "empty_intersection"


def test_unknown_event_class_still_rejected():
    with pytest.raises(ValidationError):
        _event(event_class="not_a_real_class")
