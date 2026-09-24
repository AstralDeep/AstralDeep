"""Tests for backend/orchestrator/compaction.py: the tool-block overhead charge against
the budget, protecting the live user turn and trailing addenda, never orphaning a
tool result, and convergence over re-summarizing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.compaction import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_BUDGET_RATIO as DEFAULT_RATIO,
    MIN_HISTORY_BUDGET_RATIO,
    SUMMARY_PREFIX,
    compact_messages,
    estimate_overhead_tokens,
    estimate_tokens,
    get_context_window,
)


async def _fake_llm(websocket, messages):
    assert websocket is None
    return SimpleNamespace(content="condensed history"), None


def _history(pairs: int, chars: int) -> list[dict]:
    body = "x" * chars
    out: list[dict] = []
    for index in range(pairs):
        out.append({"role": "user", "content": f"q{index} {body}"})
        out.append({"role": "assistant", "content": f"a{index} {body}"})
    return out


def test_overhead_estimator_matches_serialized_size():
    tools = [{"type": "function", "function": {"name": "roll", "parameters": {}}}]
    assert estimate_overhead_tokens(tools) == len(json.dumps(tools)) // CHARS_PER_TOKEN
    assert estimate_overhead_tokens([]) == 0
    assert estimate_overhead_tokens(None) == 0


@pytest.mark.asyncio
async def test_tool_block_overhead_charges_the_budget():
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=6, chars=1_250),
        {"role": "user", "content": "current question"},
    ]

    untouched, fired = await compact_messages(messages, "gpt-4", _fake_llm)
    assert fired is False and untouched == messages

    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=5_000
    )
    assert fired is True
    assert estimate_tokens(compacted) < estimate_tokens(messages)
    assert any(
        "condensed history" in str(msg.get("content", "")) for msg in compacted
    )


@pytest.mark.asyncio
async def test_protected_tail_keeps_user_turn_and_trailing_addenda():
    question = {"role": "user", "content": "THE REAL QUESTION"}
    datamark = {"role": "system", "content": "datamark sentinel addendum"}
    recipe = {"role": "system", "content": "learned recipe hint"}
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=8, chars=1_250),
        question,
        datamark,
        recipe,
    ]

    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=5_000
    )
    assert fired is True
    assert compacted[-3:] == [question, datamark, recipe]
    assert compacted[0] == messages[0]


@pytest.mark.asyncio
async def test_zero_overhead_behavior_unchanged_over_budget():
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=8, chars=2_500),
        {"role": "user", "content": "current question"},
    ]
    compacted, fired = await compact_messages(messages, "gpt-4", _fake_llm)
    assert fired is True
    assert compacted[-1] == {"role": "user", "content": "current question"}


@pytest.mark.asyncio
async def test_overhead_never_produces_negative_budget_crash():
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=6, chars=1_250),
        {"role": "user", "content": "current question"},
    ]
    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=10**9
    )
    assert fired is True
    assert compacted[-1] == {"role": "user", "content": "current question"}


def _inflight(rounds: int, chars: int) -> list[dict]:
    out: list[dict] = []
    for index in range(rounds):
        out.append({"role": "assistant", "content": f"calling tool {index}"})
        out.append({"role": "tool", "content": f"result{index} " + "R" * chars})
    return out


@pytest.mark.asyncio
async def test_inflight_tool_trace_is_compactable():
    question = {"role": "user", "content": "THE REAL QUESTION"}
    messages = [
        {"role": "system", "content": "sys"},
        question,
        {"role": "system", "content": "datamark addendum"},
        *_inflight(rounds=10, chars=4_000),
    ]
    before = estimate_tokens(messages)

    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=2_000
    )

    assert fired is True
    assert estimate_tokens(compacted) < before
    assert question in compacted
    assert compacted[0] == messages[0]
    assert compacted[1]["content"].startswith(SUMMARY_PREFIX)


@pytest.mark.asyncio
async def test_compaction_never_orphans_a_tool_result():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        *_inflight(rounds=12, chars=4_000),
    ]
    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=2_000
    )
    assert fired is True
    for position, msg in enumerate(compacted):
        if msg.get("role") == "tool":
            assert position > 0
            assert compacted[position - 1]["role"] in {"assistant", "tool"}


@pytest.mark.asyncio
async def test_ordering_is_preserved_so_the_question_precedes_its_trace():
    question = {"role": "user", "content": "THE REAL QUESTION"}
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=3, chars=800),
        question,
        *_inflight(rounds=10, chars=4_000),
    ]
    compacted, _ = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=2_000
    )
    kept = [msg for msg in compacted if msg in messages]
    positions = [messages.index(msg) for msg in kept]
    assert positions == sorted(positions)
    assert messages.index(question) in positions


@pytest.mark.asyncio
async def test_recovery_nudge_is_not_mistaken_for_the_user_question():
    question = {"role": "user", "content": "THE REAL QUESTION"}
    nudge = {
        "role": "user",
        "content": "SYSTEM RECOVERY ERROR: bad JSON. Return valid JSON only.",
    }
    messages = [
        {"role": "system", "content": "sys"},
        question,
        *_inflight(rounds=10, chars=4_000),
        nudge,
    ]
    compacted, fired = await compact_messages(
        messages, "gpt-4", _fake_llm, overhead_tokens=2_000
    )
    assert fired is True
    assert question in compacted


@pytest.mark.asyncio
async def test_small_window_model_does_not_compact_a_tiny_chat():
    messages = [
        {"role": "system", "content": "sys"},
        *_history(pairs=5, chars=20),
        {"role": "user", "content": "hi"},
    ]
    calls = []

    async def _counting_llm(websocket, prompt):
        calls.append(prompt)
        return SimpleNamespace(content="summary"), None

    compacted, fired = await compact_messages(
        messages, "gpt-4", _counting_llm, overhead_tokens=16_300
    )
    assert fired is False
    assert compacted == messages
    assert calls == []


def test_budget_floor_is_a_fraction_of_the_window():
    assert 0 < MIN_HISTORY_BUDGET_RATIO < DEFAULT_RATIO
    assert get_context_window("gpt-4") == 8_192


@pytest.mark.asyncio
async def test_a_summary_is_never_re_summarized_into_another_summary():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "system", "content": f"{SUMMARY_PREFIX}:\nearlier stuff"},
        {"role": "user", "content": "q"},
        *_inflight(rounds=2, chars=40),
    ]
    calls = []

    async def _counting_llm(websocket, prompt):
        calls.append(prompt)
        return SimpleNamespace(content="summary"), None

    compacted, fired = await compact_messages(
        messages, "gpt-4", _counting_llm, overhead_tokens=6_000, min_recent_turns=4
    )
    assert fired is False
    assert compacted == messages
    assert calls == []


@pytest.mark.asyncio
async def test_repeated_passes_converge_instead_of_thrashing():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        *_inflight(rounds=10, chars=4_000),
    ]
    seen = []
    for _ in range(5):
        messages, fired = await compact_messages(
            messages, "gpt-4", _fake_llm, overhead_tokens=2_000
        )
        seen.append((fired, estimate_tokens(messages)))
    sizes = [size for _, size in seen]
    assert sizes == sorted(sizes, reverse=True)
    assert seen[-1][0] is False
