"""Exercises the host compaction proposal and its final source/provider authority checks.
Core compaction and real inherited authority suites independently verify the pure algorithm and signed Plane leases.
"""

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from orchestrator import evidence_context as module
from orchestrator.context_budget import ContextBudget
from orchestrator.evidence_archive import EvidenceDenied, RetentionGrant
from orchestrator.safe_compaction import SUMMARY_PREFIX, estimate_context_tokens
from shared.feature_flags import flags


@dataclass(frozen=True)
class Config:
    owner_id: str = "owner"
    provider: str = "custom"
    base_url: str = "https://model.example/v1"
    model: str = "model"
    api_key_ciphertext: str = "ciphertext"


class Capture:
    def __init__(self):
        self._record = Config()
        self.owner_id = self._record.owner_id

    def matches(self, record):
        return self._record == record


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setitem(flags._flags, "safe_compaction", True)
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    capture = Capture()
    audits, calls, protected_reads, guards = [], [], [], []
    state = {"authorized": True, "authority": "original"}

    async def captured(owner):
        return capture

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Earlier discussion retained as evidence."))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    orch = SimpleNamespace(_llm_store=SimpleNamespace(capture_user=captured,
        open_captured_user_key=lambda _capture: "ephemeral-key"),
        _CredentialSource=SimpleNamespace(USER="user"),
        _build_llm_client=lambda config, source: (client, source, SimpleNamespace(model=config.model, base_url=config.base_url)))
    usage = SimpleNamespace()

    async def model_call(owner, chat, purpose, model, invoke):
        assert (owner, chat, purpose, model) == ("owner", "chat", "safe_compaction", "model")
        return await invoke()

    usage.model_call = model_call
    service = module.EvidenceContext(orch, usage=usage)

    async def audit(owner, chat, action, **kwargs):
        audits.append((owner, chat, action, kwargs))

    async def protected(*args):
        protected_reads.append(copy.deepcopy(state))
        return copy.deepcopy(state)

    async def permitted(text):
        return text

    async def budget(owner, selected=None):
        return ContextBudget(11000, 200, "max_completion_tokens", "budget")

    async def prepare_model(**kwargs):
        assert kwargs["provider_capture"] is capture
        assert kwargs["base_url"] == capture._record.base_url
        async def guard():
            guards.append(kwargs)
        return guard

    service._audit = audit
    service._protected = protected
    service._permitted_text = permitted
    service.budget = budget
    service.prepare_model = prepare_model
    return SimpleNamespace(service=service, capture=capture, audits=audits, calls=calls,
                           state=state, protected_reads=protected_reads, guards=guards, client=client)


def history():
    result = [{"role": "system", "content": "Immutable host policy."}]
    for index in range(12):
        result.append({"role": "user" if index % 2 == 0 else "assistant",
                       "content": (f"Earlier record {index}. " + "x" * 1000)})
    result.append({"role": "user", "content": "Current goal and constraints."})
    return result


async def prepare(host, messages=None, **kwargs):
    messages = history() if messages is None else messages
    return await host.service.prepare(messages, websocket="socket", owner="owner", chat="chat", **kwargs)


@pytest.mark.asyncio
async def test_success_preserves_original_and_bounds_owner_auxiliary_call(host):
    messages = history()
    original = copy.deepcopy(messages)
    result = await prepare(host, messages)
    assert result.status == "accepted" and result.after_tokens < result.before_tokens
    assert messages == original
    assert result.messages[0] == original[0] and result.messages[-1] == original[-1]
    assert any(item["content"].startswith(SUMMARY_PREFIX) for item in result.messages)
    assert len(host.calls) == len(host.guards) == 1
    request = host.calls[0]
    assert request["model"] == "model" and request["max_completion_tokens"] == 200
    assert estimate_context_tokens(request["messages"]) + 200 <= 11000
    assert len(host.protected_reads) >= 4
    assert host.audits[-1][2] == "compaction_proposal_validated"


@pytest.mark.asyncio
async def test_in_budget_never_allocates_auxiliary_call(host):
    messages = [{"role": "user", "content": "Small input."}]
    result = await prepare(host, messages)
    assert result.status == "unchanged" and result.messages is messages
    assert host.calls == host.guards == host.protected_reads == host.audits == []


@pytest.mark.asyncio
async def test_unsupported_history_and_unqualified_route_retain_input(host):
    for messages in ([object()], [{"role": "user", "content": "\ud800"}]):
        result = await prepare(host, messages)
        assert result.status == "context_limit" and result.reason == "unsupported_history"
        assert result.messages is messages
    async def denied(*args):
        raise ValueError("Unavailable operator qualification")
    host.service.budget = denied
    messages = history()
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.reason == "context_budget_unconfigured"
    assert result.messages is messages and host.calls == []


@pytest.mark.asyncio
async def test_packing_only_and_reserved_work_cannot_create_unreserved_summary(host, monkeypatch):
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    assert (await prepare(host)).reason == "protected_context_limit"
    monkeypatch.setitem(flags._flags, "safe_compaction", True)
    monkeypatch.setattr("persistent_agents.dispatch_context.current_dispatch", lambda: object())
    result = await prepare(host)
    assert result.reason == "auxiliary_reservation_unavailable" and host.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_capture", "foreign_capture", "source", "model", "endpoint", "protected", "audit"])
