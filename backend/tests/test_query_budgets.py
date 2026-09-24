"""Tests for backend/orchestrator/history.py's get_recent_chats and
tool_permissions.py's get_effective_tool_permissions: each resolves in a single Plane
round trip, with previews, ordering and merged-scope results unchanged.
"""

import os
import sys
import uuid
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.history import PREVIEW_MAX_CHARS
from orchestrator.tool_permissions import ToolPermissionManager
from tests.helpers.query_count import QueryCounter, count_queries
from tests.helpers.voice_plane_runtime import (
    history_manager,
    isolated_plane_runtime,
)


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("query_budgets") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def hm(plane_runtime):
    return history_manager(plane_runtime)


@contextmanager
def _count_plane_queries(plane_runtime):
    counter = QueryCounter()
    original_transaction = plane_runtime.transaction

    @contextmanager
    def counted_transaction(*, isolation=None):
        with original_transaction(isolation=isolation) as transaction:
            with count_queries(transaction) as transaction_counter:
                try:
                    yield transaction
                finally:
                    counter.count += transaction_counter.count
                    counter.queries.extend(transaction_counter.queries)

    plane_runtime.transaction = counted_transaction
    try:
        yield counter
    finally:
        plane_runtime.transaction = original_transaction


@pytest.fixture
def user_id(plane_runtime):
    uid = f"qbudget-{uuid.uuid4().hex[:12]}"
    yield uid
    plane_runtime.execute("DELETE FROM saved_components WHERE user_id = ?", (uid,))
    plane_runtime.execute("DELETE FROM messages WHERE user_id = ?", (uid,))
    plane_runtime.execute("DELETE FROM chats WHERE user_id = ?", (uid,))
    plane_runtime.execute("DELETE FROM tool_overrides WHERE user_id = ?", (uid,))
    plane_runtime.execute("DELETE FROM agent_scopes WHERE user_id = ?", (uid,))


def _seed_three_chats(hm, plane_runtime, user_id):
    c1 = hm.create_chat(user_id=user_id)
    hm.add_message(c1, "user", "first question", user_id=user_id)
    hm.add_message(c1, "assistant", "the answer to the first question", user_id=user_id)

    c2 = hm.create_chat(user_id=user_id)
    hm.add_message(
        c2,
        "assistant",
        [
            {"type": "text", "content": "Here are your results.", "variant": "markdown"},
            {"type": "table", "title": "Holdings", "rows": [["VTI", "60%"]]},
        ],
        user_id=user_id,
    )

    c3 = hm.create_chat(user_id=user_id)
    hm.add_message(c3, "user", "z" * (PREVIEW_MAX_CHARS * 2), user_id=user_id)

    plane_runtime.execute(
        "UPDATE messages SET timestamp = id WHERE user_id = ?", (user_id,)
    )
    for rank, chat_id in enumerate((c1, c2, c3), start=1):
        plane_runtime.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ? AND user_id = ?",
            (rank * 1000, chat_id, user_id),
        )
    return c1, c2, c3


def test_recent_chats_single_query(hm, plane_runtime, user_id):
    c1, c2, c3 = _seed_three_chats(hm, plane_runtime, user_id)

    with _count_plane_queries(plane_runtime) as counter:
        chats = hm.get_recent_chats(user_id=user_id)

    assert counter.count == 1
    assert [c["id"] for c in chats] == [c3, c2, c1]

    by_id = {c["id"]: c for c in chats}
    assert by_id[c1]["preview"] == "the answer to the first question"
    assert by_id[c2]["preview"] == "Here are your results. Holdings"
    assert by_id[c3]["preview"] == "z" * PREVIEW_MAX_CHARS + "..."
    for entry in chats:
        assert set(entry.keys()) == {
            "id", "title", "agent_id", "updated_at", "preview", "has_saved_components",
        }
        assert entry["has_saved_components"] is False


