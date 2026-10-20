"""Checks effect declarations and origin decisions at the host security boundary.
These tests deliberately do not treat coincident text as proof of human intent.
"""

from __future__ import annotations

import pytest

from orchestrator.source_bound import (
    Effect, Origin, action_digest, combine_origins, declaration, review_risks,
)


@pytest.mark.parametrize(("scope", "effect"), [
    ("tools:read", Effect.READ),
    ("tools:search", Effect.READ),
    ("tools:write", Effect.MUTATION),
    ("tools:files", Effect.MUTATION),
    ("tools:execute", Effect.PRIVILEGED),
    ("tools:system", Effect.PRIVILEGED),
])
def test_explicit_scope_classification(scope, effect):
    action = declaration({"send": scope}, "agent", "send")
    assert action.effect is effect
    assert action.needs_review is (effect is not Effect.READ)


@pytest.mark.parametrize("scopes", [{}, {"send": ""}, {"send": "unknown"},
                                           {"send": None}])
def test_missing_or_malformed_declaration_denied(scopes):
    with pytest.raises(ValueError, match="effect declaration"):
        declaration(scopes, "agent", "send")


def test_alias_needs_own_declaration():
    with pytest.raises(ValueError):
        declaration({"send": "tools:write"}, "agent", "alias_send")


def test_unknown_or_derived_origin_cannot_become_human():
    assert combine_origins(Origin.HUMAN, Origin.TOOL) is Origin.DERIVED
    assert combine_origins(Origin.HUMAN, Origin.UNKNOWN) is Origin.UNKNOWN
    assert combine_origins() is Origin.UNKNOWN


def test_action_digest_binds_owner_target_arguments_and_effect():
    action = declaration({"send": "tools:write"}, "agent", "send")
    first = action_digest("alice", "chat", action, {"to": "a@example.org", "body": "x"})
    assert len(first) == 64
    assert first != action_digest("bob", "chat", action, {"to": "a@example.org", "body": "x"})
    assert first != action_digest("alice", "chat", action, {"to": "b@example.org", "body": "x"})
    assert first != action_digest("alice", "chat", action, {"to": "a@example.org", "body": "y"})
    assert "source_bound" in review_risks(action)