async def test_preproposal_failures_cannot_dispatch_or_replace(host, failure):
    if failure == "missing_capture":
        async def missing(_owner):
            return None
        host.service.orchestrator._llm_store.capture_user = missing
    elif failure == "foreign_capture":
        host.capture.owner_id = "foreign"
    elif failure in {"source", "model", "endpoint"}:
        original_factory = host.service.orchestrator._build_llm_client
        def factory(config, source):
            client, source, resolved = original_factory(config, source)
            if failure == "source":
                source = "system"
            elif failure == "model":
                resolved.model = "alias"
            else:
                resolved.base_url = "https://other.example/v1"
            return client, source, resolved
        host.service.orchestrator._build_llm_client = factory
    elif failure == "protected":
        async def protected(*args):
            raise EvidenceDenied()
        host.service._protected = protected
    else:
        async def audit(*args, **kwargs):
            raise RuntimeError("Receipt unavailable")
        host.service._audit = audit
    messages = history()
    snapshot = copy.deepcopy(messages)
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.messages is messages and messages == snapshot
    assert host.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", " ", "\x00", "x" * 18000])
async def test_invalid_summary_leaves_original_complete(host, content):
    def create(**kwargs):
        host.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    host.client.chat.completions.create = create
    messages = history()
    original = copy.deepcopy(messages)
    result = await prepare(host, messages)
    assert result.status in {"rejected", "context_limit"} and result.messages is messages
    assert messages == original


@pytest.mark.asyncio
async def test_provider_and_privacy_failures_do_not_replace(host):
    async def refused(text):
        return "redacted"
    host.service._permitted_text = refused
    messages = history()
    assert (await prepare(host, messages)).status == "context_limit"
    assert host.calls == []
    async def permitted(text):
        return text
    host.service._permitted_text = permitted
    def unavailable(**kwargs):
        raise RuntimeError("Synthetic provider unavailable")
    host.client.chat.completions.create = unavailable
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.messages is messages


@pytest.mark.asyncio
async def test_malformed_provider_response_and_final_receipt_failure_retain_input(host):
    host.client.chat.completions.create = lambda **kwargs: SimpleNamespace(choices=[])
    messages = history()
    assert (await prepare(host, messages)).status == "context_limit"
    host.client.chat.completions.create = lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Evidence summary."))])
    async def audit(owner, chat, action, **kwargs):
        if action == "compaction_proposal_validated":
            raise RuntimeError("Receipt unavailable")
        host.audits.append((owner, chat, action, kwargs))
    host.service._audit = audit
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.messages is messages


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["host", "history", "reference"])
async def test_last_receipt_wait_cannot_install_stale_candidate(host, stale):
    messages = history()
    original_audit = host.service._audit
    async def audit(owner, chat, action, **kwargs):
        await original_audit(owner, chat, action, **kwargs)
        if action == "compaction_proposal_validated":
            if stale == "host":
                host.state["authority"] = "changed"
            elif stale == "history":
                messages[-1]["content"] = "Changed goal."
            else:
                def reject(*args):
                    raise EvidenceDenied()
                host.service._check_sources = reject
    host.service._audit = audit
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.messages is messages


@pytest.mark.asyncio
async def test_current_exact_references_and_malformed_handles_are_checked(host):
    grant = RetentionGrant("owner", "chat", "user:owner", "source-1", "read", datetime(2090, 1, 1, tzinfo=UTC))
    observation = host.service.archive.capture("Exact source", grant=grant, operation_id="op", source_args={}, outcome="success")
    host.service._grant = lambda *args: grant
    async def authorized(*args, **kwargs):
        return grant
    host.service._source_authorized = authorized
    messages = history()
    messages[1]["content"] += " " + observation.reference
    result = await prepare(host, messages)
    assert result.status == "accepted"
    messages = history()
    messages[1]["content"] += " obs_invalid"
    result = await prepare(host, messages)
    assert result.status == "context_limit" and result.messages is messages


@pytest.mark.asyncio
async def test_cancellation_propagates_without_replacing_history(host):
    entered = asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    host.service.prepare_model = blocked
    messages = history()
    original = copy.deepcopy(messages)
    task = asyncio.create_task(prepare(host, messages))
    async with asyncio.timeout(5):
        await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert messages == original and host.calls == []


@pytest.mark.parametrize("value", [object(), {"value": object()}, float("nan")])
def test_snapshot_serializer_rejects_unknown_or_noncanonical_authority(value):
    with pytest.raises((TypeError, ValueError)):
        module._digest(value)


def test_snapshot_serializer_handles_nested_typed_projections():
    from enum import Enum
    from uuid import UUID
    class State(Enum):
        CURRENT = "current"
    value = {"record": Config(), "state": State.CURRENT, "identity": UUID(int=1),
             "time": datetime(2026, 1, 1, tzinfo=UTC), "blob": b"private"}
    serialized = module._json(value)
    assert json.loads(serialized)["state"] == "current" and "private" not in serialized
    assert module._digest(value) == module._digest(value)
