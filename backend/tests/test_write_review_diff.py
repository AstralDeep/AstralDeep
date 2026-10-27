"""Tests for orchestrator/write_review.py through the real approval gate: write_file approval cards
carry a bounded, redacted unified diff of the current file read through the host, and an unreadable
or oversized target yields diff_unavailable without breaking the card.
"""

from __future__ import annotations

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import write_review as wr
from orchestrator.computer_hosts import ComputerHostError
from tests.test_computer_grant_flow_102 import ROOT, SCOPES, _call, _mac
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401

PATH = ROOT + "/notes.md"


@pytest.fixture(autouse=True)
def _fresh_pending_cards():
    from orchestrator import remote_confirmation as rc

    rc._PENDING_CARDS.clear()
    yield
    rc._PENDING_CARDS.clear()


def _host_reads(monkeypatch, orch, result):
    calls = []

    async def request(host, session_id, verb, args, timeout):
        calls.append((verb, args))
        if isinstance(result, BaseException):
            raise result
        return result
    monkeypatch.setattr(orch.computer_hosts, "request", request)
    return calls


async def _card(task_runtime, monkeypatch, result, **args):
    orch = task_runtime[0]
    await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": "tools:files"}
    calls = _host_reads(monkeypatch, orch, result)
    response = await _call(task_runtime, "write_file", **{"path": PATH, "content": "line one\nnew\n",
                                                          "if_exists": "overwrite", **args})
    assert response.result["_data"]["status"] == "confirmation_required"
    card, = response.ui_components
    return card, calls


def _diff(card):
    return [item for item in card["content"] if item.get("id") == f"{card['id']}_diff"]


def _decisions(card):
    return [item["payload"]["decision"] for item in card["content"] if item["type"] == "button"]


async def test_overwrite_card_carries_the_diff_against_the_current_bytes(task_runtime, monkeypatch):
    card, calls = await _card(task_runtime, monkeypatch, {"text": "line one\nold\n", "truncated": False, "size": 13})
    diff, = _diff(card)
    assert diff["type"] == "code" and diff["language"] == "diff"
    assert diff["code"].splitlines()[2:] == ["@@ -1,2 +1,2 @@", " line one", "-old", "+new"]
    assert calls == [("read_file", {"path": PATH, "max_bytes": wr.MAX_READ_BYTES})]
    assert card["content"].index(diff) == 1 and _decisions(card) == ["approve", "decline"]


@pytest.mark.parametrize("result", [ComputerHostError("permission_denied", "outside the workspace"),
                                    RuntimeError("socket closed"),
                                    {"text": "x" * 10, "truncated": True, "size": 999999},
                                    {"truncated": False}, "not-a-mapping"])
async def test_unreadable_target_yields_diff_unavailable(task_runtime, monkeypatch, result):
    card, calls = await _card(task_runtime, monkeypatch, result)
    marker, = _diff(card)
    assert marker["type"] == "text" and marker["content"].startswith("diff_unavailable")
    assert len(calls) == 1 and _decisions(card) == ["approve", "decline"]


async def test_create_card_diffs_against_an_absent_file(task_runtime, monkeypatch):
    card, _calls = await _card(task_runtime, monkeypatch, ComputerHostError("not_found", "File does not exist"), if_exists="refuse")
    diff, = _diff(card)
    assert diff["code"].splitlines()[:2] == ["--- /dev/null", f"+++ {PATH}"]
    assert diff["code"].splitlines()[-2:] == ["+line one", "+new"]


async def test_overwrite_of_a_missing_file_still_shows_a_create_diff(task_runtime, monkeypatch):
    card, _calls = await _card(task_runtime, monkeypatch, ComputerHostError("not_found", "File does not exist"))
    diff, = _diff(card)
    assert diff["code"].splitlines()[:2] == ["--- /dev/null", f"+++ {PATH}"]


async def test_unreadable_new_file_is_not_reported_as_an_empty_file(task_runtime, monkeypatch):
    card, _calls = await _card(task_runtime, monkeypatch, ComputerHostError("permission_denied", "refused"),
                               if_exists="refuse")
    marker, = _diff(card)
    assert marker["type"] == "text" and marker["content"] == wr.UNAVAILABLE_TEXT


async def test_unchanged_content_says_so(task_runtime, monkeypatch):
    card, _calls = await _card(task_runtime, monkeypatch, {"text": "line one\nnew\n", "truncated": False})
    marker, = _diff(card)
    assert marker["type"] == "text" and marker["content"] == "No changes to the current file."


async def test_a_session_that_is_no_longer_current_is_not_read(task_runtime, monkeypatch):
    orch = task_runtime[0]
    await _mac(task_runtime, remote=True)
    calls = _host_reads(monkeypatch, orch, {"text": "", "truncated": False})
    parts = await wr.diff_components(orch, object(), "owner-a", "chat-a",
                                     {"path": PATH, "content": "x", "if_exists": "overwrite"}, "card_diff")
    assert calls == [] and parts[0]["content"].startswith("diff_unavailable") and parts[0]["id"] == "card_diff"


