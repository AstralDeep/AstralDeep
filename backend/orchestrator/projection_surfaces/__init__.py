"""Registry mapping each Projection chrome surface key to its host adapter module.
get_surface() lazily imports one by key; collect_handlers() imports every surface and
merges their HANDLERS maps, logging rather than raising on a failed import.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger("Orchestrator.ProjectionSurfaces")

SURFACE_MODULES = {
    "work": "orchestrator.projection_surfaces.work",
    "agents": "orchestrator.projection_surfaces.agents",
    "agent_intro": "orchestrator.projection_surfaces.agent_intro",
    "drafts": "orchestrator.projection_surfaces.drafts",
    "llm": "orchestrator.projection_surfaces.llm",
    "llm_system": "orchestrator.projection_surfaces.llm_system",
    "personalization": "orchestrator.projection_surfaces.personalization",
    "guidance": "orchestrator.projection_surfaces.guidance",
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
    "my_computers": "orchestrator.projection_surfaces.my_computers",
    "connections": "orchestrator.projection_surfaces.connections",
    "saved_results": "orchestrator.projection_surfaces.saved_results",
}


def get_surface(key: str):
    path = SURFACE_MODULES.get(key)
    return importlib.import_module(path) if path else None


def collect_handlers() -> dict[str, tuple[str, object]]:
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
