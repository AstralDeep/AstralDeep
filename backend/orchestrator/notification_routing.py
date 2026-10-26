"""Deterministic owner notification routing and the durable inbox for AstralDeep:
notify_user payloads are classified into completion/failure/required_action, routed
against Plane-stored owner preferences, quiet hours, urgency, and active-surface
state, and recorded as Fernet-encrypted inbox entries with delivery identities.
notification_api.py exposes the authenticated owner boundary; orchestrator.py
consults route() before foreground and push fan-out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from astralplane.contracts import IsolationLevel
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryError,
    RepositoryNotFoundError,
)

from audit.schemas import AuditEventCreate, AuditEventDTO
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)

EVENT_KINDS = ("completion", "failure", "required_action")
URGENCIES = ("critical", "high", "normal")
_FAILURE_LEVELS = ("error", "critical")
MINUTES_PER_DAY = 1440
SUPPRESSION_WINDOW_SECONDS = 600
INBOX_RETENTION_SECONDS = 30 * 86400
INBOX_LIST_LIMIT = 200
POLICY_NAMESPACE = "astral-notification-policy"
POLICY_CREDENTIAL_KEY = "quiet-hours"
POLICY_VERSION = 1
DEFAULT_QUIET_START_MINUTE = 22 * 60
DEFAULT_QUIET_END_MINUTE = 8 * 60
MAX_TZ_OFFSET_MINUTES = 14 * 60
MAX_SOURCE_CHARS = 64
MAX_TITLE_CHARS = 128
MAX_BODY_CHARS = 256
MAX_ID_CHARS = 128
_DATA_KEYS = ("chat_id", "job_id", "activity_id", "assignment_id")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_CREDENTIAL_KEY_SOURCE = "CREDENTIAL_ENCRYPTION_KEY"
_CONTENT_FORMAT = "astral.notification/fernet+v1"

logger = logging.getLogger("Orchestrator.NotificationRouting")


class NotificationRoutingError(Exception):
    status = 500
    code = "notification_routing_error"


class NotificationPolicyInvalid(NotificationRoutingError):
    status = 400
    code = "notification_policy_invalid"


class NotificationPolicyConflict(NotificationRoutingError):
    status = 409
    code = "notification_policy_conflict"


class NotificationEntryNotFound(NotificationRoutingError):
    status = 404
    code = "notification_entry_not_found"


class NotificationEntryConflict(NotificationRoutingError):
    status = 409
    code = "notification_entry_conflict"


class NotificationStoreUnavailable(NotificationRoutingError):
    status = 503
    code = "notification_store_unavailable"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fernet() -> Fernet:
    key = os.environ.get(_CREDENTIAL_KEY_SOURCE, "")
    if not key:
        raise NotificationStoreUnavailable("notification encryption key is unavailable")
    try:
        return Fernet(key.encode())
    except Exception:
        raise NotificationStoreUnavailable(
            "notification encryption key is invalid"
        ) from None


def _clean_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    return _CONTROL_CHARS.sub(" ", str(value)).strip()[:maximum].strip()


def classify_event(payload: Any) -> str | None:
    if not isinstance(payload, dict) or payload.get("type") != "notification":
        return None
    kind = payload.get("event_kind")
    if isinstance(kind, str) and kind in EVENT_KINDS:
        return kind
    if payload.get("action_required"):
        return "required_action"
    if payload.get("level") in _FAILURE_LEVELS:
        return "failure"
    return "completion"


def resolve_urgency(payload: Any) -> str:
    if isinstance(payload, dict):
        urgency = payload.get("urgency")
        if isinstance(urgency, str) and urgency in URGENCIES:
            return urgency
        if payload.get("level") == "critical":
            return "critical"
        if payload.get("level") == "error":
            return "high"
    return "normal"


def validate_quiet_window(
    *, start_minute: int, end_minute: int, tz_offset_minutes: int
) -> None:
    for name, value in (
        ("start_minute", start_minute),
        ("end_minute", end_minute),
    ):
        if type(value) is not int or not 0 <= value < MINUTES_PER_DAY:
            raise NotificationPolicyInvalid(f"{name} must be within 0..1439")
    if start_minute == end_minute:
        raise NotificationPolicyInvalid("quiet window start and end must differ")
    if (
        type(tz_offset_minutes) is not int
        or abs(tz_offset_minutes) > MAX_TZ_OFFSET_MINUTES
    ):
        raise NotificationPolicyInvalid("tz_offset_minutes must be within +/-840")


def local_minute(now_s: float, tz_offset_minutes: int) -> int:
    utc_minute = int(now_s // 60) % MINUTES_PER_DAY
    return (utc_minute + tz_offset_minutes) % MINUTES_PER_DAY


def in_quiet_hours(policy: dict[str, Any] | None, now_s: float) -> bool:
    if not isinstance(policy, dict) or not policy.get("quiet_enabled"):
        return False
    start = policy.get("quiet_start_minute")
    end = policy.get("quiet_end_minute")
    offset = policy.get("tz_offset_minutes")
    if (
        type(start) is not int
        or type(end) is not int
        or type(offset) is not int
        or start == end
    ):
        return False
    minute = local_minute(now_s, offset)
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def default_quiet_hours() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "quiet_enabled": False,
        "quiet_start_minute": DEFAULT_QUIET_START_MINUTE,
        "quiet_end_minute": DEFAULT_QUIET_END_MINUTE,
        "tz_offset_minutes": 0,
        "updated_at": 0,
    }


def default_preferences() -> dict[str, Any]:
    return {
        "in_app_completion": True,
        "in_app_failure": True,
        "in_app_required_action": True,
        "inbox_completion": True,
        "inbox_failure": True,
        "inbox_required_action": True,
        "push_completion": True,
        "push_failure": True,
        "push_required_action": True,
        "revision": 0,
        "updated_at": 0,
    }


def _preference_record_dict(record: Any) -> dict[str, Any]:
    return {
        "in_app_completion": record.in_app_completion,
        "in_app_failure": record.in_app_failure,
        "in_app_required_action": record.in_app_required_action,
        "inbox_completion": record.inbox_completion,
        "inbox_failure": record.inbox_failure,
        "inbox_required_action": record.inbox_required_action,
        "push_completion": record.push_completion,
        "push_failure": record.push_failure,
        "push_required_action": record.push_required_action,
        "revision": record.revision,
        "updated_at": record.updated_at,
    }


def _deduplication_key(
    owner_id: str, kind: str, payload: dict[str, Any], bucket: int
) -> str:
    canonical = json.dumps(
        {
            "owner_id": owner_id,
            "kind": kind,
            "level": _clean_text(payload.get("level"), 32),
            "source": _clean_text(payload.get("source"), MAX_SOURCE_CHARS),
            "ids": {
                key: _clean_text(payload.get(key), MAX_ID_CHARS)
                for key in _DATA_KEYS
            },
            "title": _clean_text(payload.get("title"), MAX_TITLE_CHARS),
            "body": _clean_text(payload.get("body"), MAX_BODY_CHARS),
            "bucket": bucket,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "notif:" + hashlib.sha256(canonical.encode()).hexdigest()


def _entry_content(
    payload: dict[str, Any], kind: str, urgency: str, created_at: int
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "version": POLICY_VERSION,
        "event_kind": kind,
        "urgency": urgency,
        "level": _clean_text(payload.get("level"), 32) or "info",
        "source": _clean_text(payload.get("source"), MAX_SOURCE_CHARS)
        or "notification",
        "title": _clean_text(payload.get("title"), MAX_TITLE_CHARS),
        "body": _clean_text(payload.get("body"), MAX_BODY_CHARS),
        "created_at": created_at,
    }
    for key in _DATA_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            content[key] = _clean_text(value, MAX_ID_CHARS)
    explicit = payload.get("notification_id")
    if isinstance(explicit, str) and explicit.strip():
        content["notification_id"] = _clean_text(explicit, MAX_ID_CHARS)
    return content


def _public_entry(entry: Any, content: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "event_kind": entry.event_kind,
        "source_id": entry.source_id,
        "deduplication_key": entry.deduplication_key,
        "created_at": entry.created_at,
        "retention_expires_at": entry.retention_expires_at,
        "acknowledged_at": entry.acknowledged_at,
        "deleted_at": entry.deleted_at,
        "revision": entry.revision,
        "content": content,
    }


def _delivery_key(channel: str, entry_id: str) -> str:
    identity = json.dumps([channel, entry_id], separators=(",", ":"))
    return "delivery:" + hashlib.sha256(identity.encode()).hexdigest()


def _decrypt_entry(entry: Any) -> dict[str, Any]:
    try:
        raw = _fernet().decrypt(entry.content_ciphertext)
        content = json.loads(raw)
    except (InvalidToken, ValueError, TypeError):
        raise NotificationStoreUnavailable(
            "notification entry content is undecryptable"
        ) from None
    if not isinstance(content, dict):
        raise NotificationStoreUnavailable("notification entry content is malformed")
    return _public_entry(entry, content)


class NotificationRoutingCoordinator:
    def __init__(
        self,
        db: Any = None,
        *,
        plane_runtime: Any = None,
        plane_repositories: Any = None,
        audit_repository: Any = None,
        clock: Callable[[], float] = time.time,
        metrics: Callable[[str], None] | None = None,
    ) -> None:
        notifications, runtime = repository_from(
            "notifications",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        credentials, _ = repository_from(
            "credentials",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._notifications = PlaneRepositoryContext(
            repository=notifications,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._credentials = PlaneRepositoryContext(
            repository=credentials,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._audit = audit_repository
        self._clock = clock
        self._metrics = metrics
        self._purged_days: dict[str, int] = {}

    def _metric(self, name: str) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics(name)
        except Exception:
            logger.debug("notification routing metric rejected", exc_info=True)

    @contextmanager
    def _policy_transaction(self):
        try:
            with self._credentials.plane_runtime.transaction(
                isolation=IsolationLevel.SERIALIZABLE
            ) as transaction:
                yield transaction
        except RepositoryError:
            raise
        except Exception as exc:
            cause = exc
            for _ in range(4):
                if getattr(cause, "pgcode", None) in {"40001", "40P01"}:
                    raise NotificationPolicyConflict(
                        "notification policy changed concurrently"
                    ) from None
                cause = cause.__cause__ or cause.__context__
                if cause is None:
                    break
            raise NotificationStoreUnavailable(
                "notification policy store is unavailable"
            ) from None

    def _audit_transition(
        self,
        transaction,
        owner_id: str,
        action: str,
        meta: dict[str, Any],
    ) -> None:
        if self._audit is None:
            raise NotificationStoreUnavailable("durable notification audit unavailable")
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
            plane_runtime=self._notifications.plane_runtime,
        )
        if (
            not isinstance(receipt, AuditEventDTO)
            or receipt.event_id != event.event_id
            or receipt.action_type != action
            or receipt.outcome != "success"
        ):
            raise NotificationStoreUnavailable(
                "durable notification audit unavailable"
            )

    def preferences(self, owner_id: str) -> dict[str, Any]:
        def _read(transaction):
            record = self._notifications.repository.get_preferences(
                transaction, owner_id=owner_id
            )
            if record is None:
                return default_preferences()
            return _preference_record_dict(record)

        try:
            return self._notifications.call(_read)
        except Exception:
            raise NotificationStoreUnavailable(
                "notification preferences are unavailable"
            ) from None

    def update_preferences(self, owner_id: str, **routing: Any) -> dict[str, Any]:
        revision = routing.pop("expected_revision", None)
        if type(revision) is not int or revision < 0:
            raise NotificationPolicyInvalid(
                "expected_revision must be a non-negative integer"
            )
        fields = (
            "in_app_completion",
            "in_app_failure",
            "in_app_required_action",
            "inbox_completion",
            "inbox_failure",
            "inbox_required_action",
            "push_completion",
            "push_failure",
            "push_required_action",
        )
        values: dict[str, bool] = {}
        for name in fields:
            value = routing.pop(name, None)
            if type(value) is not bool:
                raise NotificationPolicyInvalid(f"{name} must be a boolean")
            values[name] = value
        if routing:
            raise NotificationPolicyInvalid("unknown preference fields")
        updated_at = _now_ms()

        def _write(transaction):
            record = self._notifications.repository.put_preferences(
                transaction,
                owner_id=owner_id,
                expected_revision=revision,
                updated_at=updated_at,
                **values,
            )
            self._audit_transition(
                transaction,
                owner_id,
                "notification.preference.updated",
                {"revision": record.revision, **values},
            )
            return _preference_record_dict(record)

        try:
            with self._policy_transaction() as transaction:
                return _write(transaction)
        except NotificationRoutingError:
            raise
        except RepositoryNotFoundError:
            raise NotificationPolicyConflict(
                "notification preference revision is stale"
            ) from None
        except RepositoryConflictError:
            raise NotificationPolicyConflict(
                "notification preference revision is stale"
            ) from None
        except Exception:
            raise NotificationStoreUnavailable(
                "notification preferences are unavailable"
            ) from None

    def quiet_hours(self, owner_id: str) -> dict[str, Any]:
        def _read(transaction):
            row = self._credentials.repository.get_credential(
                transaction,
                owner_id=owner_id,
                agent_id=POLICY_NAMESPACE,
                credential_key=POLICY_CREDENTIAL_KEY,
            )
            if row is None:
                return default_quiet_hours()
            try:
                record = json.loads(_fernet().decrypt(row.encrypted_value.encode()))
            except (InvalidToken, ValueError, TypeError):
                raise NotificationStoreUnavailable(
                    "notification policy record is undecryptable"
                ) from None
            if not isinstance(record, dict) or record.get("version") != POLICY_VERSION:
                raise NotificationStoreUnavailable(
                    "notification policy record is malformed"
                )
            return {
                "version": POLICY_VERSION,
                "quiet_enabled": bool(record.get("quiet_enabled")),
                "quiet_start_minute": record.get("quiet_start_minute"),
                "quiet_end_minute": record.get("quiet_end_minute"),
                "tz_offset_minutes": record.get("tz_offset_minutes"),
                "updated_at": row.updated_at,
            }

        try:
            return self._credentials.call(_read)
        except NotificationRoutingError:
            raise
        except Exception:
            raise NotificationStoreUnavailable(
                "notification policy is unavailable"
            ) from None

    def update_quiet_hours(
        self,
        owner_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        start_minute: int,
        end_minute: int,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        if type(expected_revision) is not int or expected_revision < 0:
            raise NotificationPolicyInvalid(
                "expected_revision must be a non-negative integer"
            )
        if type(enabled) is not bool:
            raise NotificationPolicyInvalid("enabled must be a boolean")
        validate_quiet_window(
            start_minute=start_minute,
            end_minute=end_minute,
            tz_offset_minutes=tz_offset_minutes,
        )
        updated_at = _now_ms()
        record = {
            "version": POLICY_VERSION,
            "quiet_enabled": enabled,
            "quiet_start_minute": start_minute,
            "quiet_end_minute": end_minute,
            "tz_offset_minutes": tz_offset_minutes,
        }
        ciphertext = _fernet().encrypt(json.dumps(record).encode())

        def _write(transaction):
            if expected_revision == 0:
                self._credentials.repository.upsert_credential(
                    transaction,
                    owner_id=owner_id,
                    agent_id=POLICY_NAMESPACE,
                    credential_key=POLICY_CREDENTIAL_KEY,
                    encrypted_value=ciphertext.decode(),
                    updated_at=updated_at,
                )
            else:
                self._credentials.repository.compare_and_set_ciphertext(
                    transaction,
                    owner_id=owner_id,
                    agent_id=POLICY_NAMESPACE,
                    credential_key=POLICY_CREDENTIAL_KEY,
                    encrypted_value=ciphertext.decode(),
                    updated_at=updated_at,
                    expected_updated_at=expected_revision,
                )
            self._audit_transition(
                transaction,
                owner_id,
                "notification.quiet_hours.updated",
                {**record},
            )
            return {**record, "updated_at": updated_at}

        try:
            with self._policy_transaction() as transaction:
                return _write(transaction)
        except NotificationRoutingError:
            raise
        except RepositoryConflictError:
            raise NotificationPolicyConflict(
                "notification policy revision is stale"
            ) from None
        except Exception:
            raise NotificationStoreUnavailable(
                "notification policy is unavailable"
            ) from None

    def list_inbox(
        self,
        owner_id: str,
        *,
        limit: int = 50,
        unacknowledged_only: bool = False,
    ) -> list[dict[str, Any]]:
        def _read(transaction):
            entries = self._notifications.repository.list_entries(
                transaction,
                owner_id=owner_id,
                limit=limit,
                unacknowledged_only=unacknowledged_only,
            )
            return [_decrypt_entry(entry) for entry in entries]

        try:
            return self._notifications.call(_read)
        except NotificationRoutingError:
            raise
        except Exception:
            raise NotificationStoreUnavailable(
                "notification inbox is unavailable"
            ) from None

    def get_entry(self, owner_id: str, entry_id: str) -> dict[str, Any]:
        def _read(transaction):
            entry = self._notifications.repository.get_entry(
                transaction, owner_id=owner_id, entry_id=entry_id
            )
            if entry is None:
                raise NotificationEntryNotFound("notification entry not found")
            return _decrypt_entry(entry)

        try:
            return self._notifications.call(_read)
        except NotificationRoutingError:
            raise
        except Exception:
            raise NotificationStoreUnavailable(
                "notification inbox is unavailable"
            ) from None

    def acknowledge(
        self, owner_id: str, entry_id: str, *, expected_revision: int
    ) -> None:
        at = _now_ms()

        def _write(transaction):
            self._notifications.repository.acknowledge_entry(
                transaction,
                owner_id=owner_id,
                entry_id=entry_id,
                expected_revision=expected_revision,
                acknowledged_at=at,
            )
            self._audit_transition(
                transaction,
                owner_id,
                "notification.inbox.acknowledged",
                {"entry_id": entry_id, "revision": expected_revision},
            )

        self._mutate_entry(_write, owner_id, entry_id, expected_revision)

    def delete_entry(
        self, owner_id: str, entry_id: str, *, expected_revision: int
    ) -> None:
        at = _now_ms()

        def _write(transaction):
            self._notifications.repository.delete_entry(
                transaction,
                owner_id=owner_id,
                entry_id=entry_id,
                expected_revision=expected_revision,
                deleted_at=at,
            )
            self._audit_transition(
                transaction,
                owner_id,
                "notification.inbox.deleted",
                {"entry_id": entry_id, "revision": expected_revision},
            )

        self._mutate_entry(_write, owner_id, entry_id, expected_revision)

    def _mutate_entry(
        self, write: Callable[[Any], None], owner_id: str, entry_id: str, expected_revision: int
    ) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise NotificationPolicyInvalid(
                "expected_revision must be a non-negative integer"
            )
        try:
            with self._policy_transaction() as transaction:
                write(transaction)
        except NotificationRoutingError:
            raise
        except RepositoryNotFoundError:
            raise NotificationEntryNotFound("notification entry not found") from None
        except RepositoryConflictError:
            raise NotificationEntryConflict(
                "notification entry revision is stale or already applied"
            ) from None

    def _purge_expired(self, owner_id: str, now_s: float) -> None:
        day = int(now_s // 86400)
        if self._purged_days.get(owner_id) == day:
            return
        self._purged_days[owner_id] = day
        try:
            self._notifications.call(
                lambda transaction: self._notifications.repository.purge_expired_entries(
                    transaction,
                    owner_id=owner_id,
                    expired_at=int(now_s),
                    limit=INBOX_LIST_LIMIT,
                )
            )
        except Exception:
            logger.debug("notification inbox purge failed", exc_info=True)
            self._purged_days.pop(owner_id, None)

    async def route(
        self,
        *,
        owner_id: str,
        payload: Any,
        has_active_surface: bool,
        push_available: bool,
    ) -> dict[str, Any] | None:
        kind = classify_event(payload)
        if kind is None:
            return None
        now_s = self._clock()
        self._purge_expired(owner_id, now_s)
        prefs = self.preferences(owner_id)
        policy = self.quiet_hours(owner_id)
        urgency = resolve_urgency(payload)
        quiet = in_quiet_hours(policy, now_s)
        quiet_blocks = quiet and urgency != "critical"
        foreground = bool(prefs.get(f"in_app_{kind}")) and not quiet_blocks
        record_inbox = bool(prefs.get(f"inbox_{kind}"))
        escalate_push = (
            bool(prefs.get(f"push_{kind}"))
            and push_available
            and not has_active_surface
            and not quiet_blocks
        )
        decision: dict[str, Any] = {
            "foreground": foreground,
            "push": escalate_push,
            "inbox": False,
            "duplicate": False,
            "kind": kind,
            "urgency": urgency,
            "quiet": quiet,
        }
        if not record_inbox:
            self._metric("notification_routing_decisions_total")
            return decision
        bucket = int(now_s // SUPPRESSION_WINDOW_SECONDS)
        deduplication_key = _deduplication_key(owner_id, kind, payload, bucket)
        created_at = int(now_s)
        content = _entry_content(payload, kind, urgency, created_at)
        ciphertext = _fernet().encrypt(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        )

        def _register_identity(transaction, entry_id: str, channel: str) -> None:
            try:
                self._notifications.repository.register_delivery_identity(
                    transaction,
                    owner_id=owner_id,
                    entry_id=entry_id,
                    delivery_key=_delivery_key(channel, entry_id),
                    created_at=created_at,
                )
            except RepositoryConflictError:
                logger.debug(
                    "notification delivery identity already registered channel=%s",
                    channel,
                )

        def _write(transaction):
            try:
                entry = self._notifications.repository.create_entry(
                    transaction,
                    owner_id=owner_id,
                    event_kind=kind,
                    source_id=content["source"],
                    deduplication_key=deduplication_key,
                    content_format=_CONTENT_FORMAT,
                    content_ciphertext=ciphertext,
                    content_sha256=hashlib.sha256(ciphertext).hexdigest(),
                    created_at=created_at,
                    retention_expires_at=created_at + INBOX_RETENTION_SECONDS,
                )
            except RepositoryConflictError:
                decision["duplicate"] = True
                decision["foreground"] = False
                decision["push"] = False
                return None
            if decision["foreground"]:
                _register_identity(transaction, entry.entry_id, "foreground")
            if decision["push"]:
                _register_identity(transaction, entry.entry_id, "push")
            return entry

        try:
            entry = self._notifications.call(_write)
        except NotificationRoutingError:
            raise
        except Exception:
            raise NotificationStoreUnavailable(
                "notification inbox is unavailable"
            ) from None
        decision["inbox"] = entry is not None
        if decision["duplicate"]:
            self._metric("notification_routing_duplicates_total")
        else:
            self._metric("notification_routing_decisions_total")
        return decision


__all__ = (
    "EVENT_KINDS",
    "INBOX_LIST_LIMIT",
    "NotificationRoutingCoordinator",
    "NotificationEntryConflict",
    "NotificationEntryNotFound",
    "NotificationPolicyConflict",
    "NotificationPolicyInvalid",
    "NotificationRoutingError",
    "NotificationStoreUnavailable",
    "URGENCIES",
    "classify_event",
    "default_preferences",
    "default_quiet_hours",
    "in_quiet_hours",
    "local_minute",
    "resolve_urgency",
    "validate_quiet_window",
)
