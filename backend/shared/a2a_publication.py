"""Shared selective A2A publication contract: owner-chosen capability/audience/lifetime
publications with explicit confirmation, expiry, withdrawal, and a sanitized inventory;
consumed by the A2A card builder, dispatch gate, admin REST router, and chrome surface.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

LIFETIMES: Tuple[Tuple[str, Optional[int]], ...] = (
    ("1h", 3_600),
    ("24h", 86_400),
    ("7d", 604_800),
    ("30d", 2_592_000),
    ("open", None),
)
_LIFETIME_MAP: Dict[str, Optional[int]] = dict(LIFETIMES)

INTENT_TTL_SECONDS = 600
_MAX_INVENTORY = 200
_MAX_PENDING_INTENTS = 50
_MAX_AUDIENCES = 8
_MAX_SCOPE_CHARS = 64

AuditSink = Callable[[Mapping[str, Any]], None]


class A2APublicationError(RuntimeError):
    pass


class UnknownCapabilityError(A2APublicationError):
    pass


class NotEligibleError(A2APublicationError):
    pass


class InvalidAudienceError(A2APublicationError):
    pass


class InvalidLifetimeError(A2APublicationError):
    pass


class StaleIntentError(A2APublicationError):
    pass


class ConfirmationMismatchError(A2APublicationError):
    pass


class UnknownPublicationError(A2APublicationError):
    pass


class AlreadyWithdrawnError(A2APublicationError):
    pass


def normalize_audiences(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise InvalidAudienceError("audiences must be a list of scopes")
    cleaned: list[str] = []
    for item in value:
        scope = str(item).strip()
        if not scope or len(scope) > _MAX_SCOPE_CHARS or any(ch.isspace() for ch in scope):
            raise InvalidAudienceError("each audience must be a non-empty scope token")
        if scope not in cleaned:
            cleaned.append(scope)
    if not cleaned:
        raise InvalidAudienceError("at least one audience is required")
    if len(cleaned) > _MAX_AUDIENCES:
        raise InvalidAudienceError("too many audiences requested")
    return tuple(cleaned)


def _fingerprint(agent_id: str, audiences: Iterable[str], lifetime: str) -> str:
    joined = "\x1f".join([agent_id, *sorted(audiences), lifetime])
    digest = 0
    for byte in joined.encode("utf-8"):
        digest = ((digest * 33) + byte) & 0xFFFFFFFFFFFFFFFF
    return f"{digest:016x}"


@dataclass(frozen=True, slots=True)
class PublicationIntent:
    intent_id: str
    agent_id: str
    audiences: Tuple[str, ...]
    lifetime: str
    fingerprint: str
    created_by: str
    created_at: float
    expires_at: float
    confirmation_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    agent_id: str
    audiences: Tuple[str, ...]
    lifetime: str
    created_by: str
    published_at: float
    expires_at: Optional[float]
    status: str
    withdrawn_at: Optional[float] = None
    withdrawn_by: Optional[str] = None

    def effective_status(self, now: float) -> str:
        if self.status == "active" and self.expires_at is not None and now >= self.expires_at:
            return "expired"
        return self.status


class A2APublicationRegistry:
    """Thread-safe in-process store of publication intents and records.

    State is deployment-level orchestration policy held in the serving process;
    a restart starts empty (fail closed: nothing is advertised until re-published)
    and every transition is mirrored to the audit sink for the hash-chained record.
    """

    def __init__(
        self,
        *,
        catalog: Callable[[], Iterable[str]],
        audiences: Callable[[str], Iterable[str]],
        time_fn: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        token_factory: Callable[[], str] = lambda: uuid.uuid4().hex + uuid.uuid4().hex,
        audit_sink: Optional[AuditSink] = None,
        intent_ttl: float = INTENT_TTL_SECONDS,
    ):
        self._catalog = catalog
        self._audiences = audiences
        self._time_fn = time_fn
        self._id_factory = id_factory
        self._token_factory = token_factory
        self._audit_sink = audit_sink
        self._intent_ttl = intent_ttl
        self._lock = threading.RLock()
        self._intents: Dict[str, PublicationIntent] = {}
        self._records: Dict[str, PublicationRecord] = {}
        self._order: list[str] = []

    def _audit(self, event: Dict[str, Any]) -> None:
        sink = self._audit_sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            pass

    def eligible_catalog(self) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
        now = self._time_fn()
        with self._lock:
            published = {
                record.agent_id: record
                for record in self._records.values()
                if record.effective_status(now) == "active"
            }
        entries = []
        for agent_id in self._catalog():
            entries.append((agent_id, tuple(self._audiences(agent_id)), agent_id in published))
        return tuple(entries)

    def create_intent(
        self,
        *,
        agent_id: str,
        audiences: Any,
        lifetime: str,
        actor: str,
    ) -> PublicationIntent:
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise UnknownCapabilityError("capability is required")
        if lifetime not in _LIFETIME_MAP:
            raise InvalidLifetimeError("unknown lifetime selection")
        requested = normalize_audiences(audiences)
        allowed = tuple(self._audiences(agent_id))
        eligible = agent_id in set(self._catalog())
        if not eligible or not allowed:
            raise NotEligibleError("capability is not in the approved A2A catalog")
        rejected = [scope for scope in requested if scope not in allowed]
        if rejected:
            raise InvalidAudienceError(f"audience not registered for this capability: {rejected[0]}")
        now = self._time_fn()
        intent = PublicationIntent(
            intent_id=self._id_factory(),
            agent_id=agent_id,
            audiences=requested,
            lifetime=lifetime,
            fingerprint=_fingerprint(agent_id, requested, lifetime),
            created_by=actor,
            created_at=now,
            expires_at=now + self._intent_ttl,
            confirmation_token=self._token_factory(),
        )
        with self._lock:
            self._sweep_intents(now)
            if len(self._intents) >= _MAX_PENDING_INTENTS:
                raise StaleIntentError("too many pending publication confirmations")
            self._intents[intent.intent_id] = intent
        self._audit({
            "action": "intent_created",
            "intent_id": intent.intent_id,
            "agent_id": agent_id,
            "audiences": list(requested),
            "lifetime": lifetime,
            "actor": actor,
            "fingerprint": intent.fingerprint,
        })
        return intent

    def confirm(self, *, intent_id: str, confirmation_token: str, actor: str) -> PublicationRecord:
        now = self._time_fn()
        with self._lock:
            intent = self._intents.get(str(intent_id or ""))
            if intent is None:
                self._sweep_intents(now)
                raise UnknownPublicationError("no pending publication confirmation")
            del self._intents[intent.intent_id]
        if not secrets.compare_digest(str(confirmation_token or ""), intent.confirmation_token):
            self._audit({
                "action": "confirm_denied",
                "intent_id": intent.intent_id,
                "agent_id": intent.agent_id,
                "actor": actor,
            })
            raise ConfirmationMismatchError("confirmation did not match the pending publication")
        if now >= intent.expires_at:
            self._audit({
                "action": "confirm_stale",
                "intent_id": intent.intent_id,
                "agent_id": intent.agent_id,
                "actor": actor,
            })
            raise StaleIntentError("publication confirmation expired; create a new intent")
        eligible = intent.agent_id in set(self._catalog())
        allowed = tuple(self._audiences(intent.agent_id))
        if not eligible or any(scope not in allowed for scope in intent.audiences):
            self._audit({
                "action": "confirm_denied",
                "intent_id": intent.intent_id,
                "agent_id": intent.agent_id,
                "actor": actor,
            })
            raise NotEligibleError("capability is no longer in the approved A2A catalog")
        lifetime = _LIFETIME_MAP[intent.lifetime]
        record = PublicationRecord(
            publication_id=self._id_factory(),
            agent_id=intent.agent_id,
            audiences=intent.audiences,
            lifetime=intent.lifetime,
            created_by=actor,
            published_at=now,
            expires_at=None if lifetime is None else now + lifetime,
            status="active",
        )
        with self._lock:
            self._remember(record)
        self._audit({
            "action": "published",
            "publication_id": record.publication_id,
            "agent_id": record.agent_id,
            "audiences": list(record.audiences),
            "lifetime": record.lifetime,
            "expires_at": record.expires_at,
            "actor": actor,
        })
        return record

    def withdraw(self, *, publication_id: str, actor: str, reason: str = "") -> PublicationRecord:
        now = self._time_fn()
        with self._lock:
            record = self._records.get(str(publication_id or ""))
            if record is None:
                raise UnknownPublicationError("unknown publication")
            if record.status != "active":
                raise AlreadyWithdrawnError("publication is not active")
            withdrawn = PublicationRecord(
                publication_id=record.publication_id,
                agent_id=record.agent_id,
                audiences=record.audiences,
                lifetime=record.lifetime,
                created_by=record.created_by,
                published_at=record.published_at,
                expires_at=record.expires_at,
                status="withdrawn",
                withdrawn_at=now,
                withdrawn_by=actor,
            )
            self._records[record.publication_id] = withdrawn
        self._audit({
            "action": "withdrawn",
            "publication_id": record.publication_id,
            "agent_id": record.agent_id,
            "actor": actor,
            "reason": str(reason or "")[:200],
        })
        return withdrawn

    def _remember(self, record: PublicationRecord) -> None:
        self._records[record.publication_id] = record
        self._order.append(record.publication_id)
        while len(self._order) > _MAX_INVENTORY:
            dropped = self._order.pop(0)
            self._records.pop(dropped, None)

    def _sweep_intents(self, now: float) -> None:
        expired = [key for key, intent in self._intents.items() if now >= intent.expires_at]
        for key in expired:
            del self._intents[key]

    def active(self) -> Tuple[PublicationRecord, ...]:
        now = self._time_fn()
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.effective_status(now) == "active"
            )

    def active_agent_ids(self) -> Tuple[str, ...]:
        return tuple(record.agent_id for record in self.active())

    def is_published(self, agent_id: str) -> bool:
        return any(record.agent_id == agent_id for record in self.active())

    def audiences_for(self, agent_id: str) -> Tuple[str, ...]:
        merged: list[str] = []
        for record in self.active():
            if record.agent_id == agent_id:
                for scope in record.audiences:
                    if scope not in merged:
                        merged.append(scope)
        return tuple(merged)

    def advertised_audiences(self) -> Tuple[str, ...]:
        merged: list[str] = []
        for record in self.active():
            for scope in record.audiences:
                if scope not in merged:
                    merged.append(scope)
        return tuple(merged)

    def get(self, publication_id: str) -> Optional[PublicationRecord]:
        with self._lock:
            return self._records.get(str(publication_id or ""))

    def inventory(self) -> Tuple[Dict[str, Any], ...]:
        now = self._time_fn()
        with self._lock:
            ordered = [self._records[key] for key in reversed(self._order)]
        entries = []
        for record in ordered:
            entries.append({
                "publication_id": record.publication_id,
                "agent_id": record.agent_id,
                "audiences": list(record.audiences),
                "lifetime": record.lifetime,
                "status": record.effective_status(now),
                "published_at": record.published_at,
                "expires_at": record.expires_at,
                "withdrawn_at": record.withdrawn_at,
                "created_by": record.created_by,
                "withdrawn_by": record.withdrawn_by,
            })
        return tuple(entries)

    def pending_intents(self, *, actor: Optional[str] = None) -> Tuple[Dict[str, Any], ...]:
        with self._lock:
            intents = list(self._intents.values())
        entries = []
        for intent in intents:
            if actor is not None and intent.created_by != actor:
                continue
            entries.append({
                "intent_id": intent.intent_id,
                "agent_id": intent.agent_id,
                "audiences": list(intent.audiences),
                "lifetime": intent.lifetime,
                "fingerprint": intent.fingerprint,
                "created_by": intent.created_by,
                "expires_at": intent.expires_at,
            })
        return tuple(entries)
