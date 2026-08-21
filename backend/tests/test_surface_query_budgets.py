"""DB round-trip budgets for the agents chrome surface (feature 052, T016/T017).

Renders the agents list and detail views through the surface's real
``render()`` against the live test Postgres (same posture as
test_query_budgets.py) with a minimal orchestrator stub, and proves with the
count_queries helper that the list view stays within 2 round trips and the
detail view within 3 — while still containing the expected agent content.
"""
import asyncio
import os
import sys
import time
import uuid
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestrator.plane_repository_context import ApplicationPlaneSource
from orchestrator.tool_permissions import ToolPermissionManager
from orchestrator.projection_surfaces import agents as surface
from tests.helpers.query_count import QueryCounter, count_queries
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

OWNER_EMAIL = "owner@example.com"


class StubSkill:
    """Card skill shape the surface reads (id/name/description/scope)."""

    def __init__(self, sid, description, scope):
        self.id = sid
        self.name = sid
        self.description = description
        self.scope = scope


class StubCard:
    """Agent-card shape the surface reads (name/description/skills/metadata)."""

    def __init__(self, agent_id, name, description, skills=None, metadata=None):
        self.agent_id = agent_id
        self.name = name
        self.description = description
        self.skills = skills or []
        self.metadata = metadata or {}


class StubOrch:
    """Minimal orchestrator bound to the application Plane source."""

    def __init__(self, plane_runtime, perms, cards):
        self.plane_repository_source = ApplicationPlaneSource(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
        )
        self.tool_permissions = perms
        self.agent_cards = cards

    def _is_draft_agent(self, agent_id):
        return False


@pytest.fixture(scope="module")
def plane_runtime():
    """One managed application Plane runtime for this integration module."""
    with isolated_plane_runtime("surface_query_budgets") as runtime:
        yield runtime


@contextmanager
def _count_plane_queries(plane_runtime):
    """Count real Plane transaction statements without a legacy DB facade."""
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
def env(plane_runtime):
    """A seeded user + two agents (owned/private and foreign/public)."""
    repositories = plane_runtime.repositories
    uid = f"sbudget-{uuid.uuid4().hex[:12]}"
    agent_a = f"sbudget-alpha-{uuid.uuid4().hex[:8]}"
    agent_b = f"sbudget-beta-{uuid.uuid4().hex[:8]}"

    now = int(time.time() * 1000)
    with plane_runtime.transaction() as transaction:
        repositories.identity.upsert_identity(
            transaction,
            owner_id=uid,
            observed_at=now,
            email=OWNER_EMAIL,
        )
        repositories.agents.upsert_ownership(
            transaction,
            agent_id=agent_a,
            owner_email=OWNER_EMAIL,
            is_public=False,
            observed_at=now,
        )
        repositories.agents.upsert_ownership(
            transaction,
            agent_id=agent_b,
            owner_email="someone-else@example.com",
            is_public=True,
            observed_at=now,
        )
        repositories.tool_policy_state.set_agent_disabled(
            transaction,
            owner_id=uid,
            agent_id=agent_b,
            disabled=True,
            updated_at=now,
        )
        # A different principal's grant for the same agent must never enter the
        # owner-scoped detail snapshot or influence the permission picker.
        repositories.tool_policy_state.set_tool_override(
            transaction,
            owner_id=f"{uid}-foreign",
            agent_id=agent_a,
            tool_name="write_data",
            permission_kind="tools:write",
            enabled=True,
            updated_at=now,
        )
        repositories.agents.set_trust(
            transaction,
            agent_id=agent_a,
            is_safe=True,
            marked_by="test-seed",
        )
    plane_runtime.execute(
        "INSERT INTO user_credentials "
        "(user_id, agent_id, credential_key, encrypted_value, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uid, agent_a, "API_KEY", "enc", now, now),
    )

    perms = ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=repositories,
    )
    perms.register_tool_scopes(agent_a, {
        "get_data": "tools:read",
        "write_data": "tools:write",
    })
    perms.set_agent_scopes(uid, agent_a, {"tools:read": True, "tools:write": False})
    perms.set_tool_permission(uid, agent_a, "get_data", "tools:read", True)

    cards = {
        agent_a: StubCard(
            agent_a, "Alpha Analysis", "Reads and writes analysis data.",
            skills=[StubSkill("get_data", "Fetch records", "tools:read"),
                    StubSkill("write_data", "Modify records", "tools:write")],
            metadata={"required_credentials": ["API_KEY"]},
        ),
        agent_b: StubCard(agent_b, "Beta Helper", "A public helper agent."),
    }
    orch = StubOrch(plane_runtime, perms, cards)
    yield orch, uid, agent_a, agent_b

    plane_runtime.execute(
        "DELETE FROM tool_overrides WHERE agent_id IN (?, ?)", (agent_a, agent_b)
    )
    plane_runtime.execute(
        "DELETE FROM agent_scopes WHERE agent_id IN (?, ?)", (agent_a, agent_b)
    )
    plane_runtime.execute("DELETE FROM user_credentials WHERE user_id = ?", (uid,))
    plane_runtime.execute(
        "DELETE FROM agent_trust WHERE agent_id IN (?, ?)", (agent_a, agent_b)
    )
    plane_runtime.execute(
        "DELETE FROM agent_ownership WHERE agent_id IN (?, ?)", (agent_a, agent_b)
    )
    plane_runtime.execute("DELETE FROM user_preferences WHERE user_id = ?", (uid,))
    plane_runtime.execute("DELETE FROM users WHERE id = ?", (uid,))