def test_the_diff_is_bounded_to_64_kib():
    proposed = "".join(f"line {index} {'y' * 60}\n" for index in range(4000))
    diff, truncated = wr.unified_diff(PATH, "", proposed)
    assert truncated and len(diff.encode("utf-8")) <= wr.DIFF_LIMIT_BYTES
    assert diff.endswith(wr.TRUNCATED_MARKER)
    small, cut = wr.unified_diff(PATH, "a\n", "b\n")
    assert not cut and wr.TRUNCATED_MARKER not in small


def test_secrets_and_identifiers_are_masked_on_both_sides():
    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1yc2EAAAADAQABAAABAQ\n-----END OPENSSH PRIVATE KEY-----\n"
    current = f"user = alice\npassword = hunter2\n{key}ssn 123-45-6789\n"
    proposed = "user = alice\ntoken sk-abcdefghijklmnopqrstuvwxyz123456\nmail bob@example.com\n\x07bell\n"
    diff, _cut = wr.unified_diff(PATH, current, proposed)
    for leaked in ("hunter2", "AAAAB3NzaC1yc2E", "123-45-6789", "sk-abcdefghijklmnop", "bob@example.com", "\x07"):
        assert leaked not in diff
    assert "-[REDACTED:secret]" in diff and "[REDACTED:private_key]" in diff
    assert "[REDACTED:ssn]" in diff and "[REDACTED:email]" in diff and " user = alice" in diff


async def test_non_card_gate_results_pass_through_unchanged():
    alert = [{"type": "alert", "message": "Still waiting", "variant": "warning"}]
    assert await wr.attach_write_diff(object(), object(), "o", "c", {"path": PATH}, alert) is alert
    card = [{"type": "card", "content": [{"type": "text", "content": "x"}]}]
    assert await wr.attach_write_diff(object(), object(), "o", "c", {"path": PATH}, card) is card


