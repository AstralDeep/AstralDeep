"""Feature-060 draft CAS, generation-claim, and tombstone races."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest

from orchestrator import agent_analyze
from orchestrator import agent_authoring as authoring
from orchestrator import user_agents
from orchestrator.agent_lifecycle import AgentLifecycleManager, BYO_ORIGIN
from tests.helpers.draft_store_double import InMemoryDraftStore
from tests.helpers.user_agent_registry import make_user_agent_registry


_OWNER = "authoring-concurrency-060"


@pytest.fixture()
def draft_store():
    return InMemoryDraftStore()


@pytest.fixture()
def user_agent_registry():
    return make_user_agent_registry()


async def test_one_hundred_same_name_drafts_have_distinct_durable_identities(
    draft_store,
):
    publication_service = MagicMock()
    manager = AgentLifecycleManager(
        draft_store=draft_store,
        generated_agent_publication_service=publication_service,
    )

    async def create_one(_index: int):
        return await manager.create_draft(
            user_id=_OWNER,
            agent_name="Same Name Agent",
            description="Performs one owner-scoped action without shared storage.",
        )

    drafts = await asyncio.gather(*(create_one(index) for index in range(100)))

    assert len({row["id"] for row in drafts}) == 100
    assert len({str(row["draft_uuid"]) for row in drafts}) == 100
    assert len({row["target_agent_id"] for row in drafts}) == 100
    assert len({row["agent_slug"] for row in drafts}) == 100
    assert all(uuid.UUID(str(row["draft_uuid"])).version == 4 for row in drafts)
    assert all(uuid.UUID(row["target_agent_id"]).version == 4 for row in drafts)
    assert all(
        row["agent_slug"].startswith("same_name_agent_") for row in drafts
    )
    publication_service.publish.assert_not_called()
    print(
        "US6 authoring identity profile: "
        "attempts=100 durable_drafts=100 distinct_targets=100 distinct_slugs=100"
    )


def _create_byo_draft(
    draft_store: InMemoryDraftStore,
    *,
    phase: str = "specify",
) -> dict:
    draft_id = str(uuid.uuid4())
    draft_store.create_draft_agent(
        draft_id=draft_id,
        user_id=_OWNER,
        agent_name="Concurrent Agent",
        agent_slug=f"concurrent_agent_{draft_id.replace('-', '')[:12]}",
        description="Reads owner-scoped data and returns a bounded result.",
        origin=BYO_ORIGIN,
    )
    if phase != "specify":
        draft_store.update_draft_agent(draft_id, phase=phase)
    return draft_store.get_draft_agent(draft_id)


def test_one_hundred_same_revision_writers_have_one_winner_and_fast_conflicts(
    draft_store,
):
    row = _create_byo_draft(draft_store)
    start = threading.Event()

    def write(index: int):
        start.wait(timeout=5)
        began = time.monotonic()
        result = authoring.cas_draft_update(
            draft_store,
            draft_id=row["id"],
            owner_user_id=_OWNER,
            expected_revision=0,
            updates={"description": f"accepted candidate {index:03d}"},
            transition_kind="save",
        )
        return result, time.monotonic() - began

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(write, index) for index in range(100)]
        start.set()
        results = [future.result(timeout=10) for future in futures]

    assert sum(result.outcome == "applied" for result, _ in results) == 1
    assert sum(result.outcome == "conflict" for result, _ in results) == 99
    assert {result.current_revision for result, _ in results} == {1}
    assert {result.refresh_action for result, _ in results} == {"refresh"}
    maximum_conflict_seconds = max(duration for _, duration in results)
    assert maximum_conflict_seconds < 1.0
    stored = draft_store.get_draft_agent(row["id"])
    assert stored["state_revision"] == 1
    assert stored["description"].startswith("accepted candidate ")
    print(
        "US6 authoring CAS profile: "
        "attempts=100 applied=1 conflicts=99 "
        f"max_response_ms={maximum_conflict_seconds * 1_000:.3f}"
    )


def test_one_hundred_generation_claimants_select_exactly_one(draft_store):
    row = _create_byo_draft(draft_store)
    start = threading.Event()

    def claim(_index: int):
        start.wait(timeout=5)
        return draft_store.claim_draft_generation(
            draft_id=row["id"],
            owner_user_id=_OWNER,
            expected_revision=0,
            claim_id=str(uuid.uuid4()),
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(claim, index) for index in range(100)]
        start.set()
        results = [future.result(timeout=10) for future in futures]

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["state_revision"] == 1
    assert winners[0]["status"] == "generating"
    current = draft_store.get_draft_agent(row["id"])
    assert str(current["generation_claim_id"]) == str(
        winners[0]["generation_claim_id"]
    )
    print(
        "US6 generation-claim profile: "
        "attempts=100 winners=1 fenced_losers=99"
    )


async def test_late_analyze_result_cannot_overwrite_a_concurrent_edit(
    draft_store,
    user_agent_registry,
    monkeypatch,
):
    row = await asyncio.to_thread(
        _create_byo_draft,
        draft_store,
        phase="analyze",
    )
    current_revision = int(row["state_revision"])
    started = threading.Event()
    release = threading.Event()
    real_check = agent_analyze.check

    def blocked_check(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(agent_analyze, "check", blocked_check)

    class _Orchestrator:
        pass

    orch = _Orchestrator()
    orch.lifecycle_manager = AgentLifecycleManager(draft_store=draft_store)
    orch.user_agent_registry = user_agent_registry

    analyzing = asyncio.create_task(
        asyncio.to_thread(
            authoring.run_analyze,
            orch,
            _OWNER,
            row["id"],
            expected_revision=current_revision,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    edit = await asyncio.to_thread(
        authoring.cas_draft_update,
        draft_store,
        draft_id=row["id"],
        owner_user_id=_OWNER,
        expected_revision=current_revision,
        updates={"description": "The owner changed this while Analyze was running."},
        transition_kind="save",
    )
    assert edit.applied
    release.set()
    result = await analyzing

    assert result == {
        "status": "conflict",
        "current_revision": edit.current_revision,
        "refresh": "refresh",
    }
    stored = await asyncio.to_thread(draft_store.get_draft_agent, row["id"])
    assert stored["description"] == (
        "The owner changed this while Analyze was running."
    )
    assert stored["analyze_result"] is None


def test_one_hundred_delete_register_interleavings_never_resurrect(
    user_agent_registry,
):
    agent_ids = [str(uuid.uuid4()) for _ in range(100)]
    for agent_id in agent_ids:
        user_agents.create_user_agent(
            user_agent_registry,
            agent_id=agent_id,
            owner_user_id=_OWNER,
            display_name="Delete Race",
        )

    def register(agent_id: str):
        try:
            user_agents.create_user_agent(
                user_agent_registry,
                agent_id=agent_id,
                owner_user_id=_OWNER,
                display_name="Delayed Registration",
            )
            return "updated"
        except user_agents.AgentDeletedError:
            return "deleted"

    with ThreadPoolExecutor(max_workers=24) as executor:
        registrations = [executor.submit(register, agent_id) for agent_id in agent_ids]
        deletions = [
            executor.submit(
                user_agents.soft_delete,
                user_agent_registry,
                agent_id,
            )
            for agent_id in agent_ids
        ]
        outcomes = [future.result(timeout=10) for future in registrations]
        for future in deletions:
            future.result(timeout=10)

    assert set(outcomes) <= {"updated", "deleted"}
    for agent_id in agent_ids:
        stored = user_agents.get_user_agent(user_agent_registry, agent_id)
        assert stored["status"] == "disabled"
        assert stored["deleted_at"] is not None
        allowed, reason = user_agents.authorize_registration(
            user_agent_registry,
            _OWNER,
            agent_id,
        )
        assert not allowed
        assert "deleted" in reason.lower() or "disabled" in reason.lower()
    print(
        "US6 delete/register profile: "
        "interleavings=100 tombstones=100 resurrected=0"
    )
