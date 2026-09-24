"""Computes a targeted ui_upsert diff between a device profile's old and new render of
each canvas component, so a viewport/orientation change updates only what actually
changed instead of a full ui_render. Gated by FF_LIVE_VIEWPORT.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Tuple

RenderFn = Callable[[Dict[str, Any]], Tuple[Any, Any]]


def viewport_enabled() -> bool:
    return os.getenv("FF_LIVE_VIEWPORT", "false").strip().lower() in ("1", "true", "yes", "on")


def targeted_ops(components: List[Dict[str, Any]],
                 render_old: RenderFn, render_new: RenderFn) -> List[Dict[str, Any]]:
    ops: List[Dict[str, Any]] = []
    for comp in (components or []):
        cid = comp.get("component_id") if isinstance(comp, dict) else None
        if not cid:
            continue
        try:
            _, old_html = render_old(comp)
            adapted, new_html = render_new(comp)
        except Exception:
            continue
        if old_html != new_html:
            ops.append({"op": "upsert", "component_id": cid,
                        "component": adapted, "html": new_html})
    return ops
