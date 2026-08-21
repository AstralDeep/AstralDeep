"""Feature-060 durable maintenance claims and atomic output recovery."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
import uuid
from unittest.mock import AsyncMock

import pytest

from orchestrator.knowledge_synthesis import (
    KnowledgeSynthesizer,
    MaintenanceOutputPublisher,
    MaintenanceUnitRepository,
)
from orchestrator.work_admission import WorkAdmissionCoordinator
from tests.helpers.voice_plane_runtime import PlaneTestRuntime, isolated_plane_runtime


_AGENT_PREFIX = "maintenance-060-"


@pytest.fixture()
def db():
    with isolated_plane_runtime("maintenance_claims") as runtime:
        yield runtime


def _seed(db: PlaneTestRuntime, agent_id: str, count: int = 2) -> list[dict]:
    for index in range(count):
        db.execute(
            "INSERT INTO interaction_log ("
            "agent_id, tool_name, success, error_message, response_time_ms, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                agent_id,
                f"tool_{index}",
                index % 2 == 0,
                None if index % 2 == 0 else "bounded failure",
                10 + index,
                index + 1,
            ),
        )
    rows = db.fetch_all(
        "SELECT * FROM interaction_log WHERE agent_id = ? ORDER BY id",
        (agent_id,),
    )
    return [dict(row) for row in rows]


def _render(claim, marker: str = "content") -> str:
    return (
        "---\n"
        f'maintenance_generation: "{claim.output_generation}"\n'
        "---\n\n"
        f"# {marker}\n"
    )


def _plane_dependencies(db: PlaneTestRuntime):
    coordinator = WorkAdmissionCoordinator.from_plane(
        plane_runtime=db,
        plane_repositories=db.repositories,
        slot_lease=timedelta(seconds=30),
    )
    return coordinator, db, db.repositories


def _maintenance_repository(db: PlaneTestRuntime) -> MaintenanceUnitRepository:
    coordinator, runtime, catalog = _plane_dependencies(db)
    return MaintenanceUnitRepository(
        coordinator=coordinator,
        lease_seconds=30,
        plane_runtime=runtime,
        plane_repositories=catalog,
    )


def _publish_and_complete(repo, publisher, claim, relative_path=None):
    path = relative_path or f"test/{claim.unit_id}.md"
    digest = publisher.publish(
        path, _render(claim, claim.unit_kind), claim.output_generation
    )
    repo.complete(claim, output_relative_path=path, output_digest=digest)
    return digest


def test_partial_failure_completes_only_successful_agent_inputs(db, tmp_path):
    agent_ok = f"{_AGENT_PREFIX}ok"
    agent_failed = f"{_AGENT_PREFIX}failed"
    interactions = _seed(db, agent_ok) + _seed(db, agent_failed)
    repo = _maintenance_repository(db)
    publisher = MaintenanceOutputPublisher(tmp_path)
    unit_ids = repo.ensure_synthesis_units(interactions)
    assert len(unit_ids) == 5

    seen = set()
    while len(seen) < 5:
        claim = repo.claim_next(
            "worker-partial", eligible_unit_ids=unit_ids
        )
        assert claim is not None
        seen.add(claim.unit_id)
        if claim.unit_kind == "agent_synthesis" and claim.scope_key == agent_failed:
            repo.fail(claim, error_code="synthetic_failure", retry_after_seconds=60)
        else:
            _publish_and_complete(repo, publisher, claim)

    ok_rows = db.fetch_all(
        "SELECT synthesized FROM interaction_log WHERE agent_id = ?", (agent_ok,)
    )
    failed_rows = db.fetch_all(
        "SELECT synthesized FROM interaction_log WHERE agent_id = ?", (agent_failed,)
    )
    assert all(row["synthesized"] for row in ok_rows)
    assert all(not row["synthesized"] for row in failed_rows)
    failed_unit = db.fetch_one(
        """
        SELECT * FROM maintenance_unit
        WHERE unit_kind = 'agent_synthesis' AND scope_key = ?
        """,
        (agent_failed,),
    )
    assert failed_unit["state"] == "failed_retryable"
    memberships = db.fetch_all(
        "SELECT state FROM maintenance_unit_input WHERE unit_id = ?",
        (str(failed_unit["unit_id"]),),
    )
    assert {row["state"] for row in memberships} == {"pending"}


def test_retry_preserves_unit_and_output_generation_identity(db, tmp_path):
    agent_id = f"{_AGENT_PREFIX}retry"
    repo = _maintenance_repository(db)
    unit_ids = repo.ensure_synthesis_units(_seed(db, agent_id, 1))
    claim = None
    while claim is None or not (
        claim.unit_kind == "agent_synthesis" and claim.scope_key == agent_id
    ):
        current = repo.claim_next(
            "worker-retry", eligible_unit_ids=unit_ids
        )
        assert current is not None
        if current.unit_kind == "agent_synthesis" and current.scope_key == agent_id:
            claim = current
        else:
            _publish_and_complete(repo, MaintenanceOutputPublisher(tmp_path), current)
    repo.fail(claim, error_code="retry_me", retry_after_seconds=0)

    retried = repo.claim_next(
        "worker-retry-2", eligible_unit_ids=(claim.unit_id,)
    )
    assert retried is not None
    assert retried.unit_id == claim.unit_id
    assert retried.output_generation == claim.output_generation
    assert retried.claim_generation == claim.claim_generation + 1
    assert retried.attempt_count == claim.attempt_count + 1
    _publish_and_complete(repo, MaintenanceOutputPublisher(tmp_path), retried)


def test_crash_after_replace_reconciles_same_output_without_republishing(
    db, tmp_path
):
    agent_id = f"{_AGENT_PREFIX}crash"
    repo = _maintenance_repository(db)
    publisher = MaintenanceOutputPublisher(tmp_path)
    unit_ids = repo.ensure_synthesis_units(_seed(db, agent_id, 1))

    claim = repo.claim_next(
        "worker-crash", eligible_unit_ids=unit_ids
    )
    assert claim is not None
    relative_path = f"test/{claim.unit_id}.md"

    def crash(boundary: str) -> None:
        if boundary == "after_replace":
            raise RuntimeError("simulated crash after replace")

    with pytest.raises(RuntimeError, match="simulated crash"):
        publisher.publish(
            relative_path,
            _render(claim, "crash recovery"),
            claim.output_generation,
            fault_hook=crash,
        )
    target = tmp_path / relative_path
    expected_digest = hashlib.sha256(target.read_bytes()).hexdigest()

    # Simulate database-time lease expiry for both the domain unit and its
    # operation slot; claim_next performs the normal recovery sweep.
    db.execute(
        "UPDATE maintenance_unit SET lease_expires_at = clock_timestamp() "
        "- interval '1 second' WHERE unit_id = ?",
        (claim.unit_id,),
    )
    db.execute(
        "UPDATE operation_admission_slot SET lease_expires_at = "
        "clock_timestamp() - interval '1 second' WHERE operation_id = ?",
        (str(claim.fence.operation_id),),
    )
    recovered = repo.claim_next(
        "worker-recovered", eligible_unit_ids=(claim.unit_id,)
    )
    assert recovered is not None
    assert recovered.unit_id == claim.unit_id
    assert recovered.output_generation == claim.output_generation
    assert publisher.reconcile(relative_path, recovered.output_generation) == (
        expected_digest
    )
    repo.complete(
        recovered,
        output_relative_path=relative_path,
        output_digest=expected_digest,
    )
    unit = db.fetch_one(
        "SELECT * FROM maintenance_unit WHERE unit_id = ?", (claim.unit_id,)
    )
    assert unit["state"] == "succeeded"
    assert unit["output_digest"] == expected_digest


def test_atomic_publisher_faults_before_replace_never_expose_partial_file(tmp_path):
    publisher = MaintenanceOutputPublisher(tmp_path)
    generation = str(uuid.uuid4())
    path = "patterns/tool_patterns.md"
    original = (
        "---\n"
        f'maintenance_generation: "{generation}"\n'
        "---\n\noriginal\n"
    )
    publisher.publish(path, original, generation)
    new_generation = str(uuid.uuid4())
    replacement = (
        "---\n"
        f'maintenance_generation: "{new_generation}"\n'
        "---\n\nreplacement\n"
    )

    def crash(boundary: str) -> None:
        if boundary == "before_replace":
            raise RuntimeError("before replace")

    with pytest.raises(RuntimeError, match="before replace"):
        publisher.publish(
            path, replacement, new_generation, fault_hook=crash
        )
    assert (tmp_path / path).read_text(encoding="utf-8") == original


async def test_synthesis_cycle_uses_claims_and_commits_all_atomic_outputs(
    db, tmp_path, monkeypatch
):
    agent_id = f"{_AGENT_PREFIX}cycle"
    await asyncio.to_thread(_seed, db, agent_id, 2)
    coordinator, runtime, catalog = _plane_dependencies(db)
    synthesizer = await asyncio.to_thread(
        KnowledgeSynthesizer,
        knowledge_dir=str(tmp_path),
        config_resolver=lambda: None,
        coordinator=coordinator,
        plane_runtime=runtime,
        plane_repositories=catalog,
    )
    synthesizer.min_interactions = 1
    monkeypatch.setattr(synthesizer, "_refresh_client", lambda: True)
    synthesizer._call_llm = AsyncMock(return_value="## Durable findings\nSafe.")

    await synthesizer._synthesis_cycle()

    sources = await asyncio.to_thread(
        db.fetch_all,
        "SELECT synthesized FROM interaction_log WHERE agent_id = ?",
        (agent_id,),
    )
    assert sources and all(row["synthesized"] for row in sources)
    units = await asyncio.to_thread(
        db.fetch_all,
        """
        SELECT unit_kind, state, output_relative_path, output_digest
        FROM maintenance_unit
        WHERE scope_key IN (?, 'system')
          AND unit_id IN (
              SELECT membership.unit_id FROM maintenance_unit_input membership
              JOIN interaction_log source
                ON source.id::text = membership.input_id
              WHERE source.agent_id = ?
          )
        """,
        (agent_id, agent_id),
    )
    assert {row["unit_kind"] for row in units} == {
        "agent_synthesis",
        "agent_capability",
        "cross_agent_synthesis",
    }
    assert {row["state"] for row in units} == {"succeeded"}
    for row in units:
        output = tmp_path / row["output_relative_path"]
        data = output.read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["output_digest"]
        assert b"maintenance_generation:" in data
    assert (tmp_path / "_index.md").is_file()