async def test_a_failure_while_building_the_diff_still_returns_the_card(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(wr, "diff_components", broken)
    card = [{"type": "card", "id": "au_approval_x", "content": [
        {"type": "text", "content": "Write file"},
        {"type": "button", "action": "remote_op_decision", "payload": {"proposal_id": "x", "decision": "approve"}}]}]
    out = await wr.attach_write_diff(object(), object(), "o", "c", {"path": PATH, "content": "x"}, card)
    assert out[0]["content"][1] == {"type": "text", "id": "au_approval_x_diff",
                                    "content": wr.UNAVAILABLE_TEXT, "variant": "caption"}


async def test_arguments_without_text_content_are_unavailable():
    parts = await wr.diff_components(object(), object(), "o", "c", {"path": PATH, "content": 3}, None)
    assert parts == [{"type": "text", "content": wr.UNAVAILABLE_TEXT, "variant": "caption"}]


async def test_source_review_card_carries_the_diff_after_the_exact_arguments(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    await _mac(task_runtime, remote=True)
    calls = _host_reads(monkeypatch, orch, {"text": "hi\n", "truncated": False})
    response = await hc.evaluate(orch, ws, "owner-a", "chat-a", "computer-use-1", "write_file",
                                 {"path": PATH, "content": "hello\n", "if_exists": "overwrite"},
                                 ("source_bound", "mutation_or_disclosure", "tools:files", "origin:signed_human"))
    card, = response.ui_components
    kinds = [item["type"] for item in card["content"]]
    diff = card["content"][kinds.index("code") + 1]
    assert diff["id"] == f"{card['id']}_diff" and diff["language"] == "diff"
    assert diff["code"].splitlines()[-2:] == ["-hi", "+hello"] and len(calls) == 1


async def test_a_failure_in_the_source_review_diff_still_shows_the_card(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    await _mac(task_runtime, remote=True)

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(wr, "diff_components", broken)
    response = await hc.evaluate(orch, ws, "owner-a", "chat-a", "computer-use-1", "write_file",
                                 {"path": PATH, "content": "hello", "if_exists": "overwrite"},
                                 ("source_bound", "mutation_or_disclosure", "tools:files", "origin:signed_human"))
    card, = response.ui_components
    assert any(item.get("content") == wr.UNAVAILABLE_TEXT for item in card["content"])


async def test_authority_that_changes_while_waiting_for_the_session_is_not_read(task_runtime, monkeypatch):
    import asyncio

    orch, ws, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    calls = _host_reads(monkeypatch, orch, {"text": "", "truncated": False})
    args = {"path": PATH, "content": "x", "if_exists": "overwrite"}
    await session.lock.acquire()
    pending = asyncio.create_task(wr.diff_components(orch, ws, "owner-a", "chat-a", args, None))
    await asyncio.sleep(0)
    await orch.computer_sessions.pause(session, "user_pause")
    session.lock.release()
    parts = await pending
    assert calls == [] and parts[0]["content"] == wr.UNAVAILABLE_TEXT


async def test_a_busy_session_times_out_to_diff_unavailable(task_runtime, monkeypatch):
    orch, ws, *_ = task_runtime
    session, _host = await _mac(task_runtime, remote=True)
    calls = _host_reads(monkeypatch, orch, {"text": "", "truncated": False})
    monkeypatch.setattr(wr, "READ_TIMEOUT_S", 0.01)
    await session.lock.acquire()
    try:
        parts = await wr.diff_components(orch, ws, "owner-a", "chat-a",
                                         {"path": PATH, "content": "x", "if_exists": "overwrite"}, None)
    finally:
        session.lock.release()
    assert calls == [] and parts[0]["content"] == wr.UNAVAILABLE_TEXT


@pytest.mark.parametrize("content", ["poem", ""])
async def test_missing_file_summary_says_create_even_when_overwrite_was_requested(task_runtime, monkeypatch, content):
    card, _calls = await _card(task_runtime, monkeypatch, ComputerHostError("not_found", "missing"), content=content)
    assert "(create)" in card["content"][0]["content"]
    assert "(overwrite)" not in card["content"][0]["content"]


async def test_missing_file_binds_absence_before_review_and_exact_dispatch(task_runtime, monkeypatch):
    from orchestrator import remote_confirmation as rc
    from tests.test_computer_grant_flow_102 import OWNER

    orch, ws, effects, *_ = task_runtime
    card, calls = await _card(task_runtime, monkeypatch, ComputerHostError("not_found", "missing"))
    payload = next(part["payload"] for part in card["content"] if part.get("payload", {}).get("decision") == "approve")
    context = rc._proposal_context(orch)
    row = context.call(context.repository.get, owner_id=OWNER, proposal_id=payload["proposal_id"])
    assert row.arguments["if_exists"] == "refuse"
    assert row.args_fingerprint == rc._fingerprint(row.arguments)
    preview = next(part for part in card["content"] if part.get("language") == "json")
    import json
    assert json.loads(preview["code"])["if_exists"] == "refuse"
    await rc.handle_decision(orch, ws, OWNER, payload)
    assert len(effects) == 1 and effects[0][1]["if_exists"] == "refuse"
    assert len(calls) == 1


@pytest.mark.parametrize("initial_bypass", [False, True])
async def test_bypass_transition_binds_missing_file_before_any_create_review(task_runtime, monkeypatch, initial_bypass):
    import json

    from orchestrator import remote_confirmation as rc
    from tests.test_computer_grant_flow_102 import OWNER

    orch, ws, effects, *_ = task_runtime
    _session, host = await _mac(task_runtime, remote=True)
    orch.tool_permissions.get_tool_scope_map.return_value = {**SCOPES, "write_file": "tools:files"}
    await orch.computer_sessions.set_bypass(host, initial_bypass)
    calls = _host_reads(monkeypatch, orch, ComputerHostError("not_found", "missing"))
    prepare = wr.prepare_write_review

    async def toggle_after_preparation(*args, **kwargs):
        snapshot = await prepare(*args, **kwargs)
        await orch.computer_sessions.set_bypass(host, not initial_bypass)
        return snapshot

    monkeypatch.setattr(wr, "prepare_write_review", toggle_after_preparation)
    response = await _call(task_runtime, "write_file", path=PATH, content="new", if_exists="overwrite")
    assert response.error is None
    if initial_bypass:
        card, = response.ui_components
        payload = next(part["payload"] for part in card["content"]
                       if part.get("payload", {}).get("decision") == "approve")
        context = rc._proposal_context(orch)
        row = context.call(context.repository.get, owner_id=OWNER, proposal_id=payload["proposal_id"])
        assert row.arguments["if_exists"] == "refuse"
        preview = next(part for part in card["content"] if part.get("language") == "json")
        assert json.loads(preview["code"])["if_exists"] == "refuse"
        await rc.handle_decision(orch, ws, OWNER, payload)
    else:
        assert not response.ui_components and not rc._PENDING_CARDS
    assert len(effects) == 1 and effects[0][1]["if_exists"] == "refuse"
    assert len(calls) == 1


async def test_unbound_overwrite_review_never_claims_create_after_a_late_missing_read(task_runtime, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(wr, "prepare_write_review", AsyncMock(return_value=None))
    card, calls = await _card(task_runtime, monkeypatch, ComputerHostError("not_found", "missing"))
    assert "(create)" not in card["content"][0]["content"]
    marker, = _diff(card)
    assert marker["type"] == "text" and marker["content"] == wr.UNAVAILABLE_TEXT
    assert len(calls) == 1
