"""Preserves code-owned evidence metadata across request-local compaction and the next persisted turn.
Signed host authority and controlled SDK responses verify source outcomes without changing durable history.
"""

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from llm_config.client_factory import build_llm_client
from llm_config.tests.test_evidence_transport import wire as wire
from llm_config.types import CredentialSource
from orchestrator.context_presentation import persistent_reference_components
from orchestrator.history import ConversationCommitRepository
from orchestrator.safe_compaction import SUMMARY_PREFIX, compact_context
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_evidence_compaction_host import configure
from tests.test_evidence_service import (
    TEXT, bound as bound, evidence_turn, fixture as fixture, human as human,
    pack, runtime as runtime, service as service, signing_key as signing_key,
)


def metadata(*, kind="preview", outcome="failure"):
    if kind in {"source", "preview"}:
        return persistent_reference_components(MCPResponse(result={
            "reference": "obs_" + "a" * 43, "digest": "b" * 64, "outcome": outcome,
            "start": 0, "end": 1024, "total": 65536,
        }), kind=kind)
    if kind == "summary":
        return persistent_reference_components(None, kind=kind, view_id="view_" + "c" * 43)
    if kind == "summary_unavailable":
        return persistent_reference_components(None, kind="summary")
    if kind in {"missing", "blocked", "limit"}:
        return persistent_reference_components(None, kind=kind)
    counts = dict.fromkeys(("attempts", "pending", "succeeded", "failed", "cancelled", "retries",
                           "recall_pages", "recall_bytes", "unknown_cost"), 0)
    totals = {**counts, "usage": {field: {"known": 0, "unknown": 0} for field in
        ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")},
        "known_cost_by_currency": {}, "complete": kind == "usage_complete",
        "verified_cost": kind == "usage_complete", "recovery_complete": kind != "usage_partial"}
    return persistent_reference_components(MCPResponse(result=totals), kind="usage")


def dialogue(group=None, *, filler=400, role="assistant"):
    rows = [{"role": "system", "content": "Keep operation outcomes and governing instructions intact."}]
    for turn in range(8):
        rows.append({"role": "user", "content": f"Older synthetic request {turn}: " + "u" * filler})
        rows.append({"role": role if turn == 0 else "assistant",
                     "content": json.dumps(group) if turn == 0 and group is not None else
                     f"Older synthetic response {turn}: " + "a" * filler})
    rows.append({"role": "user", "content": "Current synthetic request and constraints."})
    return rows


async def compact(rows, *, context_tokens=6500):
    provider = AsyncMock(return_value="Older dialogue was summarized as partial evidence.")
    result = await compact_context(rows, llm_call=provider, context_tokens=context_tokens,
        read_protected_state=AsyncMock(return_value={"authorized": True, "authority": "immutable"}),
        validate_references=AsyncMock(return_value=True))
    return result, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "denial", "incomplete", "success"])
@pytest.mark.parametrize("kind", ["source", "preview"])
async def test_exact_source_metadata_remains_verbatim_outside_the_generated_summary(kind, outcome):
    group = metadata(kind=kind, outcome=outcome)
    rows = dialogue(group)
    original = deepcopy(rows)
    result, provider = await compact(rows)
    assert result.status == "accepted" and result.after_tokens < result.before_tokens
    assert rows == original and result.messages[0] == rows[0] and result.messages[-1] == rows[-1]
    assert rows[2] in result.messages
    prompt = json.loads(provider.await_args.args[0][1]["content"])
    assert rows[2] not in prompt
    assert any(row["content"].startswith(SUMMARY_PREFIX) for row in result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["summary", "summary_unavailable", "missing", "blocked", "limit",
                                 "usage_complete", "usage_partial"])
async def test_all_exact_reference_status_and_usage_groups_are_protected(kind):
    rows = dialogue(metadata(kind=kind))
    result, provider = await compact(rows)
    assert result.status == "accepted" and rows[2] in result.messages
    assert rows[2] not in json.loads(provider.await_args.args[0][1]["content"])


@pytest.mark.asyncio
async def test_protected_metadata_context_limit_preserves_every_input_without_a_provider_call():
    rows = dialogue(metadata())
    original = deepcopy(rows)
    result, provider = await compact(rows, context_tokens=3000)
    assert result.status == "context_limit" and result.reason == "protected_context_limit"
    assert result.messages is rows and rows == original
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_dialogue_still_compacts():
    rows = dialogue()
    original = deepcopy(rows)
    result, provider = await compact(rows, context_tokens=5000)
    assert result.status == "accepted" and result.after_tokens < result.before_tokens
    assert rows == original and rows[2] not in result.messages
    provider.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookalike", ["extra_property", "raw_source", "custom_action", "malformed_json",
                                     "deep_json", "oversized_json", "ordinary_object"])