def test_recent_chats_saved_component_flag_still_one_query(
    hm, plane_runtime, user_id
):
    c1, c2, c3 = _seed_three_chats(hm, plane_runtime, user_id)
    hm.save_component(c2, {"type": "table", "rows": []}, "table", user_id=user_id)

    with _count_plane_queries(plane_runtime) as counter:
        chats = hm.get_recent_chats(user_id=user_id)

    assert counter.count == 1
    by_id = {c["id"]: c for c in chats}
    assert by_id[c2]["has_saved_components"] is True
    assert by_id[c1]["has_saved_components"] is False
    assert by_id[c3]["has_saved_components"] is False


@pytest.fixture
def perms(plane_runtime, user_id):
    manager = ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    agent_id = f"qbudget-agent-{uuid.uuid4().hex[:8]}"
    manager.register_tool_scopes(agent_id, {
        "gen_chart": "tools:read",
        "modify": "tools:write",
        "search_web": "tools:search",
        "both_tool": "tools:write",
        "sys_tool": "tools:system",
        "legacy_true_tool": "tools:read",
    })
    yield manager, agent_id
    plane_runtime.execute("DELETE FROM tool_overrides WHERE agent_id = ?", (agent_id,))
    plane_runtime.execute("DELETE FROM agent_scopes WHERE agent_id = ?", (agent_id,))


def test_effective_tool_permissions_merged_query_parity(
    perms, plane_runtime, user_id
):
    manager, agent_id = perms
    manager.set_agent_scopes(user_id, agent_id, {
        "tools:read": True,
        "tools:search": True,
        "tools:write": False,
        "tools:system": False,
    })
    manager.set_tool_permission(user_id, agent_id, "gen_chart", "tools:read", False)
    manager.set_tool_permission(user_id, agent_id, "modify", "tools:write", True)
    manager.set_tool_overrides(user_id, agent_id, {"search_web": False})
    manager.set_tool_overrides(user_id, agent_id, {"both_tool": False})
    manager.set_tool_permission(user_id, agent_id, "both_tool", "tools:write", True)
    plane_runtime.execute(
        """INSERT INTO tool_overrides
           (user_id, agent_id, tool_name, permission_kind, enabled, updated_at)
           VALUES (?, ?, ?, NULL, ?, ?)""",
        (user_id, agent_id, "legacy_true_tool", True, 0),
    )

    with _count_plane_queries(plane_runtime) as counter:
        result = manager.get_effective_tool_permissions(
            user_id, agent_id, safe_default=False)

    assert counter.count == 2, "one agent_scopes read + ONE merged tool_overrides read"
    assert result == {
        "gen_chart": {"tools:read": False},
        "modify": {"tools:write": True},
        "search_web": {"tools:search": False},
        "both_tool": {"tools:write": False},
        "sys_tool": {"tools:system": False},
        "legacy_true_tool": {"tools:read": True},
    }


def test_effective_tool_permissions_no_rows_scope_fallback(
    perms, plane_runtime, user_id
):
    manager, agent_id = perms
    manager.set_agent_scopes(user_id, agent_id, {"tools:read": True})

    with _count_plane_queries(plane_runtime) as counter:
        result = manager.get_effective_tool_permissions(
            user_id, agent_id, safe_default=False)

    assert counter.count == 2
    assert result["gen_chart"] == {"tools:read": True}
    assert result["legacy_true_tool"] == {"tools:read": True}
    assert result["modify"] == {"tools:write": False}
    assert result["search_web"] == {"tools:search": False}
    assert result["sys_tool"] == {"tools:system": False}


def test_effective_tool_permissions_safe_default_flips_absent_scopes(
    perms, plane_runtime, user_id
):
    manager, agent_id = perms
    manager.set_agent_scopes(user_id, agent_id, {"tools:read": False})

    with _count_plane_queries(plane_runtime) as counter:
        result = manager.get_effective_tool_permissions(
            user_id, agent_id, safe_default=True)

    assert counter.count == 2
    assert result["gen_chart"] == {"tools:read": False}
    assert result["legacy_true_tool"] == {"tools:read": False}
    assert result["modify"] == {"tools:write": True}
    assert result["search_web"] == {"tools:search": True}
    assert result["both_tool"] == {"tools:write": True}
    assert result["sys_tool"] == {"tools:system": True}
