"""Checks request-local compaction preservation, admission, and stale-state rejection.
The host supplies provider authorization and protected-state/reference oracles.
"""

import asyncio
import copy
from dataclasses import dataclass

import pytest

from orchestrator.safe_compaction import (
    SUMMARY_PREFIX,
    CompactionResult,
    compact_context,
    estimate_context_tokens,
)


def history():
    return [
        {"role": "system", "content": "Keep the governing instruction verbatim."},
        {"role": "user", "content": "OLDER_USER_START_" + "alpha " * 700 + "_OLDER_USER_END"},
        {"role": "assistant", "content": "OLDER_ANSWER_START_" + "beta " * 700 + "_OLDER_ANSWER_END"},
        {"role": "user", "content": "Current request must stay verbatim."},
    ]


async def protected_state():
    return {"authorized": True, "revision": 7, "pending": ["approval-a"]}


async def summary(_prompt):
    return "Earlier dialogue discussed alpha and beta."


async def run(messages, **overrides):
    options = {
        "llm_call": summary,
        "context_tokens": 20_000,
        "overhead_tokens": 18_000,
        "read_protected_state": protected_state,
        "min_recent_turns": 0,
    }
    options.update(overrides)
    return await compact_context(messages, **options)


@pytest.mark.asyncio
async def test_acceptance_is_local_labeled_and_reduces_the_complete_context():
    messages = history()
    original = copy.deepcopy(messages)
    prompts = []

    async def call(prompt):
        prompts.append(copy.deepcopy(prompt))
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call)

    assert isinstance(result, CompactionResult)
    assert result.status == "accepted"
    assert result.reason == "compacted"
    assert messages == original
    assert result.messages is not messages
    assert result.messages[0] == original[0]
    assert result.messages[-1] == original[-1]
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[1]["content"].startswith(SUMMARY_PREFIX)
    assert "partial" in SUMMARY_PREFIX.lower()
    assert "untrusted" in SUMMARY_PREFIX.lower()
    assert result.after_tokens < result.before_tokens
    assert result.after_tokens + 18_000 <= 20_000
    assert len(prompts) == 1
    assert original[1]["content"] in prompts[0][-1]["content"]
    assert original[2]["content"] in prompts[0][-1]["content"]
    result.messages[0]["content"] = "Changed result copy"
    assert messages == original


@pytest.mark.asyncio
async def test_within_budget_never_calls_a_provider_or_oracle():
    messages = [{"role": "user", "content": "Hello"}]

    async def forbidden(*_args):
        raise AssertionError("No auxiliary work is needed")

    result = await run(messages, llm_call=forbidden, read_protected_state=forbidden, overhead_tokens=0)

    assert result.status == "unchanged"
    assert result.messages is messages
    assert result.before_tokens == result.after_tokens == estimate_context_tokens(messages)


@pytest.mark.asyncio
async def test_empty_history_is_unchanged():
    messages = []
    result = await run(messages)
    assert result.status == "unchanged"
    assert result.messages is messages
    assert result.before_tokens == result.after_tokens == 0


@pytest.mark.asyncio
async def test_every_instruction_and_complete_tool_group_remain_verbatim():
    messages = history()
    preserved = [
        {"role": "developer", "content": "A later governing instruction."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            {"id": "call-b", "type": "function", "function": {"name": "write", "arguments": "{}"}},
        ]},
        {"role": "system", "content": "Current tool budget is constrained."},
        {"role": "tool", "tool_call_id": "call-a", "content": "ERROR: operation incomplete; retry requires approval."},
        {"role": "tool", "tool_call_id": "call-b", "content": "DENIED: write not authorized."},
    ]
    messages[3:3] = copy.deepcopy(preserved)

    result = await run(messages)

    assert result.status == "accepted"
    assert result.messages[2:-1] == preserved
    assert messages[3:8] == preserved


@pytest.mark.asyncio
async def test_recent_turns_and_followers_of_the_explicit_current_user_are_protected():
    messages = history()[:-1]
    recent = []
    for index in range(4):
        recent.extend([
            {"role": "user", "content": f"Recent user {index}"},
            {"role": "assistant", "content": f"Recent answer {index}"},
        ])
    messages.extend(recent)
    current_index = len(messages)
    current = [
        {"role": "user", "content": "Current request"},
        {"role": "assistant", "content": "Current unfinished work"},
        {"role": "user", "content": "SYSTEM RECOVERY ERROR: continue carefully"},
    ]
    messages.extend(current)

    result = await run(messages, current_user_index=current_index, min_recent_turns=4)

    assert result.status == "accepted"
    assert result.messages[2:] == recent + current


