"""Per-user PHI protection policy: redaction and chat reminders stay off until the user turns the
preference on. Reads the AstralPlane preferences repository through the bound orchestrator with a
short cache so tool-output paths pay no query per call; chat_notices, chat_steps, mcp_tools, api,
write_review and persistent_agents consult it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("personalization.phi_policy")

_CACHE_SECONDS = 30.0
_orchestrator: Any = None
_cache: Dict[str, Tuple[float, bool]] = {}


def bind(orchestrator: Any) -> None:
    global _orchestrator
    _orchestrator = orchestrator
    _cache.clear()


def _context(orch: Any):
    from orchestrator.plane_repository_context import (
        PlaneRepositoryContext,
        plane_source_from_orchestrator,
    )

    injected = getattr(orch, "chat_notice_preference_context", None)
    if injected is not None:
        return injected
    source = plane_source_from_orchestrator(orch)
    return PlaneRepositoryContext(
        repository=source.plane_repositories.preferences,
        plane_runtime=source.plane_runtime,
    )


def protection_enabled(user_id: Optional[str], orch: Any = None) -> bool:
    orch = orch if orch is not None else _orchestrator
    if not user_id or orch is None:
        return False
    cached = _cache.get(user_id)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        context = _context(orch)
        enabled = bool(context.call(context.repository.get_phi_protection_enabled, owner_id=user_id))
    except Exception:
        logger.warning("phi_policy.read_failed user=%s", str(user_id)[:8], exc_info=True)
        return cached[1] if cached is not None else False
    _cache[user_id] = (now + _CACHE_SECONDS, enabled)
    return enabled


def set_protection_enabled(user_id: str, enabled: bool, orch: Any = None) -> None:
    orch = orch if orch is not None else _orchestrator
    context = _context(orch)
    context.call(context.repository.set_phi_protection_enabled, owner_id=user_id, enabled=bool(enabled))
    _cache.pop(user_id, None)


def redaction_gate(user_id: Optional[str], orch: Any = None):
    if not protection_enabled(user_id, orch):
        return None
    from personalization.phi_gate import get_phi_gate

    return get_phi_gate()
