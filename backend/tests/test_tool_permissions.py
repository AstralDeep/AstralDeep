"""
Tests for ToolPermissionManager — Scope-based agent authorization.

Verifies:
1. Default scopes (all disabled)
2. Setting/getting scopes per user per agent
3. is_tool_allowed checks scope enablement
4. Tool→scope mapping registration
5. Persistence across instances
6. get_effective_permissions derives from scopes
"""
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.tool_permissions import (
    VALID_SCOPES,
    ToolPermissionManager,
    resolve_effective_tool_permissions,
)
from astralplane.repositories.tool_policy import ScopeState, ToolOverrideState


class _ToolPolicyRepository:
    """Small in-memory implementation of Plane's typed policy repository."""

    def __init__(self):
        self.scopes = {}
        self.overrides = {}

    def list_scopes(self, _transaction, *, owner_id, agent_id):
        return tuple(
            row
            for key, row in sorted(self.scopes.items())
            if key[:2] == (owner_id, agent_id)
        )

    def list_all_scopes(self, _transaction, *, owner_id):
        return tuple(
            row
            for key, row in sorted(self.scopes.items())
            if key[0] == owner_id
        )

    def set_scopes(
        self, _transaction, *, owner_id, agent_id, scopes, updated_at
    ):
        rows = []
        for scope, enabled in sorted(scopes.items()):
            row = ScopeState(
                owner_id=owner_id,
                agent_id=agent_id,
                scope=scope,
                enabled=enabled,
                updated_at=updated_at,
            )
            self.scopes[(owner_id, agent_id, scope)] = row
            rows.append(row)
        return tuple(rows)

    def list_overrides(self, _transaction, *, owner_id, agent_id):
        return tuple(
            row
            for key, row in sorted(
                self.overrides.items(),
                key=lambda item: (
                    item[0][0],
                    item[0][1],
                    item[0][2],
                    item[0][3] or "",
                ),
            )
            if key[:2] == (owner_id, agent_id)
        )

    def set_tool_override(
        self,
        _transaction,
        *,
        owner_id,
        agent_id,
        tool_name,
        permission_kind,
        enabled,
        updated_at,
    ):
        row = ToolOverrideState(
            owner_id=owner_id,
            agent_id=agent_id,
            tool_name=tool_name,
            permission_kind=permission_kind,
            enabled=enabled,
            updated_at=updated_at,
        )
        self.overrides[(owner_id, agent_id, tool_name, permission_kind)] = row
        return row

    def remove_owner_state(self, _transaction, *, owner_id):
        scope_keys = [key for key in self.scopes if key[0] == owner_id]
        override_keys = [key for key in self.overrides if key[0] == owner_id]
        for key in scope_keys:
            del self.scopes[key]
        for key in override_keys:
            del self.overrides[key]
        return len(scope_keys) + len(override_keys)

    def remove_agent_state(self, _transaction, *, owner_id, agent_id):
        scope_keys = [key for key in self.scopes if key[:2] == (owner_id, agent_id)]
        override_keys = [
            key for key in self.overrides if key[:2] == (owner_id, agent_id)
        ]
        for key in scope_keys:
            del self.scopes[key]
        for key in override_keys:
            del self.overrides[key]
        return len(scope_keys) + len(override_keys)

    def prune_agent_overrides(self, _transaction, *, agent_id, live_tool_names):
        live = set(live_tool_names)
        stale = [
            key
            for key in self.overrides
            if key[1] == agent_id and key[2] not in live
        ]
        for key in stale:
            del self.overrides[key]
        return len(stale)


class _AgentRepository:
    def __init__(self):
        self.user_agents = {}

    def get_agent_for_administration(self, _transaction, *, agent_id):
        return self.user_agents.get(agent_id)

    def get_trust(self, _transaction, *, agent_id):
        return None

    def get_ownership(self, _transaction, *, agent_id):
        return None


class _PlaneRuntime:
    def __init__(self):
        self.repositories = SimpleNamespace(
            tool_policy_state=_ToolPolicyRepository(),
            agents=_AgentRepository(),
        )

    @contextmanager
    def transaction(self):
        yield object()


def _manager(runtime):
    return ToolPermissionManager(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )


def _override_rows(manager, owner_id, agent_id):
    return manager._policy.call(  # noqa: SLF001 - verify through the typed seam
        manager._policy.repository.list_overrides,  # noqa: SLF001
        owner_id=owner_id,
        agent_id=agent_id,
    )


@pytest.fixture
def plane_runtime():
    return _PlaneRuntime()