def test_agents_list_max_2(env, plane_runtime):
    """The list view renders each tab in at most 2 DB round trips."""
    orch, uid, agent_a, agent_b = env

    with _count_plane_queries(plane_runtime) as counter:
        html = asyncio.run(surface.render(orch, uid, ["user"], {"tab": "mine"}))
    assert counter.count <= 2, counter.queries
    assert "Alpha Analysis" in html
    assert "Beta Helper" not in html
    assert "Yours" in html

    with _count_plane_queries(plane_runtime) as counter:
        html = asyncio.run(surface.render(orch, uid, ["user"], {"tab": "public"}))
    assert counter.count <= 2, counter.queries
    assert "Beta Helper" in html
    assert "Disabled by you" in html


def test_agent_detail_max_3(env, plane_runtime):
    """The detail view renders in at most 3 DB round trips with full content."""
    orch, uid, agent_a, agent_b = env

    with _count_plane_queries(plane_runtime) as counter:
        html = asyncio.run(surface.render(orch, uid, ["user"], {"agent_id": agent_a}))

    assert counter.count <= 3, counter.queries
    assert "Alpha Analysis" in html
    assert 'name="get_data::tools:read" checked' in html
    assert 'name="__scope::tools:read" checked' in html
    assert 'name="__scope::tools:write" checked' not in html
    assert 'name="write_data::tools:write" checked' not in html
    assert 'data-ui-action="chrome_visibility_set"' in html
    assert 'data-ui-action="chrome_safe_set"' in html
    assert ">Unmark safe<" in html
    assert "API_KEY" in html and ">Stored<" in html


def test_agent_detail_non_owner_hides_owner_sections(env, plane_runtime):
    """A foreign public agent renders without owner controls, same budget."""
    orch, uid, agent_a, agent_b = env

    with _count_plane_queries(plane_runtime) as counter:
        html = asyncio.run(surface.render(orch, uid, ["user"], {"agent_id": agent_b}))

    assert counter.count <= 3, counter.queries
    assert "Beta Helper" in html
    assert 'data-ui-action="chrome_visibility_set"' not in html
    assert 'data-ui-action="chrome_safe_set"' not in html
    assert ">Enable<" in html
