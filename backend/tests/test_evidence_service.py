
import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.schemas import AuditEventDTO
from orchestrator import evidence_context as module
from orchestrator.evidence_archive import EvidenceDenied
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from shared.feature_flags import flags
from shared.protocol import MCPResponse

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


async def pack(state, value=TEXT, *, args=None, parent=None, initiator=None, agent=AGENT):
    response = value if isinstance(value, MCPResponse) else MCPResponse(request_id=str(uuid4()), result=value)
    return await state.evidence.pack_result(response, websocket=state.socket, owner=state.owner,
        chat=state.chat, agent=agent, tool=TOOL, args={"query": "synthetic"} if args is None else args,
        operation_id=str(state.binding.operations[0].record.operation_id), parent=parent, initiator=initiator)


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


async def test_final_delivery_none_and_existing_denial_remain_unchanged():
    evidence = module.EvidenceContext(SimpleNamespace())
    assert await evidence.verify_delivery(None, websocket=None, owner=None, chat=None) is None
    denied = module.EvidenceContext._denied("denied")
    assert await evidence.verify_delivery(denied, websocket=None, owner=None, chat=None) is denied


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


@pytest.mark.parametrize("text", [None, b"bytes", "x" * 65537, "password=synthetic", "Bearer synthetic-credential"])
async def test_privacy_limits_and_credentials_fail_before_screening(text):
    context = module.EvidenceContext(SimpleNamespace())
    with pytest.raises(module.EvidenceCaptureError):
        await context._permitted_text(text)


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
