"""
Message Compaction — Intelligent context window management.

When the conversation history approaches the LLM context limit, older turns
are summarized into a compact representation while preserving tool-call /
tool-result pairing integrity.

Inspired by Claude Code's auto-compaction strategy.
"""
import json
import logging
from typing import List, Dict, Tuple, Callable

logger = logging.getLogger("Orchestrator.Compaction")

# Rough chars-per-token heuristic (conservative — errs on the side of
# compacting earlier rather than blowing the context window).
CHARS_PER_TOKEN = 4

# Default budget: 80% of model context window (in tokens).
DEFAULT_CONTEXT_BUDGET_RATIO = 0.80

# Floor for the history budget as a fraction of the context window. The
# tool-definitions overhead can exceed the whole budget on a small-window
# model (the built-in catalog is ~16.3k tokens; gpt-4's 80% budget is
# ~6.5k), and an unfloored subtraction clamps to zero — which would fire
# compaction on every turn of every chat, including a ten-token one, and
# summarizing history cannot shrink a tool block anyway. Below this floor
# the honest answer is "the prompt overhead is the problem", not "summarize
# harder".
MIN_HISTORY_BUDGET_RATIO = 0.25

#: Prefix of the system message this module injects, so a later pass can
#: recognize its own output and refuse to re-summarize a summary.
SUMMARY_PREFIX = "[Summary of prior conversation]"

#: The UI-component recovery retry (orchestrator.py) appends a synthetic
#: ``user`` message. It is machinery, not the human's question, and must not
#: be mistaken for the turn's real user turn.
_SYNTHETIC_USER_PREFIX = "SYSTEM RECOVERY ERROR:"

# Known context window sizes by model family keyword.
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

# Fallback if model is unknown
_DEFAULT_CONTEXT_WINDOW = 32_768


def estimate_tokens(messages: List[Dict]) -> int:
    """Estimate the token count for a list of OpenAI-style messages."""
    total_chars = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                total_chars += len(json.dumps(content))
        else:
            # ChatCompletionMessage object — serialize it
            total_chars += len(str(msg))
    return total_chars // CHARS_PER_TOKEN


def estimate_overhead_tokens(tools_desc: List[Dict] | None) -> int:
    """Estimate the per-call prompt cost of the tool-definitions block.

    Tool definitions ride a sibling kwarg, never ``messages``, yet the chat
    template renders them into the same context window (~16.3k tokens for the
    full built-in catalog, measured 2026-08-06). Without this term the budget
    check is blind to the largest single component of the prompt and fires
    far too late.
    """
    if not tools_desc:
        return 0
    try:
        return len(json.dumps(tools_desc, default=str)) // CHARS_PER_TOKEN
    except (TypeError, ValueError):
        return 0


def get_context_window(model_name: str) -> int:
    """Look up context window size for a model name."""
    model_lower = model_name.lower()
    for keyword, size in _MODEL_CONTEXT_WINDOWS.items():
        if keyword in model_lower:
            return size
    return _DEFAULT_CONTEXT_WINDOW


def _identify_turns(messages: List[Dict]) -> List[List[int]]:
    """Group messages into atomic turns that should never be split.

    A turn is:
      - A single user message, OR
      - A single system message, OR
      - An assistant message followed by any tool-result messages that
        reference tool_calls in that assistant message.

    Returns a list of turns, where each turn is a list of message indices.
    """
    turns: List[List[int]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")

        if role == "assistant":
            # Group assistant + its tool results
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
    """Indices that must survive compaction verbatim, in place.

    The current turn's user question, plus any trailing system addenda that
    were appended directly after it (the datamarking sentinel, the learned
    recipe hint). ``messages[-1]`` is NOT the user turn: those addenda follow
    it, and — because compaction runs once per ReAct iteration, not once per
    chat turn — the in-flight assistant/tool trace follows them. That trace
    is the one unbounded region in the prompt, so it stays COMPACTABLE;
    only the question itself is pinned.
    """
    protected: set = set()
    for index in range(len(messages) - 1, 0, -1):
        role, content = _role_and_content(messages[index])
        if role != "user":
            continue
        if isinstance(content, str) and content.startswith(_SYNTHETIC_USER_PREFIX):
            # Recovery-retry machinery, not the human's question.
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
    """Compact message history if it exceeds the token budget.

    Args:
        messages: Full message list (system + history + current user +
                  optional trailing system addenda).
        model_name: Model identifier for context window lookup — the model
                  that will SERVE the chat call, since its window is what the
                  prompt must fit into.
        llm_call: Async callable(websocket, messages) -> (response, usage);
                  called with ``websocket=None`` so the summary runs on the
                  system credential (feature 054 FR-019).
        budget_ratio: Fraction of context window to use as budget.
        min_recent_turns: Minimum number of recent turns to always preserve.
        overhead_tokens: Per-call prompt cost that rides OUTSIDE ``messages``
                  (the tool-definitions block — see estimate_overhead_tokens).
                  Subtracted from the budget so the check sees the real
                  prompt size.

    Returns:
        (possibly_compacted_messages, was_compacted)
    """
    if not messages:
        return messages, False

    context_window = get_context_window(model_name)
    floor_tokens = int(context_window * MIN_HISTORY_BUDGET_RATIO)
    budget_tokens = max(floor_tokens, int(context_window * budget_ratio) - overhead_tokens)
    current_tokens = estimate_tokens(messages)

    if current_tokens <= budget_tokens:
        return messages, False

    # Everything except the system prompt and the pinned user question is
    # compactable — including the in-flight assistant/tool trace, which is
    # the only unbounded region and therefore the only one worth reclaiming.
    protected = _protected_indices(messages)
    candidate_indices = [
        index for index in range(1, len(messages)) if index not in protected
    ]
    if not candidate_indices:
        return messages, False

    candidates = [messages[index] for index in candidate_indices]
    turns = _identify_turns(candidates)

    # Preserve the most recent N turns
    if len(turns) <= min_recent_turns:
        return messages, False

    compact_turn_count = len(turns) - min_recent_turns
    compact_positions = set()
    for turn in turns[:compact_turn_count]:
        compact_positions.update(turn)

    to_summarize = [candidates[position] for position in sorted(compact_positions)]

    # Refuse to re-summarize our own summary: with nothing else eligible,
    # each pass would burn a system-credential LLM call, replace one summary
    # with another, and leave the token count unchanged — every iteration,
    # forever.
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

    # Build summarization prompt
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
        # Truncate very long individual messages for the summary call
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
        # Fallback: just drop the old messages with a note
        summary_text = f"[{compact_turn_count} earlier conversation turns were removed to fit context window]"

    # Build compacted message list. Kept messages stay in their ORIGINAL
    # order, so the pinned user question keeps its place relative to the
    # in-flight trace and every assistant/tool pair stays adjacent (turns
    # are compacted whole, so a tool result can never be orphaned from the
    # tool_calls message it answers).
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
        # No progress — the summary cost more than it saved. Keep the
        # original rather than growing the prompt every iteration.
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
