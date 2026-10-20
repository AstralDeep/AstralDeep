"""Checks effect declarations and origin decisions at the host security boundary.
These tests deliberately do not treat coincident text as proof of human intent.
"""

from __future__ import annotations

import pytest

from orchestrator.source_bound import (
    READ_ONLY_GENERAL_FILE_TOOLS, Effect, Origin, SourceLedger, action_digest,
    combine_origins, declaration, read_only_stream, registered_action, review_risks,
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


def test_host_owned_file_read_exemption_matches_first_party_registry():
    from agents.general.mcp_tools import TOOL_REGISTRY

    actual = {name for name, info in TOOL_REGISTRY.items()
              if info.get("scope") == "tools:files"}
    assert actual == READ_ONLY_GENERAL_FILE_TOOLS
    assert declaration({"read_text": "tools:files"}, "general-1", "read_text",
                       host_file_read=True).effect is Effect.READ
    assert declaration({"read_text": "tools:files"}, "user-agent", "read_text"
                       ).effect is Effect.MUTATION
    assert registered_action({"read_text": "tools:files"}, "general-1", "read_text",
                             {("general-1", "read_text")}).effect is Effect.READ
    assert read_only_stream({"read_text": "tools:files"}, "general-1", "read_text",
                            host_file_read=True).effect is Effect.READ
    with pytest.raises(ValueError, match="conflicting"):
        declaration({"write_file": "tools:files"}, "general-1", "write_file",
                    host_file_read=True)


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


def test_tool_result_and_presentation_both_downgrade_lineage():
    ledger = SourceLedger()
    ledger.record_tool_output({"_data": {"text": "outside text"}},
                              [{"type": "text", "content": "presented text"}])
    assert ledger.origin_of({"body": "outside text"}) is Origin.TOOL
    assert ledger.origin_of({"body": "presented text"}) is Origin.TOOL
    assert ledger.origin_of({"body": "outside text paraphrased"}) is Origin.UNKNOWN
    assert ledger.origin_of({"body": "outside text", "note": "new"}) is Origin.UNKNOWN
    assert SourceLedger().origin_of({"body": "outside text"}) is Origin.UNKNOWN


def test_lineage_eviction_becomes_unknown():
    ledger = SourceLedger(limit=1)
    ledger.record_tool_output("old", "new")
    assert ledger.origin_of("old") is Origin.UNKNOWN
    assert ledger.origin_of("new") is Origin.TOOL


def test_stream_requires_explicit_read_effect():
    assert read_only_stream({"fetch": "tools:read"}, "agent", "fetch").effect is Effect.READ
    for scopes in ({"send": "tools:write"}, {"send": "tools:execute"}, {}):
        with pytest.raises(ValueError):
            read_only_stream(scopes, "agent", "send")
