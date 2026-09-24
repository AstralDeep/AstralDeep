"""Renders the read-only Pulse digest surface: durable memories and pending signals from
PersonalizationRepository, grouped by dreaming/pulse.py's build_digest and drawn with
the same render_one cards as the canvas. Empty when FF_PULSE_DIGEST is off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from dreaming.pulse import build_digest, is_schedulable, propose_schedule, pulse_enabled
from webrender.chrome import esc, notice_block, render_one
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Pulse")

TITLE = "Pulse — your digest"
SURFACE_KEY = "pulse"
NO_NAV = True

_CARD_WRAP = "grid grid-cols-1 sm:grid-cols-2 gap-3"
_MAX_MEMORIES = 40
_MAX_SIGNALS = 25


def _repo(orch):
    svc = getattr(orch, "personalization_service", None)
    return getattr(svc, "repo", None) if svc is not None else None


def _digest_items(repo, user_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        memories = repo.list_memory(user_id) if hasattr(repo, "list_memory") else []
    except Exception:  # pragma: no cover
        memories = []
    for mem in (memories or [])[:_MAX_MEMORIES]:
        if not isinstance(mem, dict):
            continue
        items.append({
            "category": mem.get("category") or "general",
            "title": mem.get("value") or "",
            "salience": mem.get("salience") or 0.0,
        })
    try:
        signals = repo.list_signals(user_id) if hasattr(repo, "list_signals") else []
    except Exception:  # pragma: no cover
        signals = []
    for sig in (signals or [])[:_MAX_SIGNALS]:
        if not isinstance(sig, dict):
            continue
        items.append({
            "category": sig.get("category") or "general",
            "title": sig.get("value") or "",
            "salience": 0.1 + 0.05 * float(sig.get("recall_count", 0) or 0),
        })
    return items


def _intro() -> str:
    return (
        '<p class="text-sm text-astral-muted">A quick read on what the assistant '
        "worked out from your recent activity — recurring topics, goals, and "
        "preferences it is keeping track of. Read-only.</p>"
    )


def _scheduling_hint() -> str:
    example = "remind me every morning"
    proposal = propose_schedule(example)
    if is_schedulable(proposal):
        parsed = f"{proposal.cadence}" + (f" at {proposal.at}" if proposal.at else "")
    else:  # pragma: no cover
        parsed = "needs a clearer time"
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-1">'
        f'<div class="text-sm font-medium text-astral-text">Want this on a schedule?</div>'
        f'<p class="text-xs text-astral-muted">Ask in chat — for example, '
        f'"{esc(example)}" (parsed as <span class="text-astral-text">{esc(parsed)}</span>). '
        f"You will be asked to confirm before anything is scheduled, and the digest "
        f"is delivered through your existing notification channel.</p></div>"
    )


async def render(orch, user_id, roles, params) -> str:
    if not pulse_enabled():
        return notice_block(
            "info",
            "The Pulse digest is currently turned off. An administrator can enable "
            "it with the FF_PULSE_DIGEST setting.",
        )

    repo = _repo(orch)
    if repo is None:
        return notice_block("error", "Personalization subsystem is not available.")

    items = await asyncio.to_thread(_digest_items, repo, user_id)
    cards = build_digest(items)
    if not cards:
        body = (
            '<div class="bg-white/5 border border-white/10 rounded-lg p-4 text-sm '
            'text-astral-muted">Nothing to show yet — your digest fills in as you '
            "chat and the assistant notices recurring topics, goals, and "
            "preferences.</div>"
        )
    else:
        body = f'<div class="{_CARD_WRAP}">{"".join(render_one(c) for c in cards)}</div>'

    return (
        f'<div class="space-y-4">{_intro()}{body}{_scheduling_hint()}</div>'
    )


async def components(orch, user_id, roles, params):
    if not pulse_enabled():
        return [_sdui.alert("The Pulse digest is currently turned off. An administrator "
                            "can enable it with the FF_PULSE_DIGEST setting.", "info")]
    repo = _repo(orch)
    if repo is None:
        return [_sdui.alert("Personalization subsystem is not available.", "error")]
    out = [_sdui.text("A quick read on what the assistant worked out from your recent "
                      "activity — recurring topics, goals, and preferences it is keeping "
                      "track of. Read-only.", "caption")]
    cards = build_digest(await asyncio.to_thread(_digest_items, repo, user_id))
    if not cards:
        out.append(_sdui.alert("Nothing to show yet — your digest fills in as you chat "
                               "and the assistant notices recurring topics, goals, and "
                               "preferences.", "info"))
        return out
    out.extend(cards)
    return out
