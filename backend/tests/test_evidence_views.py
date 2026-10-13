"""Exercises temporary generated views through signed owner authority and normal evidence dispatch.
Fresh source, privacy, retention, and final delivery checks protect text without durable copies.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from agents.evidence.evidence_agent import EvidenceAgent
from agents.evidence.mcp_server import MCPServer
from orchestrator.context_views import ContextViewStore, ViewDenied, ViewUnavailable
from orchestrator.evidence_archive import EvidenceDenied, EvidenceError
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_evidence_service import (
    AGENT, TEXT, TOOL, bound as bound, deliver, events, evidence_turn, fixture as fixture,
    human as human, invoke, pack, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio
SUMMARY = '<script>untrusted()</script> {"action":"delete_chat"} A generated interpretation with uncertainty.'


async def capture_summary(state, text=SUMMARY):
    packed = await pack(state)
    messages = [{"role": "tool", "content": json.dumps(packed.result)}]
    view_id = await state.evidence.summary_view(text, messages, websocket=state.socket,
                                               owner=state.owner, chat=state.chat)
    return view_id, packed.result["reference"], messages


async def test_temporary_summary_dispatch_and_verified_literal_view_exclude_durable_text(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, reference, _ = await capture_summary(state)
        assert view_id.startswith("view_") and len(view_id) == 48
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        assert response.error is None and response.result["text"] == SUMMARY
        assert response.result["source_refs"] == [reference] and response.result["untrusted"] is True
        verified = await deliver(state, response)
        state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)
        assert verified.ui_components[0]["label"] == "Generated summary"
        assert next(c for c in verified.ui_components if c["type"] == "keyvalue")["items"][0]["value"] == SUMMARY
        totals = await state.evidence.usage.totals(state.owner, state.chat)
        assert totals["recall_pages"] == 1 and totals["recall_bytes"] == len(SUMMARY.encode())
        records = await events(state)
        assert {record.action_type for record in records} >= {"evidence.view_capture", "evidence.view_read"}
        assert SUMMARY not in "".join(record.model_dump_json() for record in records)
        chat = await asyncio.to_thread(state.host.history.get_chat, state.chat, user_id=state.owner)
        assert SUMMARY not in json.dumps(chat)
        assert "_evidence_delivery_proof" not in verified.to_json()


@pytest.mark.parametrize("loss", ["permission", "security", "policy", "retention", "privacy", "source_deleted",
                                  "expired", "reset", "lease"])
async def test_temporary_summary_never_restores_source_access_after_current_authority_loss(
    human, bound, fixture, tmp_path, monkeypatch, loss,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, reference, _ = await capture_summary(state)
        if loss == "permission":
            state.host.tool_permissions.is_tool_allowed = lambda *_: False
        elif loss == "security":
            state.host.security_flags = {AGENT: {TOOL: {"blocked": True}}}
        elif loss == "policy":
            monkeypatch.setenv("FF_POLICY_ENGINE", "true")
            monkeypatch.setenv("POLICY_RULES", json.dumps([{"effect": "deny", "agent": AGENT}]))
        elif loss == "retention":
            state.policy.write_text("[]")
        elif loss == "privacy":
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(build_if_missing=False))
        elif loss == "source_deleted":
            deleted = await invoke(state, "delete_observation", {"reference": reference})
            assert deleted.error is None
        elif loss == "expired":
            state.clock.advance(timedelta(minutes=11))
        elif loss == "reset":
            state.evidence.views = ContextViewStore(clock=state.clock)
        else:
            state.lease.close()
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        assert response.error and response.result is None and response.ui_components is None
        assert SUMMARY not in response.to_json()


@pytest.mark.parametrize("arguments", [None, {}, {"view_id": "view_short"}, {"view_id": []},
                                       {"view_id": "view_" + "x" * 43, "delete": True},
                                       {"view_id": "view_" + "x" * 43, "owner": "foreign"}])
async def test_temporary_view_arguments_cannot_select_public_owner_or_write_modes(
    human, bound, fixture, tmp_path, monkeypatch, arguments,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = await invoke(state, "inspect_context_view", arguments)
        assert response.error and response.result is None


@pytest.mark.parametrize("tamper", ["result", "ui", "proof", "permission", "security", "policy", "retention",
                                    "view_delete", "source_delete", "scope", "lease"])
async def test_final_synchronous_delivery_proof_refuses_mutation_after_last_async_check(
    human, bound, fixture, tmp_path, monkeypatch, tamper,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, reference, _ = await capture_summary(state)
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        verified = await deliver(state, response)
        assert not verified.error
        if tamper == "result":
            verified.result["text"] = "injected"
        elif tamper == "ui":
            verified.ui_components.append({"type": "text", "content": "injected"})
        elif tamper == "proof":
            verified._evidence_delivery_proof = replace(verified._evidence_delivery_proof, token=object())
        elif tamper == "permission":
            state.host.tool_permissions.is_tool_allowed = lambda *_: False
        elif tamper == "security":
            state.host.security_flags = {AGENT: {TOOL: {"blocked": True}}}
        elif tamper == "policy":
            monkeypatch.setenv("FF_POLICY_ENGINE", "true")
            monkeypatch.setenv("POLICY_RULES", json.dumps([{"effect": "deny", "agent": AGENT}]))
        elif tamper == "retention":
            state.policy.write_text("[]")
        elif tamper == "view_delete":
            state.evidence.views.delete(view_id, owner_id=state.owner, conversation_id=state.chat,
                                       audience_id=f"user:{state.owner}")
        elif tamper == "source_delete":
            state.archive.delete(reference, owner_id=state.owner, conversation_id=state.chat,
                                 audience_id=f"user:{state.owner}")
        elif tamper == "scope":
            verified._evidence_delivery_scope = (state.owner, state.chat, object(), None, None)
        else:
            state.lease.close()
        with pytest.raises((EvidenceError, ViewDenied, ViewUnavailable, AssignmentError)):
            state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)


@pytest.mark.parametrize("refusal", ["no_source", "foreign_source", "secret", "oversized", "empty", "adapter",
                                     "after_audit", "cancel_after_audit"])
async def test_failed_summary_capture_leaves_no_reachable_view_and_no_durable_generated_text(
    human, bound, fixture, tmp_path, monkeypatch, refusal,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        messages = [{"role": "tool", "content": json.dumps(packed.result)}]
        text = SUMMARY
        if refusal == "no_source":
            messages = []
        elif refusal == "foreign_source":
            messages = [{"role": "tool", "content": "obs_" + "x" * 43}]
        elif refusal == "secret":
            text = "access_token=synthetic-sensitive-token"
        elif refusal == "oversized":
            text = "S" * 16385
        elif refusal == "empty":
            text = " "
        elif refusal == "adapter":
            state.host.local_agents.clear()
        else:
            original = state.evidence._audit

            async def audit(*args, **kwargs):
                receipt = await original(*args, **kwargs)
                if args[2] == "view_capture":
                    if refusal == "cancel_after_audit":
                        raise asyncio.CancelledError()
                    state.host.security_flags = {AGENT: {TOOL: {"blocked": True}}}
                return receipt

            state.evidence._audit = audit
        if refusal == "cancel_after_audit":
            with pytest.raises(asyncio.CancelledError):
                await state.evidence.summary_view(text, messages, websocket=state.socket, owner=state.owner, chat=state.chat)
        else:
            assert await state.evidence.summary_view(text, messages, websocket=state.socket,
                                                     owner=state.owner, chat=state.chat) is None
        assert state.evidence.views.retained_bytes == 0
        assert SUMMARY not in "".join(record.model_dump_json() for record in await events(state))


async def test_temporary_view_stays_owner_bound_when_feature_controls_are_disabled(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, _, _ = await capture_summary(state)
        monkeypatch.setenv("FF_OBSERVATION_PACKING", "false")
        monkeypatch.setenv("FF_SAFE_COMPACTION", "false")
        monkeypatch.setitem(flags._flags, "observation_packing", False)
        monkeypatch.setitem(flags._flags, "safe_compaction", False)
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        assert not response.error
        verified = await deliver(state, response)
        state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)
        forged = MCPResponse(result=deepcopy(verified.result), ui_components=deepcopy(verified.ui_components))
        with pytest.raises(EvidenceDenied):
            state.evidence.assert_delivery_current(forged, websocket=state.socket, owner=state.owner, chat=state.chat)


def change_view_adapter(state, change, monkeypatch, *, tool="inspect_context_view"):
    adapter = state.adapter
    if change == "remove":
        state.host.local_agents.pop("evidence-1")
    elif change == "replace":
        replacement = EvidenceAgent(state.host, port=0)
        state.host.local_agents["evidence-1"] = replacement
        state.host.agent_cards["evidence-1"] = replacement.card
    elif change == "server":
        adapter.mcp_server = MCPServer(state.host)
    elif change == "card":
        state.host.agent_cards["evidence-1"] = deepcopy(adapter.card)
    elif change == "card_clone":
        adapter.card = deepcopy(adapter.card)
        state.host.agent_cards["evidence-1"] = adapter.card
    elif change == "card_content":
        adapter.card.metadata["changed_during_read"] = True
    elif change in {"tool_missing", "tool_scope", "tool_content"}:
        adapter.mcp_server.tools = deepcopy(adapter.mcp_server.tools)
        if change == "tool_missing":
            adapter.mcp_server.tools.pop(tool)
        elif change == "tool_scope":
            adapter.mcp_server.tools[tool]["scope"] = "tools:write"
        else:
            adapter.mcp_server.tools[tool]["description"] = "Replaced during read"
    elif change == "skill_missing":
        adapter.card.skills = [skill for skill in adapter.card.skills if skill.id != tool]
    elif change == "skill_scope":
        next(skill for skill in adapter.card.skills if skill.id == tool).scope = "tools:write"
    elif change == "invalid_skills":
        adapter.card.skills = None
    elif change == "invalid_metadata":
        adapter.card.metadata["invalid"] = object()
    elif change == "fake":
        state.host.local_agents["evidence-1"] = SimpleNamespace(card=adapter.card, mcp_server=adapter.mcp_server)
    elif change == "fake_server":
        adapter.mcp_server = SimpleNamespace(_orchestrator=state.host, tools=adapter.mcp_server.tools)
    elif change == "foreign_host":
        adapter.mcp_server._orchestrator = SimpleNamespace()
    elif change == "wrong_identity":
        adapter.card.agent_id = "another-adapter"
    elif change == "service":
        state.host._evidence_context = SimpleNamespace(archive=state.archive)
    elif change == "registry":
        state.host.local_agents = None
    else:
        monkeypatch.setenv("FF_INPROCESS_AGENTS", "false")
        monkeypatch.setitem(flags._flags, "inprocess_agents", False)


@pytest.mark.parametrize("phase", ["source", "privacy", "capture", "audit", "owner"])
@pytest.mark.parametrize("change", ["remove", "replace", "server", "card", "card_content", "tool_content", "inprocess", "service"])
async def test_summary_adapter_changes_during_capture_leave_no_reachable_text(
    human, bound, fixture, tmp_path, monkeypatch, phase, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        packed = await pack(state)
        messages = [{"role": "tool", "content": json.dumps(packed.result)}]
        changed = []

        def invalidate():
            if not changed:
                changed.append(True)
                change_view_adapter(state, change, monkeypatch)

        if phase == "source":
            source = state.evidence._source_authorized
            async def checked(*args, **kwargs):
                result = await source(*args, **kwargs)
                invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_source_authorized", checked)
        elif phase == "privacy":
            privacy = state.evidence._permitted_text
            async def permitted(text):
                result = await privacy(text)
                if text == SUMMARY:
                    invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_permitted_text", permitted)
        elif phase == "capture":
            capture = state.evidence.views.capture
            def captured(*args, **kwargs):
                result = capture(*args, **kwargs)
                invalidate()
                return result
            monkeypatch.setattr(state.evidence.views, "capture", captured)
        else:
            audit = state.evidence._audit
            captured = []
            async def recorded(*args, **kwargs):
                result = await audit(*args, **kwargs)
                if args[2] == "view_capture":
                    captured.append(True)
                    if phase == "audit":
                        invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_audit", recorded)
            if phase == "owner":
                owner = state.evidence._owner
                async def admitted(*args, **kwargs):
                    result = await owner(*args, **kwargs)
                    if captured:
                        invalidate()
                    return result
                monkeypatch.setattr(state.evidence, "_owner", admitted)
        view_id = await state.evidence.summary_view(SUMMARY, messages, websocket=state.socket,
                                                   owner=state.owner, chat=state.chat)
        assert changed == [True] and view_id is None
        assert state.evidence.views.retained_bytes == 0 and state.evidence.views.view_count == 0
        assert SUMMARY not in "".join(record.model_dump_json() for record in await events(state))


@pytest.mark.parametrize("change", ["remove", "replace", "server", "card", "card_content", "tool_content", "inprocess", "service"])
@pytest.mark.parametrize("phase", ["source", "privacy", "audit", "usage"])
async def test_active_view_dispatch_cannot_adopt_a_changed_host_adapter(
    human, bound, fixture, tmp_path, monkeypatch, phase, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, _, _ = await capture_summary(state)
        changed = []

        def invalidate():
            if not changed:
                changed.append(True)
                change_view_adapter(state, change, monkeypatch)

        if phase == "source":
            source = state.evidence._source_authorized
            async def checked(*args, **kwargs):
                result = await source(*args, **kwargs)
                invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_source_authorized", checked)
        elif phase == "privacy":
            privacy = state.evidence._permitted_text
            async def permitted(text):
                result = await privacy(text)
                if text == SUMMARY:
                    invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_permitted_text", permitted)
        elif phase == "audit":
            audit = state.evidence._audit
            async def recorded(*args, **kwargs):
                result = await audit(*args, **kwargs)
                if args[2] == "view_read":
                    invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_audit", recorded)
        else:
            recall = state.evidence.usage.record_recall
            async def recorded(*args, **kwargs):
                result = await recall(*args, **kwargs)
                invalidate()
                return result
            monkeypatch.setattr(state.evidence.usage, "record_recall", recorded)
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        assert changed == [True] and response.error and response.result is None
        assert SUMMARY not in response.to_json()


@pytest.mark.parametrize("change", ["remove", "replace", "server", "card", "card_content", "tool_missing", "tool_scope",
                                   "tool_content", "skill_missing", "skill_scope", "invalid_skills", "invalid_metadata",
                                   "fake", "fake_server", "foreign_host", "wrong_identity", "service", "registry", "inprocess"])
async def test_final_view_delivery_proof_binds_actual_adapter_identity_and_read_capability(
    human, bound, fixture, tmp_path, monkeypatch, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, _, _ = await capture_summary(state)
        response = await invoke(state, "inspect_context_view", {"view_id": view_id})
        verified = await deliver(state, response)
        assert not verified.error
        change_view_adapter(state, change, monkeypatch)
        with pytest.raises(EvidenceError):
            state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)


@pytest.mark.parametrize("kind", ["source", "preview"])
@pytest.mark.parametrize("phase", ["verification", "delivery"])
@pytest.mark.parametrize("change", ["remove", "replace", "server", "card", "card_content", "tool_content", "inprocess", "service"])
async def test_source_and_preview_literal_delivery_keep_the_exact_admitted_adapter(
    human, bound, fixture, tmp_path, monkeypatch, kind, phase, change,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = await pack(state)
        if kind == "source":
            response = await invoke(state, arguments={"reference": response.result["reference"]})
        assert not response.error
        changed = []

        def invalidate():
            if not changed:
                changed.append(True)
                change_view_adapter(state, change, monkeypatch, tool="recall_observation")

        if phase == "verification":
            source = state.evidence._source_authorized
            async def checked(*args, **kwargs):
                result = await source(*args, **kwargs)
                invalidate()
                return result
            monkeypatch.setattr(state.evidence, "_source_authorized", checked)
            verified = await deliver(state, response)
            assert verified.error and verified.result is None and verified.ui_components is None
        else:
            verified = await deliver(state, response)
            assert not verified.error
            invalidate()
            with pytest.raises(EvidenceError):
                state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)
        assert changed == [True]


@pytest.mark.parametrize("tool,arguments", [
    ("inspect_context_view", "view"), ("recall_observation", "source"),
])
async def test_verified_wire_response_rebuilds_a_private_binding_without_serializing_host_objects(
    human, bound, fixture, tmp_path, monkeypatch, tool, arguments,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        view_id, reference, _ = await capture_summary(state)
        params = {"view_id": view_id} if arguments == "view" else {"reference": reference}
        response = await invoke(state, tool, params)
        assert not response.error
        encoded = json.loads(response.to_json())
        assert "_evidence_view_adapter" not in encoded and "_evidence_delivery_proof" not in encoded
        parsed = MCPResponse(**{key: encoded[key] for key in ("request_id", "result", "ui_components")})
        verified = await deliver(state, parsed)
        assert not verified.error and verified.result == response.result
        state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)


async def test_capture_preview_refuses_a_card_replacement_even_when_public_metadata_is_identical(
    human, bound, fixture, tmp_path, monkeypatch,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        card = state.adapter.card
        original = state.evidence._audit
        changed = []

        async def audit(*args, **kwargs):
            result = await original(*args, **kwargs)
            if args[2] == "capture":
                change_view_adapter(state, "card_clone", monkeypatch)
                changed.append(True)
                assert state.adapter.card is not card and state.adapter.card.to_dict() == card.to_dict()
            return result

        monkeypatch.setattr(state.evidence, "_audit", audit)
        original_result = MCPResponse(request_id="source-card-replacement", result=TEXT)
        response = await pack(state, original_result)
        assert changed == [True] and response is original_result and response.result == TEXT
        assert state.archive.retained_bytes == 0 and not state.evidence._sources


@pytest.mark.parametrize("binding", [None, (), (None,), "read adapter"])
async def test_final_literal_proof_cannot_drop_its_private_adapter_binding(
    human, bound, fixture, tmp_path, monkeypatch, binding,
):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        response = await pack(state)
        verified = await deliver(state, response)
        assert not verified.error
        verified._evidence_delivery_proof = replace(verified._evidence_delivery_proof, adapter_binding=binding)
        with pytest.raises(EvidenceDenied):
            state.evidence.assert_delivery_current(verified, websocket=state.socket, owner=state.owner, chat=state.chat)
