"""Per-user WebSocket fan-out for AuditEventDTO: delivers audit_append only to
orchestrator connections whose authenticated subject matches the event's owner.
Registered with Recorder via make_publish_callable().
"""

from __future__ import annotations

import logging
from typing import Any

from shared.protocol import AuditAppend

from .schemas import AuditEventDTO

logger = logging.getLogger("Audit.WSPublisher")


class WSPublisher:
    def __init__(self, orchestrator: Any):
        self._orch = orchestrator

    async def publish(self, event: AuditEventDTO, actor_user_id: str) -> None:
        if not actor_user_id:
            return
        msg = AuditAppend(event=event.model_dump(mode="json"))
        payload = msg.to_json()
        sessions = getattr(self._orch, "ui_sessions", {})
        targets = [
            ws for ws, claims in list(sessions.items())
            if (claims or {}).get("sub") == actor_user_id
        ]
        if not targets:
            return
        for ws in targets:
            try:
                await self._orch._safe_send(ws, payload)
            except Exception as exc:  # pragma: no cover
                logger.debug("audit_append send failed: %s", exc)


def make_publish_callable(orchestrator: Any):
    pub = WSPublisher(orchestrator)
    return pub.publish