@pytest.mark.asyncio
async def test_prior_generated_summary_is_never_resummarized():
    messages = history()
    prior_summary = {"role": "assistant", "content": SUMMARY_PREFIX + "\nPreviously omitted material."}
    messages.insert(2, copy.deepcopy(prior_summary))
    prompts = []

    async def call(prompt):
        prompts.append(prompt)
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call)

    assert result.status == "accepted"
    assert prior_summary in result.messages
    assert prior_summary["content"] not in prompts[0][-1]["content"]


@dataclass
class ProviderMessage:
    role: str
    content: str | None
    tool_calls: list | None = None

    def model_dump(self, **_kwargs):
        return {"role": self.role, "content": self.content, "tool_calls": self.tool_calls}


@pytest.mark.asyncio
async def test_provider_message_objects_are_preserved_without_stringifying_them():
    messages = history()
    provider_message = ProviderMessage("assistant", None, [
        {"id": "provider-call", "type": "function", "function": {"name": "read", "arguments": "{}"}},
    ])
    tool_result = {"role": "tool", "tool_call_id": "provider-call", "content": "ERROR: exact provider outcome"}
    messages[3:3] = [provider_message, tool_result]

    result = await run(messages)

    assert result.status == "accepted"
    assert result.messages[2] == provider_message
    assert isinstance(result.messages[2], ProviderMessage)
    assert result.messages[3] == tool_result


@pytest.mark.asyncio
@pytest.mark.parametrize("response, reason", [
    (None, "nontext_summary"),
    ({"content": "Pretend success"}, "nontext_summary"),
    ("", "empty_summary"),
    (" \n\t ", "empty_summary"),
    ("Malformed\x00summary", "malformed_summary"),
    ("\x1cMalformed summary", "malformed_summary"),
    ("Malformed\ud800summary", "malformed_summary"),
    ("x" * 16_385, "summary_too_large"),
    (" " * 16_385 + "short", "summary_too_large"),
], ids=["none", "object", "empty", "whitespace", "nul", "leading_control", "invalid_utf8", "oversized", "oversized_whitespace"])
async def test_bad_provider_results_keep_history_and_pause(response, reason):
    messages = history()
    original = copy.deepcopy(messages)
    calls = 0

    async def call(_prompt):
        nonlocal calls
        calls += 1
        return response

    result = await run(messages, llm_call=call)

    assert result.status == "context_limit"
    assert result.reason == reason
    assert result.messages is messages
    assert messages == original
    assert result.before_tokens == result.after_tokens
    assert calls == 1


@pytest.mark.asyncio
async def test_summary_byte_bound_counts_multibyte_text():
    async def call(_prompt):
        return "🌌" * 3

    result = await run(history(), llm_call=call, max_summary_bytes=10)
    assert result.status == "context_limit"
    assert result.reason == "summary_too_large"


@pytest.mark.asyncio
async def test_provider_error_preserves_the_original_and_never_retries():
    messages = history()
    original = copy.deepcopy(messages)
    calls = 0

    async def call(_prompt):
        nonlocal calls
        calls += 1
        raise RuntimeError("Sensitive provider error body")

    result = await run(messages, llm_call=call)

    assert result.status == "context_limit"
    assert result.reason == "provider_error"
    assert result.messages is messages
    assert messages == original
    assert calls == 1
    assert "Sensitive" not in repr(result)


@pytest.mark.asyncio
async def test_timeout_preserves_history_and_cancels_the_one_attempt():
    messages = history()
    original = copy.deepcopy(messages)
    cancelled = False

    async def call(_prompt):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    result = await run(messages, llm_call=call, timeout_seconds=0.001)

    assert result.status == "context_limit"
    assert result.reason == "provider_timeout"
    assert result.messages is messages
    assert messages == original
    assert cancelled


@pytest.mark.asyncio
async def test_cancellation_propagates_without_history_mutation():
    messages = history()
    original = copy.deepcopy(messages)

    async def call(_prompt):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run(messages, llm_call=call)
    assert messages == original


