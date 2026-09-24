"""Tests for tool_permissions.py and user_agents.py: user-agent owner isolation at the
dispatch gate and tool-list build, including the owner's own deny→allow baseline and
its precedence against opt-outs and invalid scopes.
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
    assert ua.can_user_use_agent(plane_env.registry, OWNER, UA_ID) is True
    assert ua.can_user_use_agent(plane_env.registry, FOREIGN, UA_ID) is False


def test_builtins_unaffected_by_isolation(plane_env):
    assert ua.can_user_use_agent(plane_env.registry, FOREIGN, "general") is True


def test_dispatch_gate_denies_foreign_user_agent_tool(plane_env):
    assert plane_env.permissions.is_tool_allowed(FOREIGN, UA_ID, "any_tool") is False


def test_isolation_wins_over_a_stray_scope_row(plane_env):
    plane_env.permissions.set_agent_scopes(
        FOREIGN, UA_ID, {"tools:read": True}
    )
    assert plane_env.permissions.is_tool_allowed(FOREIGN, UA_ID, "any_tool") is False


def test_owner_is_not_blocked_by_isolation(plane_env):
    assert ua.can_user_use_agent(plane_env.registry, OWNER, UA_ID) is True


def test_owner_may_use_their_agent_tool_without_a_scope_grant(plane_env):
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    assert tp.is_tool_allowed(OWNER, UA_ID, "greet") is True
    assert tp.is_tool_allowed(FOREIGN, UA_ID, "greet") is False


def test_owner_baseline_yields_to_an_explicit_opt_out(plane_env):
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"greet": "tools:write"})
    tp.set_agent_scopes(OWNER, UA_ID, {"tools:write": False})
    assert tp.is_tool_allowed(OWNER, UA_ID, "greet") is False


def test_owner_baseline_does_not_leak_to_an_invalid_scope(plane_env):
    tp = plane_env.permissions
    tp.register_tool_scopes(UA_ID, {"weird": "tools:nonexistent"})
    assert tp.is_tool_allowed(OWNER, UA_ID, "weird") is False