@pytest.fixture
def manager(plane_runtime):
    m = _manager(plane_runtime)
    # Register tool→scope mapping for a test agent
    m.register_tool_scopes("agent1", {
        "get_system_status": "tools:system",
        "get_cpu_info": "tools:system",
        "modify_data": "tools:write",
        "search_wikipedia": "tools:search",
        "search_arxiv": "tools:search",
        "generate_chart": "tools:read",
    })
    return m


TOOLS = ["get_system_status", "get_cpu_info", "modify_data", "search_wikipedia", "search_arxiv", "generate_chart"]


class TestDefaultScopes:
    def test_all_scopes_disabled_by_default(self, manager):
        """By default, all 4 scopes are disabled."""
        scopes = manager.get_agent_scopes("user1", "agent1")
        assert all(v is False for v in scopes.values())
        assert set(scopes.keys()) == set(VALID_SCOPES)

    def test_is_scope_enabled_default_false(self, manager):
        """Default: all scopes disabled."""
        for scope in VALID_SCOPES:
            assert manager.is_scope_enabled("user1", "agent1", scope) is False

    def test_is_tool_allowed_default_false(self, manager):
        """Default: no tools are allowed (scopes disabled)."""
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is False
        assert manager.is_tool_allowed("user1", "agent1", "get_system_status") is False
        assert manager.is_tool_allowed("user1", "agent1", "search_wikipedia") is False

    def test_effective_permissions_all_false(self, manager):
        """Effective permissions default to False for all tools."""
        result = manager.get_effective_permissions("user1", "agent1", TOOLS)
        assert all(v is False for v in result.values())
        assert set(result.keys()) == set(TOOLS)


class TestOwnerIsolation:
    def test_foreign_user_denied_even_with_stray_scope_grant(
        self, manager, plane_runtime
    ):
        plane_runtime.repositories.agents.user_agents["agent1"] = SimpleNamespace(
            owner_id="user1",
            deleted_at=None,
        )
        manager.set_agent_scopes("user2", "agent1", {"tools:read": True})

        assert manager.is_tool_allowed("user2", "agent1", "generate_chart") is False
        assert manager.is_tool_allowed("user1", "agent1", "generate_chart") is True


class TestSetGetScopes:
    def test_enable_single_scope(self, manager):
        """Enabling a single scope."""
        manager.set_agent_scopes("user1", "agent1", {"tools:read": True})
        assert manager.is_scope_enabled("user1", "agent1", "tools:read") is True
        assert manager.is_scope_enabled("user1", "agent1", "tools:write") is False

    def test_enable_scope_allows_tools(self, manager):
        """Enabling tools:write allows write tools."""
        manager.set_agent_scopes("user1", "agent1", {"tools:write": True})
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is True
        # Read tools still blocked
        assert manager.is_tool_allowed("user1", "agent1", "generate_chart") is False

    def test_enable_multiple_scopes(self, manager):
        """Enabling multiple scopes."""
        manager.set_agent_scopes("user1", "agent1", {
            "tools:read": True,
            "tools:search": True,
        })
        assert manager.is_tool_allowed("user1", "agent1", "generate_chart") is True
        assert manager.is_tool_allowed("user1", "agent1", "search_wikipedia") is True
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is False  # write not enabled
        assert manager.is_tool_allowed("user1", "agent1", "get_system_status") is False  # system not enabled

    def test_different_users_different_scopes(self, manager):
        """Different users have different scope settings."""
        manager.set_agent_scopes("user1", "agent1", {"tools:write": True})
        manager.set_agent_scopes("user2", "agent1", {"tools:write": False, "tools:read": True})
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is True
        assert manager.is_tool_allowed("user2", "agent1", "modify_data") is False
        assert manager.is_tool_allowed("user2", "agent1", "generate_chart") is True

    def test_invalid_scope_ignored(self, manager):
        """Invalid scopes are silently ignored."""
        manager.set_agent_scopes("user1", "agent1", {"tools:invalid": True})
        scopes = manager.get_agent_scopes("user1", "agent1")
        assert "tools:invalid" not in scopes

    def test_disable_scope(self, manager):
        """Disabling a previously enabled scope."""
        manager.set_agent_scopes("user1", "agent1", {"tools:write": True})
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is True
        manager.set_agent_scopes("user1", "agent1", {"tools:write": False})
        assert manager.is_tool_allowed("user1", "agent1", "modify_data") is False


