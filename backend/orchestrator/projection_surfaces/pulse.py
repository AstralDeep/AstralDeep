"""Deep-owned host adapter for the Pulse Projection surface.

A read-only surface that shows the user "here's what I worked out while you
were away": the dreaming sweep's durable memories + still-pending short-term
signals, grouped into a compact card grid by :func:`dreaming.pulse.build_digest`
and rendered to HTML via the same ``render_one`` the canvas uses (so the cards
look identical to canvas cards). It also explains the conversational-scheduling
affordance backed by :func:`dreaming.pulse.propose_schedule` (ask in chat;
confirm; delivery rides the existing scheduler/push path).

Behind the ``FF_PULSE_DIGEST`` flag (default OFF, :func:`dreaming.pulse.pulse_enabled`):
when the flag is off the surface renders a single "feature is off" notice and
no digest cards — the matching top-bar icon is likewise absent (see
``webrender/chrome/topbar.py``).

Never HTTP-to-self — it reads the SAME ``PersonalizationRepository`` the REST
personalization endpoints use, strictly user-scoped. Every dynamic string is
escaped (cards go through ``render_one``, which is escape-by-default).
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

_CARD_WRAP = "grid grid-cols-1 sm:grid-cols-2 gap-3"
# How many durable memories / pending signals to feed the digest builder.
_MAX_MEMORIES = 40
_MAX_SIGNALS = 25


def _repo(orch):
    """The orchestrator's PersonalizationRepository (same as the REST routers)."""
    svc = getattr(orch, "personalization_service", None)
    return getattr(svc, "repo", None) if svc is not None else None


def _digest_items(repo, user_id: str) -> List[Dict[str, Any]]:
    """Map this user's durable memories + pending signals into digest items.

    Each item is the loose dict ``build_digest`` expects
    (``{category, title/value, salience}``). Durable memories carry their
    stored salience; freshly-captured signals get a small floor so recurring
    ones still surface but never outrank promoted memories.
    """
    items: List[Dict[str, Any]] = []
    try:
        memories = repo.list_memory(user_id) if hasattr(repo, "list_memory") else []
    except Exception:  # pragma: no cover - defensive
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
    except Exception:  # pragma: no cover - defensive
        signals = []
    for sig in (signals or [])[:_MAX_SIGNALS]:
        if not isinstance(sig, dict):
            continue
        # Signals are scored below salient memories but above 0 so recurring,
        # not-yet-promoted topics still appear in the digest.
        items.append({
            "category": sig.get("category") or "general",
            "title": sig.get("value") or "",
            "salience": 0.1 + 0.05 * float(sig.get("recall_count", 0) or 0),
        })
    return items


def _intro() -> str:
    """The surface header explaining what Pulse is."""
    return (
        '<p class="text-sm text-astral-muted">A quick read on what the assistant '
        "worked out from your recent activity — recurring topics, goals, and "
        "preferences it is keeping track of. Read-only.</p>"
    )


def _scheduling_hint() -> str:
    """Explain conversational scheduling, demonstrating ``propose_schedule``.

    The example parse is real (it runs ``propose_schedule`` so the hint always
    matches the parser's behavior) — scheduling itself happens in chat behind a
    confirmation, riding the existing scheduler/push path.
    """
    example = "remind me every morning"
    proposal = propose_schedule(example)
    if is_schedulable(proposal):
        parsed = f"{proposal.cadence}" + (f" at {proposal.at}" if proposal.at else "")
    else:  # pragma: no cover - example is always schedulable
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
    """Render the Pulse digest surface body.

    Args:
        orch: Orchestrator (``personalization_service.repo`` is read off it).
        user_id: Authenticated session user; all reads are strictly scoped to it.
        roles: Session roles (unused — Pulse is per-user, not gated).
        params: Unused (the digest is computed from current state).

    Returns:
        Body HTML for the chrome modal (cards via ``render_one``,
        escape-by-default). When ``FF_PULSE_DIGEST`` is off, a single
        "feature is off" notice and nothing else.
    """
    if not pulse_enabled():
        # Flag OFF: the surface is intentionally empty (the matching top-bar
        # icon is also absent), so a direct open just explains the state.
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
    """Feature 044 — the Pulse digest as native SDUI components.

    Same data as ``render()``: intro text + the ``build_digest`` card dicts
    (already astralprims-shaped, so they render through the client's normal
    component renderer). Flag OFF (``FF_PULSE_DIGEST``) → a single notice Alert
    and nothing else, matching the web "feature is off" state. No new handlers.
    The web ``render()`` HTML is unchanged (contract §3.2). Read-only,
    strictly user-scoped (same repo the REST personalization endpoints use).
    """
    if not pulse_enabled():
        # Flag OFF: intentionally empty except a single explanatory notice.
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
    # build_digest returns astralprims-shaped card dicts — the same primitives
    # render_one draws for the web canvas — so they ride the native renderer
    # unchanged.
    out.extend(cards)
    return out