@pytest.mark.asyncio
async def test_no_summary_call_when_protected_material_cannot_fit():
    messages = history()
    messages[0]["content"] = "Governing instruction " * 400

    async def forbidden(_prompt):
        raise AssertionError("Protected context cannot fit")

    result = await run(messages, llm_call=forbidden)

    assert result.status == "context_limit"
    assert result.reason == "protected_context_limit"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_complete_summary_input_must_fit_before_provider_dispatch():
    messages = history()

    async def forbidden(_prompt):
        raise AssertionError("Oversized summary input cannot be sent")

    result = await run(messages, llm_call=forbidden, context_tokens=2_000, overhead_tokens=0)

    assert result.status == "context_limit"
    assert result.reason == "summary_input_limit"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_reduction_must_be_effective_and_fit():
    messages = history()

    async def ineffective(_prompt):
        return messages[1]["content"] + messages[2]["content"] + "x" * 500

    ineffective_result = await run(messages, llm_call=ineffective)
    assert ineffective_result.status == "context_limit"
    assert ineffective_result.reason == "ineffective_summary"
    assert ineffective_result.messages is messages

    async def oversized_candidate(_prompt):
        return "x" * 2_000

    oversized_result = await run(messages, llm_call=oversized_candidate)
    assert oversized_result.status == "context_limit"
    assert oversized_result.reason == "candidate_context_limit"
    assert oversized_result.after_tokens == oversized_result.before_tokens


@pytest.mark.asyncio
async def test_insufficient_eligible_old_turns_pause_without_a_model_call():
    messages = history()

    async def forbidden(_prompt):
        raise AssertionError("No old turns are eligible")

    result = await run(messages, llm_call=forbidden, min_recent_turns=4)
    assert result.status == "context_limit"
    assert result.reason == "no_eligible_history"
    assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, False, {"authorized": False}, {"authority": False}, {"current": False}])
async def test_unavailable_or_denied_protected_oracle_prevents_provider_work(state):
    messages = history()

    async def read():
        return state

    async def forbidden(_prompt):
        raise AssertionError("Current authority was not established")

    result = await run(messages, llm_call=forbidden, read_protected_state=read)
    assert result.status == "context_limit"
    assert result.reason == "protected_state_unavailable"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_missing_or_failing_protected_oracle_fails_closed():
    async def fail():
        raise RuntimeError("Sensitive protected-state error")

    for oracle in (None, fail):
        messages = history()
        result = await run(messages, read_protected_state=oracle)
        assert result.status == "context_limit"
        assert result.reason == "protected_state_unavailable"
        assert result.messages is messages


@pytest.mark.asyncio
async def test_mutable_protected_snapshot_cannot_hide_concurrent_change():
    messages = history()
    state = {"authorized": True, "revision": 1, "approvals": ["pending"]}

    async def read():
        return state

    async def call(_prompt):
        state["approvals"].clear()
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call, read_protected_state=read)

    assert result.status == "context_limit"
    assert result.reason == "stale_protected_state"
    assert result.messages is messages
    assert state["approvals"] == []


@pytest.mark.asyncio
async def test_revocation_after_provider_call_rejects_the_candidate():
    messages = history()
    state = {"authorized": True, "revision": 1}

    async def read():
        return state

    async def call(_prompt):
        state["authorized"] = False
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call, read_protected_state=read)
    assert result.status == "context_limit"
    assert result.reason == "protected_state_unavailable"
    assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "nested"])
async def test_concurrent_history_change_rejects_without_overwriting_newer_input(change):
    messages = history()

    async def call(_prompt):
        if change == "append":
            messages.append({"role": "user", "content": "A newer user turn"})
        else:
            messages[0]["content"] = "A changed governing instruction"
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call)

    assert result.status == "context_limit"
    assert result.reason == "stale_history"
    assert result.messages is messages
    assert result.after_tokens == estimate_context_tokens(messages)
    if change == "append":
        assert result.messages[-1]["content"] == "A newer user turn"
    else:
        assert result.messages[0]["content"] == "A changed governing instruction"


@pytest.mark.asyncio
async def test_stale_attempt_is_rejected_if_current_history_now_fits():
    messages = history()

    async def call(_prompt):
        messages[:] = [{"role": "user", "content": "New, fitting context"}]
        return "Stale summary"

    result = await run(messages, llm_call=call)
    assert result.status == "rejected"
    assert result.reason == "stale_history"
    assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["obs_unknown", "obs_", "obs_-___", "evidence://unknown", "observation:unknown"])
async def test_unverifiable_generated_evidence_reference_rejects(reference):
    messages = history()

    async def call(_prompt):
        return f"Consult {reference} for source evidence."

    result = await run(messages, llm_call=call)
    assert result.status == "context_limit"
    assert result.reason == "unverified_references"
    assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [False, None, "approved"])
