"""Tests for orchestrator/tool_permissions.py's turn_permission_memo: repeated checks
inside one turn skip Plane reads, decisions propagate through asyncio tasks and
to_thread, and memos never leak across turns or concurrent contexts.
"""

import asyncio
from contextlib import contextmanager
import threading
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("psycopg2")

from orchestrator.tool_permissions import ToolPermissionManager, turn_permission_memo
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


@contextmanager
def count_repository_operations(manager):
    counter = SimpleNamespace(count=0)
    lock = threading.Lock()
    targets = (
        (manager._agents.repository, "get_agent_for_administration"),
        (manager._agents.repository, "get_ownership"),
        (manager._agents.repository, "get_trust"),
        (manager._policy.repository, "list_overrides"),
        (manager._policy.repository, "list_scopes"),
    )
    saved = []

    def wrap(original):
        def counted(*args, **kwargs):
            with lock:
                counter.count += 1
            return original(*args, **kwargs)

        return counted

    for repository, name in targets:
        original = getattr(repository, name)
        saved.append((repository, name, original))
        setattr(repository, name, wrap(original))
    try:
        yield counter
    finally:
        for repository, name, original in reversed(saved):
            setattr(repository, name, original)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("permission_memo") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def manager(plane_runtime):
    return ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture
def grant(manager):
    user_id = f"memo-{uuid.uuid4().hex[:12]}"
    agent_id = f"memo-agent-{uuid.uuid4().hex[:8]}"
    manager.register_tool_scopes(agent_id, {
        "lookup": "tools:read",
        "writer": "tools:write",
    })
    manager.set_agent_scopes(user_id, agent_id, {
        "tools:read": True,
        "tools:write": False,
    })
    yield user_id, agent_id
    manager.remove_agent_permissions(user_id, agent_id)


def test_memo_repeat_call_zero_repository_operations(manager, grant):
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert counter.count == 0


def test_memo_keys_are_per_tool(manager, grant):
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert manager.is_tool_allowed(user_id, agent_id, "writer") is False
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
            assert manager.is_tool_allowed(user_id, agent_id, "writer") is False
        assert counter.count == 0


def test_no_memo_no_behavior_change(manager, grant):
    user_id, agent_id = grant
    assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    with count_repository_operations(manager) as counter:
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    assert counter.count > 0


def test_revocation_visible_in_next_memo(manager, grant):
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    manager.set_agent_scopes(user_id, agent_id, {"tools:read": False})
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is False
    assert manager.is_tool_allowed(user_id, agent_id, "lookup") is False


async def test_concurrent_memo_contexts_are_isolated(manager, grant):
    user_id, agent_id = grant
    started = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def turn_a():
        with turn_permission_memo():
            seen["a_before"] = await asyncio.to_thread(
                manager.is_tool_allowed, user_id, agent_id, "lookup")
            started.set()
            await release.wait()
            seen["a_after"] = await asyncio.to_thread(
                manager.is_tool_allowed, user_id, agent_id, "lookup")

    task = asyncio.create_task(turn_a())
    await started.wait()
    await asyncio.to_thread(
        manager.set_agent_scopes, user_id, agent_id, {"tools:read": False}
    )
    with turn_permission_memo():
        seen["b"] = await asyncio.to_thread(
            manager.is_tool_allowed, user_id, agent_id, "lookup")
    release.set()
    await task
    assert seen == {"a_before": True, "a_after": True, "b": False}


async def test_memo_propagates_to_tasks_and_threads(manager, grant):
    user_id, agent_id = grant

    async def check_in_task():
        return manager.is_tool_allowed(user_id, agent_id, "lookup")

    with turn_permission_memo():
        assert await asyncio.to_thread(
            manager.is_tool_allowed, user_id, agent_id, "lookup") is True
        with count_repository_operations(manager) as counter:
            assert await asyncio.create_task(check_in_task()) is True
            assert await asyncio.to_thread(
                manager.is_tool_allowed, user_id, agent_id, "lookup"
            ) is True
        assert counter.count == 0


async def test_memo_write_back_from_thread(manager, grant):
    user_id, agent_id = grant
    with turn_permission_memo():
        assert await asyncio.to_thread(
            manager.is_tool_allowed, user_id, agent_id, "lookup"
        ) is True
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert counter.count == 0
