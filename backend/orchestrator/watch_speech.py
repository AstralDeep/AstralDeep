"""Attaches spoken SSML/text to watch-profile socket deliveries, rendered from the same
adapted components already on screen via webrender's voice target. Called by
orchestrator.py; other profiles get no speech field.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("orchestrator.watch_speech")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def build_speech(components: Optional[List[Any]]) -> Optional[Dict[str, str]]:
    comps = [c for c in (components or []) if isinstance(c, dict)]
    if not comps:
        return None
    try:
        from webrender.voice import render_voice
        ssml = render_voice(comps)
    except Exception:
        logger.exception("watch_speech: voice rendition failed (delivery stays visual-only)")
        return None
    text = html.unescape(_WS_RE.sub(" ", _TAG_RE.sub(" ", ssml or ""))).strip()
    if not text:
        return None
    return {"ssml": ssml, "text": text}


def speech_for_profile(profile: Any, components: Optional[List[Any]]) -> Optional[Dict[str, str]]:
    try:
        from rote.capabilities import DeviceType
        if profile is None or getattr(profile, "device_type", None) != DeviceType.WATCH:
            return None
    except Exception:
        return None
    return build_speech(components)
