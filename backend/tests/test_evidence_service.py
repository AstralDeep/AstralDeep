"""Checks evidence_context.py's host policy, privacy, audit, and retention boundaries.
Signed IAM and public Plane facades provide real owner authority while typed dispatch fixtures isolate service failures.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.evidence.evidence_agent import EvidenceAgent
from audit.schemas import AuditEventDTO
from llm_config.audit_events import record_llm_call
from llm_config.types import CredentialSource, ResolvedConfig
from orchestrator import evidence_context as module
from orchestrator.context_authority import capture_context_authority, use_context_authority
from orchestrator.evidence_archive import EvidenceArchive, EvidenceDenied, EvidenceUnavailable
from orchestrator.history import HistoryManager
from orchestrator.hooks import HookEvent, HookManager
from orchestrator.orchestrator import GateRefusal, Orchestrator, PreparedDispatch
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill, MCPResponse
from tests.test_context_authority import admitted_turn
from tests.test_human_request_authority_088 import human as human
from tests.test_recursive_delegation import base_token
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio
AGENT, TOOL = "web-research-1", "fetch_page"
TEXT = "Beginning preserved evidence; middle retained facts; final conclusion. " * 100


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)
        self.elapsed = 0.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.elapsed

    def advance(self, delta):
        self.now += delta
        self.elapsed += delta.total_seconds()


@asynccontextmanager
async def evidence_turn(human, bound, fixture, tmp_path, monkeypatch):
    monkeypatch.setitem(flags._flags, "observation_packing", True)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "evidence-agent.pem"))
    monkeypatch.setenv("FF_POLICY_ENGINE", "false")
    monkeypatch.setenv("POLICY_RULES", "[]")
    gate = PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: []))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        host, socket, chat, binding = turn
        host.history = HistoryManager(str(tmp_path / "history"), plane_runtime=binding.origin.binding.runtime)
        await asyncio.to_thread(host.history.create_chat, chat, fixture[1])
        host.ui_sessions = {socket: {"sub": fixture[1], "_raw_token": binding.origin.token,
                                    "realm_access": {"roles": ["user"]}}}
        host.cancelled_sessions = {}
        host.agent_cards[AGENT] = AgentCard("Reader", "Reads authorized sources", AGENT,
            skills=[AgentSkill("Read source", "Reads one source", TOOL, scope="tools:read")])
        adapter = EvidenceAgent(host, port=0)
        host.local_agents = getattr(host, "local_agents", {})
        host.local_agents[adapter.agent_id] = adapter
        host.agent_cards[adapter.agent_id] = adapter.card
        host._policy_roles = lambda _: ["user"]
        host._hitl_pending_calls = {}
        host._dispatch_context = {}
        host._pending_cap_entries = {}
        host._job_context = {}
        calls, releases, notices = [], [], []
        async def authorize(ws, agent, tool, args, conversation, owner, **kwargs):
            calls.append((ws, agent, tool, deepcopy(args), conversation, owner, deepcopy(kwargs)))
            return PreparedDispatch({**args, "user_id": owner, "session_id": conversation,
                                     "_delegation_token": "synthetic-runtime-token"}, {}, None, None)
        async def release(*args):
            releases.append(args)
        async def notice(*args, **kwargs):
            notices.append((args, kwargs))
        host._authorize_and_prepare = authorize
        host.concurrency_cap = SimpleNamespace(release=release)
        host._release_hop_cap_slot = release
        host.send_ui_render = notice
        clock = Clock()
        policy_path = tmp_path / "retention.json"
        policy_path.write_text(json.dumps([{
            "owner_id": fixture[1], "conversation_id": chat, "audience_id": f"user:{fixture[1]}",
            "source_agent": AGENT, "source_tool": TOOL, "expires_at": (clock.now + timedelta(hours=2)).isoformat(),
        }]))
        monkeypatch.setenv("ASTRAL_OBSERVATION_POLICY", str(policy_path))
        archive = EvidenceArchive(clock=clock, monotonic_clock=clock.monotonic)
        evidence = module.EvidenceContext(host, archive=archive, clock=clock)
        host._evidence_context = evidence
        lease = await capture_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
        state = SimpleNamespace(host=host, socket=socket, chat=chat, owner=fixture[1], binding=binding,
            evidence=evidence, archive=archive, clock=clock, policy=policy_path, calls=calls,
            releases=releases, notices=notices, phi_gate=gate, lease=lease, adapter=adapter)
        try:
            with use_context_authority(lease):
                yield state
        finally:
            await evidence.close()


async def pack(state, value=TEXT, *, args=None, parent=None, initiator=None, agent=AGENT):
    response = value if isinstance(value, MCPResponse) else MCPResponse(request_id=str(uuid4()), result=value)
    return await state.evidence.pack_result(response, websocket=state.socket, owner=state.owner,
        chat=state.chat, agent=agent, tool=TOOL, args={"query": "synthetic"} if args is None else args,
        operation_id=str(state.binding.operations[0].record.operation_id), parent=parent, initiator=initiator)


async def test_capture_refusal_logs_only_the_code_and_source_check(
        human, bound, fixture, tmp_path, monkeypatch, caplog):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        async def changed_arguments(*args, **kwargs):
            return PreparedDispatch({"query": "private synthetic input"}, {}, None, None)
        state.host._authorize_and_prepare = changed_arguments
        result = await pack(state)
        assert result.error is not None
        assert "evidence_unavailable_or_not_authorized at _probe_source:" in caplog.text
        assert "private synthetic input" not in caplog.text and TEXT not in caplog.text


@pytest.mark.parametrize("rules,allowed", [("", True), ("[]", True), ("invalid", False), (" ", False), ("{}", False)])
async def test_source_rechecks_accept_default_policy_and_deny_malformed_rules(
        human, bound, fixture, tmp_path, monkeypatch, rules, allowed):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        monkeypatch.setenv("FF_POLICY_ENGINE", "true")
        monkeypatch.setenv("POLICY_RULES", rules)
        result = await pack(state)
        assert (result.error is None) is allowed
        if allowed:
            recalled = await invoke(state, arguments={"reference": result.result["reference"]})
            assert recalled.error is None and recalled.result["text"] == TEXT
        else:
            assert result.error["code"] == "evidence_unavailable_or_not_authorized"


async def invoke(state, name="recall_observation", arguments=None, *, parent=None, initiator=None, overrides=None):
    request = str(uuid4())
    with module.dispatch_requester(state.host, parent, initiator, state.owner, state.chat, state.socket):
        state.host._dispatch_context[request] = {"agent_id": "evidence-1", **module.requester_binding(state.host)}
    state.host._dispatch_context[request].update(overrides or {})
    try:
        return await asyncio.create_task(state.evidence.tool(request, name, arguments or {}))
    finally:
        state.host._dispatch_context.pop(request, None)


async def deliver(state, response, **overrides):
    arguments = {"websocket": state.socket, "owner": state.owner, "chat": state.chat}
    arguments.update(overrides)
    return await state.evidence.verify_delivery(response, **arguments)


async def events(state):
    records, _ = await asyncio.to_thread(state.host.audit_repo.list_for_user, state.owner, limit=200)
    return records


async def test_authorized_preview_exact_recall_and_durable_receipts_exclude_source_text(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        original = MCPResponse(request_id="original", result=TEXT)
        packed = await pack(state, original, args={"query": "synthetic", "_credentials": "runtime only"})
        assert original.result == TEXT and packed is not original
        assert packed.result["view"] == "partial_preview" and packed.result["omitted"] is True
        assert packed.result["untrusted"] is True and module.transient_result(packed, AGENT)
        reference = packed.result["reference"]
        observation = state.archive.inspect(reference, owner_id=state.owner,
            conversation_id=state.chat, audience_id=f"user:{state.owner}")
        assert observation.source_args == {"query": "synthetic"}
        recalled = await invoke(state, arguments={"reference": reference})
        assert recalled.error is None and recalled.result["text"] == TEXT
        assert recalled.result["untrusted"] is True and recalled.result["at_end"] is True
        assert recalled.result["digest"] == packed.result["digest"]
        assert all(call[1:3] == (AGENT, TOOL) and call[6]["auto_subscribe_stream"] is False for call in state.calls)
        assert len(state.calls) >= 4
        ledger = await state.evidence.usage.totals(state.owner, state.chat)
        assert ledger["recall_bytes"] == len(TEXT.encode())
        records = await events(state)
        assert {record.action_type for record in records} >= {"evidence.capture", "evidence.recall", "context.usage"}
        durable = "\n".join(record.model_dump_json() for record in records)
        assert "Beginning preserved" not in durable and "runtime only" not in durable


@pytest.mark.parametrize("preview", [False, True])
async def test_final_delivery_rebuilds_owned_literal_ui_and_never_recaptures_or_recharges(
    human, bound, fixture, tmp_path, monkeypatch, preview,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        response = packed if preview else await invoke(state, arguments={"reference": packed.result["reference"]})
        original_data, original_ui = deepcopy(response.result), deepcopy(response.ui_components)
        totals = await state.evidence.usage.totals(state.owner, state.chat)
        response.ui_components = [{"type": "text", "content": "injected-ui-content"}]
        verified = await deliver(state, response)
        assert verified.error is None and verified is not response
        assert verified.result == original_data and verified.ui_components == original_ui
        assert "injected-ui-content" not in json.dumps(verified.ui_components)
        assert verified._evidence_delivery_scope[:3] == (state.owner, state.chat, state.socket)
        assert "_evidence_delivery_scope" not in json.loads(verified.to_json())
        assert await state.evidence.usage.totals(state.owner, state.chat) == totals
        assert state.archive.observation_count == 1
        response.result["preview" if preview else "text"] = "late-source-mutation"
        assert verified.result == original_data


@pytest.mark.parametrize("tamper", ["text", "digest", "start", "end", "total", "next_offset", "untrusted", "extra"])
async def test_final_delivery_rejects_tampered_exact_page_wire_data(human, bound, fixture, tmp_path, monkeypatch, tamper):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        if tamper == "text":
            response.result["text"] = "injected-source-content"
        elif tamper == "digest":
            response.result["digest"] = "a" * 64
        elif tamper == "untrusted":
            response.result["untrusted"] = False
        elif tamper == "extra":
            response.result["instructions"] = "injected-tool-authority"
        else:
            response.result[tamper] = 1
        denied = await deliver(state, response)
        assert denied.error is not None and denied.result is None and denied.ui_components is None


@pytest.mark.parametrize("tamper", ["preview", "omitted", "total_bytes", "recall", "view"])
async def test_final_delivery_rejects_tampered_preview_wire_data(human, bound, fixture, tmp_path, monkeypatch, tamper):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = await pack(state)
        response.result[tamper] = False if tamper == "omitted" else "injected"
        denied = await deliver(state, response)
        assert denied.error is not None and denied.result is None


@pytest.mark.parametrize("loss", ["permission", "security", "manifest", "policy", "retention", "privacy", "expired", "lease"])
async def test_final_delivery_rechecks_current_source_privacy_policy_and_retention(human, bound, fixture, tmp_path, monkeypatch, loss):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        if loss == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        elif loss == "security":
            state.host.security_flags[AGENT] = {TOOL: {"blocked": True}}
        elif loss == "manifest":
            state.host.agent_cards.pop(AGENT)
        elif loss == "policy":
            monkeypatch.setenv("FF_POLICY_ENGINE", "true")
            monkeypatch.setenv("POLICY_RULES", '[{"effect":"deny"}]')
        elif loss == "retention":
            state.policy.write_text("[]")
        elif loss == "privacy":
            analyzer = SimpleNamespace(analyze=lambda **kwargs: [
                SimpleNamespace(start=0, end=5, entity_type="PERSON")
            ] if kwargs["text"].startswith("Beginning") else [])
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(analyzer=analyzer))
        elif loss == "expired":
            state.clock.advance(timedelta(hours=3))
        else:
            state.lease.close()
        refused = await deliver(state, response)
        assert refused.result is None or "text" not in refused.result and "preview" not in refused.result
        if loss == "expired":
            assert refused.result == {"status": "unavailable", "reason": "expired"}
        else:
            assert refused.error is not None
        if loss == "privacy":
            assert state.archive.retained_bytes == 0 and not state.evidence._sources


@pytest.mark.parametrize("mismatch", ["owner", "chat", "websocket", "scope"])
async def test_final_delivery_scope_cannot_be_rebound(human, bound, fixture, tmp_path, monkeypatch, mismatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = await deliver(state, await pack(state))
        overrides = {}
        if mismatch == "scope":
            response._evidence_delivery_scope = ("foreign", state.chat, state.socket, None, None)
        else:
            overrides[mismatch] = object() if mismatch == "websocket" else "foreign"
        refused = await deliver(state, response, **overrides)
        assert refused.error is not None and refused.result is None


async def test_final_delivery_preserves_attenuated_parent_scope_and_rechecks_it(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        parent = base_token(sub=state.owner, agent="requester", scopes=["tools:read"])
        response = await deliver(state, await pack(state, parent=parent, initiator="requester"),
                                 parent=parent, initiator="requester")
        parent["scope"] = "tools:write"
        assert response._evidence_delivery_scope[3]["scope"] == "tools:read"
        assert (await deliver(state, response)).error is None
        scope = list(response._evidence_delivery_scope)
        scope[3]["exp"] = 1
        response._evidence_delivery_scope = tuple(scope)
        denied = await deliver(state, response)
        assert denied.error is not None and denied.result is None


@pytest.mark.parametrize("kind", ["usage", "deleted", "unavailable"])
async def test_final_delivery_rebuilds_owned_metadata_ui_without_source_content(human, bound, fixture, tmp_path, monkeypatch, kind):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        if kind == "usage":
            response = await invoke(state, "context_usage")
        else:
            packed = await pack(state)
            reference = packed.result["reference"]
            if kind == "deleted":
                response = await invoke(state, "delete_observation", {"reference": reference})
            else:
                state.clock.advance(timedelta(hours=3))
                response = await invoke(state, arguments={"reference": reference})
        original_data, original_ui = deepcopy(response.result), deepcopy(response.ui_components)
        response.ui_components = [{"type": "text", "content": "injected-metadata-content"}]
        verified = await deliver(state, response)
        assert verified.error is None and verified.result == original_data
        assert verified.ui_components == original_ui
        assert "injected-metadata-content" not in json.dumps(verified.ui_components)


@pytest.mark.parametrize("data", ["untrusted-text", {"text": "injected-text"}, {"status": "deleted", "text": "injected-text"}])
async def test_final_delivery_refuses_unrecognized_content_without_reference(human, bound, fixture, tmp_path, monkeypatch, data):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = MCPResponse(request_id="forged", result=data,
                               ui_components=[{"type": "text", "content": "injected-text"}])
        denied = await deliver(state, response)
        assert denied.error is not None and denied.result is None and denied.ui_components is None


async def test_final_delivery_none_and_existing_denial_remain_unchanged():
    evidence = module.EvidenceContext(SimpleNamespace())
    assert await evidence.verify_delivery(None, websocket=None, owner=None, chat=None) is None
    denied = module.EvidenceContext._denied("denied")
    assert await evidence.verify_delivery(denied, websocket=None, owner=None, chat=None) is denied


async def test_exact_unicode_pages_reconstruct_the_retained_text(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        text = "First αβγ😀 preserved fact. " * 1100 + "Final finding."
        packed = await pack(state, text)
        reference, offset, parts = packed.result["reference"], 0, []
        while True:
            response = await invoke(state, arguments={"reference": reference, "offset": offset})
            page = response.result
            assert response.error is None and len(page["text"].encode()) <= 16384
            assert page["start"] == offset and page["digest"] == packed.result["digest"]
            assert page["end"] - page["start"] == len(page["text"].encode())
            parts.append(page["text"])
            if page["at_end"]:
                assert page["next_offset"] is None and page["end"] == len(text.encode())
                break
            assert page["partial"] is True and page["next_offset"] == page["end"]
            offset = page["next_offset"]
        assert len(parts) >= 2 and "".join(parts) == text
        response = await invoke(state, "context_usage")
        assert response.error is None and response.result["recall_bytes"] == len(text.encode())


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "incomplete"])
async def test_structured_observations_preserve_exact_order_and_outcome(human, bound, fixture, tmp_path, monkeypatch, status):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        value = {"text": TEXT, "status": status, "rows": [3, 1, 2], "z": "end", "a": "begin"}
        packed = await pack(state, value)
        recalled = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert recalled.result["text"] == json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        assert recalled.result["outcome"] == status


@pytest.mark.parametrize("arguments", [{}, {"reference": "obs_bad"}, {"offset": 0},
                                      {"reference": "REF", "offset": True},
                                      {"reference": "REF", "offset": -1},
                                      {"reference": "REF", "offset": "0"},
                                      {"reference": "REF", "offset": 999999},
                                      {"reference": "REF", "owner_id": "other"},
                                      {"reference": "REF", "session_id": "other"}])
async def test_invalid_recall_arguments_never_release_content(human, bound, fixture, tmp_path, monkeypatch, arguments):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        args = {key: packed.result["reference"] if value == "REF" else value for key, value in arguments.items()}
        response = await invoke(state, arguments=args)
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("name,arguments", [("not_a_tool", {}), ("context_usage", {"owner_id": "other"}),
                                           ("delete_observation", {"reference": "REF", "offset": 0})])
async def test_unknown_and_widened_tool_requests_fail_closed(human, bound, fixture, tmp_path, monkeypatch, name, arguments):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        args = {key: packed.result["reference"] if value == "REF" else value for key, value in arguments.items()}
        response = await invoke(state, name, args)
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes > 0


@pytest.mark.parametrize("ref", ["unknown", "foreign"])
async def test_unknown_and_foreign_references_have_the_same_denial(human, bound, fixture, tmp_path, monkeypatch, ref):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        reference = "obs_" + "x" * 43
        if ref == "foreign":
            grant = replace(state.evidence._grant(state.owner, state.chat, AGENT, TOOL), owner_id="foreign")
            observation = state.archive.capture("foreign content", grant=grant, operation_id="foreign",
                                                source_args={}, outcome="success")
            reference = observation.reference
        response = await invoke(state, arguments={"reference": reference})
        assert response.error == module.EvidenceContext._denied(response.request_id).error
        assert response.result is None and response.ui_components is None


@pytest.mark.parametrize("overrides", [{"requester_verified": False}, {"agent_id": "other"},
                                       {"requester_owner": "foreign"}, {"requester_chat": "foreign"}])
async def test_private_dispatch_bindings_are_required(human, bound, fixture, tmp_path, monkeypatch, overrides):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        response = await invoke(state, arguments={"reference": packed.result["reference"]}, overrides=overrides)
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("change", ["permission", "security", "manifest", "policy", "retention", "conversation"])
async def test_recall_rechecks_source_controls_after_capture(human, bound, fixture, tmp_path, monkeypatch, change):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        if change == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        elif change == "security":
            state.host.security_flags[AGENT] = {TOOL: {"blocked": True}}
        elif change == "manifest":
            state.host.agent_cards.pop(AGENT)
        elif change == "policy":
            monkeypatch.setenv("FF_POLICY_ENGINE", "true")
            monkeypatch.setenv("POLICY_RULES", '[{"effect":"deny"}]')
        elif change == "retention":
            state.policy.write_text("[]")
        else:
            await asyncio.to_thread(state.host.history.delete_chat, state.chat, state.owner)
        response = await invoke(state, arguments={"reference": reference})
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("action", ["evidence.capture", "evidence.recall"])
async def test_permissions_revoked_while_receipt_is_awaited_never_release_content(
    human, bound, fixture, tmp_path, monkeypatch, action,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        prior = await pack(state) if action == "evidence.recall" else None
        original = state.host.audit_repo.insert
        def revoke(event):
            record = original(event)
            if event.action_type == action:
                state.host.tool_permissions.set_tool_overrides(state.owner, AGENT, {TOOL: False})
            return record
        monkeypatch.setattr(state.host.audit_repo, "insert", revoke)
        if prior is None:
            original_result = MCPResponse(request_id="capture", result=TEXT)
            response = await pack(state, original_result)
            assert response.error is not None and response.result is None
            assert state.archive.observation_count == 0
        else:
            response = await invoke(state, arguments={"reference": prior.result["reference"]})
            assert response.error is not None and response.result is None


@pytest.mark.parametrize("action", ["evidence.capture", "evidence.recall"])
async def test_phi_changes_while_receipt_is_awaited_cannot_publish_old_permitted_text(
    human, bound, fixture, tmp_path, monkeypatch, action,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        prior = await pack(state) if action == "evidence.recall" else None
        original = state.host.audit_repo.insert
        analyzer = SimpleNamespace(analyze=lambda **kwargs: [
            SimpleNamespace(start=0, end=5, entity_type="PERSON")
        ] if kwargs["text"].startswith("Beginning") else [])
        def change(event):
            record = original(event)
            if event.action_type == action:
                monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(analyzer=analyzer))
            return record
        monkeypatch.setattr(state.host.audit_repo, "insert", change)
        response = await pack(state) if prior is None else await invoke(state, arguments={"reference": prior.result["reference"]})
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("loss", ["owner", "scope", "expiry", "chain"])
async def test_current_delegated_source_authority_is_required(human, bound, fixture, tmp_path, monkeypatch, loss):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        parent = base_token(sub=state.owner, agent="requester", scopes=["tools:read"])
        packed = await pack(state, parent=parent, initiator="requester")
        assert packed.error is None and packed.result["reference"]
        assert all(call[6]["parent_token"] == parent and call[6]["initiating_agent_id"] == "requester" for call in state.calls)
        invalid = deepcopy(parent)
        if loss == "owner":
            invalid["sub"] = "foreign-owner"
        elif loss == "scope":
            invalid["scope"] = "tools:write"
        elif loss == "expiry":
            invalid["exp"] = 1
        else:
            invalid["act"] = {"sub": "agent:requester", "act": None}
        response = await invoke(state, arguments={"reference": packed.result["reference"]},
                                parent=invalid, initiator="requester")
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("loss", ["missing", "owner", "token", "cancelled", "lease"])
async def test_delivery_requires_current_authenticated_socket_and_original_lease(human, bound, fixture, tmp_path, monkeypatch, loss):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        session = state.host.ui_sessions[state.socket]
        if loss == "missing":
            state.host.ui_sessions.pop(state.socket)
        elif loss == "owner":
            session["sub"] = "foreign-owner"
        elif loss == "token":
            session.pop("_raw_token")
        elif loss == "cancelled":
            state.host.cancelled_sessions[id(state.socket)] = True
        else:
            state.lease.close()
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("loss", ["lease", "iam", "history"])
async def test_capture_never_falls_back_to_raw_response_when_current_authority_is_unavailable(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        original = MCPResponse(request_id="original", result=TEXT)
        if loss == "lease":
            state.lease.close()
        elif loss == "iam":
            from orchestrator import auth
            async def unavailable(_):
                raise RuntimeError("private-identity-diagnostic")
            monkeypatch.setattr(auth, "verify_production_token", unavailable)
        else:
            def unavailable(*args):
                raise RuntimeError("private-history-diagnostic")
            monkeypatch.setattr(state.host.history, "get_conversation_record", unavailable)
        response = await pack(state, original)
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("loss", ["owner", "permission", "policy"])
async def test_capture_refusal_receipt_cannot_outlive_the_owner_authority(human, bound, fixture, tmp_path, monkeypatch, loss):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        original = state.host.audit_repo.insert
        def revoke(event):
            receipt = original(event)
            if event.action_type == "evidence.capture_refused":
                if loss == "owner":
                    state.host.ui_sessions.pop(state.socket)
                elif loss == "permission":
                    state.host.tool_permissions.set_tool_overrides(state.owner, AGENT, {TOOL: False})
                else:
                    monkeypatch.setenv("FF_POLICY_ENGINE", "true")
                    monkeypatch.setenv("POLICY_RULES", '[{"effect":"deny"}]')
            return receipt
        monkeypatch.setattr(state.host.audit_repo, "insert", revoke)
        state.policy.write_text("[]")
        response = await pack(state)
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes == 0


async def test_capture_refusal_notice_cannot_outlive_the_owner_authority(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        async def revoke(*args, **kwargs):
            state.host.ui_sessions.pop(state.socket)
        monkeypatch.setattr(state.host, "send_ui_render", revoke)
        state.policy.write_text("[]")
        response = await pack(state)
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("parallel", [False, True])
async def test_ordinary_dispatch_rechecks_recall_after_post_tool_hooks(human, bound, fixture, tmp_path, monkeypatch, parallel):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        monkeypatch.setitem(flags._flags, "hook_system", True)
        state.host.hooks = HookManager()
        state.host._evidence_context = state.evidence
        async def guard(*args):
            return None
        async def execute(*args, **kwargs):
            return await invoke(state, arguments={"reference": reference})
        async def revoke(context):
            if context.event == HookEvent.POST_TOOL_USE and context.agent_id == "evidence-1":
                assert context.tool_result["text"] == TEXT
                await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        state.host._guard_machine_meta_tool = guard
        state.host._execute_with_retry = execute
        state.host._protected_dispatch_channel = lambda *args, **kwargs: "websocket"
        state.host.hooks.register(HookEvent.POST_TOOL_USE, revoke)
        if parallel:
            response = await Orchestrator._execute_with_retry_audited(state.host, state.socket, "evidence-1",
                "recall_observation", {"reference": reference}, state.chat, state.owner)
        else:
            call = SimpleNamespace(function=SimpleNamespace(name="recall_observation",
                                                            arguments=json.dumps({"reference": reference})))
            response = await Orchestrator.execute_single_tool(state.host, state.socket, call,
                {"recall_observation": "evidence-1"}, state.chat, state.owner)
        assert response.error is not None and response.result is None


@pytest.mark.parametrize("failure", ["audit", "notice", "both"])
async def test_authorized_retention_refusal_still_preserves_result_when_auxiliary_delivery_fails(
    human, bound, fixture, tmp_path, monkeypatch, caplog, failure,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        async def failed(*args, **kwargs):
            raise RuntimeError("private-synthetic-diagnostic")
        if failure in {"audit", "both"}:
            monkeypatch.setattr(state.evidence, "_audit", failed)
        if failure in {"notice", "both"}:
            monkeypatch.setattr(state.host, "send_ui_render", failed)
        state.policy.write_text("[]")
        original = MCPResponse(request_id="original", result=TEXT)
        assert await pack(state, original) is original
        assert state.archive.retained_bytes == 0
        assert "RuntimeError" in caplog.text and "private-synthetic-diagnostic" not in caplog.text


@pytest.mark.parametrize("rules", ["broken", "{}", '[{"effect":"unknown"}]',
                                  '[{"effect":"confirm"}]', '[{"effect":"require_token"}]',
                                  '[{"effect":"rewrite","rewrite":{"redact_args":["query"]}}]'])
async def test_malformed_or_nonallow_source_policy_fails_closed(human, bound, fixture, tmp_path, monkeypatch, rules):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        original = MCPResponse(request_id="capture", result=TEXT)
        monkeypatch.setenv("FF_POLICY_ENGINE", "true")
        monkeypatch.setenv("POLICY_RULES", rules)
        result = await pack(state, original)
        assert result.error is not None and result.result is None
        assert state.archive.retained_bytes == 0 and not state.evidence._sources


@pytest.mark.parametrize("gate_kind", ["refusal", "rewrite", "capacity"])
async def test_typed_gate_results_and_capacity_cleanup(human, bound, fixture, tmp_path, monkeypatch, gate_kind):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        async def gate(*args, **kwargs):
            if gate_kind == "refusal":
                return GateRefusal(MCPResponse(request_id="refusal", error={"code": "denied"}))
            cap = "capacity-1"
            state.host._pending_cap_entries[cap] = object()
            state.host._job_context[cap] = {}
            prepared = dict(args[3])
            if gate_kind == "rewrite":
                prepared["query"] = "different source"
            return PreparedDispatch(prepared, {}, cap, None)
        monkeypatch.setattr(state.host, "_authorize_and_prepare", gate)
        original = MCPResponse(request_id="capture", result=TEXT)
        result = await pack(state, original)
        if gate_kind == "capacity":
            assert module.transient_result(result, AGENT) and len(state.releases) == 4
        else:
            assert result.error is not None and result.result is None and state.archive.retained_bytes == 0
        assert not state.host._pending_cap_entries and not state.host._job_context
        if gate_kind == "rewrite":
            assert len(state.releases) == 2


@pytest.mark.parametrize("kind", ["capture", "recall", "all"])
async def test_missing_audit_receipt_cannot_publish_retained_content(human, bound, fixture, tmp_path, monkeypatch, kind):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        prior = await pack(state) if kind == "recall" else None
        original = state.host.audit_repo.insert
        def missing(event):
            if kind == "all" or event.action_type == f"evidence.{kind}":
                return None
            return original(event)
        monkeypatch.setattr(state.host.audit_repo, "insert", missing)
        if kind == "all":
            result = await pack(state)
            assert result.error is not None and result.result is None
            assert state.archive.retained_bytes == 0 and not state.evidence._sources
        elif prior is None:
            result = MCPResponse(request_id="capture", result=TEXT)
            response = await pack(state, result)
            assert response.error is not None and response.result is None
            assert state.archive.retained_bytes == 0
        else:
            response = await invoke(state, arguments={"reference": prior.result["reference"]})
            assert response.error is not None and response.result is None


async def test_cancelled_provisional_capture_does_not_leave_untracked_content(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        entered = asyncio.Event()
        original = state.evidence._audit
        async def hold(owner, chat, action, **kwargs):
            if action == "capture":
                entered.set()
                await asyncio.Event().wait()
            return await original(owner, chat, action, **kwargs)
        monkeypatch.setattr(state.evidence, "_audit", hold)
        task = asyncio.create_task(pack(state))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.archive.retained_bytes == state.archive.observation_count == 0
        assert not state.evidence._sources


@pytest.mark.parametrize("change", ["missing", "invalid", "expired", "rebound"])
async def test_retention_is_explicit_and_grant_changes_revoke_old_observations(human, bound, fixture, tmp_path, monkeypatch, change):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        if change == "missing":
            monkeypatch.delenv("ASTRAL_OBSERVATION_POLICY")
        elif change == "invalid":
            state.policy.write_text("malformed")
        elif change == "expired":
            state.clock.advance(timedelta(hours=3))
        else:
            contents = json.loads(state.policy.read_text())
            contents[0]["expires_at"] = (state.clock.now + timedelta(hours=1)).isoformat()
            state.policy.write_text(json.dumps(contents))
        await state.evidence.sweep()
        assert state.archive.retained_bytes == 0 and not state.evidence._sources
        response = await invoke(state, arguments={"reference": reference})
        assert response.error is not None or response.result["status"] == "unavailable"
        assert response.result is None or "text" not in response.result


@pytest.mark.parametrize("loss", ["permission", "retention", "privacy"])
@pytest.mark.parametrize("boundary", ["recall", "delivery"])
async def test_revoked_references_never_disclose_prior_source_existence(
    human, bound, fixture, tmp_path, monkeypatch, loss, boundary,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        if loss == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
            await state.evidence.sweep()
        elif loss == "retention":
            state.policy.write_text("[]")
            await state.evidence.sweep()
        else:
            analyzer = SimpleNamespace(analyze=lambda **kwargs: [
                SimpleNamespace(start=0, end=5, entity_type="PERSON")
            ] if kwargs["text"].startswith("Beginning") else [])
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(analyzer=analyzer))
            assert (await invoke(state, arguments={"reference": reference})).error is not None
        assert state.archive.retained_bytes == 0
        unknown = await invoke(state, arguments={"reference": "obs_" + "z" * 43})
        response = await invoke(state, arguments={"reference": reference}) if boundary == "recall" else await deliver(state, packed)
        assert response.error == unknown.error == module.EvidenceContext._denied(response.request_id).error
        assert response.result is None and response.ui_components is None


@pytest.mark.parametrize("state_name", ["expired", "expired_cleaned", "deleted"])
@pytest.mark.parametrize("loss", ["permission", "delegation", "retention"])
@pytest.mark.parametrize("boundary", ["recall", "delivery"])
async def test_unavailable_reference_metadata_still_requires_current_source_authority(
    human, bound, fixture, tmp_path, monkeypatch, state_name, loss, boundary,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        if state_name == "deleted":
            assert (await invoke(state, "delete_observation", {"reference": reference})).error is None
        else:
            state.clock.advance(timedelta(hours=3))
            if state_name == "expired_cleaned":
                await state.evidence.sweep()
        parent = None
        if loss == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        elif loss == "retention":
            state.policy.write_text("[]")
        else:
            from orchestrator.delegation import authorize_chained_tool_call
            parent = base_token(sub=state.owner, agent="requester", scopes=["tools:read", "tool:recall_observation"])
            assert authorize_chained_tool_call(parent, "recall_observation", "tools:read")[0]
            assert not authorize_chained_tool_call(parent, TOOL, "tools:read")[0]
        unknown = await invoke(state, arguments={"reference": "obs_" + "z" * 43},
                               parent=parent, initiator="requester" if parent else None)
        response = await invoke(state, arguments={"reference": reference},
                                parent=parent, initiator="requester" if parent else None) if boundary == "recall" else await deliver(
                                    state, packed, parent=parent, initiator="requester" if parent else None)
        assert response.error == unknown.error == module.EvidenceContext._denied(response.request_id).error
        assert response.result is None and response.ui_components is None
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("loss", ["permission", "lease", "audit_failure", "audit_missing"])
async def test_unavailable_status_waits_for_audit_and_rechecks_current_authority(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        state.clock.advance(timedelta(hours=3))
        original = state.host.audit_repo.insert
        def record(event):
            if event.action_type != "evidence.recall_refused":
                return original(event)
            if loss == "audit_failure":
                raise RuntimeError("synthetic unavailable audit")
            if loss == "audit_missing":
                return None
            receipt = original(event)
            if loss == "permission":
                state.host.tool_permissions.set_tool_overrides(state.owner, AGENT, {TOOL: False})
            else:
                state.lease.close()
            return receipt
        monkeypatch.setattr(state.host.audit_repo, "insert", record)
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error == module.EvidenceContext._denied(response.request_id).error
        assert response.result is None and response.ui_components is None
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("loss", ["permission", "delegation", "retention", "cleanup"])
async def test_unavailable_metadata_final_delivery_rechecks_its_original_source(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        parent = base_token(sub=state.owner, agent="requester", scopes=["tools:read"]) if loss == "delegation" else None
        initiator = "requester" if parent else None
        packed = await pack(state, parent=parent, initiator=initiator)
        state.clock.advance(timedelta(hours=3))
        response = await invoke(state, arguments={"reference": packed.result["reference"]}, parent=parent, initiator=initiator)
        assert response.error is None and response.result == {"status": "unavailable", "reason": "expired"}
        if loss == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        elif loss == "delegation":
            response._evidence_delivery_scope[3]["exp"] = 1
        elif loss == "retention":
            state.policy.write_text("[]")
        else:
            await state.evidence.sweep()
            assert not state.evidence._sources
        refused = await deliver(state, response, parent=parent, initiator=initiator)
        assert refused.error == module.EvidenceContext._denied(refused.request_id).error
        assert refused.result is None and refused.ui_components is None


@pytest.mark.parametrize("forged", [False, True])
async def test_unavailable_metadata_cannot_forge_or_change_the_archive_state(
    human, bound, fixture, tmp_path, monkeypatch, forged,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        state.clock.advance(timedelta(hours=3))
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error is None and response.result["reason"] == "expired"
        if forged:
            response = MCPResponse(request_id="forged", result=deepcopy(response.result))
        else:
            response.result["reason"] = "integrity_failed"
        refused = await deliver(state, response)
        assert refused.error == module.EvidenceContext._denied(refused.request_id).error
        assert refused.result is None and refused.ui_components is None


async def test_delete_and_expiry_are_explicit_and_remove_stale_source_metadata(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        first = await pack(state)
        reference = first.result["reference"]
        response = await invoke(state, "delete_observation", {"reference": reference})
        assert response.result == {"status": "deleted"} and reference not in state.evidence._sources
        assert state.archive.retained_bytes == 0
        second = await pack(state)
        state.clock.advance(timedelta(hours=3))
        unavailable = await invoke(state, arguments={"reference": second.result["reference"]})
        assert unavailable.result == {"status": "unavailable", "reason": "expired"}
        assert state.archive.retained_bytes == 0
        await state.evidence.sweep()
        assert not state.evidence._sources


async def test_cleanup_drops_every_scope_entry_before_audit_failure(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        await pack(state)
        await pack(state)
        state.policy.write_text("[]")
        async def failed(*args, **kwargs):
            raise RuntimeError("private-audit-diagnostic")
        monkeypatch.setattr(state.evidence, "_audit", failed)
        with pytest.raises(RuntimeError):
            await state.evidence.sweep()
        assert state.archive.retained_bytes == 0 and not state.evidence._sources


async def test_recalled_content_is_rechecked_under_current_phi_screening(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        analyzer = SimpleNamespace(analyze=lambda **kwargs: [
            SimpleNamespace(start=0, end=5, entity_type="PERSON")
        ] if kwargs["text"].startswith("Beginning") else [])
        monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(analyzer=analyzer))
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("boundary", ["recall_first", "deliver_preview", "deliver_first"])
async def test_newly_prohibited_late_page_revokes_the_complete_retained_source(
    human, bound, fixture, tmp_path, monkeypatch, boundary,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        marker = "Late privately classified identity"
        text = TEXT * 3 + marker + TEXT
        assert text.index(marker) > 16384 and len(text.encode()) <= 65536
        packed = await pack(state, text)
        reference = packed.result["reference"]
        prior = await invoke(state, arguments={"reference": reference}) if boundary == "deliver_first" else packed
        def analyze(**kwargs):
            start = kwargs["text"].find(marker)
            return [] if start < 0 else [SimpleNamespace(start=start, end=start + len(marker), entity_type="PERSON")]
        monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(analyzer=SimpleNamespace(analyze=analyze)))
        response = await invoke(state, arguments={"reference": reference}) if boundary == "recall_first" else await deliver(state, prior)
        assert response.error is not None and response.result is None
        assert state.archive.retained_bytes == 0 and not state.evidence._sources


@pytest.mark.parametrize("args", [{"query": "patient@example.invalid"}, {"query": "password=synthetic"}])
async def test_phi_or_credentials_in_source_arguments_refuse_shortening(human, bound, fixture, tmp_path, monkeypatch, args):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        result = MCPResponse(request_id="capture", result=TEXT)
        assert await pack(state, result, args=args) is result
        assert state.archive.retained_bytes == 0


@pytest.mark.parametrize("value", ["small", {"value": float("nan")}, {"value": object()}, None])
async def test_unsupported_or_small_results_preserve_original_response(human, bound, fixture, tmp_path, monkeypatch, value):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        result = MCPResponse(request_id="original", result=value)
        assert await pack(state, result) is result
        assert not state.calls and state.archive.retained_bytes == 0


@pytest.mark.parametrize("skip", ["disabled", "none", "error", "evidence"])
async def test_dispatch_results_that_do_not_need_packing_preserve_existing_behavior(human, bound, fixture, tmp_path, monkeypatch, skip):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        result = MCPResponse(request_id="original", result=TEXT)
        if skip == "disabled":
            monkeypatch.setitem(flags._flags, "observation_packing", False)
        elif skip == "none":
            assert await state.evidence.pack_result(None, websocket=None, owner=None, chat=None,
                agent=AGENT, tool=TOOL, args={}, operation_id="ignored") is None
            return
        elif skip == "error":
            result.error = {"code": "source_error"}
        assert await pack(state, result, agent="evidence-1" if skip == "evidence" else AGENT) is result
        assert not state.calls and state.archive.retained_bytes == 0


async def test_retained_evidence_remains_authorized_after_feature_flags_turn_off(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        state.host._evidence_context = state.evidence
        monkeypatch.setitem(flags._flags, "observation_packing", False)
        monkeypatch.setitem(flags._flags, "safe_compaction", False)
        assert not module.enabled() and module.needs_authority(state.host)
        assert module.get_context(state.host) is state.evidence
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error is None and response.result["text"] == TEXT
        await invoke(state, "delete_observation", {"reference": packed.result["reference"]})
        assert state.archive.retained_bytes == 0


async def test_context_usage_recovers_only_owner_conversation_receipts_after_restart(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        assert (await invoke(state, arguments={"reference": packed.result["reference"]})).error is None
        expected = await state.evidence.usage.totals(state.owner, state.chat)
        await state.evidence.close()
        state.evidence = module.EvidenceContext(state.host, clock=state.clock)
        response = await invoke(state, "context_usage")
        assert response.error is None and response.result == expected
        assert response.result["recall_bytes"] == len(TEXT.encode())
        missing = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert missing.error is not None and missing.result is None


@pytest.mark.parametrize("scope", ["current", "unscoped", "other"])
@pytest.mark.parametrize("loaded", [False, True])
async def test_existing_llm_audit_history_cannot_be_reported_as_complete_zero_usage(
    human, bound, fixture, tmp_path, monkeypatch, scope, loaded,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        if loaded:
            initial = await invoke(state, "context_usage")
            assert initial.error is None and initial.result["attempts"] == 0
        receipts = []
        async def record(event):
            if scope != "unscoped":
                event = event.model_copy(update={"conversation_id": state.chat if scope == "current" else str(uuid4())})
            receipts.append(await asyncio.to_thread(state.host.audit_repo.insert, event))
        await record_llm_call(SimpleNamespace(record=record), actor_user_id=state.owner,
            auth_principal=state.owner, feature="chat", credential_source=CredentialSource.USER,
            resolved=ResolvedConfig("https://provider.invalid/v1", "synthetic-model"),
            total_tokens=120, outcome="success")
        persisted = [item for item in await events(state) if item.event_id == receipts[0].event_id]
        assert len(persisted) == 1 and persisted[0].action_type == "llm.call.chat"
        assert persisted[0].outputs_meta == {"total_tokens": 120}
        response = await invoke(state, "context_usage")
        assert response.error is None
        assert response.result["attempts"] == 0
        assert all(value["known"] == 0 for value in response.result["usage"].values())
        if scope == "other":
            assert response.result["recovery_complete"] and response.result["complete"]
            assert response.result["verified_cost"]
        else:
            assert not response.result["recovery_complete"]
            assert not response.result["complete"] and not response.result["verified_cost"]
            assert "Partial accounting" in json.dumps(response.ui_components)
            assert "Complete accounting" not in json.dumps(response.ui_components)


async def test_audit_recovery_paginates_typed_public_dtos_and_filters_conversation():
    now, chat = datetime.now(UTC), str(uuid4())
    def record(conversation, action):
        return AuditEventDTO(event_id=str(uuid4()), event_class="llm_call", action_type=action,
            description="Synthetic context metadata", conversation_id=conversation,
            correlation_id=str(uuid4()), outcome="success", started_at=now, recorded_at=now)
    selected, legacy = record(chat, "context.usage"), record(chat, "llm.attempt")
    calls = []
    def page(owner, *, limit, cursor, event_classes):
        assert owner == "owner" and 1 <= limit <= 200 and event_classes == ["llm_call"]
        calls.append(cursor)
        if cursor is None:
            return [record("other", "context.usage"), legacy], "next"
        assert cursor == "next"
        return [selected], None
    evidence = module.EvidenceContext(SimpleNamespace(audit_repo=SimpleNamespace(list_for_user=page)))
    assert await evidence._recover("owner", chat) == [
        {**item.model_dump(), "actor_user_id": "owner"} for item in (legacy, selected)
    ]
    assert calls == [None, "next"]


async def test_audit_recovery_refuses_unbounded_owner_history():
    now = datetime.now(UTC)
    item = AuditEventDTO(event_id=str(uuid4()), event_class="llm_call", action_type="context.usage",
        description="Synthetic metadata", conversation_id="other", correlation_id=str(uuid4()),
        outcome="success", started_at=now, recorded_at=now)
    calls = []
    def page(owner, *, limit, cursor, event_classes):
        assert owner == "owner" and 1 <= limit <= 200
        calls.append(cursor)
        return [item] * limit, str(len(calls))
    evidence = module.EvidenceContext(SimpleNamespace(audit_repo=SimpleNamespace(list_for_user=page)))
    with pytest.raises(EvidenceDenied):
        await evidence._recover("owner", "target")
    assert len(calls) <= 8192 // 200 + 1


@pytest.mark.parametrize("change", ["permission", "security", "conversation", "expired", "removed"])
async def test_sweep_revokes_unavailable_current_sources_and_keeps_live_sources(human, bound, fixture, tmp_path, monkeypatch, change):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        await state.evidence.sweep()
        assert state.archive.retained_bytes > 0 and reference in state.evidence._sources
        if change == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_tool_overrides, state.owner, AGENT, {TOOL: False})
        elif change == "security":
            state.host.security_flags[AGENT] = {TOOL: {"blocked": True}}
        elif change == "conversation":
            await asyncio.to_thread(state.host.history.delete_chat, state.chat, state.owner)
        elif change == "expired":
            state.clock.advance(timedelta(hours=3))
        else:
            state.archive.delete(reference, owner_id=state.owner, conversation_id=state.chat, audience_id=f"user:{state.owner}")
        await state.evidence.sweep()
        assert state.archive.retained_bytes == 0 and not state.evidence._sources
        records = await events(state)
        action = "evidence.expiry_cleanup" if change == "expired" else "evidence.revocation_cleanup"
        assert change == "removed" or any(item.action_type == action for item in records)


async def test_close_tolerates_previously_deleted_observation_and_audits_without_raw_text(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        state.archive.delete(packed.result["reference"], owner_id=state.owner,
                             conversation_id=state.chat, audience_id=f"user:{state.owner}")
        await state.evidence.close()
        assert not state.evidence._sources and state.archive.retained_bytes == 0
        records = await events(state)
        assert any(item.action_type == "evidence.shutdown_cleanup" for item in records)
        assert all("Beginning preserved" not in item.model_dump_json() for item in records)


@pytest.mark.parametrize("answer", ["plain", ["plain", False], ("plain",), ("plain", 1),
                                   (None, False), ("Bearer synthetic-credential", False)])
async def test_phi_gate_result_uses_exact_real_tuple_shape(monkeypatch, answer):
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: SimpleNamespace(redact_for_storage=lambda _: answer))
    context = module.EvidenceContext(SimpleNamespace())
    with pytest.raises(module.EvidenceCaptureError):
        await context._permitted_text("plain")


async def test_actual_phi_gate_tuple_retains_only_screened_text(monkeypatch):
    gate = PHIGate(analyzer=SimpleNamespace(analyze=lambda **_: []))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    context = module.EvidenceContext(SimpleNamespace())
    permitted = await context._permitted_text("Contact patient@example.invalid for the public report.")
    assert "patient@example.invalid" not in permitted and "REDACTED" in permitted
    assert await context._permitted_text("public report") == "public report"


async def test_capture_and_recall_retain_exact_screened_text_with_only_digest_audit(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        original = "patient@example.invalid\n" + TEXT
        permitted, redacted = state.phi_gate.redact_for_storage(original)
        assert redacted and original != permitted
        packed = await pack(state, original)
        response = await invoke(state, arguments={"reference": packed.result["reference"]})
        assert response.error is None and response.result["text"] == permitted
        assert "patient@example.invalid" not in packed.result["preview"]
        assert all("patient@example.invalid" not in item.model_dump_json() for item in await events(state))


@pytest.mark.parametrize("text", [None, b"bytes", "x" * 65537, "password=synthetic", "Bearer synthetic-credential"])
async def test_privacy_limits_and_credentials_fail_before_screening(text):
    context = module.EvidenceContext(SimpleNamespace())
    with pytest.raises(module.EvidenceCaptureError):
        await context._permitted_text(text)


async def test_sweeper_reports_only_exception_class_and_shutdown_clears_memory(human, bound, fixture, tmp_path, monkeypatch, caplog):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        entered = asyncio.Event()
        async def failed():
            entered.set()
            raise RuntimeError("private-source-text")
        monkeypatch.setattr(state.evidence, "sweep", failed)
        state.evidence.start()
        same = state.evidence._sweep_task
        state.evidence.start()
        assert state.evidence._sweep_task is same
        await asyncio.wait_for(entered.wait(), 10)
        await state.evidence.close()
        assert state.evidence._sweep_task is None and not state.evidence._sources
        assert state.archive.retained_bytes == 0
        assert "RuntimeError" in caplog.text and "private-source-text" not in caplog.text
        with pytest.raises(EvidenceUnavailable):
            state.archive.inspect(packed.result["reference"], owner_id=state.owner,
                conversation_id=state.chat, audience_id=f"user:{state.owner}")


async def test_source_snapshots_and_reference_validation_fail_closed(human, bound, fixture, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        reference = packed.result["reference"]
        snapshot = state.evidence._source_states(state.owner, state.chat)
        assert snapshot[reference]["integrity"]
        state.evidence._check_sources([{"role": "tool", "content": reference}], state.owner, state.chat)
        with pytest.raises(EvidenceDenied):
            state.evidence._check_sources([{"content": "obs_bad"}], state.owner, state.chat)
        assert state.evidence._source_states("other-owner", state.chat) == {}
        changed = json.loads(state.policy.read_text())
        changed[0]["expires_at"] = (state.clock.now + timedelta(hours=1)).isoformat()
        state.policy.write_text(json.dumps(changed))
        with pytest.raises(EvidenceDenied):
            state.evidence._source_states(state.owner, state.chat)


async def test_context_service_flags_and_host_requester_bindings(monkeypatch):
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    host = SimpleNamespace()
    assert module.get_context(host) is None
    monkeypatch.setitem(flags._flags, "safe_compaction", True)
    context = module.get_context(host)
    assert context is module.get_context(host)
    parent = {"sub": "owner", "scope": "tools:read"}
    socket = object()
    with module.dispatch_requester(host, parent, "actor", "owner", "chat", socket):
        binding = module.requester_binding(host)
        parent["scope"] = "changed"
        assert binding["requester_parent"]["scope"] == "tools:read"
        assert binding["requester_websocket"] is socket and binding["requester_verified"]
        assert module.requester_binding(object()) == {"requester_verified": False}
    assert module.requester_binding(host) == {"requester_verified": False}


@pytest.mark.parametrize("message", ["/evidence", "/evidence recall obs_bad", "/evidence usage extra", "/evidence delete"])
async def test_incomplete_evidence_commands_are_not_dispatched(message):
    assert module.command(message) == ("", {})


async def test_command_and_transient_reference_boundaries():
    reference = "obs_" + "a" * 42 + "-"
    assert module.command("ordinary") is None
    assert module.command("/evidence usage") == ("context_usage", {})
    assert module.command(f"/evidence delete {reference}") == ("delete_observation", {"reference": reference})
    assert module.command(f"/evidence recall {reference} 16") == ("recall_observation", {"reference": reference, "offset": 16})
    assert module.command(f"/evidence recall {reference} 999999999") == ("", {})
    assert module.transient_result(MCPResponse(), "evidence-1")
    assert not module.transient_result(None, "evidence-1")


async def test_protected_snapshot_serialization_is_strict_stable_and_redacts_binary_content():
    class Status(Enum):
        RUNNING = "running"
    @dataclass(frozen=True)
    class Record:
        identity: object
        status: Status
        created_at: datetime
        payload: bytes
    now = datetime(2026, 1, 1, tzinfo=UTC)
    record = Record(uuid4(), Status.RUNNING, now, b"private-synthetic-payload")
    encoded = module._json({"record": record})
    assert "private-synthetic-payload" not in encoded and "byte_digest" in encoded
    assert "running" in encoded and now.isoformat() in encoded and str(record.identity) in encoded
    assert module._digest(record) == module._digest(replace(record))
    with pytest.raises(TypeError, match="context_snapshot_invalid"):
        module._json({"record": object()})
    with pytest.raises(ValueError):
        module._json({"number": float("nan")})


@pytest.mark.parametrize("value,expected", [("text", "success"), ({}, "success"),
                                          ({"status": "denied"}, "denied"),
                                          ({"status": "unexpected"}, "success"),
                                          ({"complete": False}, "incomplete"),
                                          ({"partial": True}, "incomplete")])
async def test_observation_outcome_does_not_turn_failure_or_partial_content_into_success(value, expected):
    assert module._outcome(value) == expected