class TestEffectivePermissions:
    def test_effective_from_scopes(self, manager):
        """Effective permissions are derived from scopes."""
        manager.set_agent_scopes("user1", "agent1", {
            "tools:read": True,
            "tools:system": True,
        })
        result = manager.get_effective_permissions("user1", "agent1", TOOLS)
        assert result["generate_chart"] is True     # read
        assert result["get_system_status"] is True   # system
        assert result["get_cpu_info"] is True        # system
        assert result["modify_data"] is False        # write (not enabled)
        assert result["search_wikipedia"] is False   # search (not enabled)
        assert result["search_arxiv"] is False       # search (not enabled)

    def test_detached_snapshot_uses_canonical_precedence(self):
        """Legacy deny wins, then kind override, scope, and safe baseline."""
        scopes = (
            ScopeState("user1", "agent1", "tools:read", True, None),
            ScopeState("user1", "agent1", "tools:write", False, None),
        )
        overrides = (
            ToolOverrideState(
                "user1", "agent1", "read", "tools:read", True, None
            ),
            ToolOverrideState("user1", "agent1", "read", None, False, None),
            ToolOverrideState(
                "user1", "agent1", "write", "tools:write", True, None
            ),
        )

        assert resolve_effective_tool_permissions(
            {
                "read": "tools:read",
                "write": "tools:write",
                "search": "tools:search",
            },
            owner_id="user1",
            agent_id="agent1",
            scope_rows=scopes,
            override_rows=overrides,
            safe_default=True,
        ) == {
            "read": {"tools:read": False},
            "write": {"tools:write": True},
            "search": {"tools:search": True},
        }

    def test_detached_snapshot_rejects_cross_owner_rows(self):
        """A foreign principal's durable grant fails closed before resolution."""
        foreign = ScopeState(
            "user2", "agent1", "tools:read", True, None
        )

        with pytest.raises(RuntimeError, match="owner fence"):
            resolve_effective_tool_permissions(
                {"read": "tools:read"},
                owner_id="user1",
                agent_id="agent1",
                scope_rows=(foreign,),
                override_rows=(),
            )

    @pytest.mark.parametrize(
        ("scope_rows", "override_rows", "message"),
        [
            (
                (SimpleNamespace(
                    owner_id="user1",
                    agent_id="agent1",
                    scope="tools:read",
                    enabled="yes",
                ),),
                (),
                "scope snapshot is invalid",
            ),
            (
                (),
                (ToolOverrideState(
                    "user2", "agent1", "read", "tools:read", True, None
                ),),
                "owner fence",
            ),
            (
                (),
                (SimpleNamespace(
                    owner_id="user1",
                    agent_id="agent1",
                    tool_name="read",
                    permission_kind=7,
                    enabled=True,
                ),),
                "override snapshot is invalid",
            ),
        ],
    )
    def test_detached_snapshot_rejects_invalid_typed_rows(
        self, scope_rows, override_rows, message
    ):
        with pytest.raises(RuntimeError, match=message):
            resolve_effective_tool_permissions(
                {"read": "tools:read"},
                owner_id="user1",
                agent_id="agent1",
                scope_rows=scope_rows,
                override_rows=override_rows,
            )

    @pytest.mark.parametrize(
        ("owner_id", "agent_id"),
        [("", "agent1"), ("user1", "")],
    )
    def test_detached_snapshot_requires_exact_identity(self, owner_id, agent_id):
        with pytest.raises(ValueError, match="non-empty string"):
            resolve_effective_tool_permissions(
                {},
                owner_id=owner_id,
                agent_id=agent_id,
                scope_rows=(),
                override_rows=(),
            )


class TestGetAllowedTools:
    def test_filter_by_scope(self, manager):
        manager.set_agent_scopes("user1", "agent1", {
            "tools:search": True,
            "tools:system": True,
        })
        allowed = manager.get_allowed_tools("user1", "agent1", TOOLS)
        assert "search_wikipedia" in allowed
        assert "search_arxiv" in allowed
        assert "get_system_status" in allowed
        assert "get_cpu_info" in allowed
        assert "modify_data" not in allowed
        assert "generate_chart" not in allowed


class TestEnabledScopeNames:
    def test_enabled_scope_names(self, manager):
        manager.set_agent_scopes("user1", "agent1", {
            "tools:read": True,
            "tools:write": False,
            "tools:search": True,
        })
        names = manager.get_enabled_scope_names("user1", "agent1")
        assert "tools:read" in names
        assert "tools:search" in names
        assert "tools:write" not in names
        assert "tools:system" not in names


class TestToolScopeMapping:
    def test_get_tool_scope(self, manager):
        assert manager.get_tool_scope("agent1", "modify_data") == "tools:write"
        assert manager.get_tool_scope("agent1", "search_wikipedia") == "tools:search"
        assert manager.get_tool_scope("agent1", "get_system_status") == "tools:system"
        assert manager.get_tool_scope("agent1", "generate_chart") == "tools:read"

    def test_unknown_tool_defaults_to_read(self, manager):
        assert manager.get_tool_scope("agent1", "unknown_tool") == "tools:read"

    def test_unknown_agent_defaults_to_read(self, manager):
        assert manager.get_tool_scope("nonexistent_agent", "some_tool") == "tools:read"

    def test_get_tool_scope_map(self, manager):
        scope_map = manager.get_tool_scope_map("agent1")
        assert scope_map["modify_data"] == "tools:write"
        assert scope_map["search_wikipedia"] == "tools:search"
        assert len(scope_map) == 6


