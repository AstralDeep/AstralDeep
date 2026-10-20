"""Durable A2A publication journal over AstralPlane's system-owned canvas component
store, using Plane's public repositories for idempotent creation and compare-and-set
updates. Built by orchestrator/a2a_publication_policy.py when the A2A server is enabled.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from astralplane.repositories.workspaces import CanvasComponentRecord

from shared.a2a_publication import (
    PublicationStateStore,
    PublicationStoreConflict,
    PublicationStoreUnavailable,
)

logger = logging.getLogger("A2APublicationStore")

STATE_FORMAT = "astral.a2a-publication-state/v1"
SYSTEM_OWNER_ID = "system:astral-a2a-publication"
SYSTEM_CONVERSATION_ID = "astral-a2a-publication-state"
SYSTEM_COMPONENT_ID = "a2a-publication-state"
_COMPONENT_TYPE = "a2a_publication_state"
_TITLE = "A2A publication state"


def initial_state() -> dict[str, Any]:
    return {
        "format": STATE_FORMAT,
        "version": 0,
        "intents": [],
        "publications": [],
    }


class PlaneA2APublicationStore(PublicationStateStore):
    def __init__(
        self,
        *,
        runtime: Any,
        repositories: Any,
        clock=time.time,
        owner_id: str = SYSTEM_OWNER_ID,
        conversation_id: str = SYSTEM_CONVERSATION_ID,
        component_id: str = SYSTEM_COMPONENT_ID,
    ):
        self._runtime = runtime
        self._repositories = repositories
        self._clock = clock
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._component_id = component_id

    def _record(self, transaction) -> CanvasComponentRecord | None:
        self._repositories.history.conversations.create(
            transaction,
            conversation_id=self._conversation_id,
            owner_id=self._owner_id,
            title=_TITLE,
            agent_id=None,
            created_at=0,
        )
        record = self._repositories.workspaces.canvas.get_scoped(
            transaction,
            owner_id=self._owner_id,
            conversation_id=self._conversation_id,
            component_id=self._component_id,
        )
        if record is not None:
            return record
        return self._repositories.workspaces.canvas.create(
            transaction,
            CanvasComponentRecord(
                row_id=self._component_id,
                conversation_id=self._conversation_id,
                owner_id=self._owner_id,
                component_id=self._component_id,
                payload=initial_state(),
                component_type=_COMPONENT_TYPE,
                title=_TITLE,
                position=0,
                created_at=0,
                updated_at=1,
            ),
        )

    def load(self) -> Mapping[str, Any]:
        try:
            with self._runtime.transaction() as transaction:
                record = self._record(transaction)
                payload = record.payload
        except Exception as exc:
            raise PublicationStoreUnavailable(
                "A2A publication state could not be read"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PublicationStoreUnavailable("A2A publication state is malformed")
        return payload

    def save(self, state: Mapping[str, Any], expected_version: int) -> None:
        candidate = dict(state)
        version = candidate.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise PublicationStoreConflict("A2A publication state version is invalid")
        if version != expected_version + 1:
            raise PublicationStoreConflict("A2A publication state version did not advance")
        try:
            with self._runtime.transaction() as transaction:
                record = self._record(transaction)
                current = record.payload
                if not isinstance(current, Mapping):
                    raise PublicationStoreConflict("A2A publication state is malformed")
                current_version = current.get("version")
                if current_version != expected_version:
                    raise PublicationStoreConflict("A2A publication state changed")
                updated_at = max(
                    int(record.updated_at or 0) + 1,
                    int(self._clock() * 1000),
                )
                replaced = self._repositories.workspaces.canvas.replace(
                    transaction,
                    owner_id=self._owner_id,
                    conversation_id=self._conversation_id,
                    component_id=self._component_id,
                    payload=candidate,
                    component_type=_COMPONENT_TYPE,
                    title=_TITLE,
                    expected_updated_at=record.updated_at,
                    updated_at=updated_at,
                )
                if replaced is None:
                    raise PublicationStoreConflict("A2A publication state changed")
        except PublicationStoreConflict:
            raise
        except Exception as exc:
            raise PublicationStoreUnavailable(
                "A2A publication state could not be written"
            ) from exc