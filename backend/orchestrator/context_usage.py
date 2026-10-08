"""Records owner-bound physical model attempts and recall traffic through the
existing audit facade. Shielded provider tasks retain late charges after caller
cancellation, while replay reconstructs bounded totals without retaining content.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import logging
import re
from typing import Any
from uuid import UUID, uuid4, uuid5

from audit.schemas import AuditEventCreate

_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")
_RATES = ("input_per_million", "output_per_million", "cached_input_per_million")
_FORMAT = "astral.context-usage/v1"
_NAMESPACE = UUID("5ec7c457-6c65-43b2-b43e-19be1b6165a7")
_MAX_TOKENS = 2**63 - 1
_MAX_PAGE_BYTES = 16 * 1024
logger = logging.getLogger("Orchestrator.ContextUsage")


class ContextUsageError(ValueError):
    pass


def _identity(value, maximum):
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ContextUsageError("context_usage_identity_invalid")
    return value


def _uuid(value):
    try:
        if type(value) is not str or len(value) > 36:
            raise ValueError
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContextUsageError("context_usage_identity_invalid") from exc


def _bound(owner, conversation, attempt):
    return _identity(owner, 512), _identity(conversation, 128), _uuid(attempt)


def _count(value):
    return value if type(value) is int and 0 <= value <= _MAX_TOKENS else None


def _value(value, key):
    try:
        return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    except Exception:
        return None


def _usage(value):
    counts = {key: _count(_value(value, key)) for key in _FIELDS}
    if counts["cached_tokens"] is None:
        details = _value(value, "prompt_tokens_details") or _value(value, "input_tokens_details")
        counts["cached_tokens"] = _count(_value(details, "cached_tokens"))
    if counts["prompt_tokens"] is None:
        counts["prompt_tokens"] = _count(_value(value, "input_tokens"))
    if counts["completion_tokens"] is None:
        counts["completion_tokens"] = _count(_value(value, "output_tokens"))
    prompt, completion, cached = (counts[key] for key in _FIELDS[:3])
    if prompt is not None and completion is not None and counts["total_tokens"] != prompt + completion:
        counts["total_tokens"] = None
    if prompt is not None and cached is not None and cached > prompt:
        counts["cached_tokens"] = None
    return counts


def _reported_usage(response):
    if isinstance(response, tuple) and len(response) == 2:
        return response[1]
    return _value(response, "usage")


def _rate(value):
    if value is None:
        return None
    if type(value) not in {str, int, float, Decimal} or len(str(value)) > 32:
        raise ValueError
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError from exc
    if not amount.is_finite() or amount < 0 or amount > 10**9 or amount.as_tuple().exponent < -12:
        raise ValueError
    return format(amount, "f")


def _pricing(value):
    if not isinstance(value, dict) or set(value) - {*_RATES, "date", "currency"}:
        return None
    try:
        quote = {key: _rate(value.get(key)) for key in _RATES}
        when = value.get("date")
        currency = value.get("currency")
        if when is not None and (type(when) is not str or date.fromisoformat(when).isoformat() != when):
            raise ValueError
        if currency is not None and (type(currency) is not str or re.fullmatch(r"[A-Z]{3}", currency) is None):
            raise ValueError
        return {**quote, "date": when, "currency": currency}
    except (ValueError, TypeError, OverflowError):
        return None


def _cost(attempt):
    quote, counts = attempt.pricing, attempt.usage
    if not attempt.settled or quote is None or not quote["date"] or not quote["currency"]:
        return None
    prompt, completion, cached = (counts[key] for key in _FIELDS[:3])
    if (any(value is None for value in counts.values()) or quote["input_per_million"] is None
            or quote["output_per_million"] is None
            or cached and quote["cached_input_per_million"] is None):
        return None
    with localcontext() as context:
        context.prec = 64
        amount = ((prompt - cached) * Decimal(quote["input_per_million"])
                  + completion * Decimal(quote["output_per_million"]))
        if cached:
            amount += cached * Decimal(quote["cached_input_per_million"])
        return quote["currency"], amount / Decimal(1_000_000)


def _money(value):
    return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


@dataclass(frozen=True, slots=True)
class _Attempt:
    owner_id: str
    conversation_id: str
    attempt_id: str
    purpose: str
    model: str
    retry_of: str | None
    pricing: dict | None
    kind: str
    phase: str
    revision: int
    usage: dict
    settled: bool
    caller_cancelled: bool
    dispatched: bool
    outcome: str | None
    recall_bytes: int | None
    started_at: datetime
    updated_at: datetime

    @property
    def key(self):
        return self.owner_id, self.conversation_id, self.attempt_id

    def snapshot(self):
        values = {name: deepcopy(getattr(self, name)) for name in self.__dataclass_fields__}
        for key in ("started_at", "updated_at"):
            values[key] = values[key].isoformat()
        return {"format": _FORMAT, **values}


def _event_id(attempt):
    return str(uuid5(_NAMESPACE, f"{attempt.owner_id}\0{attempt.conversation_id}\0{attempt.attempt_id}\0{attempt.revision}"))


def _event(attempt):
    outcome = "in_progress"
    if attempt.settled:
        outcome = "interrupted" if attempt.outcome == "cancelled" else attempt.outcome
    return AuditEventCreate(
        event_id=_event_id(attempt), actor_user_id=attempt.owner_id, auth_principal=attempt.owner_id,
        event_class="llm_call", action_type="context.usage", description="Conversation usage attempt",
        conversation_id=attempt.conversation_id, correlation_id=attempt.attempt_id, outcome=outcome,
        outputs_meta={"usage_record": attempt.snapshot()}, started_at=attempt.started_at,
        completed_at=attempt.updated_at if attempt.settled else None,
    )


def _consistent_attempts(attempts):
    ordered = sorted(attempts, key=lambda value: value.revision)
    initial = ordered[0]
    fields = ("owner_id", "conversation_id", "attempt_id", "purpose", "model", "retry_of", "pricing", "kind", "started_at")
    if any(tuple(getattr(value, key) for key in fields) != tuple(getattr(initial, key) for key in fields)
           for value in ordered):
        raise ContextUsageError("context_usage_recovery_conflict")
    for previous, current in zip(ordered, ordered[1:]):
        if (previous.revision == current.revision and previous != current
                or previous.caller_cancelled and not current.caller_cancelled
                or previous.dispatched and not current.dispatched
                or previous.settled and (not current.settled or previous.outcome != current.outcome
                                        or previous.usage != current.usage)):
            raise ContextUsageError("context_usage_recovery_conflict")
    return ordered[-1]


def _recover_attempt(value, owner, conversation):
    values = deepcopy(value)
    if type(values) is not dict or set(values) != {"format", *_Attempt.__dataclass_fields__} or values.pop("format") != _FORMAT:
        raise ContextUsageError("context_usage_recovery_invalid")
    if _bound(values["owner_id"], values["conversation_id"], values["attempt_id"]) != (owner, conversation, values["attempt_id"]):
        raise ContextUsageError("context_usage_identity_invalid")
    if (values["kind"] not in ("model", "recall") or values["phase"] not in ("begin", "pending", "cancelled", "settled")
            or type(values["revision"]) is not int or not 1 <= values["revision"] <= 4
            or type(values["settled"]) is not bool or type(values["caller_cancelled"]) is not bool
            or type(values["dispatched"]) is not bool
            or values["outcome"] not in (None, "success", "failure", "cancelled")
            or values["settled"] != (values["outcome"] is not None)
            or not isinstance(values["usage"], dict) or set(values["usage"]) != set(_FIELDS)
            or any(item is not None and _count(item) is None for item in values["usage"].values())
            or values["pricing"] != _pricing(values["pricing"])
            or values["usage"] != _usage(values["usage"])):
        raise ContextUsageError("context_usage_recovery_invalid")
    _identity(values["purpose"], 64)
    if values["kind"] == "model":
        _identity(values["model"], 128)
        if (values["recall_bytes"] is not None or "://" in values["model"]
                or re.fullmatch(r"[a-z][a-z0-9_.:-]*", values["purpose"]) is None
                or values["phase"] == "begin" and (values["revision"] != 1 or values["settled"] or values["caller_cancelled"] or values["dispatched"])
                or values["phase"] == "pending" and (values["revision"] != 2 or values["settled"] or values["caller_cancelled"] or not values["dispatched"])
                or values["phase"] == "cancelled" and not values["caller_cancelled"]
                or values["phase"] == "settled" and not values["settled"]
                or values["settled"] and (not values["dispatched"] or values["revision"] < 2)
                or not values["settled"] and any(item is not None for item in values["usage"].values())):
            raise ContextUsageError("context_usage_recovery_invalid")
    elif (values["phase"] != "settled" or not values["settled"] or values["outcome"] != "success"
            or values["revision"] != 1 or values["purpose"] != "recall" or values["model"] != ""
            or values["caller_cancelled"] or values["dispatched"] or values["pricing"] is not None
            or values["retry_of"] is not None or any(item is not None for item in values["usage"].values())
            or type(values["recall_bytes"]) is not int or not 0 <= values["recall_bytes"] <= _MAX_PAGE_BYTES):
        raise ContextUsageError("context_usage_recovery_invalid")
    if values["retry_of"] is not None:
        values["retry_of"] = _uuid(values["retry_of"])
    try:
        for key in ("started_at", "updated_at"):
            values[key] = datetime.fromisoformat(values[key])
            if values[key].utcoffset() is None:
                raise ValueError
        if values["updated_at"] < values["started_at"]:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ContextUsageError("context_usage_recovery_invalid") from exc
    return _Attempt(**values)


class ContextUsage:
    def __init__(self, record: Callable[[AuditEventCreate], Awaitable[Any]],
                 recover: Callable[[str, str], Awaitable[Sequence[Any]]] | None = None,
                 *, max_attempts: int = 8192, max_inflight: int = 64):
        if (not callable(record) or recover is not None and not callable(recover)
                or type(max_attempts) is not int or not 1 <= max_attempts <= 65536
                or type(max_inflight) is not int or not 1 <= max_inflight <= max_attempts):
            raise ContextUsageError("context_usage_configuration_invalid")
        self._record, self._recover = record, recover
        self._max_attempts, self._max_inflight = max_attempts, max_inflight
        self._lock = asyncio.Lock()
        self._attempts: dict[tuple, _Attempt] = {}
        self._prepared: dict[tuple, tuple[_Attempt, AuditEventCreate]] = {}
        self._inflight: set[tuple] = set()
        self._tasks: set[asyncio.Task] = set()
        self._loaded: set[tuple] = set()
        self._incomplete: set[tuple] = set()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def finished(completed):
            self._tasks.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.warning("usage task failed exception_class=%s", type(error).__name__)

        task.add_done_callback(finished)
        return task

    async def _protected(self, coroutine):
        return await asyncio.shield(self._spawn(coroutine))

    async def _load(self, owner, conversation, *, refresh=False):
        scope = owner, conversation
        if scope in self._loaded and not refresh:
            return
        if self._recover is not None:
            events = await self._recover(owner, conversation)
            if not isinstance(events, Sequence) or len(events) > self._max_attempts * 4:
                raise ContextUsageError("context_usage_recovery_capacity")
            revisions, incomplete = {}, False
            for event in events:
                envelope = event.model_dump() if hasattr(event, "model_dump") else event
                if not isinstance(envelope, dict):
                    raise ContextUsageError("context_usage_recovery_invalid")
                if envelope.get("action_type") != "context.usage":
                    if envelope.get("event_class") == "llm_call":
                        recorded_conversation = envelope.get("conversation_id")
                        if envelope.get("actor_user_id") != owner:
                            raise ContextUsageError("context_usage_identity_invalid")
                        if recorded_conversation is not None:
                            _identity(recorded_conversation, 128)
                        if recorded_conversation is None or recorded_conversation == conversation:
                            incomplete = True
                    continue
                if (envelope.get("actor_user_id", owner) != owner
                        or envelope.get("conversation_id") != conversation):
                    raise ContextUsageError("context_usage_identity_invalid")
                metadata = envelope.get("outputs_meta")
                if not isinstance(metadata, dict):
                    raise ContextUsageError("context_usage_recovery_invalid")
                snapshot = metadata.get("usage_record")
                if not isinstance(snapshot, dict):
                    raise ContextUsageError("context_usage_recovery_invalid")
                key = _uuid(snapshot.get("attempt_id")), snapshot.get("revision")
                try:
                    duplicate = revisions.get(key)
                except TypeError as exc:
                    raise ContextUsageError("context_usage_recovery_invalid") from exc
                if duplicate is not None and duplicate != (snapshot, envelope.get("event_id")):
                    raise ContextUsageError("context_usage_recovery_conflict")
                revisions[key] = snapshot, envelope.get("event_id")
            grouped = {}
            for (identity, revision), (snapshot, event_id) in revisions.items():
                attempt = _recover_attempt(snapshot, owner, conversation)
                if event_id != _event_id(attempt):
                    raise ContextUsageError("context_usage_identity_invalid")
                grouped.setdefault(identity, []).append(attempt)
            recovered = {}
            for attempts in grouped.values():
                attempts.sort(key=lambda value: value.revision)
                last = attempts[-1]
                if [value.revision for value in attempts] != list(range(1, last.revision + 1)):
                    incomplete = True
                local = self._attempts.get(last.key)
                prepared = self._prepared.get(last.key)
                _consistent_attempts([*attempts, *([local] if local is not None else []),
                                      *([prepared[0]] if prepared is not None else [])])
                recovered[last.key] = local if local is not None and local.revision > last.revision else last
            if len(set(self._attempts) | set(self._prepared) | set(recovered)) > self._max_attempts:
                raise ContextUsageError("context_usage_recovery_capacity")
            self._attempts.update(recovered)
            if incomplete:
                self._incomplete.add(scope)
        if len(self._loaded) >= self._max_attempts:
            occupied = {key[:2] for key in self._attempts}
            self._loaded.intersection_update(occupied)
            self._incomplete.intersection_update(occupied | {scope})
        self._loaded.add(scope)

    async def _commit(self, attempt):
        prepared = self._prepared.get(attempt.key)
        if prepared is None:
            prepared = attempt, _event(attempt)
            self._prepared[attempt.key] = prepared
        elif replace(attempt, updated_at=prepared[0].updated_at, started_at=prepared[0].started_at) != prepared[0]:
            raise ContextUsageError("context_usage_delivery_conflict")
        await self._record(prepared[1].model_copy(deep=True))
        self._attempts[attempt.key] = prepared[0]
        del self._prepared[attempt.key]
        return deepcopy(prepared[0].snapshot())

    async def begin(self, owner_id, conversation_id, purpose, model, *, pricing=None, attempt_id=None, retry_of=None):
        key = _bound(owner_id, conversation_id, str(uuid4()) if attempt_id is None else attempt_id)
        _identity(purpose, 64)
        _identity(model, 128)
        if re.fullmatch(r"[a-z][a-z0-9_.:-]*", purpose) is None or "://" in model:
            raise ContextUsageError("context_usage_identity_invalid")
        retry = None if retry_of is None else _uuid(retry_of)
        return await self._protected(self._begin(key, purpose, model, _pricing(pricing), retry))

    async def _begin(self, key, purpose, model, pricing, retry):
        async with self._lock:
            await self._load(*key[:2])
            existing = self._attempts.get(key) or (self._prepared.get(key) or (None,))[0]
            if existing is not None:
                if (existing.purpose, existing.model, existing.pricing, existing.retry_of, existing.kind) != (purpose, model, pricing, retry, "model"):
                    raise ContextUsageError("context_usage_attempt_conflict")
                if key in self._attempts:
                    return existing.snapshot()
                result = await self._commit(existing)
                self._inflight.add(key)
                return result
            if (len(self._attempts) + len(self._prepared) >= self._max_attempts
                    or len(self._inflight) >= self._max_inflight):
                raise ContextUsageError("context_usage_capacity")
            if retry is not None:
                prior = self._attempts.get((*key[:2], retry))
                if prior is None or prior.kind != "model" or not prior.settled:
                    raise ContextUsageError("context_usage_retry_invalid")
            now = datetime.now(timezone.utc)
            attempt = _Attempt(*key, purpose, model, retry, pricing, "model", "begin", 1,
                               dict.fromkeys(_FIELDS), False, False, False, None, None, now, now)
            result = await self._commit(attempt)
            self._inflight.add(key)
            return result

    async def _transition(self, key, phase, usage=None, outcome=None):
        async with self._lock:
            await self._load(*key[:2])
            previous = self._attempts.get(key)
            if previous is None or previous.kind != "model":
                raise ContextUsageError("context_usage_identity_invalid")
            if phase == "settled" and previous.settled:
                if (previous.usage, previous.outcome) != (usage, outcome):
                    raise ContextUsageError("context_usage_settlement_conflict")
                return previous.snapshot()
            if phase == "cancelled" and previous.caller_cancelled:
                return previous.snapshot()
            if phase == "pending" and previous.phase != "begin":
                raise ContextUsageError("context_usage_duplicate_dispatch")
            update = {"phase": phase, "revision": previous.revision + 1,
                      "updated_at": max(datetime.now(timezone.utc), previous.started_at)}
            if phase == "settled":
                update.update(settled=True, usage=usage, outcome=outcome, dispatched=True)
            elif phase == "cancelled":
                update["caller_cancelled"] = True
            else:
                update["dispatched"] = True
            result = await self._commit(replace(previous, **update))
            if phase == "settled":
                self._inflight.discard(key)
            return result

    async def settle(self, owner_id, conversation_id, attempt_id, *, usage=None, outcome="success"):
        if outcome not in ("success", "failure", "cancelled"):
            raise ContextUsageError("context_usage_outcome_invalid")
        return await self._protected(self._transition(_bound(owner_id, conversation_id, attempt_id),
                                                       "settled", _usage(usage), outcome))

    async def cancel(self, owner_id, conversation_id, attempt_id):
        return await self._protected(self._transition(_bound(owner_id, conversation_id, attempt_id), "cancelled"))

    async def model_call(self, owner_id, conversation_id, purpose, model, invoke, pricing=None, *, attempt_id=None, retry_of=None):
        if not callable(invoke):
            raise ContextUsageError("context_usage_invocation_invalid")
        identity = _uuid(attempt_id) if attempt_id is not None else str(uuid4())
        task = None
        try:
            attempt = await self.begin(owner_id, conversation_id, purpose, model, pricing=pricing,
                                       attempt_id=identity, retry_of=retry_of)
            if attempt["phase"] != "begin":
                raise ContextUsageError("context_usage_duplicate_dispatch")
            task = self._spawn(self._invoke(_bound(owner_id, conversation_id, identity), invoke))
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                marker = self._spawn(self.cancel(owner_id, conversation_id, identity))
                while not marker.done():
                    try:
                        await asyncio.shield(marker)
                    except asyncio.CancelledError:
                        pass
                marker.result()
                if task is None:
                    self._inflight.discard((owner_id, conversation_id, identity))
            raise

    async def _invoke(self, key, invoke):
        await self._transition(key, "pending")
        try:
            response = await invoke()
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failure"
            await self.settle(*key, usage=_value(exc, "usage"), outcome=outcome)
            raise
        await self.settle(*key, usage=_reported_usage(response), outcome="success")
        return response

    async def record_recall(self, owner_id, conversation_id, byte_count, *, attempt_id=None):
        if type(byte_count) is not int or not 0 <= byte_count <= _MAX_PAGE_BYTES:
            raise ContextUsageError("context_usage_recall_invalid")
        key = _bound(owner_id, conversation_id, str(uuid4()) if attempt_id is None else attempt_id)
        return await self._protected(self._recall(key, byte_count))

    async def _recall(self, key, byte_count):
        async with self._lock:
            await self._load(*key[:2])
            previous = self._attempts.get(key) or (self._prepared.get(key) or (None,))[0]
            if previous is not None:
                if previous.kind != "recall" or previous.recall_bytes != byte_count:
                    raise ContextUsageError("context_usage_recall_conflict")
                return previous.snapshot() if key in self._attempts else await self._commit(previous)
            if len(self._attempts) + len(self._prepared) >= self._max_attempts:
                raise ContextUsageError("context_usage_capacity")
            now = datetime.now(timezone.utc)
            return await self._commit(_Attempt(*key, "recall", "", None, None, "recall", "settled", 1,
                                                dict.fromkeys(_FIELDS), True, False, False, "success", byte_count, now, now))

    async def totals(self, owner_id, conversation_id, *, refresh=False):
        if type(refresh) is not bool:
            raise ContextUsageError("context_usage_refresh_invalid")
        owner, conversation = _identity(owner_id, 512), _identity(conversation_id, 128)
        async with self._lock:
            await self._load(owner, conversation, refresh=refresh)
            attempts = [value for key, value in self._attempts.items() if key[:2] == (owner, conversation)]
            return self._totals(owner, conversation, attempts)

    def _totals(self, owner, conversation, attempts):
        models = [value for value in attempts if value.kind == "model"]
        recalls = [value for value in attempts if value.kind == "recall"]
        usage = {key: {"known": sum(value.usage[key] for value in models if value.usage[key] is not None),
                       "unknown": sum(value.usage[key] is None for value in models)} for key in _FIELDS}
        costs, unknown_cost = Counter(), 0
        for attempt in models:
            cost = _cost(attempt)
            if cost is None:
                unknown_cost += 1
            else:
                with localcontext() as context:
                    context.prec = 64
                    costs[cost[0]] += cost[1]

        def known_charge(value):
            return any(value.usage[key] is not None and value.usage[key] > 0 for key in _FIELDS)

        unknown_usage = sum(any(value is None for value in attempt.usage.values()) for attempt in models)
        recovered = (owner, conversation) not in self._incomplete
        return {
            "owner_id": owner, "conversation_id": conversation, "attempts": len(models), "model_calls": sum(value.dispatched for value in models),
            "pending": sum(not value.settled for value in models),
            "succeeded": sum(value.outcome == "success" for value in models),
            "failed": sum(value.outcome == "failure" for value in models),
            "cancelled": sum(value.caller_cancelled for value in models),
            "provider_cancelled": sum(value.outcome == "cancelled" for value in models),
            "charged_failed": sum(value.outcome == "failure" and known_charge(value) for value in models),
            "charged_cancelled": sum((value.caller_cancelled or value.outcome == "cancelled") and known_charge(value) for value in models),
            "retries": sum(value.retry_of is not None for value in models), "by_purpose": dict(Counter(value.purpose for value in models)),
            "usage": usage, "unknown_usage": unknown_usage, "known_cost_by_currency": {key: _money(value) for key, value in sorted(costs.items())},
            "unknown_cost": unknown_cost, "recall_pages": len(recalls), "recall_bytes": sum(value.recall_bytes for value in recalls),
            "recovery_complete": recovered, "complete": recovered and unknown_usage == 0 and unknown_cost == 0,
            "verified_cost": recovered and unknown_cost == 0,
        }

    async def drain(self):
        while self._tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._tasks)), return_exceptions=True)