async def test_reference_oracle_requires_an_explicit_true_verdict(verdict):
    messages = history()
    validated = []

    async def validate(text):
        validated.append(text)
        return verdict

    result = await run(messages, validate_references=validate)
    assert result.status == "context_limit"
    assert result.reason == "unverified_references"
    assert validated == ["Earlier dialogue discussed alpha and beta."]


@pytest.mark.asyncio
async def test_validated_references_are_accepted_and_reference_validation_failures_preserve_input():
    async def call(_prompt):
        return "Consult obs_valid for exact source evidence."

    async def validate(text):
        return "obs_valid" in text

    messages = history()
    accepted = await run(messages, llm_call=call, validate_references=validate)
    assert accepted.status == "accepted"
    assert "obs_valid" in accepted.messages[1]["content"]

    async def fail(_text):
        raise RuntimeError("Sensitive reference failure")

    rejected = await run(messages, validate_references=fail)
    assert rejected.status == "context_limit"
    assert rejected.reason == "unverified_references"
    assert rejected.messages is messages


@pytest.mark.asyncio
async def test_authority_and_history_are_rechecked_after_reference_validation():
    messages = history()
    state = {"authorized": True, "revision": 1}

    async def read():
        return state

    async def validate(_text):
        state["revision"] = 2
        return True

    result = await run(messages, read_protected_state=read, validate_references=validate)
    assert result.status == "context_limit"
    assert result.reason == "stale_protected_state"

    async def mutate_during_final_read():
        if getattr(mutate_during_final_read, "seen", False):
            messages.append({"role": "user", "content": "Appended during final oracle await"})
        mutate_during_final_read.seen = True
        return {"authorized": True, "revision": 1}

    changed = await run(messages, read_protected_state=mutate_during_final_read)
    assert changed.status == "context_limit"
    assert changed.reason == "stale_history"
    assert changed.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_messages", [
    [{"role": "tool", "tool_call_id": "orphan", "content": "ERROR: orphan outcome"}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "missing", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}],
    [{"role": "assistant", "content": None, "tool_calls": "malformed"}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "same", "type": "function", "function": {"name": "read", "arguments": "{}"}}, {"id": "same", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}],
    [{"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "a", "content": "SUCCESS"}, {"role": "tool", "tool_call_id": "a", "content": "Duplicate"}],
])
async def test_unresolved_or_malformed_tool_relationships_prevent_compaction(tool_messages):
    messages = history()
    messages[3:3] = tool_messages
    original = copy.deepcopy(messages)

    async def forbidden(_prompt):
        raise AssertionError("Invalid tool relationships cannot be summarized")

    result = await run(messages, llm_call=forbidden)
    assert result.status == "context_limit"
    assert result.reason == "invalid_tool_group"
    assert result.messages is messages
    assert messages == original


@pytest.mark.asyncio
@pytest.mark.parametrize("current_index", [-1, 100, 0, True])
async def test_invalid_current_user_identity_fails_closed(current_index):
    messages = history()
    result = await run(messages, current_user_index=current_index)
    assert result.status == "context_limit"
    assert result.reason == "current_user_unavailable"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_missing_current_user_and_unsupported_content_preserve_original():
    no_user = [{"role": "assistant", "content": "x" * 5_000}]
    result = await run(no_user)
    assert result.status == "context_limit"
    assert result.reason == "current_user_unavailable"
    assert result.messages is no_user

    messages = history()
    messages.insert(2, {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/image"}}]})
    unsupported = await run(messages)
    assert unsupported.status == "context_limit"
    assert unsupported.reason == "unsupported_history"
    assert unsupported.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"context_tokens": 0}, {"context_tokens": True}, {"overhead_tokens": -1},
    {"overhead_tokens": True}, {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": 2 ** 4096},
    {"max_summary_bytes": 0}, {"min_recent_turns": -1}, {"min_recent_turns": True},
])
async def test_invalid_bounds_refuse_provider_work(options):
    messages = history()

    async def forbidden(_prompt):
        raise AssertionError("Invalid compaction bounds")

    result = await run(messages, llm_call=forbidden, **options)
    assert result.status == "context_limit"
    assert result.reason == "invalid_limits"
    assert result.messages is messages


