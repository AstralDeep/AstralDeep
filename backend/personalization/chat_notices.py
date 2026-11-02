"""Opt-in, non-persistent chat notices flagging detected PHI awareness, separate from
the mandatory phi_gate.py enforcement; surfaced via orchestrator's
projection_surfaces/personalization.py.
"""

from __future__ import annotations

import asyncio
import logging

from astralprims import Alert

from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    plane_source_from_orchestrator,
)
from personalization.phi_gate import get_phi_gate

logger = logging.getLogger("Personalization.ChatNotices")


def _context(orch):
    injected = getattr(orch, "chat_notice_preference_context", None)
    if injected is not None:
        return injected
    source = plane_source_from_orchestrator(orch)
    return PlaneRepositoryContext(
        repository=source.plane_repositories.preferences,
        plane_runtime=source.plane_runtime,
    )


def notices_enabled(orch, user_id: str) -> bool:
    from personalization.phi_policy import protection_enabled

    return protection_enabled(user_id, orch)


def set_notices_enabled(orch, user_id: str, enabled: bool) -> None:
    from personalization.phi_policy import set_protection_enabled

    set_protection_enabled(user_id, enabled, orch)


async def notify_if_detected(orch, websocket, chat_id: str, user_id: str, message: str) -> None:
    try:
        if not message or not chat_id or not user_id:
            return
        if not await asyncio.to_thread(notices_enabled, orch, user_id):
            return
        if not hasattr(orch, "_phi_notified"):
            orch._phi_notified = set()
        key = (id(websocket), chat_id)
        if key in orch._phi_notified:
            return
        hit = await asyncio.to_thread(get_phi_gate().detect_for_notice, message)
        if not hit:
            return
        if not await asyncio.to_thread(notices_enabled, orch, user_id):
            return
        orch._phi_notified.add(key)
        await orch.send_ui_render(websocket, [Alert(
            title="Possible PHI in your message",
            message=("This message may contain protected health information. Prefer "
                     "synthetic or de-identified data where possible. You can turn off "
                     "these chat reminders in Personalization → Soul. Privacy and "
                     "memory protections stay active."),
            variant="warning",
        ).to_dict()], target="chat")
        logger.info("phi_notice.shown")
    except Exception:
        # Non-fatal: must never block chat dispatch on failure
        logger.debug("phi notice failed (non-fatal)", exc_info=True)
