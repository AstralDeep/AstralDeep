"""Tests for ConcurrencyCap wiring in orchestrator/orchestrator.py: agent-card
long-running-tool metadata, per-user/per-agent cap isolation, and pending-entry
tracking for release under contention.
"""

import asyncio

import pytest

from orchestrator.concurrency_cap import ConcurrencyCap
from orchestrator.orchestrator import Orchestrator
from shared.protocol import AgentCard


def _make_orch_with_cards(cards: dict) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.agent_cards = cards
    orch.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    orch._pending_cap_entries = {}
    return orch


def _card_with_long_running(agent_id: str, long_running: list) -> AgentCard:
    return AgentCard(
        name=agent_id,
        description="test agent",
        agent_id=agent_id,
        metadata={"long_running_tools": long_running},
    )


def test_is_long_running_tool_reads_card_metadata() -> None:
    orch = _make_orch_with_cards({
        "classify-1": _card_with_long_running("classify-1", ["train_classifier", "retest_model"]),
        "llm-factory-1": _card_with_long_running("llm-factory-1", []),
    })
    assert orch._is_long_running_tool("classify-1", "train_classifier") is True
    assert orch._is_long_running_tool("classify-1", "retest_model") is True
    assert orch._is_long_running_tool("classify-1", "get_ml_options") is False
    assert orch._is_long_running_tool("llm-factory-1", "chat_with_model") is False


def test_is_long_running_tool_handles_unknown_agent() -> None:
    orch = _make_orch_with_cards({})
    assert orch._is_long_running_tool("ghost-1", "anything") is False
    assert orch._is_long_running_tool(None, "anything") is False
    assert orch._is_long_running_tool("", "anything") is False


def test_is_long_running_tool_handles_missing_metadata_field() -> None:
    card = AgentCard(name="x", description="", agent_id="x", metadata={})
    orch = _make_orch_with_cards({"x": card})
    assert orch._is_long_running_tool("x", "anything") is False


@pytest.mark.asyncio
async def test_cap_state_isolated_per_user_agent() -> None:
    orch = _make_orch_with_cards({
        "classify-1": _card_with_long_running("classify-1", ["start_training_job"]),
        "forecaster-1": _card_with_long_running("forecaster-1", ["start_training_job"]),
    })
    cap = orch.concurrency_cap
    for jid in ("a1", "a2", "a3"):
        assert await cap.acquire("alice", "classify-1", jid)
    assert await cap.acquire("alice", "classify-1", "a4") is False
    assert await cap.acquire("bob", "classify-1", "b1") is True
    assert await cap.acquire("alice", "forecaster-1", "f1") is True


@pytest.mark.asyncio
async def test_pending_cap_entries_tracks_user_agent_for_release() -> None:
    orch = _make_orch_with_cards({
        "classify-1": _card_with_long_running("classify-1", ["train_classifier"]),
    })
    cap = orch.concurrency_cap
    cap_job_id = "cap_train_classifier_abc12345"
    assert await cap.acquire("alice", "classify-1", cap_job_id)
    orch._pending_cap_entries[cap_job_id] = ("alice", "classify-1")
    entry = orch._pending_cap_entries.pop(cap_job_id, None)
    assert entry == ("alice", "classify-1")
    await cap.release(*entry, cap_job_id)
    assert cap.inflight_count("alice", "classify-1") == 0


@pytest.mark.asyncio
async def test_concurrent_acquires_under_contention_respect_cap() -> None:
    orch = _make_orch_with_cards({
        "classify-1": _card_with_long_running("classify-1", ["train_classifier"]),
    })
    cap = orch.concurrency_cap
    results = await asyncio.gather(*[
        cap.acquire("alice", "classify-1", f"j{i}") for i in range(8)
    ])
    assert sum(results) == 3
    assert cap.inflight_count("alice", "classify-1") == 3
