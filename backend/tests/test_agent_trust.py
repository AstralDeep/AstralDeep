"""Tests for orchestrator/agent_trust.py and tool_permissions.py: the safe-agent
deny→allow baseline flip, explicit opt-outs, admin-gated mark_safe,
reset-on-revision, and delegation-token scope mirroring at dispatch.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytest.importorskip("psycopg2")

from orchestrator.user_agents import UserAgentRegistry  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("agent_trust") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def db(plane_runtime):
    return UserAgentRegistry(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture
def pm(db, plane_runtime):
    from orchestrator.tool_permissions import ToolPermissionManager

    return ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        user_agent_registry=db,
    )


def _fresh_ids():
    suffix = uuid.uuid4().hex[:10]
    return f"pytest-user-{suffix}", f"pytest-safe-agent-{suffix}"


def test_safe_agent_allows_fresh_user(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is True


def test_non_safe_agent_defaults_deny(db, pm):
    user_id, agent_id = _fresh_ids()
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is False


def test_explicit_optout_wins_over_safe(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    pm.set_agent_scopes(user_id, agent_id, {"tools:read": False})
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is False


def test_safe_public_agent_allows_fresh_user(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is True
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is True


def test_safe_private_agent_denies_fresh_user(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=False)
    assert pm.is_tool_allowed(user_id, agent_id, "some_tool") is False


def test_safe_agent_scope_names_for_fresh_user(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    pm.register_tool_scopes(
        agent_id, {"web_search": "tools:search", "fetch_page": "tools:read"})
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is True
    assert pm.get_enabled_scope_names(user_id, agent_id) == [
        "tools:read", "tools:search"]


def test_safe_agent_scope_names_respect_explicit_optout(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    pm.register_tool_scopes(
        agent_id, {"web_search": "tools:search", "fetch_page": "tools:read"})
    pm.set_agent_scopes(user_id, agent_id, {"tools:search": False})
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is False
    assert pm.get_enabled_scope_names(user_id, agent_id) == ["tools:read"]


def test_safe_agent_scope_names_respect_per_tool_optout(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    pm.register_tool_scopes(
        agent_id, {"post_data": "tools:write", "fetch_page": "tools:read"})
    pm.set_skill_enabled(user_id, agent_id, "post_data", False)
    assert pm.is_tool_allowed(user_id, agent_id, "post_data") is False
    assert pm.get_enabled_scope_names(user_id, agent_id) == ["tools:read"]


def test_per_tool_grant_without_scope_row_yields_scope(db, pm):
    user_id, agent_id = _fresh_ids()
    pm.register_tool_scopes(
        agent_id, {"web_search": "tools:search", "post_data": "tools:write"})
    pm.set_skill_enabled(user_id, agent_id, "web_search", True)
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is True
    assert pm.get_enabled_scope_names(user_id, agent_id) == ["tools:search"]


def test_non_safe_agent_scope_names_stay_empty(db, pm):
    user_id, agent_id = _fresh_ids()
    pm.register_tool_scopes(agent_id, {"web_search": "tools:search"})
    assert pm.get_enabled_scope_names(user_id, agent_id) == []


def test_safe_private_agent_scope_names_stay_empty(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=False)
    pm.register_tool_scopes(agent_id, {"web_search": "tools:search"})
    assert pm.get_enabled_scope_names(user_id, agent_id) == []


def test_safe_agent_scope_names_default_read_without_tool_map(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    assert pm.get_enabled_scope_names(user_id, agent_id) == ["tools:read"]


def test_unknown_declared_scope_denied_and_unminted(db, pm):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    pm.register_tool_scopes(agent_id, {"weird_tool": "tools:bogus"})
    assert pm.is_tool_allowed(user_id, agent_id, "weird_tool") is False
    assert pm.get_enabled_scope_names(user_id, agent_id) == []


def test_explicit_enabled_scope_names_unchanged(db, pm):
    user_id, agent_id = _fresh_ids()
    pm.set_agent_scopes(
        user_id, agent_id, {"tools:read": True, "tools:write": True})
    assert pm.get_enabled_scope_names(user_id, agent_id) == [
        "tools:read", "tools:write"]


@pytest.mark.asyncio
async def test_mark_safe_requires_admin(db):
    from orchestrator import agent_trust

    _, agent_id = _fresh_ids()
    denied = await agent_trust.mark_safe(db, agent_id, True, "alice", roles=[])
    assert denied["ok"] is False and denied["error"] == "forbidden"
    assert await asyncio.to_thread(db.get_agent_is_safe, agent_id) is False

    ok = await agent_trust.mark_safe(db, agent_id, True, "admin-user", roles=["admin"])
    assert ok["ok"] is True and ok["is_safe"] is True
    assert await asyncio.to_thread(db.get_agent_is_safe, agent_id) is True


@pytest.mark.asyncio
async def test_reset_on_revision_clears_marker(db):
    from orchestrator import agent_trust

    _, agent_id = _fresh_ids()
    await agent_trust.mark_safe(db, agent_id, True, "admin-user", roles=["admin"])
    assert await asyncio.to_thread(db.get_agent_is_safe, agent_id) is True

    res = await agent_trust.reset_on_revision(db, agent_id, actor_user="reviser")
    assert res["reset"] is True
    assert await asyncio.to_thread(db.get_agent_is_safe, agent_id) is False


@pytest.mark.asyncio
async def test_seed_safe_idempotent(db):
    from orchestrator import agent_trust

    _, agent_id = _fresh_ids()
    first = await agent_trust.seed_safe(db, [agent_id])
    assert agent_id in first
    second = await agent_trust.seed_safe(db, [agent_id])
    assert agent_id not in second


def _kind_rows(pm, user_id, agent_id):
    return {
        (row.tool_name, row.permission_kind, bool(row.enabled))
        for row in pm._policy.call(
            pm._policy.repository.list_overrides,
            owner_id=user_id,
            agent_id=agent_id,
        )
        if row.permission_kind is not None
    }


def _safe_public(db, pm, tool_map):
    user_id, agent_id = _fresh_ids()
    db.upsert_agent_safe(agent_id, True, marked_by="pytest")
    db.set_agent_ownership(agent_id, "o@e.com", is_public=True)
    pm.register_tool_scopes(agent_id, tool_map)
    return user_id, agent_id


def test_backfill_writes_nothing_when_user_has_no_scope_rows(db, pm):
    user_id, agent_id = _safe_public(
        db, pm, {"web_search": "tools:search", "fetch_page": "tools:read"})
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is True

    assert pm.backfill_per_tool_rows(user_id, agent_id) == 0
    assert _kind_rows(pm, user_id, agent_id) == set()
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is True
    assert pm.get_enabled_scope_names(user_id, agent_id) == [
        "tools:read", "tools:search"]


def test_backfill_carries_forward_explicit_scope_rows(db, pm):
    user_id, agent_id = _safe_public(
        db, pm, {"web_search": "tools:search", "fetch_page": "tools:read"})
    pm.set_agent_scopes(
        user_id, agent_id, {"tools:read": True, "tools:search": False})

    assert pm.backfill_per_tool_rows(user_id, agent_id) == 2
    assert _kind_rows(pm, user_id, agent_id) == {
        ("fetch_page", "tools:read", True),
        ("web_search", "tools:search", False),
    }
    assert pm.is_tool_allowed(user_id, agent_id, "fetch_page") is True
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is False
    assert pm.backfill_per_tool_rows(user_id, agent_id) == 0


def test_backfill_skips_only_the_scopes_with_no_row(db, pm):
    user_id, agent_id = _safe_public(
        db, pm, {"web_search": "tools:search", "fetch_page": "tools:read"})
    pm.set_agent_scopes(user_id, agent_id, {"tools:read": True})

    assert pm.backfill_per_tool_rows(user_id, agent_id) == 1
    assert _kind_rows(pm, user_id, agent_id) == {
        ("fetch_page", "tools:read", True)}
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is True


def test_backfill_still_leaves_non_safe_agent_denied(db, pm):
    user_id, agent_id = _fresh_ids()
    pm.register_tool_scopes(agent_id, {"web_search": "tools:search"})

    assert pm.backfill_per_tool_rows(user_id, agent_id) == 0
    assert _kind_rows(pm, user_id, agent_id) == set()
    assert pm.is_tool_allowed(user_id, agent_id, "web_search") is False
