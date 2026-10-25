"""Durable owner-bound mobile push delivery over the Plane outbox: idempotent
per-device enqueue from notify_user payloads, a leased dispatcher with bounded
retries and stale-destination suppression, and audited queued/delivered/failed/
unavailable/client-acknowledged outcome transitions consumed by push_api.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from astralplane.contracts import ClaimedOutboxEntry, OutboxEntry
from astralplane.errors import PlaneError
from audit.schemas import AuditEventCreate, AuditEventDTO

from orchestrator.plane_repository_context import repository_from
from orchestrator.push_destinations import DestinationNotFound, PushDestinationStore
from orchestrator.push_providers import PushProviderError

logger = logging.getLogger("Orchestrator.PushDelivery")

OUTBOX_TOPIC = "push.notification"
MAX_DELIVERY_ATTEMPTS = 5
BASE_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 3600
MAX_TITLE_CHARS = 128
MAX_BODY_CHARS = 256
LEASE_DURATION = timedelta(minutes=5)
BATCH_LIMIT = 20
RECLAIM_LIMIT = 20
POLL_SECONDS = 5.0

OUTCOME_QUEUED = "queued"
OUTCOME_DELIVERED = "provider_accepted"
OUTCOME_ACKED = "client_acknowledged"
OUTCOME_FAILED = "failed"
OUTCOME_UNAVAILABLE = "unavailable"

_NOTIFICATION_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_DATA_KEYS = ("chat_id", "job_id", "activity_id", "assignment_id")


class PushDeliveryError(Exception):
    status = 500
    code = "push_delivery_error"


class AckInvalid(PushDeliveryError):
    status = 400
    code = "ack_invalid"


class AckRejected(PushDeliveryError):
    status = 404
    code = "ack_rejected"


class AckDestinationRevoked(AckRejected):
    status = 410
    code = "destination_revoked"


def _clean_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()[:maximum].strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def notification_identity(
    user_id: str,
    payload: dict[str, Any],
    *,
    now_s: float,
) -> str | None:
    if not isinstance(payload, dict) or payload.get("type") != "notification":
        return None
    explicit = payload.get("notification_id")
    if isinstance(explicit, str) and _NOTIFICATION_ID.fullmatch(explicit):
        return explicit
    title = _clean_text(payload.get("title"), MAX_TITLE_CHARS)
    body = _clean_text(payload.get("body"), MAX_BODY_CHARS)
    if not title and not body:
        return None
    # The minute bucket deduplicates same-minute re-invocations; recurring
    # notifications across runs stay distinct through their own content changes.
    bucket = int(now_s // 60)
    canonical = json.dumps(
        {
            "user_id": user_id,
            "level": _clean_text(payload.get("level"), 32),
            "source": _clean_text(payload.get("source"), 64),
            "ids": {
                key: _clean_text(payload.get(key), 128) for key in _DATA_KEYS
            },
            "title": title,
            "body": body,
            "bucket": bucket,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def minimal_payload(
    payload: dict[str, Any], notification_id: str
) -> dict[str, Any] | None:
    title = _clean_text(payload.get("title"), MAX_TITLE_CHARS)
    body = _clean_text(payload.get("body"), MAX_BODY_CHARS)
    if not title and not body:
        return None
    data: dict[str, Any] = {
        "notification_id": notification_id,
        "kind": _clean_text(payload.get("source"), 64) or "notification",
        "level": _clean_text(payload.get("level"), 32) or "info",
        "title": title,
        "body": body,
    }
    for key in _DATA_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            data[key] = _clean_text(value, 128)
    return data


def _retry_delay(attempt: int) -> int:
    return min(BASE_RETRY_SECONDS * (2 ** max(0, attempt - 1)), MAX_RETRY_SECONDS)


class PushDeliveryCoordinator:
    def __init__(
        self,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        destinations: PushDestinationStore,
        adapters: dict[str, Any],
        audit_repository: Any = None,
        clock: Callable[[], float] = time.time,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[..., Any] = asyncio.sleep,
        metrics: Callable[[str, dict[str, str]], None] | None = None,
        batch_limit: int = BATCH_LIMIT,
    ) -> None:
        outbox, runtime = repository_from(
            "outbox",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
        )
        self._outbox = outbox
        self._runtime = runtime
        self._destinations = destinations
        self._adapters = adapters
        self._audit = audit_repository
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._metrics = metrics
        self._batch_limit = batch_limit
        self._worker_id = f"push-{uuid.uuid4().hex[:12]}"

    def _metric(self, name: str, labels: dict[str, str]) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics(name, labels)
        except Exception:
            logger.debug("push delivery metric rejected", exc_info=True)

    @property
    def destinations(self) -> PushDestinationStore:
        return self._destinations

    @property
    def audit_repository(self) -> Any:
        return self._audit

    def provider_configured(self, provider: str) -> bool:
        adapter = self._adapters.get(provider)
        return bool(getattr(adapter, "configured", False))

    def _audit_outcome(
        self,
        *,
        owner_id: str,
        action: str,
        outcome: str,
        meta: dict[str, Any],
    ) -> None:
        if self._audit is None:
            raise PushDeliveryError("durable push delivery audit unavailable")
        event = AuditEventCreate(
            event_id=str(uuid.uuid4()),
            actor_user_id=owner_id,
            auth_principal=f"owner:{owner_id}",
            event_class="agent_lifecycle",
            action_type=action,
            description=action,
            correlation_id=str(uuid.uuid4()),
            inputs_meta=meta,
            outcome=outcome,
            started_at=datetime.now(timezone.utc),
        )
        try:
            receipt = self._audit.insert(event)
        except Exception:
            raise PushDeliveryError(
                "durable push delivery audit unavailable"
            ) from None
        if (
            not isinstance(receipt, AuditEventDTO)
            or receipt.event_id != event.event_id
            or receipt.action_type != action
            or receipt.outcome != outcome
        ):
            raise PushDeliveryError("durable push delivery audit unavailable")

    def _audit_transition(self, transaction, *, owner_id, action, meta) -> None:
        event = AuditEventCreate(
            event_id=str(uuid.uuid4()),
            actor_user_id=owner_id,
            auth_principal=f"owner:{owner_id}",
            event_class="agent_lifecycle",
            action_type=action,
            description=action,
            correlation_id=str(uuid.uuid4()),
            inputs_meta=meta,
            outcome="success",
            started_at=datetime.now(timezone.utc),
        )
        receipt = self._audit.insert_in_transaction(
            event,
            transaction=transaction,
            plane_runtime=self._runtime,
        )
        if (
            not isinstance(receipt, AuditEventDTO)
            or receipt.event_id != event.event_id
            or receipt.action_type != action
            or receipt.outcome != "success"
        ):
            raise PushDeliveryError("durable push delivery audit unavailable")

    async def enqueue_notification(self, user_id: str, payload: dict[str, Any]) -> int:
        now_s = self._clock()
        identity = await asyncio.to_thread(
            notification_identity, user_id, payload, now_s=now_s
        )
        if identity is None:
            return 0
        data = await asyncio.to_thread(minimal_payload, payload, identity)
        if data is None:
            logger.warning(
                "push_delivery.enqueue_dropped",
                extra={
                    "user_id": user_id,
                    "notification_id": identity,
                    "reason": "payload_not_push_eligible",
                },
            )
            return 0
        targets = await asyncio.to_thread(self._destinations.delivery_targets, user_id)
        if not targets:
            return 0
        available_at = _utc_now()
        queued = 0
        for target in targets:
            idempotency_key = f"push:{identity}:{target['device_id']}"
            entry_id = "push-" + hashlib.sha256(idempotency_key.encode()).hexdigest()
            canonical = {
                "notification_id": identity,
                "owner_id": user_id,
                "device_id": target["device_id"],
                "provider": target["provider"],
                "token_fingerprint": target["token_fingerprint"],
                **data,
            }
            payload_bytes = json.dumps(
                canonical, sort_keys=True, separators=(",", ":")
            ).encode()
            entry = OutboxEntry(
                entry_id=entry_id,
                topic=OUTBOX_TOPIC,
                canonical_payload=payload_bytes,
                payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
                idempotency_key=idempotency_key,
                available_at=available_at,
            )
            with self._runtime.transaction() as transaction:
                try:
                    result = self._outbox.enqueue(transaction, entry)
                except PlaneError as exc:
                    # A conflicting idempotency key means this notification was
                    # already enqueued for the device; the first entry delivers.
                    if getattr(exc, "code", "") != "outbox_idempotency_conflict":
                        raise
                    logger.debug(
                        "push_delivery.duplicate_suppressed key=%s",
                        idempotency_key,
                    )
                    continue
                if getattr(result, "rowcount", 1) == 1:
                    self._audit_transition(
                        transaction,
                        owner_id=user_id,
                        action="push.delivery.queued",
                        meta={
                            "notification_id": identity,
                            "device_id": target["device_id"],
                            "provider": target["provider"],
                        },
                    )
                    queued += 1
        return queued

    async def dispatch_once(self, *, now: datetime | None = None) -> int:
        moment = now or _utc_now()
        with self._runtime.transaction() as transaction:
            self._outbox.reclaim_expired(transaction, now=moment, limit=RECLAIM_LIMIT)
        with self._runtime.transaction() as transaction:
            claimed = self._outbox.claim(
                transaction,
                worker_id=self._worker_id,
                topics=(OUTBOX_TOPIC,),
                now=moment,
                lease_duration=LEASE_DURATION,
                limit=self._batch_limit,
            )
        processed = 0
        for item in claimed:
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "push_delivery.item_failed entry=%s",
                    item.entry.entry_id,
                    exc_info=True,
                )
            else:
                processed += 1
        return processed

    async def _deliver(self, claimed: ClaimedOutboxEntry) -> None:
        entry = claimed.entry
        try:
            canonical = json.loads(entry.canonical_payload)
            owner_id = canonical["owner_id"]
            device_id = canonical["device_id"]
            notification_id = canonical["notification_id"]
        except Exception:
            self._fence(self._outbox.dead_letter, claimed, error_code="payload_malformed")
            return
        target = await asyncio.to_thread(
            self._destinations.delivery_target, owner_id, device_id
        )
        if target is None:
            self._suppress_stale(claimed, canonical, "destination_unavailable")
            return
        if target["token_fingerprint"] != canonical.get("token_fingerprint"):
            self._suppress_stale(claimed, canonical, "token_rotated")
            return
        adapter = self._adapters.get(canonical.get("provider"))
        try:
            if adapter is None:
                raise PushProviderError(
                    transient=True,
                    code="provider_unconfigured",
                    detail="no adapter for provider",
                )
            await asyncio.to_thread(
                adapter.send,
                device_token=target["push_token"],
                title=canonical["title"],
                body=canonical["body"],
                notification_id=notification_id,
            )
        except Exception as exc:
            await self._record_provider_error(claimed, canonical, exc)
            return
        self._audit_outcome(
            owner_id=owner_id,
            action="push.delivery.delivered",
            outcome="success",
            meta={
                "notification_id": notification_id,
                "device_id": device_id,
                "provider": canonical.get("provider"),
                "attempt": claimed.attempt,
                "delivery_outcome": OUTCOME_DELIVERED,
            },
        )
        self._fence(self._outbox.ack, claimed)
        self._metric(
            "push_delivery_outcome_total",
            {
                "outcome": OUTCOME_DELIVERED,
                "provider": canonical.get("provider", "unknown"),
            },
        )

    async def _record_provider_error(
        self,
        claimed: ClaimedOutboxEntry,
        canonical: dict[str, Any],
        exc: Exception,
    ) -> None:
        transient = bool(getattr(exc, "transient", False))
        code = str(getattr(exc, "code", "delivery_error"))
        exhausted = claimed.attempt >= MAX_DELIVERY_ATTEMPTS
        if transient and not exhausted:
            self._audit_outcome(
                owner_id=canonical["owner_id"],
                action="push.delivery.unavailable",
                outcome="failure",
                meta={
                    "notification_id": canonical["notification_id"],
                    "device_id": canonical["device_id"],
                    "provider": canonical.get("provider"),
                    "attempt": claimed.attempt,
                    "error_code": code,
                    "delivery_outcome": OUTCOME_UNAVAILABLE,
                },
            )
            self._fence(
                self._outbox.retry,
                claimed,
                available_at=_utc_now()
                + timedelta(seconds=_retry_delay(claimed.attempt)),
                error_code=code,
            )
            self._metric(
                "push_delivery_outcome_total",
                {
                    "outcome": OUTCOME_UNAVAILABLE,
                    "provider": canonical.get("provider", "unknown"),
                },
            )
            return
        self._audit_outcome(
            owner_id=canonical["owner_id"],
            action="push.delivery.failed",
            outcome="failure",
            meta={
                "notification_id": canonical["notification_id"],
                "device_id": canonical["device_id"],
                "provider": canonical.get("provider"),
                "attempt": claimed.attempt,
                "error_code": code,
                "attempts_exhausted": bool(transient and exhausted),
                "delivery_outcome": OUTCOME_FAILED,
            },
        )
        self._fence(self._outbox.dead_letter, claimed, error_code=code)
        self._metric(
            "push_delivery_outcome_total",
            {
                "outcome": OUTCOME_FAILED,
                "provider": canonical.get("provider", "unknown"),
            },
        )
        if code == "token_unregistered":
            await asyncio.to_thread(
                self._destinations.revoke,
                canonical["owner_id"],
                canonical["device_id"],
                reason="token_unregistered",
            )

    def _fence(
        self,
        operation: Callable[..., Any],
        claimed: ClaimedOutboxEntry,
        **kwargs: Any,
    ) -> None:
        try:
            with self._runtime.transaction() as transaction:
                operation(
                    transaction,
                    entry_id=claimed.entry.entry_id,
                    worker_id=claimed.worker_id,
                    expected_version=claimed.expected_version,
                    now=_utc_now(),
                    **kwargs,
                )
        except Exception as exc:
            logger.debug(
                "push_delivery.fence_lost entry=%s error=%s",
                claimed.entry.entry_id,
                type(exc).__name__,
            )

    def _suppress_stale(
        self,
        claimed: ClaimedOutboxEntry,
        canonical: dict[str, Any],
        reason: str,
    ) -> None:
        self._audit_outcome(
            owner_id=canonical["owner_id"],
            action="push.delivery.stale_suppressed",
            outcome="success",
            meta={
                "notification_id": canonical.get("notification_id"),
                "device_id": canonical.get("device_id"),
                "reason": reason,
                "delivery_outcome": OUTCOME_QUEUED,
            },
        )
        self._fence(self._outbox.ack, claimed)
        self._metric("push_delivery_stale_suppressed_total", {"reason": reason})

    async def run_loop(self) -> None:
        while True:
            try:
                processed = await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("push delivery dispatch pass failed", exc_info=True)
                processed = 0
            if processed < self._batch_limit:
                await self._sleep(self._poll_seconds)

    async def acknowledge(
        self,
        owner_id: str,
        device_id: str,
        notification_id: str,
    ) -> None:
        if not isinstance(notification_id, str) or not _NOTIFICATION_ID.fullmatch(
            notification_id
        ):
            self._audit_outcome(
                owner_id=owner_id,
                action="push.delivery.ack_denied",
                outcome="failure",
                meta={"device_id": device_id, "reason": "malformed_notification_id"},
            )
            raise AckInvalid("notification_id is malformed")
        try:
            destination = await asyncio.to_thread(
                self._destinations.get_destination, owner_id, device_id
            )
        except DestinationNotFound:
            self._audit_outcome(
                owner_id=owner_id,
                action="push.delivery.ack_denied",
                outcome="failure",
                meta={"device_id": device_id, "reason": "destination_unknown"},
            )
            raise AckRejected("destination is unknown for this owner") from None
        if destination.get("status") != "active":
            self._audit_outcome(
                owner_id=owner_id,
                action="push.delivery.ack_denied",
                outcome="failure",
                meta={"device_id": device_id, "reason": "destination_revoked"},
            )
            raise AckDestinationRevoked("destination is revoked")
        self._audit_outcome(
            owner_id=owner_id,
            action="push.delivery.client_acked",
            outcome="success",
            meta={
                "device_id": device_id,
                "notification_id": notification_id,
                "delivery_outcome": OUTCOME_ACKED,
            },
        )
        self._metric(
            "push_delivery_outcome_total",
            {
                "outcome": OUTCOME_ACKED,
                "provider": destination.get("provider", "unknown"),
            },
        )
