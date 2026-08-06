"""Compaction budget realism (067 follow-up).

The tool-definitions block rides a sibling kwarg, never ``messages``, so the
message-only estimate was blind to the largest single prompt component
(~16.3k tokens for the full built-in catalog, measured 2026-08-06) and the
budget fired far too late. ``compact_messages`` now takes ``overhead_tokens``
(from ``estimate_overhead_tokens``) and charges it against the budget, and the
protected tail is the current USER message plus every trailing system
addendum — not blindly ``messages[-1]``, which the datamarking sentinel and
learned-recipe addenda displace.
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
    assert websocket is None  # system-credential contract (054 FR-019)
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
    # gpt-4 window 8_192 → budget 6_553 tokens. Content ≈ 3_800 tokens sits
    # comfortably under it, so the message-only check never fires — but with
    # a 5_000-token tool block the real prompt is over budget.
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
    # The tail survives verbatim, in order, at the very end — the trailing
    # system addenda never displace the user turn into summarizable history.
    assert compacted[-3:] == [question, datamark, recipe]
    assert compacted[0] == messages[0]


@pytest.mark.asyncio
async def test_zero_overhead_behavior_unchanged_over_budget():
    # Content alone over the gpt-4 budget still compacts exactly as before.
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


# ── The in-flight ReAct shape: compaction runs once per LOOP ITERATION ──────


def _inflight(rounds: int, chars: int) -> list[dict]:
    """Assistant/tool pairs the ReAct loop appends AFTER the user turn.

    Each message carries unique content so order assertions can tell one
    from another.
    """
    out: list[dict] = []
    for index in range(rounds):
        out.append({"role": "assistant", "content": f"calling tool {index}"})
        out.append({"role": "tool", "content": f"result{index} " + "R" * chars})
    return out


@pytest.mark.asyncio
async def test_inflight_tool_trace_is_compactable():
    # The regression the first cut shipped: pinning "everything after the
    # last user message" protected the entire in-flight trace — the ONLY
    # unbounded region — so compaction became a no-op exactly when needed.
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
    # The question survives, in place, and never becomes summary fodder.
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
    # Every tool message must still be preceded by its assistant message —
    # a dangling tool result is an outright API error.
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
    # Every surviving original message keeps its relative order — the
    # compacted list is a subsequence of the original plus the summary.
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


# ── Budget floor + convergence ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_small_window_model_does_not_compact_a_tiny_chat():
    # gpt-4's 80% budget (6_553) is smaller than the real tool block
    # (~16.3k), so an unfloored subtraction clamped to 0 and fired
    # compaction on every iteration of every chat — including this one,
    # which fits the window a thousand times over.
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
    # Steady state after one pass: the only compactable turn left is the
    # previous summary. Re-summarizing it burns a system-credential LLM
    # call per iteration and changes nothing.
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
    # Monotonically non-increasing, and it stops firing once there is
    # nothing left worth summarizing.
    sizes = [size for _, size in seen]
    assert sizes == sorted(sizes, reverse=True)
    assert seen[-1][0] is False
