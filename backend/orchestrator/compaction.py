"""Summarizes older chat turns to fit the model's context window while keeping
tool-call/tool-result pairs intact; called by orchestrator.py's ReAct loop before
each LLM call.
"""

import asyncio
import json
import logging
from typing import List, Dict, Tuple, Callable

from orchestrator.context_token_accounting import (
    FALLBACK_CONTEXT_WINDOW,
    IMAGE_TOKEN_ALLOWANCE,
    TokenAccountant,
    TokenAccountingError,
    build_model_profile,
    fallback_profile,
    get_context_window as _qualified_context_window,
)

logger = logging.getLogger("Orchestrator.Compaction")

CHARS_PER_TOKEN = 4

DEFAULT_CONTEXT_BUDGET_RATIO = 0.80

MIN_HISTORY_BUDGET_RATIO = 0.25

SUMMARY_PREFIX = "[Summary of prior conversation]"

_SYNTHETIC_USER_PREFIX = "SYSTEM RECOVERY ERROR:"

_DEFAULT_CONTEXT_WINDOW = FALLBACK_CONTEXT_WINDOW
_IMAGE_PART_CHARS = IMAGE_TOKEN_ALLOWANCE * CHARS_PER_TOKEN


def _accountant(
    model_name: str | None,
    provider: str | None,
    *,
    context_window: int | None = None,
    encoding_name: str | None = None,
    reserved_output_tokens: int | None = None,
    assets_dir: str | None = None,
) -> TokenAccountant:
    profile = build_model_profile(
        provider,
        model_name,
        context_window=context_window,
        encoding=encoding_name,
        reserved_output_tokens=reserved_output_tokens,
    ) or fallback_profile(provider, model_name)
    return TokenAccountant(profile, assets_dir=assets_dir)


def estimate_tokens(
    messages: List[Dict],
    model_name: str | None = None,
    provider: str | None = None,
    *,
    assets_dir: str | None = None,
) -> int:
    return _accountant(model_name, provider, assets_dir=assets_dir).count_messages(messages)


def estimate_overhead_tokens(
    tools_desc: List[Dict] | None,
    model_name: str | None = None,
    provider: str | None = None,
    *,
    assets_dir: str | None = None,
) -> int:
    return _accountant(model_name, provider, assets_dir=assets_dir).count_tools(tools_desc)


def estimate_request_tokens(
    messages: List[Dict],
    tools_desc: List[Dict] | None = None,
    model_name: str | None = None,
    provider: str | None = None,
    *,
    context_window: int | None = None,
    encoding_name: str | None = None,
    reserved_output_tokens: int | None = None,
    assets_dir: str | None = None,
) -> int:
    accountant = _accountant(
        model_name,
        provider,
        context_window=context_window,
        encoding_name=encoding_name,
        reserved_output_tokens=reserved_output_tokens,
        assets_dir=assets_dir,
    )
    return accountant.account_request(
        messages, tools_desc, reserved_output_tokens=reserved_output_tokens
    ).total_tokens


def get_context_window(
    model_name: str | None,
    provider: str | None = None,
    *,
    context_window: int | None = None,
) -> int:
    return _qualified_context_window(model_name, provider, context_window=context_window)