def test_estimation_includes_arguments_and_multibyte_content():
    small = [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "read", "arguments": "{}"}}]}]
    large = copy.deepcopy(small)
    large[0]["tool_calls"][0]["function"]["arguments"] = "x" * 1_000
    assert estimate_context_tokens(large) - estimate_context_tokens(small) >= 998
    assert estimate_context_tokens([{"role": "user", "content": "🌌"}]) > estimate_context_tokens([{"role": "user", "content": "a"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, (), [object()], [{"role": "user", "content": float("nan")}]])
async def test_unserializable_history_is_retained_with_an_explicit_limit(malformed):
    result = await run(malformed)
    assert result.status == "context_limit"
    assert result.reason == "unsupported_history"
    assert result.messages is malformed


class InvalidProviderMessage:
    def model_dump(self, **_kwargs):
        return "Invalid provider object"


class UncopyableMessage(dict):
    def __deepcopy__(self, _memo):
        raise ValueError("Cannot capture a reliable snapshot")


@pytest.mark.asyncio
async def test_invalid_provider_objects_and_uncapturable_input_refuse_compaction():
    for messages in ([InvalidProviderMessage()], [UncopyableMessage(message) for message in history()]):
        result = await run(messages)
        assert result.status == "context_limit"
        assert result.reason == "unsupported_history"
        assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, "function", []])
async def test_malformed_roles_are_rejected_without_raising(role):
    messages = history()
    messages.insert(2, {"role": role, "content": "Unsupported role"})
    result = await run(messages)
    assert result.status == "context_limit"
    assert result.reason == "unsupported_history"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_null_nonassistant_content_and_legacy_function_calls_fail_closed():
    null_content = history()
    null_content.insert(2, {"role": "developer", "content": None})
    assert (await run(null_content)).reason == "unsupported_history"

    legacy_function = history()
    legacy_function.insert(2, {"role": "assistant", "content": "", "function_call": {"name": "read", "arguments": "{}"}})
    assert (await run(legacy_function)).reason == "invalid_tool_group"


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [{}, ["Invalid call"], [{"id": "invalid", "function": {"name": "", "arguments": "{}"}}]])
async def test_invalid_call_payloads_remain_in_original_history(calls):
    messages = history()
    messages.insert(2, {"role": "assistant", "content": None, "tool_calls": calls})
    result = await run(messages)
    assert result.status == "context_limit"
    assert result.reason == "invalid_tool_group"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_inflight_tool_call_at_end_of_history_is_retained():
    messages = history()
    messages.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": "inflight", "function": {"name": "read", "arguments": "{}"}},
    ]})
    result = await run(messages)
    assert result.status == "context_limit"
    assert result.reason == "invalid_tool_group"
    assert result.messages[-1]["tool_calls"][0]["id"] == "inflight"


@pytest.mark.asyncio
async def test_an_immutable_host_revision_can_supply_the_protected_oracle():
    async def read():
        return ("owner-a", "conversation-a", 7, "manifest-a")

    assert (await run(history(), read_protected_state=read)).status == "accepted"


@pytest.mark.asyncio
async def test_change_during_initial_oracle_read_prevents_provider_work():
    messages = history()

    async def read():
        messages.append({"role": "user", "content": "A newer request arrived before provider work"})
        return {"authorized": True, "revision": 1}

    async def forbidden(_prompt):
        raise AssertionError("The input snapshot is already stale")

    result = await run(messages, llm_call=forbidden, read_protected_state=read)
    assert result.status == "context_limit"
    assert result.reason == "stale_history"
    assert result.messages is messages


@pytest.mark.asyncio
async def test_new_unserializable_history_during_provider_work_is_retained():
    messages = history()

    async def call(_prompt):
        messages.append(object())
        return "Earlier dialogue discussed alpha and beta."

    result = await run(messages, llm_call=call)
    assert result.status == "context_limit"
    assert result.reason == "stale_history"
    assert result.messages is messages
    assert result.after_tokens >= result.before_tokens


@pytest.mark.asyncio
async def test_failed_final_protected_read_rejects_the_candidate():
    messages = history()
    read_count = 0

    async def read():
        nonlocal read_count
        read_count += 1
        if read_count > 1:
            raise RuntimeError("Failed to verify protected records")
        return {"authorized": True, "revision": 1}

    result = await run(messages, read_protected_state=read)
    assert result.status == "context_limit"
    assert result.reason == "protected_state_unavailable"
    assert result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["protected_state", "reference_validation"])
async def test_the_whole_attempt_bounds_stalled_host_oracles(stage):
    messages = history()

    async def stalled(*_args):
        await asyncio.Event().wait()

    options = {"read_protected_state" if stage == "protected_state" else "validate_references": stalled}
    result = await run(messages, timeout_seconds=0.001, **options)
    assert result.status == "context_limit"
    assert result.reason == f"{stage}_timeout"
    assert result.messages is messages
