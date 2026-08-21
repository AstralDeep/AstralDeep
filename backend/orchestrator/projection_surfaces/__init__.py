"""Deep-owned host adapters for AstralProjection chrome surfaces.

AstralProjection owns reusable view models, rendering, adaptation, and static
resources. These adapters remain in AstralDeep because they authorize and
query Deep services or execute Deep commands. Importing Projection must never
import these host modules.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger("Orchestrator.ProjectionSurfaces")

SURFACE_MODULES = {
    "agents": "orchestrator.projection_surfaces.agents",
    "drafts": "orchestrator.projection_surfaces.drafts",
    "llm": "orchestrator.projection_surfaces.llm",
    "llm_system": "orchestrator.projection_surfaces.llm_system",
    "personalization": "orchestrator.projection_surfaces.personalization",
    "audit": "orchestrator.projection_surfaces.audit",
    "theme": "orchestrator.projection_surfaces.theme",
    "tour": "orchestrator.projection_surfaces.tour",
    "guide": "webrender.chrome.surfaces.guide",
    "admin_tools": "orchestrator.projection_surfaces.admin_tools",
    "workspace_timeline": "orchestrator.projection_surfaces.workspace_timeline",
    "attachments": "orchestrator.projection_surfaces.attachments",
    "pulse": "orchestrator.projection_surfaces.pulse",
    "agent_authoring": "orchestrator.projection_surfaces.authoring",
    "remote_machines": "orchestrator.projection_surfaces.remote_machines",
}


def get_surface(key: str):
    """Resolve a registered host adapter, or return ``None`` for unknown keys."""

    path = SURFACE_MODULES.get(key)
    return importlib.import_module(path) if path else None


def collect_handlers() -> dict[str, tuple[str, object]]:
    """Collect host-authorized action handlers outside the Projection package."""

    handlers: dict[str, tuple[str, object]] = {}
    for key, path in SURFACE_MODULES.items():
        try:
            module = importlib.import_module(path)
        except Exception:
            logger.exception("projection surface adapter %s failed to import", path)
            continue
        for action, handler in (getattr(module, "HANDLERS", None) or {}).items():
            if action in handlers:
                logger.warning(
                    "duplicate projection handler action=%s surface=%s", action, key
                )
            handlers[action] = (key, handler)
    return handlers


__all__ = ["SURFACE_MODULES", "collect_handlers", "get_surface"]