def _identify_turns(messages: List[Dict]) -> List[List[int]]:
    turns: List[List[int]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")

        if role == "assistant":
            turn_indices = [i]
            j = i + 1
            while j < len(messages):
                next_msg = messages[j]
                next_role = next_msg.get("role", "") if isinstance(next_msg, dict) else getattr(next_msg, "role", "")
                if next_role == "tool":
                    turn_indices.append(j)
                    j += 1
                else:
                    break
            turns.append(turn_indices)
            i = j
        else:
            turns.append([i])
            i += 1

    return turns


def _role_and_content(msg) -> tuple:
    if isinstance(msg, dict):
        return msg.get("role", ""), msg.get("content", "")
    return getattr(msg, "role", ""), getattr(msg, "content", "")


def _protected_indices(messages: List[Dict]) -> set:
    protected: set = set()
    for index in range(len(messages) - 1, 0, -1):
        role, content = _role_and_content(messages[index])
        if role != "user":
            continue
        if isinstance(content, str) and content.startswith(_SYNTHETIC_USER_PREFIX):
            continue
        protected.add(index)
        for follower in range(index + 1, len(messages)):
            if _role_and_content(messages[follower])[0] != "system":
                break
            protected.add(follower)
        break
    return protected


def _is_summary(msg) -> bool:
    role, content = _role_and_content(msg)
    return role == "system" and isinstance(content, str) and content.startswith(
        SUMMARY_PREFIX
    )


async def compact_messages(
    messages: List[Dict],
    model_name: str,
    llm_call: Callable,
    budget_ratio: float = DEFAULT_CONTEXT_BUDGET_RATIO,
    min_recent_turns: int = 4,
    overhead_tokens: int = 0,
    *,
    provider: str | None = None,
    tools_desc: List[Dict] | None = None,
    reserved_output_tokens: int | None = None,
    context_window: int | None = None,
    encoding_name: str | None = None,
    assets_dir: str | None = None,
) -> Tuple[List[Dict], bool]:
    if not messages:
        return messages, False

    try:
        accountant = _accountant(
            model_name,
            provider,
            context_window=context_window,
            encoding_name=encoding_name,
            reserved_output_tokens=reserved_output_tokens,
            assets_dir=assets_dir,
        )
    except TokenAccountingError:
        return messages, False
    context_window = accountant.profile.context_window
    reserved_output = (
        accountant.profile.reserved_output_tokens
        if reserved_output_tokens is None
        else reserved_output_tokens
    )
    accounted_overhead = max(0, overhead_tokens) + accountant.count_tools(tools_desc)
    fixed_overhead = accounted_overhead + reserved_output
    if fixed_overhead >= context_window:
        logger.info(
            "Compaction cannot fit fixed request overhead of %d tokens in a %d-token window",
            fixed_overhead,
            context_window,
        )
        return messages, False
    safe_history_capacity = context_window - fixed_overhead
    floor_tokens = int(context_window * MIN_HISTORY_BUDGET_RATIO)
    budget_tokens = min(
        max(floor_tokens, int(context_window * budget_ratio) - fixed_overhead),
        safe_history_capacity,
    )
    current_tokens = accountant.count_messages(messages)

    if current_tokens <= budget_tokens:
        return messages, False

    protected = _protected_indices(messages)
    candidate_indices = [
        index for index in range(1, len(messages)) if index not in protected
    ]
    if not candidate_indices:
        return messages, False

    candidates = [messages[index] for index in candidate_indices]
    turns = _identify_turns(candidates)

    if len(turns) <= min_recent_turns:
        return messages, False

    compact_turn_count = len(turns) - min_recent_turns
    compact_positions = set()
    for turn in turns[:compact_turn_count]:
        compact_positions.update(turn)

    to_summarize = [candidates[position] for position in sorted(compact_positions)]

    # Refuse to re-summarize our own summary (infinite loop)
    if all(_is_summary(msg) for msg in to_summarize):
        return messages, False

    logger.info(
        f"Compaction triggered: {current_tokens} tokens > {budget_tokens} budget "
        f"(model={model_name}, window={context_window}, "
        f"overhead={accounted_overhead}, reserved_output={reserved_output}, "
        f"encoding={accountant.profile.encoding or 'fallback'})"
    )

    compact_indices = {
        candidate_indices[position] for position in compact_positions
    }
    system_msg = messages[0]

    summary_input = []
    for msg in to_summarize:
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = json.dumps(content)
        if len(str(content)) > 3000:
            content = str(content)[:3000] + "... [truncated]"
        summary_input.append(f"[{role}]: {content}")

    summary_prompt = [
        {
            "role": "system",
            "content": (
                "Summarize the following conversation history into a concise paragraph. "
                "Preserve all factual results, data points, file paths, and key decisions. "
                "Do NOT include tool names, turn counts, or system mechanics. "
                "Write as a factual summary that would help an AI assistant continue the conversation."
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(summary_input),
        },
    ]

    try:
        response, _ = await llm_call(None, summary_prompt)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Compaction LLM call failed: {e}")
        return messages, False
    summary_text = getattr(response, "content", None)
    if not isinstance(summary_text, str) or not summary_text.strip():
        logger.warning("Compaction LLM call returned no usable summary; keeping original history")
        return messages, False

    summary_msg = {
        "role": "system",
        "content": f"{SUMMARY_PREFIX}:\n{summary_text}",
    }
    kept = [
        messages[index]
        for index in range(1, len(messages))
        if index not in compact_indices
    ]

    compacted = [system_msg, summary_msg, *kept]

    new_tokens = accountant.count_messages(compacted)
    if new_tokens >= current_tokens:
        logger.info(
            "Compaction made no progress (%d -> %d tokens); keeping original",
            current_tokens,
            new_tokens,
        )
        return messages, False
    logger.info(
        f"Compaction complete: {current_tokens} → {new_tokens} tokens "
        f"({compact_turn_count} turns summarized, {min_recent_turns} preserved)"
    )

    return compacted, True
