"""Feature 027 — settings-surface registry.

Each surface is a module exporting:

* ``TITLE: str`` — modal title.
* ``ADMIN_ONLY: bool`` (optional, default False) — server-side role gate.
* ``async def render(orch, user_id, roles, params) -> str`` — surface body
  HTML (escape-by-default via ``esc()``; may embed rendered primitives via
  ``render_one``).
* ``HANDLERS: dict[str, handler]`` (optional) — surface-action handlers with
  signature ``async fn(orch, websocket, user_id, roles, payload) -> None |
  (surface_key, params, notice_html)``. Returning a tuple makes the
  dispatcher re-render that surface with the notice prepended (the
  explicit-save → re-render-with-notice contract, FR-016); returning None
  means the handler pushed its own output.

Keeping ``render`` + ``HANDLERS`` together per module keeps surface work
file-disjoint (see plan.md) and lets the dispatcher aggregate handlers
without central edits.
"""
import importlib
import logging

logger = logging.getLogger("Orchestrator.Chrome")

# surface key -> module path (lazy-imported so a broken surface degrades to
# an in-modal error instead of breaking orchestrator startup).
SURFACE_MODULES = {
    "agents": "webrender.chrome.surfaces.agents",
    "drafts": "webrender.chrome.surfaces.drafts",
    "llm": "webrender.chrome.surfaces.llm",
    # Feature 054 — admin-only System LLM credential (web-only carve-out).
    "llm_system": "webrender.chrome.surfaces.llm_system",
    "personalization": "webrender.chrome.surfaces.personalization",
    "audit": "webrender.chrome.surfaces.audit",
    "theme": "webrender.chrome.surfaces.theme",
    "tour": "webrender.chrome.surfaces.tour",
    "guide": "webrender.chrome.surfaces.guide",
    "admin_tools": "webrender.chrome.surfaces.admin_tools",
    # Feature 028 — read-only workspace timeline (research D14).
    "workspace_timeline": "webrender.chrome.surfaces.workspace_timeline",
    # Feature 031 — attachment library (browse / reuse / delete).
    "attachments": "webrender.chrome.surfaces.attachments",
    # Feature 033 (C-U8) — Pulse "morning digest" (flag-gated, default OFF).
    "pulse": "webrender.chrome.surfaces.pulse",
    # Feature 058 — BYO agent authoring + management (flag-gated, default OFF:
    # the surface and every one of its handlers refuse when FF_BYO_AGENTS is off).
    "agent_authoring": "webrender.chrome.surfaces.authoring",
    # Feature 063 — remote machines inventory + per-machine credentials (flag-gated).
    "remote_machines": "webrender.chrome.surfaces.remote_machines",
}


def get_surface(key: str):
    """Resolve a surface module by key; None for unknown keys."""
    path = SURFACE_MODULES.get(key)
    if not path:
        return None
    return importlib.import_module(path)


def collect_handlers() -> dict:
    """Aggregate every surface's HANDLERS dict ({action: (surface_key, fn)})."""
    handlers = {}
    for key, path in SURFACE_MODULES.items():
        try:
            mod = importlib.import_module(path)
        except Exception:
            logger.exception("chrome: surface module %s failed to import", path)
            continue
        for action, fn in (getattr(mod, "HANDLERS", None) or {}).items():
            if action in handlers:
                logger.warning("chrome: duplicate handler for action %s (surface %s)", action, key)
            handlers[action] = (key, fn)
    return handlers
