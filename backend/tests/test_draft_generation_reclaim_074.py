from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from astralplane.repositories import RepositoryValidationError

from orchestrator.draft_plane_store import PlaneDraftStore
from tests.helpers.draft_store_double import InMemoryDraftStore
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


@pytest.fixture(scope="module")
def postgres_runtime() -> Iterator[PlaneTestRuntime]:
    with isolated_plane_runtime("draft_claim_reclaim_074") as runtime:
        yield runtime


def _expire_claim(
    runtime: PlaneTestRuntime,
    *,
    draft_id: str,
    owner_id: str,
    expected_revision: int,
    claim_id: str,
) -> None:
    result = runtime.execute(
        """
        UPDATE draft_agents
        SET generation_claim_expires_at = clock_timestamp() - interval '1 second'
        WHERE id = ? AND user_id = ? AND state_revision = ?
          AND generation_claim_id = ?
        """,
        (draft_id, owner_id, expected_revision, claim_id),
    )
    assert result.rowcount == 1


def test_plane_adapter_reclaims_only_expired_exact_claim_and_fences_zombie(
    postgres_runtime: PlaneTestRuntime,
) -> None:
    runtime = postgres_runtime
    draft_id = str(uuid.uuid4())
    owner_id = f"reclaim-owner-{uuid.uuid4().hex}"
    claim_id = str(uuid.uuid4())
    successor_claim_id = str(uuid.uuid4())
    adapter = PlaneDraftStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    with runtime.transaction() as transaction:
        created = runtime.repositories.draft_agents.create_draft(
            transaction,
            draft_id=draft_id,
            owner_id=owner_id,
            agent_name="Expired claim recovery agent",
            agent_slug=f"expired-claim-{uuid.uuid4().hex}",
            description="DB-time generation reclaim proof",
            observed_at=1_720_000_000_000,
            draft_uuid=draft_id,
            target_agent_id=f"reclaim-agent-{uuid.uuid4().hex}",
        )
        claimed = runtime.repositories.draft_agents.claim_generation(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=created.state_revision,
            claim_id=claim_id,
            lease_seconds=300,
        )

    active_expiry = claimed.generation_claim_expires_at
    assert active_expiry is not None
    assert (
        adapter.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claimed.state_revision,
            claim_id=claim_id,
        )
        is None
    )
    with runtime.transaction() as transaction:
        still_active = runtime.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
        )
    assert still_active is not None
    assert still_active.state_revision == claimed.state_revision
    assert still_active.generation_claim_expires_at == active_expiry

    _expire_claim(
        runtime,
        draft_id=draft_id,
        owner_id=owner_id,
        expected_revision=claimed.state_revision,
        claim_id=claim_id,
    )
    with runtime.transaction() as transaction:
        expired = runtime.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
        )
    assert expired is not None
    expired_at = expired.generation_claim_expires_at
    assert expired_at is not None
    assert expired_at < active_expiry
    assert not adapter.append_generation_log(
        draft_id,
        "expired progress",
        owner_user_id=owner_id,
        expected_revision=claimed.state_revision,
        claim_id=claim_id,
    )
    assert (
        adapter.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=f"foreign-{owner_id}",
            expected_revision=claimed.state_revision,
            claim_id=claim_id,
        )
        is None
    )
    assert (
        adapter.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claimed.state_revision,
            claim_id=str(uuid.uuid4()),
        )
        is None
    )

    reclaimed = adapter.reclaim_expired_draft_generation(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_revision=claimed.state_revision,
        claim_id=claim_id,
        lease_seconds=240,
    )

    assert reclaimed is not None
    assert reclaimed["state_revision"] == claimed.state_revision + 1
    assert reclaimed["generation_claim_id"] == claim_id
    assert reclaimed["status"] == "generating"
    assert reclaimed["published_revision_id"] is None
    assert reclaimed["generation_claim_expires_at"] > expired_at
    assert (
        adapter.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=int(reclaimed["state_revision"]),
            claim_id=claim_id,
        )
        is None
    )

    assert (
        adapter.finish_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claimed.state_revision,
            claim_id=claim_id,
            status="generated",
        )
        is None
    )

    reclaimed_revision = int(reclaimed["state_revision"])
    _expire_claim(
        runtime,
        draft_id=draft_id,
        owner_id=owner_id,
        expected_revision=reclaimed_revision,
        claim_id=claim_id,
    )
    with runtime.transaction() as transaction:
        successor = runtime.repositories.draft_agents.claim_generation(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=reclaimed_revision,
            claim_id=successor_claim_id,
            lease_seconds=300,
        )
    assert successor.state_revision == reclaimed_revision + 1
    assert successor.generation_claim_id == successor_claim_id
    for expected_revision in (reclaimed_revision, successor.state_revision):
        assert (
            adapter.reclaim_expired_draft_generation(
                draft_id=draft_id,
                owner_user_id=owner_id,
                expected_revision=expected_revision,
                claim_id=claim_id,
            )
            is None
        )

    with pytest.raises(ValueError, match="supplied together"):
        adapter.append_generation_log(
            draft_id,
            "partial fence",
            owner_user_id=owner_id,
        )
    assert not adapter.append_generation_log(
        draft_id,
        "zombie progress",
        owner_user_id=owner_id,
        expected_revision=reclaimed_revision,
        claim_id=claim_id,
    )
    assert adapter.append_generation_log(
        draft_id,
        "successor progress",
        owner_user_id=owner_id,
        expected_revision=successor.state_revision,
        claim_id=successor_claim_id,
    )

    with runtime.transaction() as transaction:
        current = runtime.repositories.draft_agents.get_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
        )
    assert current is not None
    assert current.state_revision == successor.state_revision
    assert current.generation_claim_id == successor_claim_id
    log_entries = json.loads(current.generation_log or "[]")
    assert [entry["message"] for entry in log_entries] == ["successor progress"]
    assert type(log_entries[0]["timestamp"]) is int


