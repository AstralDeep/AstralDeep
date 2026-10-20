"""Commits the system-owned A2A journal and normal hash-chained audit together through Plane's public repositories.
The registry supplies version-fenced transitions; failed audits or commits preserve the previous journal.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from astralplane.errors import RepositoryConflictError
from astralplane.repositories.workspaces import CanvasComponentRecord

from audit.pii import private_binding_key
from audit.repository import AuditRepository
from audit.schemas import AuditEventCreate, AuditEventDTO
from shared.a2a_publication import (
    PublicationForbiddenError,
    PublicationStateStore,
    PublicationStoreConflict,
    PublicationStoreUnavailable,
)

STATE_FORMAT = "astral.a2a-publication-state/v1"
SYSTEM_OWNER_ID = "system:astral-a2a-publication"
SYSTEM_CONVERSATION_ID = "astral-a2a-publication-state"
SYSTEM_COMPONENT_ID = "a2a-publication-state"
_COMPONENT_TYPE = "a2a_publication_state"
_TITLE = "A2A publication state"


def initial_state() -> dict[str, Any]:
    return {"format": STATE_FORMAT, "version": 0, "intents": [], "publications": []}


class PlaneA2APublicationStore(PublicationStateStore):
    def __init__(self, *, runtime: Any, repositories: Any, audit: AuditRepository,
                 clock=time.time, owner_id: str = SYSTEM_OWNER_ID,
                 conversation_id: str = SYSTEM_CONVERSATION_ID,
                 component_id: str = SYSTEM_COMPONENT_ID):
        self._runtime = runtime
        self._repositories = repositories
        self._audit = audit
        self._clock = clock
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._component_id = component_id

    def _current(self):
        if (self._runtime.repositories is not self._repositories
                or type(self._audit) is not AuditRepository
                or self._audit._audit.plane_runtime is not self._runtime
                or self._audit._audit.repository is not self._repositories.audit):
            raise PublicationStoreUnavailable("A2A publication audit binding is unavailable")
        private_binding_key()

    def _record(self, transaction) -> CanvasComponentRecord:
        self._repositories.agents.lock_owner(transaction, owner_id=self._owner_id)
        self._repositories.history.conversations.create(
            transaction, conversation_id=self._conversation_id, owner_id=self._owner_id,
            title=_TITLE, agent_id=None, created_at=0,
        )
        canvas = self._repositories.workspaces.canvas
        record = canvas.get_scoped(transaction, owner_id=self._owner_id,
                                   conversation_id=self._conversation_id,
                                   component_id=self._component_id)
        if record is None:
            record = canvas.create(transaction, CanvasComponentRecord(
                row_id=self._component_id, conversation_id=self._conversation_id,
                owner_id=self._owner_id, component_id=self._component_id,
                payload=initial_state(), component_type=_COMPONENT_TYPE, title=_TITLE,
                position=0, created_at=0, updated_at=1,
            ))
        record = canvas.get_current_by_row_id(transaction, owner_id=self._owner_id,
                                               row_id=record.row_id, for_update=True)
        if (record is None or record.owner_id != self._owner_id
                or record.conversation_id != self._conversation_id
                or record.component_id != self._component_id
                or record.component_type != _COMPONENT_TYPE
                or not isinstance(record.payload, Mapping)):
            raise PublicationStoreUnavailable("A2A publication journal identity is invalid")
        return record

    def load(self) -> Mapping[str, Any]:
        try:
            self._current()
            with self._runtime.transaction() as transaction:
                payload = self._record(transaction).payload
            return payload
        except Exception as exc:
            raise PublicationStoreUnavailable("A2A publication state could not be read") from exc

    def save(self, state: Mapping[str, Any], expected_version: int,
             event: Mapping[str, Any], *, authorize=None) -> None:
        candidate = dict(state)
        version = candidate.get("version")
        if (isinstance(version, bool) or not isinstance(version, int)
                or version != expected_version + 1):
            raise PublicationStoreConflict("A2A publication state version did not advance")
        try:
            self._current()
            with self._runtime.transaction() as transaction:
                record = self._record(transaction)
                if record.payload.get("version") != expected_version:
                    raise PublicationStoreConflict("A2A publication state changed")
                if authorize is not None and not authorize(str(event.get("agent_id") or "")):
                    raise PublicationForbiddenError("Publication management authority changed")
                self._repositories.workspaces.canvas.replace(
                    transaction, owner_id=self._owner_id, conversation_id=self._conversation_id,
                    component_id=self._component_id, payload=candidate,
                    component_type=_COMPONENT_TYPE, title=_TITLE,
                    expected_updated_at=record.updated_at,
                    updated_at=max(int(record.updated_at or 0) + 1, int(self._clock() * 1000)),
                )
                action = str(event["action"])
                actor = str(event["actor"])
                audit_event = AuditEventCreate(
                    event_id=str(uuid4()), actor_user_id=actor, auth_principal=actor,
                    event_class="settings", action_type=f"a2a_publication.{action}",
                    description=f"A2A publication {action}", correlation_id=str(uuid4()),
                    outcome="failure" if "denied" in action or "stale" in action else "success",
                    inputs_meta=dict(event), started_at=datetime.now(timezone.utc),
                )
                receipt = self._audit.insert_in_transaction(
                    audit_event, transaction=transaction, plane_runtime=self._runtime,
                )
                if (type(receipt) is not AuditEventDTO or receipt.event_id != audit_event.event_id
                        or receipt.action_type != audit_event.action_type
                        or receipt.correlation_id != audit_event.correlation_id
                        or receipt.event_class != audit_event.event_class
                        or receipt.inputs_meta != dict(event)
                        or receipt.outcome != audit_event.outcome):
                    raise PublicationStoreUnavailable("A2A publication audit receipt is invalid")
                self._current()
        except (PublicationStoreConflict, PublicationForbiddenError):
            raise
        except RepositoryConflictError as exc:
            raise PublicationStoreConflict("A2A publication state changed") from exc
        except Exception as exc:
            raise PublicationStoreUnavailable("A2A publication state and audit could not be committed") from exc
