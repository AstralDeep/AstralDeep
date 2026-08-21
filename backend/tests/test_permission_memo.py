"""Per-turn permission memo tests (feature 052, FR-019).

``turn_permission_memo()`` memoizes ``is_tool_allowed`` decisions keyed
``(user_id, agent_id, tool_name, kind)`` for the duration of one chat turn.
Verified here:

* a repeated identical check inside an active memo issues ZERO extra Plane
  repository operations;
* the memo propagates through ``asyncio`` tasks and ``asyncio.to_thread``
  (contextvars), including write-back from the thread to the parent context;
* decisions never cross two separate memo contexts — a revocation is visible
  in the next turn's memo even while a prior turn's memo is still open;
* with no memo active, behavior is exactly the per-call resolution.

Runs against an isolated current AstralPlane PostgreSQL composition.
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
    """Count typed reads used by one permission decision.

    Each wrapped repository method is a stable Plane query boundary. Counting
    here keeps this product-policy test independent of PostgreSQL driver
    internals and prevents it from borrowing a connection.
    """

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
    """A ToolPermissionManager backed by one isolated Plane runtime."""
    return ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture
def grant(manager):
    """A unique (user, agent) with tools:read granted and tools:write denied.

    Explicit scope rows for both kinds keep every resolution on the
    deterministic scope path (no safe-agent lookups). Rows are removed on
    teardown.
    """
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
    """Inside a memo, a repeated check performs no Plane repository reads."""
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert counter.count == 0


def test_memo_keys_are_per_tool(manager, grant):
    """Distinct tools resolve independently, then both replay query-free."""
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert manager.is_tool_allowed(user_id, agent_id, "writer") is False
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
            assert manager.is_tool_allowed(user_id, agent_id, "writer") is False
        assert counter.count == 0


def test_no_memo_no_behavior_change(manager, grant):
    """Without an active memo every call resolves against Plane."""
    user_id, agent_id = grant
    assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    with count_repository_operations(manager) as counter:
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    assert counter.count > 0


def test_revocation_visible_in_next_memo(manager, grant):
    """Decisions never cross memo contexts; the next turn re-reads Plane."""
    user_id, agent_id = grant
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
    manager.set_agent_scopes(user_id, agent_id, {"tools:read": False})
    with turn_permission_memo():
        assert manager.is_tool_allowed(user_id, agent_id, "lookup") is False
    assert manager.is_tool_allowed(user_id, agent_id, "lookup") is False


async def test_concurrent_memo_contexts_are_isolated(manager, grant):
    """An open turn keeps its memoized decision; a new turn sees the revocation.

    Every resolving check runs via ``asyncio.to_thread`` (as production code
    must) so the loop guard stays clean; the memo still propagates into and
    back out of each worker thread.
    """
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
    """A warmed decision replays repository-free in tasks and worker threads.

    The warming check runs off-loop (loop-guard clean); the in-task replay is
    memo-only, so it never enters Plane from the loop thread.
    """
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
    """A decision resolved inside to_thread populates the turn's shared memo."""
    user_id, agent_id = grant
    with turn_permission_memo():
        assert await asyncio.to_thread(
            manager.is_tool_allowed, user_id, agent_id, "lookup"
        ) is True
        with count_repository_operations(manager) as counter:
            assert manager.is_tool_allowed(user_id, agent_id, "lookup") is True
        assert counter.count == 0