def test_plane_adapter_recovers_only_the_exact_live_lost_ack_claim(
    postgres_runtime: PlaneTestRuntime,
) -> None:
    runtime = postgres_runtime
    owner_id = f"claim-ack-owner-{uuid.uuid4().hex}"
    draft_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    adapter = PlaneDraftStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    with runtime.transaction() as transaction:
        created = runtime.repositories.draft_agents.create_draft(
            transaction,
            draft_id=draft_id,
            owner_id=owner_id,
            agent_name="Lost acknowledgement agent",
            agent_slug=f"lost-ack-{uuid.uuid4().hex}",
            description="exact post-claim recovery proof",
            observed_at=1_720_000_000_000,
        )
        claimed = runtime.repositories.draft_agents.claim_generation(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=created.state_revision,
            claim_id=claim_id,
            lease_seconds=300,
        )

    recovered = adapter.get_exact_live_draft_generation_claim(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_preclaim_revision=created.state_revision,
        claim_id=claim_id,
    )
    assert recovered is not None
    assert recovered["state_revision"] == created.state_revision + 1
    assert recovered["generation_claim_id"] == claim_id
    assert recovered["status"] == "generating"
    assert recovered["published_revision_id"] is None

    for changes in (
        {"owner_user_id": f"foreign-{owner_id}"},
        {"claim_id": str(uuid.uuid4())},
        {"expected_preclaim_revision": created.state_revision + 1},
    ):
        kwargs: dict[str, object] = {
            "draft_id": draft_id,
            "owner_user_id": owner_id,
            "expected_preclaim_revision": created.state_revision,
            "claim_id": claim_id,
        }
        kwargs.update(changes)
        assert adapter.get_exact_live_draft_generation_claim(**kwargs) is None

    with runtime.transaction() as transaction:
        mutated = runtime.repositories.draft_agents.compare_and_set_draft(
            transaction,
            owner_id=owner_id,
            draft_id=draft_id,
            expected_revision=claimed.state_revision,
            updates={"description": "same-claim successor state"},
            updated_at=1_720_000_000_001,
        )
    assert mutated.generation_claim_id == claim_id
    assert mutated.state_revision == claimed.state_revision + 1
    assert (
        adapter.get_exact_live_draft_generation_claim(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=created.state_revision,
            claim_id=claim_id,
        )
        is None
    )

    expired_draft_id = str(uuid.uuid4())
    expired_claim_id = str(uuid.uuid4())
    with runtime.transaction() as transaction:
        expired_created = runtime.repositories.draft_agents.create_draft(
            transaction,
            draft_id=expired_draft_id,
            owner_id=owner_id,
            agent_name="Expired acknowledgement agent",
            agent_slug=f"expired-ack-{uuid.uuid4().hex}",
            description="DB-time expiry denial proof",
            observed_at=1_720_000_000_000,
        )
        expired_claimed = runtime.repositories.draft_agents.claim_generation(
            transaction,
            owner_id=owner_id,
            draft_id=expired_draft_id,
            expected_revision=expired_created.state_revision,
            claim_id=expired_claim_id,
            lease_seconds=300,
        )
    _expire_claim(
        runtime,
        draft_id=expired_draft_id,
        owner_id=owner_id,
        expected_revision=expired_claimed.state_revision,
        claim_id=expired_claim_id,
    )
    assert (
        adapter.get_exact_live_draft_generation_claim(
            draft_id=expired_draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=expired_created.state_revision,
            claim_id=expired_claim_id,
        )
        is None
    )
    cleanup = runtime.execute(
        "UPDATE draft_agents SET status = 'pending' WHERE id = ? AND user_id = ?",
        (expired_draft_id, owner_id),
    )
    assert cleanup.rowcount == 1


