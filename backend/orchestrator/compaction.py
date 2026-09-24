"""Summarizes older chat turns to fit the model's context window while keeping
tool-call/tool-result pairs intact; called by orchestrator.py's ReAct loop before
each LLM call.
"""

import json
import logging
from typing import List, Dict, Tuple, Callable

logger = logging.getLogger("Orchestrator.Compaction")

CHARS_PER_TOKEN = 4

DEFAULT_CONTEXT_BUDGET_RATIO = 0.80

MIN_HISTORY_BUDGET_RATIO = 0.25

SUMMARY_PREFIX = "[Summary of prior conversation]"

_SYNTHETIC_USER_PREFIX = "SYSTEM RECOVERY ERROR:"

_MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5": 16_384,
    "llama-3.2-90b": 131_072,
    "llama-3.1": 131_072,
    "llama-3": 8_192,
    "deepseek": 65_536,
    "qwen": 32_768,
    "mistral": 32_768,
    "mixtral": 32_768,
}

_DEFAULT_CONTEXT_WINDOW = 32_768
_IMAGE_PART_CHARS = 1_500 * CHARS_PER_TOKEN


def estimate_tokens(messages: List[Dict]) -> int:
    total_chars = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        total_chars += _IMAGE_PART_CHARS
                    else:
                        total_chars += len(json.dumps(part))
        else:
            total_chars += len(str(msg))
    return total_chars // CHARS_PER_TOKEN


def estimate_overhead_tokens(tools_desc: List[Dict] | None) -> int:
    if not tools_desc:
        return 0
    try:
        return len(json.dumps(tools_desc, default=str)) // CHARS_PER_TOKEN
    except (TypeError, ValueError):
        return 0


def get_context_window(model_name: str) -> int:
    model_lower = model_name.lower()
    for keyword, size in _MODEL_CONTEXT_WINDOWS.items():
        if keyword in model_lower:
            return size
    return _DEFAULT_CONTEXT_WINDOW


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
) -> Tuple[List[Dict], bool]:
    if not messages:
        return messages, False

    context_window = get_context_window(model_name)
    floor_tokens = int(context_window * MIN_HISTORY_BUDGET_RATIO)
    budget_tokens = max(floor_tokens, int(context_window * budget_ratio) - overhead_tokens)
    current_tokens = estimate_tokens(messages)

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
        f"overhead={overhead_tokens})"
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
        if response and hasattr(response, "content") and response.content:
            summary_text = response.content
        else:
            summary_text = "Prior conversation context was summarized but the summary could not be generated."
    except Exception as e:
        logger.warning(f"Compaction LLM call failed: {e}")
        summary_text = f"[{compact_turn_count} earlier conversation turns were removed to fit context window]"

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

    new_tokens = estimate_tokens(compacted)
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
