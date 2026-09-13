"""Current/legacy result selection over genuine privately qualified PG receipts."""
from dataclasses import replace

import pytest

from persistent_agents import research_episode
from persistent_agents.tests.test_research_episode_postgres_088 import outcome
from tests.test_work_result_postgres_088 import (
    _mutate_action, completed as completed, project, unavailable,
    fixture as fixture, gate_orchestrator as gate_orchestrator,
    operation as operation, plane as plane, research as research,
    signing_key as signing_key,
)

runtime = plane
pytestmark = [pytest.mark.asyncio,
              pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def legacy(research, monkeypatch):
    # Execute the genuine old writer's key choice, including ordinary dispatch,
    # settlement and keyed proof; do not manufacture signed result receipts.
    with monkeypatch.context() as old_writer:
        old_writer.setattr(research_episode, "research_action_keys",
                           lambda _: (research_episode.SOURCE_KEY, research_episode.MODEL_KEY))
        runner, result = await outcome(research)
        research.completed = await runner._finish_operation(research.executor, result)
    repository = research.runtime.repositories.assignments
    research.model = await research.executor.store.transaction(lambda tx, _: repository.get_action(
        tx, owner_id=research.owner, assignment_id=research.completed.assignment_id,
        action_id=result.research.model_action_id))
    return research


async def test_completed_legacy_result_uses_exact_old_proof_without_execution(legacy):
    op = legacy
    assert op.model.intent.action_key == "research-v1-" + research_episode.MODEL_KEY
    assert (await project(op))["available"] is True
    assert len(op.physical) == 2 and len(op.model_calls) == 1


@pytest.mark.parametrize("change", ["epoch", "revision", "receipt"])
async def test_legacy_fallback_cannot_adopt_another_generation_or_proof(legacy, change):
    def corrupt(data):
        if change == "receipt":
            data["result"]["result_digest"] = "0" * 64
        else:
            field = "control_epoch" if change == "epoch" else "instruction_revision"
            data[field] += 1
    await _mutate_action(legacy, legacy.model, corrupt)
    unavailable(await project(legacy))


@pytest.mark.parametrize("malformed", ["wrong_key", "old_epoch", "invalid_state"])
async def test_current_evidence_refusal_never_selects_valid_legacy_alternate(legacy, monkeypatch, malformed):
    op = legacy
    repository = op.runtime.repositories.assignments
    original = repository.get_action_by_key
    current_key = "research-v1-" + research_episode.research_action_keys(op.completed)[1]
    candidate = replace(op.model, intent=replace(op.model.intent, action_key=current_key))
    if malformed == "wrong_key":
        candidate = op.model
    elif malformed == "old_epoch":
        candidate = replace(candidate, control_epoch=candidate.control_epoch + 1)
    else:
        candidate = replace(candidate, state="uncertain")
    lookups = []
    def read(tx, **kwargs):
        lookups.append(kwargs["action_key"])
        # Controlled repository response boundary; actual valid legacy receipt
        # remains stored and independently proved readable before interception.
        return candidate if kwargs["action_key"] == current_key else original(tx, **kwargs)
    assert (await project(op))["available"] is True
    monkeypatch.setattr(repository, "get_action_by_key", read)
    unavailable(await project(op))
    assert lookups == [current_key]


async def test_current_evidence_appearing_after_legacy_discovery_blocks_fallback(legacy, monkeypatch):
    op = legacy
    repository = op.runtime.repositories.assignments
    original = repository.get_action_by_key
    key = "research-v1-" + research_episode.research_action_keys(op.completed)[1]
    calls = []
    def read(tx, **kwargs):
        if kwargs["action_key"] == key:
            calls.append(key)
            if len(calls) == 2:
                return op.model
        return original(tx, **kwargs)
    monkeypatch.setattr(repository, "get_action_by_key", read)
    unavailable(await project(op))
    assert calls == [key, key]


async def test_current_result_never_reads_legacy_alternate(completed, monkeypatch):
    op = completed
    repository = op.runtime.repositories.assignments
    original = repository.get_action_by_key
    calls = []
    def read(tx, **kwargs):
        calls.append(kwargs["action_key"])
        assert kwargs["action_key"] != "research-v1-" + research_episode.MODEL_KEY
        return original(tx, **kwargs)
    monkeypatch.setattr(repository, "get_action_by_key", read)
    assert (await project(op))["available"] is True
    assert calls == ["research-v1-" + research_episode.research_action_keys(op.completed)[1]]


async def test_current_model_cannot_select_noncanonical_source_key(completed):
    op = completed
    await _mutate_action(op, op.source, lambda data: data["intent"].update(action_key="other-observation"))
    unavailable(await project(op))