def test_in_memory_draft_double_matches_expired_reclaim_fences() -> None:
    store = InMemoryDraftStore()
    draft_id = str(uuid.uuid4())
    owner_id = f"double-owner-{uuid.uuid4().hex}"
    claim_id = str(uuid.uuid4())
    successor_claim_id = str(uuid.uuid4())
    store.create_draft_agent(
        draft_id=draft_id,
        user_id=owner_id,
        agent_name="Double reclaim agent",
        agent_slug=f"double-reclaim-{uuid.uuid4().hex}",
        description="test-double parity",
    )
    claimed = store.claim_draft_generation(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_revision=0,
        claim_id=claim_id,
    )
    assert claimed is not None
    claim_revision = int(claimed["state_revision"])

    assert (
        store.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claim_revision,
            claim_id=claim_id,
        )
        is None
    )
    assert store.update_draft_agent(draft_id, generation_claim_expires_at=0)
    assert not store.append_generation_log(
        draft_id,
        "expired progress",
        owner_user_id=owner_id,
        expected_revision=claim_revision,
        claim_id=claim_id,
    )
    assert (
        store.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=f"foreign-{owner_id}",
            expected_revision=claim_revision,
            claim_id=claim_id,
        )
        is None
    )
    assert (
        store.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claim_revision,
            claim_id=str(uuid.uuid4()),
        )
        is None
    )

    reclaimed = store.reclaim_expired_draft_generation(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_revision=claim_revision,
        claim_id=claim_id,
    )
    assert reclaimed is not None
    assert reclaimed["state_revision"] == claim_revision + 1
    assert (
        store.finish_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=claim_revision,
            claim_id=claim_id,
            status="generated",
        )
        is None
    )

    reclaimed_revision = int(reclaimed["state_revision"])
    assert store.update_draft_agent(draft_id, generation_claim_expires_at=0)
    successor = store.claim_draft_generation(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_revision=reclaimed_revision,
        claim_id=successor_claim_id,
    )
    assert successor is not None
    assert successor["generation_claim_id"] == successor_claim_id
    assert (
        store.reclaim_expired_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=int(successor["state_revision"]),
            claim_id=claim_id,
        )
        is None
    )
    with pytest.raises(ValueError, match="supplied together"):
        store.append_generation_log(
            draft_id,
            "partial fence",
            claim_id=successor_claim_id,
        )
    assert not store.append_generation_log(
        draft_id,
        "zombie progress",
        owner_user_id=owner_id,
        expected_revision=reclaimed_revision,
        claim_id=claim_id,
    )
    assert store.append_generation_log(
        draft_id,
        "successor progress",
        owner_user_id=owner_id,
        expected_revision=int(successor["state_revision"]),
        claim_id=successor_claim_id,
    )
    current = store.get_draft_agent(draft_id)
    assert current is not None
    assert [
        entry["message"] for entry in json.loads(str(current["generation_log"]))
    ] == ["successor progress"]


