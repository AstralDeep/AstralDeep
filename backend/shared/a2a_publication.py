"""Owner-controlled A2A publication state: proposal intents, explicit confirmation,
bounded lifetimes, withdrawal, and a sanitized inventory. It is consumed by the A2A
card/dispatch policy, the publication REST API, and the durable Plane state adapter.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, Tuple

logger = logging.getLogger("A2APublication")

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
_STATE_FORMAT = "astral.a2a-publication-state/v1"

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


class PublicationCapacityError(A2APublicationError):
    pass


class PublicationForbiddenError(A2APublicationError):
    pass


class PublicationStoreError(A2APublicationError):
    pass


class PublicationStoreUnavailable(PublicationStoreError):
    pass


class PublicationStoreConflict(PublicationStoreError):
    pass


class PublicationStateStore(Protocol):
    def load(self) -> Optional[Mapping[str, Any]]: ...

    def save(self, state: Mapping[str, Any], expected_version: int,
             event: Mapping[str, Any], *, authorize=None) -> None: ...


def _serialized_transition(method):
    @wraps(method)
    def transition(self, *args, **kwargs):
        authorize = kwargs.pop("authorize", None)
        with self._lock:
            self._authorize = authorize
            try:
                for attempt in range(3):
                    self._ensure_loaded()
                    try:
                        return method(self, *args, **kwargs)
                    except PublicationStoreConflict:
                        if attempt == 2:
                            raise
            finally:
                self._authorize = None
    return transition


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
    owner: Optional[str] = None

    def to_state(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "agent_id": self.agent_id,
            "audiences": list(self.audiences),
            "lifetime": self.lifetime,
            "fingerprint": self.fingerprint,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "confirmation_token": self.confirmation_token,
            "owner": self.owner,
        }

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> "PublicationIntent":
        return cls(
            intent_id=str(value["intent_id"]),
            agent_id=str(value["agent_id"]),
            audiences=tuple(str(item) for item in value["audiences"]),
            lifetime=str(value["lifetime"]),
            fingerprint=str(value["fingerprint"]),
            created_by=str(value["created_by"]),
            created_at=float(value["created_at"]),
            expires_at=float(value["expires_at"]),
            confirmation_token=str(value["confirmation_token"]),
            owner=None if value.get("owner") is None else str(value["owner"]),
        )


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
    owner: Optional[str] = None

    def effective_status(self, now: float) -> str:
        if self.status == "active" and self.expires_at is not None and now >= self.expires_at:
            return "expired"
        return self.status

    def to_state(self) -> Dict[str, Any]:
        return {
            "publication_id": self.publication_id,
            "agent_id": self.agent_id,
            "audiences": list(self.audiences),
            "lifetime": self.lifetime,
            "created_by": self.created_by,
            "published_at": self.published_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "withdrawn_at": self.withdrawn_at,
            "withdrawn_by": self.withdrawn_by,
            "owner": self.owner,
        }

    @classmethod
    def from_state(cls, value: Mapping[str, Any]) -> "PublicationRecord":
        return cls(
            publication_id=str(value["publication_id"]),
            agent_id=str(value["agent_id"]),
            audiences=tuple(str(item) for item in value["audiences"]),
            lifetime=str(value["lifetime"]),
            created_by=str(value["created_by"]),
            published_at=float(value["published_at"]),
            expires_at=(
                None if value.get("expires_at") is None else float(value["expires_at"])
            ),
            status=str(value["status"]),
            withdrawn_at=(
                None if value.get("withdrawn_at") is None else float(value["withdrawn_at"])
            ),
            withdrawn_by=(
                None if value.get("withdrawn_by") is None else str(value["withdrawn_by"])
            ),
            owner=None if value.get("owner") is None else str(value["owner"]),
        )


class A2APublicationRegistry:
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
        store: Optional[PublicationStateStore] = None,
        max_inventory: int = _MAX_INVENTORY,
    ):
        self._catalog = catalog
        self._audiences = audiences
        self._time_fn = time_fn
        self._id_factory = id_factory
        self._token_factory = token_factory
        self._audit_sink = audit_sink
        self._intent_ttl = intent_ttl
        self._store = store
        self._max_inventory = max_inventory
        self._lock = threading.RLock()
        self._intents: Dict[str, PublicationIntent] = {}
        self._records: Dict[str, PublicationRecord] = {}
        self._order: list[str] = []
        self._version = 0
        self._loaded = store is None
        self._degraded = False
        self._authorize = None

    @property
    def available(self) -> bool:
        return not self._degraded

    @property
    def version(self) -> int:
        return self._version

    def refresh(self) -> bool:
        with self._lock:
            if self._store is None:
                self._loaded = True
                self._degraded = False
                return True
            try:
                state = self._store.load()
                intents, records, order, version = self._parse_state(state)
            except Exception:
                logger.exception("A2A publication state could not be loaded")
                self._intents = {}
                self._records = {}
                self._order = []
                self._version = 0
                self._loaded = True
                self._degraded = True
                return False
            self._intents = intents
            self._records = records
            self._order = order
            self._version = version
            self._loaded = True
            self._degraded = False
            return True

    def _parse_state(
        self, state: Optional[Mapping[str, Any]]
    ) -> tuple[Dict[str, PublicationIntent], Dict[str, PublicationRecord], list[str], int]:
        if state is None:
            return {}, {}, [], 0
        if not isinstance(state, Mapping):
            raise PublicationStoreError("publication state is not an object")
        if state.get("format") != _STATE_FORMAT:
            raise PublicationStoreError("publication state format is unsupported")
        raw_intents = state.get("intents", [])
        raw_publications = state.get("publications", [])
        if not isinstance(raw_intents, (list, tuple)) or not isinstance(raw_publications, (list, tuple)):
            raise PublicationStoreError("publication state collections are invalid")
        intents = {
            intent.intent_id: intent
            for intent in (
                PublicationIntent.from_state(value) for value in raw_intents
            )
        }
        records = {
            record.publication_id: record
            for record in (
                PublicationRecord.from_state(value) for value in raw_publications
            )
        }
        order = [record.publication_id for record in records.values()]
        version = state.get("version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PublicationStoreError("publication state version is invalid")
        return intents, records, order, version

    def _state_locked(self) -> Dict[str, Any]:
        return {
            "format": _STATE_FORMAT,
            "version": self._version + 1,
            "intents": [
                intent.to_state()
                for intent in sorted(self._intents.values(), key=lambda item: item.created_at)
            ],
            "publications": [
                self._records[key].to_state()
                for key in self._order
                if key in self._records
            ],
        }

    def _ensure_loaded(self) -> None:
        if not self.refresh():
            raise PublicationStoreUnavailable("A2A publication state is unavailable")

    def _commit_locked(
        self,
        intents: Dict[str, PublicationIntent],
        records: Dict[str, PublicationRecord],
        order: list[str],
        event: Dict[str, Any],
    ) -> None:
        previous = (self._intents, self._records, self._order, self._version)
        self._intents, self._records, self._order = intents, records, order

        def authorize(agent_id):
            if self._authorize is not None and not self._authorize(agent_id):
                return False
            if event["action"] in {"intent_created", "published"}:
                if (agent_id not in set(self._catalog())
                        or any(scope not in self._audiences(agent_id) for scope in event["audiences"])):
                    raise PublicationStoreConflict("Publication eligibility changed")
            if event["action"] == "published":
                intent = previous[0][event["intent_id"]]
                if self._time_fn() >= intent.expires_at:
                    raise PublicationStoreConflict("Publication confirmation expired")
            return True

        try:
            if self._store is None:
                if not authorize(event["agent_id"]):
                    raise PublicationForbiddenError("Publication management authority changed")
                self._audit(event)
            else:
                self._store.save(self._state_locked(), previous[3], event, authorize=authorize)
        except Exception as exc:
            self._intents, self._records, self._order, self._version = previous
            if isinstance(exc, A2APublicationError):
                raise
            raise PublicationStoreUnavailable(
                "A2A publication state and audit could not be committed"
            ) from exc
        self._version += 1

    def _audit(self, event: Dict[str, Any]) -> None:
        if self._store is not None:
            self._ensure_loaded()
            self._commit_locked(dict(self._intents), dict(self._records),
                                list(self._order), event)
        elif self._audit_sink is not None:
            try:
                self._audit_sink(event)
            except Exception as exc:
                raise PublicationStoreUnavailable("A2A publication audit unavailable") from exc

    def record_event(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            self._audit(dict(event))

    def eligible_catalog(self) -> Tuple[Tuple[str, Tuple[str, ...], bool], ...]:
        self.refresh()
        if self._degraded:
            return ()
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

    @_serialized_transition
    def create_intent(
        self,
        *,
        agent_id: str,
        audiences: Any,
        lifetime: str,
        actor: str,
        owner: Optional[str] = None,
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
            owner=owner,
        )
        with self._lock:
            intents = dict(self._intents)
            for key, pending in list(intents.items()):
                if now >= pending.expires_at:
                    del intents[key]
            if len(intents) >= _MAX_PENDING_INTENTS:
                raise StaleIntentError("too many pending publication confirmations")
            intents[intent.intent_id] = intent
            self._commit_locked(intents, dict(self._records), list(self._order), {
                "action": "intent_created",
                "intent_id": intent.intent_id,
                "agent_id": agent_id,
                "audiences": list(requested),
                "lifetime": lifetime,
                "actor": actor,
                "owner": owner,
                "fingerprint": intent.fingerprint,
            })
        return intent

    @_serialized_transition
    def confirm(
        self,
        *,
        intent_id: str,
        confirmation_token: str,
        actor: str,
        privileged: bool = False,
    ) -> PublicationRecord:
        now = self._time_fn()
        with self._lock:
            intent = self._intents.get(str(intent_id or ""))
            if intent is None:
                raise UnknownPublicationError("no pending publication confirmation")

        def _consume(reason: str) -> None:
            with self._lock:
                intents = dict(self._intents)
                intents.pop(intent.intent_id, None)
                self._commit_locked(intents, dict(self._records), list(self._order), {
                    "action": reason,
                    "intent_id": intent.intent_id,
                    "agent_id": intent.agent_id,
                    "actor": actor,
                })

        if not secrets.compare_digest(str(confirmation_token or ""), intent.confirmation_token):
            _consume("confirm_denied")
            raise ConfirmationMismatchError("confirmation did not match the pending publication")
        if not privileged and intent.created_by != actor:
            _consume("confirm_denied")
            raise PublicationForbiddenError("only the proposing owner can confirm this publication")
        if now >= intent.expires_at:
            _consume("confirm_stale")
            raise StaleIntentError("publication confirmation expired; create a new intent")
        eligible = intent.agent_id in set(self._catalog())
        allowed = tuple(self._audiences(intent.agent_id))
        if not eligible or any(scope not in allowed for scope in intent.audiences):
            _consume("confirm_denied")
            raise NotEligibleError("capability is no longer in the approved A2A catalog")
        if any(record.agent_id == intent.agent_id and record.effective_status(now) == "active"
               for record in self._records.values()):
            _consume("confirm_denied")
            raise AlreadyWithdrawnError("capability already has an active publication")
        order = list(self._order)
        records = dict(self._records)
        needed = max(0, len(order) + 1 - self._max_inventory)
        inactive = [key for key in order if records[key].effective_status(now) != "active"]
        if len(inactive) < needed:
            _consume("confirm_denied")
            raise PublicationCapacityError("active publication capacity is full; create a new intent after capacity is available")
        for key in inactive[:needed]:
            order.remove(key)
            records.pop(key)
        lifetime = _LIFETIME_MAP[intent.lifetime]
        record = PublicationRecord(
            publication_id=self._id_factory(),
            agent_id=intent.agent_id,
            audiences=intent.audiences,
            lifetime=intent.lifetime,
            created_by=intent.created_by,
            published_at=now,
            expires_at=None if lifetime is None else now + lifetime,
            status="active",
            owner=intent.owner,
        )
        with self._lock:
            intents = dict(self._intents)
            intents.pop(intent.intent_id, None)
            records[record.publication_id] = record
            order.append(record.publication_id)
            self._commit_locked(intents, records, order, {
                "action": "published",
                "intent_id": intent.intent_id,
                "publication_id": record.publication_id,
                "agent_id": record.agent_id,
                "audiences": list(record.audiences),
                "lifetime": record.lifetime,
                "expires_at": record.expires_at,
                "actor": actor,
                "owner": record.owner,
            })
        return record

    @_serialized_transition
    def withdraw(
        self,
        *,
        publication_id: str,
        actor: str,
        reason: str = "",
        privileged: bool = False,
    ) -> PublicationRecord:
        now = self._time_fn()
        with self._lock:
            record = self._records.get(str(publication_id or ""))
            if record is None:
                raise UnknownPublicationError("unknown publication")
            if record.effective_status(now) != "active":
                raise AlreadyWithdrawnError("publication is not active")
            if not privileged and record.created_by != actor:
                self._audit({
                    "action": "withdraw_denied",
                    "publication_id": record.publication_id,
                    "agent_id": record.agent_id,
                    "actor": actor,
                })
                raise PublicationForbiddenError("only the publishing owner can withdraw")
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
                owner=record.owner,
            )
            records = dict(self._records)
            records[withdrawn.publication_id] = withdrawn
            self._commit_locked(dict(self._intents), records, list(self._order), {
                "action": "withdrawn",
                "publication_id": record.publication_id,
                "agent_id": record.agent_id,
                "actor": actor,
                "reason": str(reason or "")[:200],
            })
        return withdrawn

    def active(self) -> Tuple[PublicationRecord, ...]:
        self.refresh()
        if self._degraded:
            return ()
        now = self._time_fn()
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if record.effective_status(now) == "active"
            )

    def active_agent_ids(self) -> Tuple[str, ...]:
        return tuple(record.agent_id for record in self.active())

    def active_publication(self, agent_id: str) -> Optional[PublicationRecord]:
        for record in self.active():
            if record.agent_id == agent_id:
                return record
        return None

    def is_published(self, agent_id: str) -> bool:
        return self.active_publication(agent_id) is not None

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
        self.refresh()
        if self._degraded:
            return None
        with self._lock:
            return self._records.get(str(publication_id or ""))

    def get_intent(self, intent_id: str) -> Optional[PublicationIntent]:
        self.refresh()
        if self._degraded:
            return None
        with self._lock:
            return self._intents.get(str(intent_id or ""))

    def inventory(self, *, actor: Optional[str] = None) -> Tuple[Dict[str, Any], ...]:
        self.refresh()
        if self._degraded:
            return ()
        now = self._time_fn()
        with self._lock:
            ordered = [self._records[key] for key in reversed(self._order) if key in self._records]
        entries = []
        for record in ordered:
            if actor is not None and record.created_by != actor:
                continue
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
                "owner": record.owner,
            })
        return tuple(entries)

    def pending_intents(
        self, *, actor: Optional[str] = None
    ) -> Tuple[Dict[str, Any], ...]:
        self.refresh()
        if self._degraded:
            return ()
        now = self._time_fn()
        with self._lock:
            intents = list(self._intents.values())
        entries = []
        for intent in intents:
            if now >= intent.expires_at:
                continue
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
                "owner": intent.owner,
            })
        return tuple(entries)