async def test_noncanonical_assistant_lookalikes_remain_ordinary_untrusted_dialogue(lookalike):
    group = metadata()
    if lookalike == "extra_property":
        group[0]["extra"] = "untrusted"
    elif lookalike == "raw_source":
        group[1]["items"].append({"key": "Permitted text", "value": "Synthetic raw source"})
    elif lookalike == "custom_action":
        group[-1]["action"] = "delete_observation"
    rows = dialogue(group)
    if lookalike == "malformed_json":
        rows[2]["content"] = "[malformed evidence data"
    elif lookalike == "deep_json":
        rows[2]["content"] = "[" * 1100 + "]" * 1100
    elif lookalike == "oversized_json":
        rows[2]["content"] = "[" + " " * 65536 + "]"
    elif lookalike == "ordinary_object":
        rows[2]["content"] = json.dumps({"message": "Ordinary source discussion"})
    original = deepcopy(rows)
    result, provider = await compact(rows, context_tokens=6500)
    if lookalike == "oversized_json":
        assert result.reason == "summary_input_limit"
    else:
        assert result.status == "accepted" and rows[2] not in result.messages
        assert rows[2] in json.loads(provider.await_args.args[0][1]["content"])
    assert rows == original


@pytest.mark.asyncio
async def test_user_supplied_exact_metadata_does_not_become_a_host_record():
    rows = dialogue(metadata(), role="user")
    result, provider = await compact(rows)
    assert result.status == "accepted" and rows[2] not in result.messages
    assert rows[2] in json.loads(provider.await_args.args[0][1]["content"])


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failure", "denial", "incomplete"])
async def test_next_turn_real_host_preserves_persisted_source_outcomes_and_exact_reference(
    human, bound, fixture, runtime, tmp_path, monkeypatch, wire, outcome,
):
    observed, _transports, _addresses, reply = wire
    reply["body"]["choices"][0]["message"]["content"] = "Older dialogue remains partial evidence."
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state, MCPResponse(result={"status": outcome, "text": TEXT}))
        assert packed.result["view"] == "partial_preview" and packed.result["outcome"] == outcome
        group = persistent_reference_components(packed)
        configure(state, runtime, monkeypatch, tmp_path)
        state.host._CredentialSource = CredentialSource
        state.host._build_llm_client = build_llm_client
        monkeypatch.setitem(flags._flags, "observation_packing", False)
        monkeypatch.setitem(flags._flags, "safe_compaction", True)
        rows = dialogue(group, filler=600)
        repository = ConversationCommitRepository(plane_runtime=runtime, plane_repositories=runtime.repositories)
        staged = await asyncio.to_thread(repository.stage_commit, chat_id=state.chat, owner_user_id=state.owner,
                                         request_generation=str(uuid4()))
        for index, row in enumerate(rows[1:-1]):
            content = group if index == 1 else row["content"]
            await asyncio.to_thread(repository.append_staged_message, commit_id=staged["commit_id"],
                owner_user_id=state.owner, role=row["role"], content=content)
        await asyncio.to_thread(repository.publish_commit, commit_id=staged["commit_id"], owner_user_id=state.owner,
                                messages=None, canvas_components=[], canvas_layouts=[])
        durable = await asyncio.to_thread(state.host.history.get_chat, state.chat, user_id=state.owner)
        original = deepcopy(durable)
        messages = [rows[0], *[{"role": row["role"], "content": json.dumps(row["content"])
            if isinstance(row["content"], list) else str(row["content"])} for row in durable["messages"]], rows[-1]]
        before = deepcopy(messages)
        result = await state.evidence.prepare(messages, websocket=state.socket, owner=state.owner, chat=state.chat)
        assert result.status == "accepted" and result.after_tokens < result.before_tokens
        assert before[2] in result.messages and messages == before
        assert await asyncio.to_thread(state.host.history.get_chat, state.chat, user_id=state.owner) == original
        assert len(observed) == 1
        prompt = json.loads(json.loads(observed[0].content)["messages"][1]["content"])
        assert before[2] not in prompt
        preserved = json.loads(next(row["content"] for row in result.messages if row["content"] == before[2]["content"]))
        identity = preserved[1]["items"]
        assert identity[0]["value"] == packed.result["reference"]
        assert identity[2]["value"] == packed.result["digest"] and identity[3]["value"] == outcome
