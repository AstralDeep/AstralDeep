"""Product-facing audit facade over AstralPlane's typed audit repositories."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from astralplane.repositories.audit import (
    AuditCursor,
    AuditEvent,
    AuditRecord,
)
from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

from .pii import AuditAnchorAuthenticator, chain_hmac, get_active_key_id
from .schemas import AuditEventCreate, AuditEventDTO, ArtifactPointer


def _authenticate(key_id: str, payload: bytes) -> bytes:
    """Adapt Deep's key custody to Plane's complete chain-payload callback."""

    if len(payload) < 32:
        raise ValueError("audit chain payload is missing its previous digest")
    digest, _used_key = chain_hmac(payload[:32], payload[32:], key_id=key_id)
    return digest


def _record_to_dto(
    record: AuditRecord,
    availability_resolver=None,
) -> AuditEventDTO:
    event = record.event
    pointers_raw = json.loads(event.artifact_pointers_json)
    pointers: List[ArtifactPointer] = []
    for value in pointers_raw:
        item = dict(value)
        if availability_resolver is not None:
            try:
                item["available"] = bool(availability_resolver(item))
            except Exception:  # pragma: no cover - availability is advisory
                item["available"] = True
        else:
            item.setdefault("available", True)
        pointers.append(
            ArtifactPointer(
                **{
                    key: item.get(key)
                    for key in (
                        "artifact_id",
                        "store",
                        "extension",
                        "size_bytes",
                        "available",
                    )
                }
            )
        )
    return AuditEventDTO(
        event_id=event.event_id,
        event_class=event.event_class,
        action_type=event.action_type,
        description=event.description,
        agent_id=event.agent_id,
        conversation_id=event.conversation_id,
        correlation_id=event.correlation_id,
        outcome=event.outcome,
        outcome_detail=event.outcome_detail,
        inputs_meta=json.loads(event.inputs_json),
        outputs_meta=json.loads(event.outputs_json),
        artifact_pointers=pointers,
        started_at=event.started_at,
        completed_at=event.completed_at,
        recorded_at=record.recorded_at,
    )


class AuditRepository:
    """Keep audit policy and DTO shaping in Deep; delegate durability to Plane."""

    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        audit_repository=None,
        audit_retention_repository=None,
    ) -> None:
        audit, runtime = repository_from(
            "audit",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        retention, retention_runtime = repository_from(
            "audit_retention",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._audit = PlaneRepositoryContext(
            repository=audit_repository or audit,
            plane_runtime=runtime,
            legacy_database=db,
        )
        self._retention = PlaneRepositoryContext(
            repository=audit_retention_repository or retention,
            plane_runtime=retention_runtime,
            legacy_database=db,
        )

    def insert(self, event: AuditEventCreate) -> AuditEventDTO:
        durable_event = AuditEvent(
            event_id=str(uuid.uuid4()),
            chain_id=event.actor_user_id,
            auth_principal=event.auth_principal,
            agent_id=event.agent_id,
            event_class=event.event_class,
            action_type=event.action_type,
            description=event.description,
            conversation_id=event.conversation_id,
            correlation_id=event.correlation_id,
            outcome=event.outcome,
            outcome_detail=event.outcome_detail,
            inputs_json=json.dumps(event.inputs_meta),
            outputs_json=json.dumps(event.outputs_meta),
            artifact_pointers_json=json.dumps(
                [pointer.model_dump() for pointer in event.artifact_pointers]
            ),
            started_at=event.started_at,
            completed_at=event.completed_at,
            key_id=get_active_key_id(),
            schema_version=2,
        )
        with self._audit.transaction() as transaction:
            record = self._audit.repository.append(
                transaction,
                durable_event,
                _authenticate,
            )
        return _record_to_dto(record)

    def list_for_user(
        self,
        actor_user_id: str,
        *,
        limit: int = 50,
        cursor: Optional[str] = None,
        event_classes: Optional[List[str]] = None,
        outcomes: Optional[List[str]] = None,
        from_ts: Optional[datetime] = None,
        to_ts: Optional[datetime] = None,
        keyword: Optional[str] = None,
        availability_resolver=None,
    ) -> Tuple[List[AuditEventDTO], Optional[str]]:
        typed_cursor = None
        if cursor:
            try:
                recorded_at, event_id = cursor.split("|", 1)
                uuid.UUID(event_id)
                typed_cursor = AuditCursor(
                    recorded_at=datetime.fromisoformat(recorded_at),
                    event_id=event_id,
                )
            except Exception as exc:
                raise ValueError(f"invalid cursor: {exc}") from exc
        page = self._audit.call(
            self._audit.repository.list_page,
            owner_id=actor_user_id,
            event_classes=event_classes,
            outcomes=outcomes,
            from_ts=from_ts,
            to_ts=to_ts,
            keyword=keyword,
            cursor=typed_cursor,
            limit=limit,
        )
        next_cursor = (
            None
            if page.next_cursor is None
            else (
                f"{page.next_cursor.recorded_at.isoformat()}|"
                f"{page.next_cursor.event_id}"
            )
        )
        return (
            [
                _record_to_dto(record, availability_resolver)
                for record in page.records
            ],
            next_cursor,
        )

    def get_for_user(
        self,
        actor_user_id: str,
        event_id: str,
        availability_resolver=None,
    ) -> Optional[AuditEventDTO]:
        try:
            uuid.UUID(event_id)
        except (TypeError, ValueError):
            return None
        record = self._audit.call(
            self._audit.repository.get,
            chain_id=actor_user_id,
            event_id=event_id,
        )
        return (
            None
            if record is None
            else _record_to_dto(record, availability_resolver)
        )

    def verify_chain(self, actor_user_id: str) -> Optional[str]:
        with self._audit.transaction() as transaction:
            result = self._retention.repository.verify_retained_chain(
                transaction,
                chain_id=actor_user_id,
                audit_repository=self._audit.repository,
                authenticate_event=_authenticate,
                authenticate_anchor=AuditAnchorAuthenticator(),
            )
        return result.first_invalid_event_id

    def purge_older_than(self, actor_user_id: str, cutoff: datetime) -> int:
        """Prune one owner's expired prefix after authenticating its boundary.

        At least one event is retained so the remaining chain has a concrete
        authenticated boundary. The operator must name the owner explicitly;
        cross-owner bulk deletion is intentionally unavailable.
        """

        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        after_sequence = 0
        first_retained = None
        last_record = None
        with self._audit.transaction() as transaction:
            while True:
                records = self._audit.repository.list_for_chain(
                    transaction,
                    chain_id=actor_user_id,
                    after_sequence=after_sequence,
                    limit=1000,
                )
                if not records:
                    break
                for record in records:
                    last_record = record
                    if record.recorded_at >= cutoff:
                        first_retained = record
                        break
                if first_retained is not None or len(records) < 1000:
                    break
                after_sequence = records[-1].sequence
            boundary = first_retained or last_record
            if boundary is None or boundary.sequence <= 1:
                return 0
            policy = json.dumps(
                {
                    "cutoff": cutoff.astimezone(timezone.utc).isoformat(),
                    "policy": "astraldeep-owner-prefix/v1",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            policy_digest = hashlib.sha256(policy).digest()
            result = self._retention.repository.prune_prefix(
                transaction,
                chain_id=actor_user_id,
                first_retained_sequence=boundary.sequence,
                anchor_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "astraldeep:audit-retention:"
                        f"{actor_user_id}:{boundary.sequence}:{policy_digest.hex()}",
                    )
                ),
                policy_digest=policy_digest,
                created_at=datetime.now(timezone.utc),
                key_id=get_active_key_id(),
                authenticator=AuditAnchorAuthenticator(),
            )
        return result.deleted_events


__all__ = ("AuditRepository",)
