"""Feature 057 (US3) — untrusted-at-the-boundary owner isolation (SC-003).

Covers the boundary guarantees that are enforceable at the permission layer
today: user-agent owner isolation at the dispatch gate and tool-list build
(is_tool_allowed) and the pre-existing private-agent grant-hole fix
(can_user_use_agent). The transport-level scenarios (forged identity over the
tunnel, per-owner flood bound, honest-offline) land with the tunnel tasks
(T008/T009/T011/T021) and their own tests.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.tool_permissions import ToolPermissionManager  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.user_agents import UserAgentRegistry  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402

OWNER = "__t057adv__owner"
FOREIGN = "__t057adv__foreign"
UA_ID = "__t057adv__myagent"


@pytest.fixture()
def plane_env():
    with isolated_plane_runtime("byo_boundary") as runtime:
        registry = UserAgentRegistry(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        ua.create_user_agent(
            registry,
            agent_id=UA_ID,
            owner_user_id=OWNER,
            display_name="Mine",
        )
        permissions = ToolPermissionManager(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
            user_agent_registry=registry,
        )
        yield SimpleNamespace(registry=registry, permissions=permissions)


def test_grant_hole_predicate_blocks_foreign_user(plane_env):
    # T019: a foreign user cannot manage a private user agent (the endpoint 403s
    # on exactly this predicate).
    assert ua.can_user_use_agent(plane_env.registry, OWNER, UA_ID) is True
    assert ua.can_user_use_agent(plane_env.registry, FOREIGN, UA_ID) is False


def test_builtins_unaffected_by_isolation(plane_env):
    # can_user_use_agent returns True for any non-user-agent, so built-in/public
    # management + dispatch is unchanged.
    assert ua.can_user_use_agent(plane_env.registry, FOREIGN, "general") is True


def test_dispatch_gate_denies_foreign_user_agent_tool(plane_env):
    # T020: is_tool_allowed short-circuits a foreign user on a user agent.
    assert plane_env.permissions.is_tool_allowed(FOREIGN, UA_ID, "any_tool") is False


def test_isolation_wins_over_a_stray_scope_row(plane_env):
    # FR-019: even if a stray enabled agent_scopes row exists for the foreign
    # user, isolation (step 0) still denies — visibility/use is NOT reliant on
    # scope hygiene.
    plane_env.permissions.set_agent_scopes(
        FOREIGN, UA_ID, {"tools:read": True}
    )
    assert plane_env.permissions.is_tool_allowed(FOREIGN, UA_ID, "any_tool") is False


def test_owner_is_not_blocked_by_isolation(plane_env):
    # The isolation step (step 0 of _resolve_tool_allowed) must NOT short-circuit
    # the owner — the predicate returns True for the owner, so normal per-user
    # scope resolution proceeds (a granted scope would then allow).
    assert ua.can_user_use_agent(plane_env.registry, OWNER, UA_ID) is True


def test_owner_may_use_their_agent_tool_without_a_scope_grant(plane_env):
    # Feature 057/058 (found live 2026-07-14): a user must be able to USE the
    # agent they authored — a private user-agent has no permission UI to grant a
    # scope, so requiring one made the authored tool unusable by its own creator
    # (the chat LLM couldn't see it and fell back to agentic-creation). The owner
    # now gets a deny→allow baseline on their own agent's validly-scoped tool.
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    assert tp.is_tool_allowed(OWNER, UA_ID, "greet") is True
    # …but a FOREIGN user is still denied (owner isolation is unchanged).
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "greet") is False


def test_owner_baseline_yields_to_an_explicit_opt_out(plane_env):
    # The owner-allow baseline is exactly that — a baseline. An explicit opt-out
    # (a stored agent_scopes row with enabled=False) still wins, same as the
    # feature-040 safe-agent flip.
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    tp.set_agent_scopes(OWNER, UA_ID, {"tools:write": False})
    assert tp.is_tool_allowed(OWNER, UA_ID, "greet") is False


def test_owner_baseline_does_not_leak_to_an_invalid_scope(plane_env):
    # A tool declaring a non-grantable scope has no authority the delegation mint
    # could assert — the owner-allow baseline must not admit it (the VALID_SCOPES
    # gate runs before the owner check).
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"weird": "tools:nonexistent"})
    assert tp.is_tool_allowed(OWNER, UA_ID, "weird") is False