def test_in_memory_draft_double_matches_exact_lost_ack_claim_fence() -> None:
    store = InMemoryDraftStore()
    draft_id = str(uuid.uuid4())
    owner_id = f"double-ack-owner-{uuid.uuid4().hex}"
    claim_id = str(uuid.uuid4())
    store.create_draft_agent(
        draft_id=draft_id,
        user_id=owner_id,
        agent_name="Double lost acknowledgement agent",
        agent_slug=f"double-lost-ack-{uuid.uuid4().hex}",
        description="exact claim acknowledgement parity",
    )
    claimed = store.claim_draft_generation(
        draft_id=draft_id,
        owner_user_id=owner_id,
        expected_revision=0,
        claim_id=claim_id,
    )
    assert claimed is not None
    assert (
        store.get_exact_live_draft_generation_claim(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=0,
            claim_id=claim_id,
        )
        == claimed
    )
    assert (
        store.get_exact_live_draft_generation_claim(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=0,
            claim_id=str(uuid.uuid4()),
        )
        is None
    )

    assert store.update_draft_agent(
        draft_id,
        state_revision=int(claimed["state_revision"]) + 1,
    )
    successor = store.get_draft_agent(draft_id)
    assert successor is not None
    assert successor["generation_claim_id"] == claim_id
    assert (
        store.get_exact_live_draft_generation_claim(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=0,
            claim_id=claim_id,
        )
        is None
    )

    assert store.update_draft_agent(
        draft_id,
        state_revision=1,
        generation_claim_expires_at=0,
    )
    assert (
        store.get_exact_live_draft_generation_claim(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_preclaim_revision=0,
            claim_id=claim_id,
        )
        is None
    )


def test_plane_adapter_expired_inventory_is_db_timed_bounded_and_ordered(
    postgres_runtime: PlaneTestRuntime,
) -> None:
    runtime = postgres_runtime
    owner_id = f"inventory-owner-{uuid.uuid4().hex}"
    suffix = uuid.uuid4().hex
    draft_ids = {
        "oldest": f"inventory-0-{suffix}",
        "tie_a": f"inventory-1a-{suffix}",
        "tie_b": f"inventory-1b-{suffix}",
        "active": f"inventory-2-{suffix}",
        "wrong_status": f"inventory-3-{suffix}",
    }
    with runtime.transaction() as transaction:
        for label, draft_id in draft_ids.items():
            created = runtime.repositories.draft_agents.create_draft(
                transaction,
                draft_id=draft_id,
                owner_id=owner_id,
                agent_name=f"Inventory {label}",
                agent_slug=f"inventory-{label}-{suffix}",
                description="expired claim inventory proof",
                observed_at=1_720_000_000_000,
            )
            runtime.repositories.draft_agents.claim_generation(
                transaction,
                owner_id=owner_id,
                draft_id=draft_id,
                expected_revision=created.state_revision,
                claim_id=str(uuid.uuid4()),
                lease_seconds=300,
            )

    for label, timestamp in (
        ("oldest", "1999-01-01T00:00:00Z"),
        ("tie_a", "2000-01-01T00:00:00Z"),
        ("tie_b", "2000-01-01T00:00:00Z"),
    ):
        result = runtime.execute(
            """
            UPDATE draft_agents
            SET generation_claim_expires_at = ?::timestamptz
            WHERE id = ? AND user_id = ?
            """,
            (timestamp, draft_ids[label], owner_id),
        )
        assert result.rowcount == 1
    result = runtime.execute(
        """
        UPDATE draft_agents
        SET generation_claim_expires_at = '2001-01-01T00:00:00Z'::timestamptz,
            status = 'pending'
        WHERE id = ? AND user_id = ?
        """,
        (draft_ids["wrong_status"], owner_id),
    )
    assert result.rowcount == 1

    adapter = PlaneDraftStore(
        plane_runtime=runtime,
        plane_repositories=runtime.repositories,
    )
    bounded = adapter.list_expired_draft_generations_for_administration(limit=2)
    complete = adapter.list_expired_draft_generations_for_administration(limit=10)

    assert [row["id"] for row in bounded] == [
        draft_ids["oldest"],
        draft_ids["tie_a"],
    ]
    assert [row["id"] for row in complete] == [
        draft_ids["oldest"],
        draft_ids["tie_a"],
        draft_ids["tie_b"],
    ]
    assert draft_ids["active"] not in {row["id"] for row in complete}
    assert draft_ids["wrong_status"] not in {row["id"] for row in complete}
    cursor_expiry = bounded[-1]["generation_claim_expires_at"]
    assert cursor_expiry is not None
    second_page = adapter.list_expired_draft_generations_for_administration(
        limit=2,
        after_generation_claim_expires_at=cursor_expiry,
        after_draft_id=str(bounded[-1]["id"]),
    )
    assert [row["id"] for row in second_page] == [draft_ids["tie_b"]]
    assert len({row["id"] for row in [*bounded, *second_page]}) == 3
    assert [row["id"] for row in [*bounded, *second_page]] == [
        draft_ids["oldest"],
        draft_ids["tie_a"],
        draft_ids["tie_b"],
    ]
    assert (
        adapter.list_expired_draft_generations_for_administration(
            limit=2,
            after_generation_claim_expires_at=second_page[-1][
                "generation_claim_expires_at"
            ],
            after_draft_id=str(second_page[-1]["id"]),
        )
        == []
    )
    with pytest.raises(RepositoryValidationError, match="limit"):
        adapter.list_expired_draft_generations_for_administration(limit=1001)
    with pytest.raises(RepositoryValidationError, match="supplied together"):
        adapter.list_expired_draft_generations_for_administration(
            after_generation_claim_expires_at=cursor_expiry,
        )


