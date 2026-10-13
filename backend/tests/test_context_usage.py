"""Exercises conversation usage accounting through recorded physical attempts and
late provider settlement, including unknown charges, replay, and owner isolation.
The synthetic recorder stands in for the application's durable audit callback.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from audit.schemas import AuditEventCreate
from orchestrator.context_usage import ContextUsage, ContextUsageError

PRICE = {
    "input_per_million": "2", "output_per_million": "4",
    "cached_input_per_million": "1", "date": "2026-10-08", "currency": "USD",
}


def response(prompt=100, completion=20, cached=30, *, total=None):
    return SimpleNamespace(
        content="synthetic private response",
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            total_tokens=prompt + completion if total is None else total,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


class Audit:
    def __init__(self):
        self.events = []
        self.unavailable = False

    async def record(self, event):
        if self.unavailable:
            raise RuntimeError("synthetic recorder unavailable")
        self.events.append(event.model_copy(deep=True))

    async def recover(self, owner, conversation):
        return [event.model_copy(deep=True) for event in self.events
                if event.actor_user_id == owner and event.conversation_id == conversation]


@pytest.fixture
def audit():
    return Audit()


@pytest.fixture
def ledger(audit):
    return ContextUsage(audit.record, audit.recover)


async def call(ledger, result=None, **kwargs):
    async def invoke():
        return response() if result is None else result

    return await ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                                   pricing=PRICE, **kwargs)


async def test_attempt_is_recorded_before_dispatch_and_raw_result_validation(ledger, audit):
    malformed = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                           "total_tokens": 120, "cached_tokens": 30},
                 "unexpected_sensitive_payload": "never record this"}

    async def invoke():
        assert [event.outputs_meta["usage_record"]["phase"] for event in audit.events] == ["begin", "pending"]
        return malformed

    assert await ledger.model_call("alice", "conversation", "summary", "model-a", invoke,
                                   pricing=PRICE) is malformed
    total = await ledger.totals("alice", "conversation")
    assert total["attempts"] == total["succeeded"] == 1
    assert total["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert total["known_cost_by_currency"] == {"USD": "0.00025"}
    assert total["complete"] and total["verified_cost"]
    assert total["by_purpose"]["summary"] == 1
    assert len({event.event_id for event in audit.events}) == 3
    serialized = "\n".join(event.model_dump_json() for event in audit.events)
    assert "never record this" not in serialized and "unexpected_sensitive_payload" not in serialized


async def test_charged_exception_and_retry_reconcile_once(ledger, audit):
    failed_id = str(uuid4())
    error = RuntimeError("private upstream error containing a credential")
    error.usage = response(prompt=8, completion=2, cached=0).usage

    async def failed():
        raise error

    with pytest.raises(RuntimeError) as caught:
        await ledger.model_call("alice", "conversation", "direct", "model-a", failed,
                                pricing=PRICE, attempt_id=failed_id)
    assert caught.value is error
    await call(ledger, response(prompt=10, completion=5, cached=0), retry_of=failed_id)
    totals = await ledger.totals("alice", "conversation")
    assert totals["attempts"] == 2 and totals["failed"] == totals["succeeded"] == 1
    assert totals["retries"] == totals["charged_failed"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 25, "unknown": 0}
    assert totals["known_cost_by_currency"] == {"USD": "0.000064"}
    assert "private upstream error" not in "\n".join(e.model_dump_json() for e in audit.events)


async def test_caller_cancellation_retains_provider_and_late_charge(ledger, audit):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def invoke():
        entered.set()
        await finish.wait()
        return response()

    task = asyncio.create_task(ledger.model_call("alice", "conversation", "compaction", "model-a",
                                               invoke, pricing=PRICE))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = await ledger.totals("alice", "conversation")
    assert pending["cancelled"] == pending["pending"] == 1
    assert pending["usage"]["total_tokens"] == {"known": 0, "unknown": 1}
    assert not pending["verified_cost"]
    finish.set()
    await ledger.drain()
    settled = await ledger.totals("alice", "conversation")
    assert settled["pending"] == 0 and settled["cancelled"] == settled["charged_cancelled"] == 1
    assert settled["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert settled["complete"]
    assert [e.outputs_meta["usage_record"]["phase"] for e in audit.events] == ["begin", "pending", "cancelled", "settled"]


async def test_provider_cancellation_has_its_own_known_charge(ledger):
    error = asyncio.CancelledError()
    error.usage = response(prompt=10, completion=0, cached=0).usage

    async def invoke():
        raise error

    with pytest.raises(asyncio.CancelledError):
        await ledger.model_call("alice", "conversation", "summary", "model-a", invoke, pricing=PRICE)
    total = await ledger.totals("alice", "conversation")
    assert total["provider_cancelled"] == 1 and total["cancelled"] == 0
    assert total["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
    assert total["pending"] == 0


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "7"},
                                 {"prompt_tokens": 3.5, "completion_tokens": float("inf"), "total_tokens": 2**64}])
async def test_missing_or_malformed_usage_is_unknown(ledger, usage):
    await call(ledger, {"usage": usage})
    total = await ledger.totals("alice", "conversation")
    assert all(field == {"known": 0, "unknown": 1} for field in total["usage"].values())
    assert total["unknown_usage"] == total["unknown_cost"] == 1
    assert not total["complete"] and not total["verified_cost"]


async def test_partial_cache_and_inconsistent_totals_never_claim_completeness(ledger):
    await call(ledger, {"usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 99}})
    total = await ledger.totals("alice", "conversation")
    assert total["usage"]["prompt_tokens"] == {"known": 8, "unknown": 0}
    assert total["usage"]["total_tokens"] == {"known": 0, "unknown": 1}
    assert total["usage"]["cached_tokens"]["unknown"] == 1 and not total["complete"]


async def test_impossible_cache_charge_is_unknown_and_zero_is_explicit(ledger):
    await call(ledger, response(prompt=1, completion=0, cached=2))
    await call(ledger, response(prompt=0, completion=0, cached=0))
    total = await ledger.totals("alice", "conversation")
    assert total["usage"]["cached_tokens"] == {"known": 0, "unknown": 1}
    assert total["unknown_cost"] == 1
    assert total["known_cost_by_currency"] == {"USD": "0"}


@pytest.mark.parametrize("pricing", [None, {}, {**PRICE, "input_per_million": -1},
                                   {**PRICE, "output_per_million": "NaN"},
                                   {**PRICE, "date": "tomorrow"}, {**PRICE, "currency": "usd"},
                                   {**PRICE, "secret": "never retain this"}])
async def test_missing_or_untrusted_pricing_is_unknown(audit, pricing):
    ledger = ContextUsage(audit.record)

    async def invoke():
        return response()

    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke, pricing=pricing)
    totals = await ledger.totals("alice", "conversation")
    assert totals["unknown_cost"] == 1 and not totals["verified_cost"]
    assert "never retain this" not in "".join(e.model_dump_json() for e in audit.events)


async def test_explicit_uncached_usage_does_not_need_cached_price(ledger):
    price = {key: value for key, value in PRICE.items() if key != "cached_input_per_million"}

    async def invoke():
        return response(prompt=10, completion=0, cached=0)

    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke, pricing=price)
    assert (await ledger.totals("alice", "conversation"))["verified_cost"]


async def test_duplicate_settlement_is_idempotent_and_conflicts_refuse(ledger, audit):
    identity = str(uuid4())
    await ledger.begin("alice", "conversation", "direct", "model-a", pricing=PRICE, attempt_id=identity)
    first = await ledger.settle("alice", "conversation", identity, usage=response().usage, outcome="success")
    assert await ledger.settle("alice", "conversation", identity, usage=response().usage, outcome="success") == first
    assert len(audit.events) == 2
    with pytest.raises(ContextUsageError, match="conflict"):
        await ledger.settle("alice", "conversation", identity, usage=response(prompt=101).usage, outcome="success")
    assert (await ledger.totals("alice", "conversation"))["attempts"] == 1


async def test_begin_replay_and_model_dispatch_identity_cannot_repeat(ledger, audit):
    identity = str(uuid4())
    first = await ledger.begin("alice", "conversation", "direct", "model-a", pricing=PRICE, attempt_id=identity)
    assert await ledger.begin("alice", "conversation", "direct", "model-a", pricing=PRICE, attempt_id=identity) == first
    assert len(audit.events) == 1
    with pytest.raises(ContextUsageError, match="conflict"):
        await ledger.begin("alice", "conversation", "summary", "model-a", pricing=PRICE, attempt_id=identity)
    await ledger.settle("alice", "conversation", identity, usage=response().usage, outcome="success")
    with pytest.raises(ContextUsageError, match="duplicate"):
        await call(ledger, attempt_id=identity)


async def test_recalls_count_pages_not_event_delivery_and_stay_owner_scoped(ledger, audit):
    identity = str(uuid4())
    first = await ledger.record_recall("alice", "conversation", 16_384, attempt_id=identity)
    assert await ledger.record_recall("alice", "conversation", 16_384, attempt_id=identity) == first
    with pytest.raises(ContextUsageError, match="conflict"):
        await ledger.record_recall("alice", "conversation", 3, attempt_id=identity)
    await ledger.record_recall("alice", "conversation", 0)
    total = await ledger.totals("alice", "conversation")
    assert total["attempts"] == 0 and total["recall_pages"] == 2 and total["recall_bytes"] == 16_384
    assert (await ledger.totals("bob", "conversation"))["recall_pages"] == 0
    assert len(audit.events) == 2


async def test_recovery_orders_revisions_deduplicates_and_keeps_pending_unknown(ledger, audit):
    await call(ledger)
    await ledger.record_recall("alice", "conversation", 4)
    await ledger.begin("alice", "conversation", "summary", "model-a", pricing=PRICE)
    await ledger.record_recall("bob", "conversation", 9)

    async def recover(owner, conversation):
        return list(reversed(await audit.recover(owner, conversation))) * 2

    recovered = ContextUsage(audit.record, recover)
    assert await recovered.totals("alice", "conversation") == await ledger.totals("alice", "conversation")
    total = await recovered.totals("alice", "conversation")
    assert total["pending"] == 1 and total["unknown_cost"] == 1 and not total["complete"]
    assert total["recall_bytes"] == 4


async def test_recovery_refuses_foreign_or_conflicting_metadata(ledger, audit):
    await call(ledger)
    original = audit.events[-1]
    conflicting = original.model_copy(deep=True)
    conflicting.outputs_meta["usage_record"]["usage"]["total_tokens"] += 1

    async def duplicates(*_args):
        return [original, conflicting]

    with pytest.raises(ContextUsageError, match="conflict"):
        await ContextUsage(audit.record, duplicates).totals("alice", "conversation")

    async def foreign(*_args):
        return audit.events

    with pytest.raises(ContextUsageError, match="identity"):
        await ContextUsage(audit.record, foreign).totals("bob", "conversation")


async def test_recovery_missing_revision_prevents_complete_claim(ledger, audit):
    await call(ledger)

    async def incomplete(*_args):
        return [audit.events[-1]]

    total = await ContextUsage(audit.record, incomplete).totals("alice", "conversation")
    assert total["usage"]["total_tokens"]["known"] == 120
    assert not total["recovery_complete"] and not total["verified_cost"]


def legacy_event(*, owner="alice", conversation="conversation", correlation=None):
    return AuditEventCreate(event_id=str(uuid4()), actor_user_id=owner, auth_principal=owner,
        event_class="llm_call", action_type="llm.call.chat", description="Synthetic legacy model metadata",
        conversation_id=conversation, correlation_id=correlation or str(uuid4()), outcome="success",
        outputs_meta={"total_tokens": 120}, started_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("conversation", ["conversation", None, "other"])
async def test_unlinked_legacy_history_is_incomplete_without_fabricating_attempts(audit, loaded, conversation):
    events = []

    async def recover(*_args):
        return events

    ledger = ContextUsage(audit.record, recover)
    if loaded:
        assert (await ledger.totals("alice", "conversation"))["complete"]
    events.append(legacy_event(conversation=conversation))
    totals = await ledger.totals("alice", "conversation", refresh=True)
    assert totals["attempts"] == totals["model_calls"] == 0
    assert all(value == {"known": 0, "unknown": 0} for value in totals["usage"].values())
    assert totals["known_cost_by_currency"] == {} and totals["unknown_cost"] == 0
    assert totals["recovery_complete"] == totals["complete"] == totals["verified_cost"] == (conversation == "other")


async def test_forced_recovery_rereads_loaded_scope_and_keeps_known_local_charge(audit):
    events, reads = [], []

    async def recover(owner, conversation):
        reads.append((owner, conversation))
        return events

    ledger = ContextUsage(audit.record, recover)
    await call(ledger)
    assert (await ledger.totals("alice", "conversation"))["complete"]
    events.append(legacy_event(conversation=None))
    assert (await ledger.totals("alice", "conversation"))["complete"]
    totals = await ledger.totals("alice", "conversation", refresh=True)
    assert reads == [("alice", "conversation"), ("alice", "conversation")]
    assert totals["attempts"] == 1 and totals["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert totals["known_cost_by_currency"] == {"USD": "0.00025"} and not totals["complete"]


async def test_coincidental_legacy_correlation_does_not_prove_attempt_linkage(audit):
    events = []

    async def recover(*_args):
        return events

    ledger = ContextUsage(audit.record, recover)
    identity = str(uuid4())
    await call(ledger, attempt_id=identity)
    events.append(legacy_event(correlation=identity))
    totals = await ledger.totals("alice", "conversation", refresh=True)
    assert totals["attempts"] == 1 and totals["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert not totals["recovery_complete"] and not totals["verified_cost"]


@pytest.mark.parametrize("change", ["missing_owner", "foreign_owner", "malformed_conversation", "empty_conversation"])
async def test_legacy_recovery_refuses_malformed_or_foreign_owner_metadata_atomically(audit, change):
    event = legacy_event().model_dump()
    if change == "missing_owner":
        event.pop("actor_user_id")
    elif change == "foreign_owner":
        event["actor_user_id"] = "bob"
    elif change == "malformed_conversation":
        event["conversation_id"] = []
    else:
        event["conversation_id"] = ""
    responses = [[legacy_event(conversation=None), event], []]

    async def recover(*_args):
        return responses.pop(0)

    ledger = ContextUsage(audit.record, recover)
    with pytest.raises(ContextUsageError):
        await ledger.totals("alice", "conversation", refresh=True)
    assert (await ledger.totals("alice", "conversation", refresh=True))["complete"]


async def test_refresh_deduplicates_existing_attempts_at_capacity(ledger, audit):
    single = ContextUsage(audit.record, audit.recover, max_attempts=1, max_inflight=1)
    await call(single)
    expected = await single.totals("alice", "conversation")
    assert await single.totals("alice", "conversation", refresh=True) == expected


async def test_refresh_retains_newer_validated_local_attempt_when_recovery_is_stale(audit):
    async def recover(*_args):
        return audit.events[:1]

    ledger = ContextUsage(audit.record, recover)
    await call(ledger)
    expected = await ledger.totals("alice", "conversation")
    assert await ledger.totals("alice", "conversation", refresh=True) == expected


async def test_refresh_accepts_newer_durable_settlement_without_counting_a_second_attempt(audit):
    ledger = ContextUsage(audit.record, audit.recover)
    identity = str(uuid4())
    await ledger.begin("alice", "conversation", "direct", "model-a", pricing=PRICE, attempt_id=identity)
    restored = ContextUsage(audit.record, audit.recover)
    await restored.settle("alice", "conversation", identity, usage=response().usage)
    totals = await ledger.totals("alice", "conversation", refresh=True)
    assert totals["attempts"] == totals["succeeded"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert totals["complete"] and totals["verified_cost"]


async def test_refresh_keeps_prepared_record_identity_and_capacity_reserved(audit):
    ledger = ContextUsage(audit.record, audit.recover, max_attempts=1, max_inflight=1)
    identity = str(uuid4())
    audit.unavailable = True
    with pytest.raises(RuntimeError):
        await ledger.begin("alice", "conversation", "direct", "model-a", attempt_id=identity)
    audit.unavailable = False
    other = ContextUsage(audit.record)
    await other.begin("alice", "conversation", "direct", "model-a")
    with pytest.raises(ContextUsageError, match="capacity"):
        await ledger.totals("alice", "conversation", refresh=True)
    assert not ledger._attempts and ("alice", "conversation", identity) in ledger._prepared


async def test_refresh_recognizes_exact_persisted_receipt_after_recorder_acknowledgment_failure(audit):
    async def record(event):
        await audit.record(event)
        if event.outputs_meta["usage_record"]["phase"] == "settled":
            raise RuntimeError("synthetic lost acknowledgment")

    ledger = ContextUsage(record, audit.recover)
    with pytest.raises(RuntimeError, match="acknowledgment"):
        await call(ledger)
    totals = await ledger.totals("alice", "conversation", refresh=True)
    assert totals["attempts"] == 1 and totals["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert totals["complete"] and totals["verified_cost"]


@pytest.mark.parametrize("change", ["identity", "settlement", "same_revision"])
async def test_refresh_refuses_conflicts_with_newer_local_validated_state(audit, change):
    deliveries = []

    async def recover(*_args):
        return deliveries

    ledger = ContextUsage(audit.record, recover)
    await call(ledger)
    previous = await ledger.totals("alice", "conversation")
    selected = audit.events[0 if change == "identity" else -1].model_dump()
    snapshot = selected["outputs_meta"]["usage_record"]
    if change == "identity":
        snapshot["model"] = "conflicting-model"
    elif change == "settlement":
        snapshot["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": 0, "total_tokens": 2}
    else:
        snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
    deliveries.append(selected)
    with pytest.raises(ContextUsageError, match="conflict"):
        await ledger.totals("alice", "conversation", refresh=True)
    assert await ledger.totals("alice", "conversation") == previous


async def test_failed_refresh_preserves_validated_state_and_next_disclosure_retries(audit):
    failures = []

    async def recover(*_args):
        if failures:
            failures.pop()
            raise RuntimeError("synthetic provenance unavailable")
        return audit.events

    ledger = ContextUsage(audit.record, recover)
    await call(ledger)
    previous = await ledger.totals("alice", "conversation")
    failures.append(True)
    with pytest.raises(RuntimeError):
        await ledger.totals("alice", "conversation", refresh=True)
    assert await ledger.totals("alice", "conversation") == previous
    assert await ledger.totals("alice", "conversation", refresh=True) == previous


async def test_legacy_only_loaded_scope_caches_remain_bounded(audit):
    async def recover(_owner, conversation):
        return [legacy_event(conversation=conversation)]

    ledger = ContextUsage(audit.record, recover, max_attempts=2, max_inflight=1)
    for index in range(12):
        totals = await ledger.totals("alice", f"conversation-{index}", refresh=True)
        assert not totals["complete"]
        assert len(ledger._loaded) <= 2 and len(ledger._incomplete) <= 2
    assert not (await ledger.totals("alice", "conversation-0", refresh=True))["complete"]


@pytest.mark.parametrize("refresh", [None, "true", 1, []])
async def test_refresh_option_is_typed_and_cannot_skip_provenance_checks(ledger, refresh):
    with pytest.raises(ContextUsageError, match="refresh"):
        await ledger.totals("alice", "conversation", refresh=refresh)


async def test_audit_failure_stops_dispatch_and_retry_keeps_identical_event(audit):
    observed, dispatched = [], []
    identity = str(uuid4())

    async def record(event):
        observed.append(event.model_dump_json())
        if len(observed) == 1:
            raise RuntimeError("temporarily unavailable")
        await audit.record(event)

    ledger = ContextUsage(record)

    async def invoke():
        dispatched.append(True)
        return response()

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                                pricing=PRICE, attempt_id=identity)
    assert dispatched == []
    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                            pricing=PRICE, attempt_id=identity)
    assert observed[0] == observed[1]
    assert len(dispatched) == 1


async def test_inflight_and_retained_capacity_refuse_without_extra_dispatch(audit):
    entered, finish = asyncio.Event(), asyncio.Event()
    ledger = ContextUsage(audit.record, max_attempts=2, max_inflight=1)

    async def invoke():
        entered.set()
        await finish.wait()
        return response()

    first = asyncio.create_task(ledger.model_call("alice", "conversation", "direct", "model-a", invoke))
    await entered.wait()
    with pytest.raises(ContextUsageError, match="capacity"):
        await call(ledger)
    finish.set()
    await first
    await ledger.record_recall("alice", "conversation", 1)
    with pytest.raises(ContextUsageError, match="capacity"):
        await ledger.record_recall("alice", "conversation", 1)
    assert (await ledger.totals("alice", "conversation"))["attempts"] == 1


@pytest.mark.parametrize("kwargs", [{"owner_id": ""}, {"conversation_id": ""}, {"purpose": "private text\n"},
                                  {"model": "https://private.example/key"}, {"attempt_id": "invalid"},
                                  {"retry_of": str(uuid4())}])
async def test_invalid_identity_or_retry_never_dispatches(audit, kwargs):
    ledger = ContextUsage(audit.record)
    parameters = {"owner_id": "alice", "conversation_id": "conversation", "purpose": "direct", "model": "model-a"}
    parameters.update(kwargs)
    dispatched = []

    async def invoke():
        dispatched.append(True)
        return response()

    with pytest.raises(ContextUsageError):
        await ledger.model_call(**parameters, invoke=invoke)
    assert dispatched == [] and audit.events == []


@pytest.mark.parametrize("size", [-1, True, 16_385, "3"])
async def test_recall_byte_count_is_bounded(ledger, size):
    with pytest.raises(ContextUsageError):
        await ledger.record_recall("alice", "conversation", size)


async def test_recorded_metadata_is_detached_from_mutable_pricing_and_results(ledger, audit):
    price, result = deepcopy(PRICE), response()

    async def invoke():
        price["input_per_million"] = "9999"
        return result

    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke, pricing=price)
    result.usage.total_tokens = 99
    for event in audit.events:
        event.outputs_meta["usage_record"]["pricing"] = None
    totals = await ledger.totals("alice", "conversation")
    assert totals["usage"]["total_tokens"]["known"] == 120
    assert totals["known_cost_by_currency"] == {"USD": "0.00025"}


async def test_tuple_usage_aliases_and_unreadable_provider_usage_remain_safe(ledger):
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "input_tokens_details": {"cached_tokens": 3}}
    result = ("unvalidated output", usage)
    assert await call(ledger, result) is result

    class Unreadable:
        @property
        def usage(self):
            raise RuntimeError("private provider state")

    await call(ledger, Unreadable())
    total = await ledger.totals("alice", "conversation")
    assert total["usage"]["total_tokens"] == {"known": 12, "unknown": 1}
    assert total["usage"]["cached_tokens"] == {"known": 3, "unknown": 1}
    assert total["unknown_cost"] == 1


async def test_decimal_quotes_preserve_exact_cost_across_large_counts(ledger):
    price = {**PRICE, "input_per_million": Decimal("999999999.123456789012"),
             "output_per_million": "1000000000", "cached_input_per_million": 0.25}
    count = 2**62

    async def invoke():
        return response(prompt=count, completion=count - 1, cached=1)

    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke, pricing=price)
    total = await ledger.totals("alice", "conversation")
    assert total["complete"]
    assert total["known_cost_by_currency"] == {
        "USD": "9223372032812433735339.192469632952121836",
    }


@pytest.mark.parametrize("rate", [True, "9" * 33, "1e-13", "1e10", object()])
async def test_unbounded_or_unsupported_quote_rates_cannot_verify_cost(audit, rate):
    ledger = ContextUsage(audit.record)

    async def invoke():
        return response()

    await ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                            pricing={**PRICE, "input_per_million": rate})
    assert not (await ledger.totals("alice", "conversation"))["verified_cost"]


async def test_cancellation_during_begin_never_dispatches_and_drain_finishes_writes(audit):
    entered, finish, dispatched = asyncio.Event(), asyncio.Event(), []

    async def record(event):
        if event.outputs_meta["usage_record"]["phase"] == "begin":
            entered.set()
            await finish.wait()
        await audit.record(event)

    async def invoke():
        dispatched.append(True)
        return response()

    ledger = ContextUsage(record)
    task = asyncio.create_task(ledger.model_call("alice", "conversation", "direct", "model-a", invoke))
    await entered.wait()
    task.cancel()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ledger.drain()
    assert not dispatched
    total = await ledger.totals("alice", "conversation")
    assert total["attempts"] == total["cancelled"] == total["pending"] == 1
    assert total["model_calls"] == 0 and not total["complete"]
    assert [event.outputs_meta["usage_record"]["phase"] for event in audit.events] == ["begin", "cancelled"]


async def test_concurrent_redelivery_cannot_dispatch_the_same_attempt_twice(audit):
    entered, finish, calls = asyncio.Event(), asyncio.Event(), []

    async def invoke():
        calls.append(True)
        entered.set()
        await finish.wait()
        return response()

    ledger = ContextUsage(audit.record)
    identity = str(uuid4())
    task = asyncio.create_task(ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                                               pricing=PRICE, attempt_id=identity))
    await entered.wait()
    with pytest.raises(ContextUsageError, match="duplicate"):
        await ledger.model_call("alice", "conversation", "direct", "model-a", invoke,
                                pricing=PRICE, attempt_id=identity)
    finish.set()
    await task
    await ledger.drain()
    assert calls == [True]
    assert (await ledger.totals("alice", "conversation"))["model_calls"] == 1


async def test_failed_settlement_delivery_replays_identical_bytes_once(audit):
    observed, unavailable = [], True
    identity = str(uuid4())

    async def record(event):
        if event.outputs_meta["usage_record"]["phase"] == "settled":
            observed.append(event.model_dump_json())
            if unavailable:
                raise RuntimeError("recorder unavailable")
        await audit.record(event)

    ledger = ContextUsage(record)
    with pytest.raises(RuntimeError, match="recorder unavailable"):
        await call(ledger, attempt_id=identity)
    assert not (await ledger.totals("alice", "conversation"))["complete"]
    with pytest.raises(ContextUsageError, match="delivery_conflict"):
        await ledger.settle("alice", "conversation", identity, usage=response(prompt=101).usage)
    unavailable = False
    await ledger.settle("alice", "conversation", identity, usage=response().usage)
    assert observed[0] == observed[1]
    assert (await ledger.totals("alice", "conversation"))["usage"]["total_tokens"]["known"] == 120


async def test_recall_delivery_failure_is_replayable_without_counting_failed_delivery(audit):
    ledger = ContextUsage(audit.record)
    identity = str(uuid4())
    audit.unavailable = True
    with pytest.raises(RuntimeError):
        await ledger.record_recall("alice", "conversation", 7, attempt_id=identity)
    assert (await ledger.totals("alice", "conversation"))["recall_pages"] == 0
    audit.unavailable = False
    await ledger.record_recall("alice", "conversation", 7, attempt_id=identity)
    assert (await ledger.totals("alice", "conversation"))["recall_bytes"] == 7
    with pytest.raises(ContextUsageError, match="identity"):
        await ledger.settle("alice", "conversation", identity)


@pytest.mark.parametrize("change", [
    {"kind": []}, {"revision": []}, {"settled": False}, {"model": ""},
    {"usage": {"prompt_tokens": 1, "completion_tokens": 2, "cached_tokens": 3, "total_tokens": 3}},
    {"recall_bytes": 2}, {"updated_at": "bad timestamp"}, {"started_at": "2026-10-08T00:00:00"},
])
async def test_recovery_rejects_malformed_records_without_installing_partial_state(ledger, audit, change):
    await call(ledger)
    altered = audit.events[-1].model_dump()
    altered["outputs_meta"]["usage_record"].update(change)
    deliveries = [[altered], await audit.recover("alice", "conversation")]

    async def recover(*_args):
        return deliveries.pop(0)

    rebuilt = ContextUsage(audit.record, recover)
    with pytest.raises(ContextUsageError):
        await rebuilt.totals("alice", "conversation")
    assert (await rebuilt.totals("alice", "conversation"))["complete"]


@pytest.mark.parametrize("events", [None, [None], [{"action_type": "context.usage", "conversation_id": "conversation",
                                                 "outputs_meta": [1]}]])
async def test_recovery_unavailable_or_invalid_envelope_never_returns_empty_success(audit, events):
    async def recover(*_args):
        return events

    with pytest.raises(ContextUsageError):
        await ContextUsage(audit.record, recover).totals("alice", "conversation")


async def test_recovery_failure_capacity_and_conflicting_history_remain_unavailable(ledger, audit):
    await call(ledger)

    async def unavailable(*_args):
        raise RuntimeError("recovery truncated")

    with pytest.raises(RuntimeError, match="truncated"):
        await ContextUsage(audit.record, unavailable).totals("alice", "conversation")
    changed = audit.events[-1].model_copy(deep=True)
    changed.outputs_meta["usage_record"]["purpose"] = "summary"

    async def conflict(*_args):
        return [audit.events[0], changed]

    with pytest.raises(ContextUsageError, match="conflict"):
        await ContextUsage(audit.record, conflict).totals("alice", "conversation")

    async def oversize(*_args):
        return audit.events * 2

    with pytest.raises(ContextUsageError, match="capacity"):
        await ContextUsage(audit.record, oversize, max_attempts=1, max_inflight=1).totals("alice", "conversation")


@pytest.mark.parametrize("kwargs", [{"record": None}, {"recover": 1}, {"max_attempts": True},
                                  {"max_attempts": 65537}, {"max_inflight": 0}])
def test_configuration_bounds_are_enforced(audit, kwargs):
    parameters = {"record": audit.record, **kwargs}
    with pytest.raises(ContextUsageError, match="configuration"):
        ContextUsage(**parameters)


async def test_invalid_invocation_outcome_and_unknown_cancellation_are_rejected(ledger):
    with pytest.raises(ContextUsageError, match="invocation"):
        await ledger.model_call("alice", "conversation", "direct", "model-a", None)
    with pytest.raises(ContextUsageError, match="outcome"):
        await ledger.settle("alice", "conversation", str(uuid4()), outcome="accepted")
    with pytest.raises(ContextUsageError, match="identity"):
        await ledger.cancel("alice", "conversation", str(uuid4()))


@pytest.mark.parametrize("identity", ["", False, 0, []])
async def test_optional_attempt_identity_cannot_silently_allocate_on_malformed_input(ledger, audit, identity):
    with pytest.raises(ContextUsageError, match="identity"):
        await ledger.begin("alice", "conversation", "direct", "model-a", attempt_id=identity)
    with pytest.raises(ContextUsageError, match="identity"):
        await ledger.record_recall("alice", "conversation", 0, attempt_id=identity)
    assert not audit.events


async def test_unhashable_outcome_is_a_typed_denial(ledger):
    with pytest.raises(ContextUsageError, match="outcome"):
        await ledger.settle("alice", "conversation", str(uuid4()), outcome=[])


async def test_recovery_of_recall_and_cancelled_completed_attempt_preserves_charges(ledger, audit):
    identity = str(uuid4())
    await call(ledger, attempt_id=identity)
    await ledger.cancel("alice", "conversation", identity)
    await ledger.cancel("alice", "conversation", identity)
    await ledger.record_recall("alice", "conversation", 11)
    recovered = ContextUsage(audit.record, audit.recover)
    total = await recovered.totals("alice", "conversation")
    assert total["charged_cancelled"] == total["cancelled"] == 1
    assert total["recall_bytes"] == 11 and total["complete"]
