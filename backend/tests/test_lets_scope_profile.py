"""Fixed six-scope LETS profile and fail-closed mapping tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from orchestrator.lets_scope_profile import (
    RESOURCE_DIMENSIONS,
    SCOPE_BINDINGS,
    SCOPE_PROFILE_SHA256,
    SCOPE_PROFILE_VERSION,
    ScopeProfileError,
    binding_for_scope,
    binding_for_tool,
    binding_for_transition,
    profile_sha256,
    require_single_audience,
    validate_allocation,
)

EXPECTED = (
    ("tools:read", "astral.tools.read", "tool_read", 0),
    ("tools:write", "astral.tools.write", "tool_write", 1),
    ("tools:search", "astral.tools.search", "tool_search", 2),
    ("tools:system", "astral.tools.system", "tool_system", 3),
    ("tools:files", "astral.tools.files", "tool_files", 4),
    ("tools:execute", "astral.tools.execute", "tool_execute", 5),
)


def test_exact_six_scope_profile_and_unit_costs() -> None:
    assert SCOPE_PROFILE_VERSION == "astral.tools/v1"
    assert RESOURCE_DIMENSIONS == len(SCOPE_BINDINGS) == 6
    assert (
        tuple(
            (
                entry.scope,
                entry.capability,
                entry.transition,
                entry.resource_dimension,
            )
            for entry in SCOPE_BINDINGS
        )
        == EXPECTED
    )

    for entry in SCOPE_BINDINGS:
        assert binding_for_scope(entry.scope) is entry
        assert binding_for_transition(entry.transition) is entry
        assert entry.unit_cost() == tuple(
            1 if index == entry.resource_dimension else 0 for index in range(6)
        )


@pytest.mark.parametrize(
    "allocation",
    [(), (1, 2, 3, 4, 5), (1, 2, 3, 4, 5, 6, 7), "1,2,3,4,5,6"],
)
def test_incomplete_or_misshaped_allocation_is_denied(allocation: object) -> None:
    with pytest.raises(ScopeProfileError, match="exactly 6"):
        validate_allocation(allocation)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "allocation",
    [(0, 0, 0, 0, 0, -1), (0, 0, 0, 0, 0, True), (0, 0, 0, 0, 0, 1.5)],
)
def test_invalid_allocation_dimension_is_denied(allocation: object) -> None:
    with pytest.raises(ScopeProfileError, match="non-negative integers"):
        validate_allocation(allocation)  # type: ignore[arg-type]


def test_unknown_scope_transition_and_tool_are_denied() -> None:
    with pytest.raises(ScopeProfileError, match="unknown LETS tool scope"):
        binding_for_scope("tools:admin")
    with pytest.raises(ScopeProfileError, match="unknown LETS transition"):
        binding_for_transition("tool_admin")
    with pytest.raises(ScopeProfileError, match="no registered scope"):
        binding_for_tool("unknown", {"known": "tools:read"})
    with pytest.raises(ScopeProfileError, match="unknown LETS tool scope"):
        binding_for_tool("known", {"known": "tools:admin"})


def test_registered_tool_resolves_one_exact_binding() -> None:
    assert binding_for_tool(
        "search_web", {"search_web": "tools:search"}
    ) is binding_for_scope("tools:search")
    assert validate_allocation((5, 4, 3, 2, 1, 0)) == (5, 4, 3, 2, 1, 0)


@pytest.mark.parametrize("audiences", [(), ("one", "two"), ("same", "same"), "one"])
def test_missing_or_ambiguous_audience_is_denied(audiences: object) -> None:
    with pytest.raises(ScopeProfileError):
        require_single_audience(audiences)  # type: ignore[arg-type]


def test_profile_digest_detects_any_mapping_change() -> None:
    assert len(SCOPE_PROFILE_SHA256) == 64
    assert profile_sha256() == SCOPE_PROFILE_SHA256
    changed = (replace(SCOPE_BINDINGS[0], transition="tool_read_changed"),) + tuple(
        SCOPE_BINDINGS[1:]
    )
    assert profile_sha256(changed) != SCOPE_PROFILE_SHA256


def test_one_canonical_audience_is_accepted() -> None:
    assert require_single_audience(("astral-gateway",)) == "astral-gateway"