def test_in_memory_expired_inventory_is_bounded_and_deterministic() -> None:
    store = InMemoryDraftStore()
    owner_id = "inventory-double-owner"
    expiries = {
        "inventory-double-c": 2,
        "inventory-double-a": 1,
        "inventory-double-b": 1,
    }
    for draft_id, expiry in expiries.items():
        store.create_draft_agent(
            draft_id=draft_id,
            user_id=owner_id,
            agent_name=draft_id,
            agent_slug=draft_id,
            description="inventory double",
        )
        claimed = store.claim_draft_generation(
            draft_id=draft_id,
            owner_user_id=owner_id,
            expected_revision=0,
            claim_id=str(uuid.uuid4()),
        )
        assert claimed is not None
        assert store.update_draft_agent(
            draft_id,
            generation_claim_expires_at=expiry,
        )
    store.create_draft_agent(
        draft_id="inventory-double-active",
        user_id=owner_id,
        agent_name="active",
        agent_slug="inventory-double-active",
        description="inventory double active exclusion",
    )
    active = store.claim_draft_generation(
        draft_id="inventory-double-active",
        owner_user_id=owner_id,
        expected_revision=0,
        claim_id=str(uuid.uuid4()),
    )
    assert active is not None

    assert [
        row["id"]
        for row in store.list_expired_draft_generations_for_administration(limit=2)
    ] == ["inventory-double-a", "inventory-double-b"]
    assert [
        row["id"]
        for row in store.list_expired_draft_generations_for_administration(limit=10)
    ] == ["inventory-double-a", "inventory-double-b", "inventory-double-c"]
    first_page = store.list_expired_draft_generations_for_administration(limit=2)
    second_page = store.list_expired_draft_generations_for_administration(
        limit=2,
        after_generation_claim_expires_at=int(
            first_page[-1]["generation_claim_expires_at"]
        ),
        after_draft_id=str(first_page[-1]["id"]),
    )
    assert [row["id"] for row in [*first_page, *second_page]] == [
        "inventory-double-a",
        "inventory-double-b",
        "inventory-double-c",
    ]
    assert len({row["id"] for row in [*first_page, *second_page]}) == 3
    for invalid_limit in (0, 1001, True):
        with pytest.raises(ValueError, match="limit"):
            store.list_expired_draft_generations_for_administration(limit=invalid_limit)
    for invalid_cursor in (
        {"after_generation_claim_expires_at": 1},
        {"after_draft_id": "inventory-double-a"},
        {
            "after_generation_claim_expires_at": True,
            "after_draft_id": "inventory-double-a",
        },
        {
            "after_generation_claim_expires_at": 1,
            "after_draft_id": "",
        },
    ):
        with pytest.raises(ValueError, match=r"cursor|integer|after_draft_id"):
            store.list_expired_draft_generations_for_administration(**invalid_cursor)