class TestPersistence:
    def test_save_and_reload(self, plane_runtime):
        """Typed repository state is visible across manager instances."""
        m1 = _manager(plane_runtime)
        m1.register_tool_scopes("agent1", {"modify_data": "tools:write"})
        m1.set_agent_scopes("user1", "agent1", {"tools:write": True})

        m2 = _manager(plane_runtime)
        m2.register_tool_scopes("agent1", {"modify_data": "tools:write"})
        assert m2.is_scope_enabled("user1", "agent1", "tools:write") is True
        assert m2.is_tool_allowed("user1", "agent1", "modify_data") is True

    def test_typed_repository_connected(self, plane_runtime):
        m = _manager(plane_runtime)
        m.set_agent_scopes("user1", "agent1", {"tools:read": True})
        # Verify state was persisted through Plane's typed repository contract.
        assert m.is_scope_enabled("user1", "agent1", "tools:read") is True


class TestCleanup:
    def test_remove_user_permissions(self, manager):
        manager.set_agent_scopes("user1", "agent1", {"tools:write": True})
        manager.remove_user_permissions("user1")
        assert manager.is_scope_enabled("user1", "agent1", "tools:write") is False

    def test_remove_agent_permissions(self, manager):
        manager.set_agent_scopes("user1", "agent1", {"tools:write": True})
        manager.set_agent_scopes("user1", "agent2", {"tools:write": True})
        manager.remove_agent_permissions("user1", "agent1")
        assert manager.is_scope_enabled("user1", "agent1", "tools:write") is False
        assert manager.is_scope_enabled("user1", "agent2", "tools:write") is True

    def test_cleanup_stale_tool_overrides_prunes_removed_tool(self, manager):
        """A tool override for a tool no longer in the live registry is pruned."""
        manager.set_tool_permission("user1", "agent1", "modify_data", "tools:write", False)
        manager.set_tool_permission("user1", "agent1", "removed_tool", "tools:read", False)
        live_tools = ["modify_data", "search_wikipedia", "generate_chart"]
        deleted = manager.cleanup_stale_tool_overrides("agent1", live_tools)
        assert deleted >= 1
        rows = _override_rows(manager, "user1", "agent1")
        names = {row.tool_name for row in rows}
        assert "removed_tool" not in names
        assert "modify_data" in names

    def test_cleanup_stale_tool_overrides_empty_live_list(self, manager):
        """An empty live tool list deletes every override row for the agent."""
        manager.set_tool_permission("user1", "agent1", "modify_data", "tools:write", False)
        manager.set_tool_permission("user1", "agent1", "search_arxiv", "tools:search", False)
        manager.cleanup_stale_tool_overrides("agent1", [])
        assert _override_rows(manager, "user1", "agent1") == ()

    def test_cleanup_stale_tool_overrides_preserves_other_agents(self, manager):
        """Cleanup is scoped to the given agent_id; other agents untouched."""
        manager.register_tool_scopes("agent2", {"some_tool": "tools:read"})
        manager.set_tool_permission("user1", "agent1", "modify_data", "tools:write", False)
        manager.set_tool_permission("user1", "agent2", "some_tool", "tools:read", False)
        manager.cleanup_stale_tool_overrides("agent1", [])
        agent1_rows = _override_rows(manager, "user1", "agent1")
        agent2_rows = _override_rows(manager, "user1", "agent2")
        assert agent1_rows == ()
        assert {row.tool_name for row in agent2_rows} == {"some_tool"}

    def test_cleanup_stale_tool_overrides_idempotent(self, manager):
        """Running cleanup twice yields zero deletions on the second call."""
        manager.set_tool_permission("user1", "agent1", "removed_tool", "tools:read", False)
        live_tools = ["modify_data"]
        first = manager.cleanup_stale_tool_overrides("agent1", live_tools)
        second = manager.cleanup_stale_tool_overrides("agent1", live_tools)
        assert first >= 1
        assert second == 0


class TestGetAllAgentPermissions:
    def test_returns_all_agents(self, manager):
        manager.set_agent_scopes("user1", "agent1", {"tools:read": True})
        manager.set_agent_scopes("user1", "agent2", {"tools:write": True, "tools:search": True})
        result = manager.get_all_agent_permissions("user1")
        assert "agent1" in result
        assert "agent2" in result
        assert result["agent1"]["tools:read"] is True
        assert result["agent1"]["tools:write"] is False
        assert result["agent2"]["tools:write"] is True
        assert result["agent2"]["tools:search"] is True
