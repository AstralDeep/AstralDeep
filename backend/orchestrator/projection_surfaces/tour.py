"""Deep-owned host adapter for the product-tour Projection surface.

Renders the guided-tour launcher. The modal body carries the ordered,
audience-filtered tutorial steps (same read path as ``GET
/api/tutorial/steps`` — ``orch.onboarding_repo.list_steps_for_user``) as
escaped JSON in a hidden ``[data-tour-steps]`` holder; ``client.js``
detects the holder in the opened modal, closes the modal, and runs the
step sequence itself (highlighting ``[data-tour-target]`` elements).

The ``chrome_tour_event`` handler persists tour lifecycle outcomes via
the SAME onboarding-state internals the REST endpoints use
(``PUT /api/onboarding/state``, ``POST /api/onboarding/replay``,
``POST /api/onboarding/dismiss`` in ``onboarding/api.py``) — repository
writes first, audit emission after, mirroring the endpoint bodies. The
handler always returns ``None``: the tour runs outside the modal, so no
surface re-render is pushed.
"""
import asyncio
import json
import logging

from onboarding.recorder import (
    record_onboarding_completed,
    record_onboarding_replayed,
    record_onboarding_skipped,
    record_onboarding_started,
)
from webrender import esc
from webrender.chrome import notice_block

logger = logging.getLogger("Orchestrator.Chrome.Tour")

TITLE = "Take the tour"

_VALID_EVENTS = ("started", "completed", "skipped", "dismissed")
_TERMINAL_STATUSES = ("completed", "skipped")


def _repo(orch):
    """Return the onboarding repository wired on the orchestrator, or None."""
    return getattr(orch, "onboarding_repo", None)


def _principal(orch, websocket, user_id):
    """Resolve the audit ``auth_principal`` for the WS session.

    Mirrors ``onboarding.api._principal_of`` using the validated
    ``register_ui`` JWT claims stored in ``orch.ui_sessions`` (the same
    source the chrome dispatcher uses for roles).

    Args:
        orch: Orchestrator instance.
        websocket: The client websocket (key into ``orch.ui_sessions``).
        user_id: Authenticated user id (JWT subject) as fallback.

    Returns:
        The preferred username, subject, user id, or ``"unknown"``.
    """
    try:
        claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket) or {}
    except TypeError:
        claims = {}
    return (
        claims.get("preferred_username")
        or claims.get("sub")
        or user_id
        or "unknown"
    )


def _validated_step_id(repo, raw, is_admin):
    """Validate an optional ``step_id`` the way ``PUT /api/onboarding/state`` does.

    The REST endpoint rejects invalid ids with HTTP 400; mid-tour we drop
    the value instead (warning logged) so an expected bad input never
    interrupts the running tour with an error modal.

    Args:
        repo: ``OnboardingRepository`` (or compatible fake).
        raw: The ``step_id`` value from the event payload (may be None).
        is_admin: Whether the caller holds the admin role.

    Returns:
        The validated integer step id, or ``None`` when absent/invalid.
    """
    if raw is None:
        return None
    try:
        step_id = int(raw)
    except (TypeError, ValueError):
        logger.warning("chrome tour: non-integer step_id %r dropped", raw)
        return None
    audience = repo.get_step_audience(step_id)
    if audience is None:
        logger.warning(
            "chrome tour: step_id %s does not reference a visible step; dropped", step_id
        )
        return None
    if audience == "admin" and not is_admin:
        logger.warning(
            "chrome tour: step_id %s references an admin-only step; dropped", step_id
        )
        return None
    return step_id


async def render(orch, user_id, roles, params) -> str:
    """Render the tour surface body: intro paragraph + step-payload holder.

    Fetches the ordered, audience-filtered steps exactly like
    ``GET /api/tutorial/steps`` (``list_steps_for_user``; ``user``
    audience for everyone, ``admin`` steps included only for admins).

    Args:
        orch: Orchestrator instance (uses ``orch.onboarding_repo``).
        user_id: Authenticated user id (unused — steps are role-scoped).
        roles: Session roles from the validated JWT.
        params: Surface params (unused).

    Returns:
        Body HTML. When steps exist it includes a hidden div carrying
        ``data-tour-steps='<json>'`` (escaped; fields ``id``, ``slug``,
        ``title``, ``body``, ``target_kind``, ``target_key``,
        ``display_order``) that ``client.js`` auto-detects to start the
        tour. With no steps (or no repository) a notice is rendered
        instead and no holder is emitted, so the client does not start.
    """
    intro = (
        '<p class="text-sm text-astral-text/80">The guided tour walks you through '
        "the main controls of AstralDeep, step by step. It starts automatically "
        "&mdash; you can skip or dismiss it at any time.</p>"
    )
    repo = _repo(orch)
    if repo is None:
        return intro + notice_block(
            "error", "The tour is unavailable right now (onboarding subsystem offline)."
        )
    include_admin = "admin" in (roles or [])
    steps = await asyncio.to_thread(repo.list_steps_for_user, include_admin=include_admin)
    if not steps:
        return intro + notice_block("info", "No tour steps are available yet.")
    payload = json.dumps(
        [
            {
                "id": s.id,
                "slug": s.slug,
                "title": s.title,
                "body": s.body,
                "target_kind": s.target_kind,
                "target_key": s.target_key,
                "display_order": s.display_order,
            }
            for s in steps
        ]
    )
    return (
        f"{intro}"
        f'<div hidden aria-hidden="true" data-tour-steps=\'{esc(payload)}\'></div>'
    )


async def _handle_tour_event(orch, websocket, user_id, roles, payload):
    """Persist one tour lifecycle event (``chrome_tour_event``).

    Event mapping (same internals as the onboarding REST endpoints):

    * ``started`` — replay semantics (``POST /replay`` audit) plus, when
      the prior state is not terminal, the ``PUT state=in_progress``
      upsert (incl. the first-start audit) — the endpoint's 409 guard
      against terminal→in_progress becomes a no-op here.
    * ``completed`` / ``skipped`` — ``PUT state=<status>`` semantics:
      upsert, then the matching audit only on an actual transition.
    * ``dismissed`` — ``POST /dismiss`` semantics
      (``record_dismissal(max_dismissals=2)``).

    Args:
        orch: Orchestrator instance.
        websocket: Client websocket (for principal resolution).
        user_id: Authenticated user id (JWT subject).
        roles: Session roles from the validated JWT.
        payload: ``{event: started|completed|skipped|dismissed, step_id?}``.

    Returns:
        ``None`` always — the tour runs outside the modal, so the
        dispatcher must not re-render any surface.
    """
    payload = payload or {}
    event = str(payload.get("event") or "")
    if event not in _VALID_EVENTS:
        logger.warning("chrome tour: unknown tour event %r dropped", event)
        return None

    repo = _repo(orch)
    if repo is None:
        logger.warning("chrome tour: onboarding repository unavailable; %s not persisted", event)
        return None

    principal = _principal(orch, websocket, user_id)
    is_admin = "admin" in (roles or [])
    step_id = await asyncio.to_thread(
        _validated_step_id, repo, payload.get("step_id"), is_admin)

    if event == "dismissed":
        new_state = await asyncio.to_thread(
            repo.record_dismissal, user_id, max_dismissals=2)
        logger.info(
            "chrome tour: user %s dismissed tour (count=%d, status=%s)",
            user_id, new_state.dismiss_count, new_state.status,
        )
        return None

    if event == "started":
        prior = await asyncio.to_thread(repo.get_state, user_id)
        await record_onboarding_replayed(
            actor_user_id=user_id, auth_principal=principal, prior_status=prior.status,
        )
        if prior.status not in _TERMINAL_STATUSES:
            new_state, prior_status = await asyncio.to_thread(
                repo.upsert_state,
                user_id=user_id, status="in_progress", last_step_id=step_id,
            )
            if prior_status is None or prior_status == "not_started":
                await record_onboarding_started(
                    actor_user_id=user_id,
                    auth_principal=principal,
                    step_slug=new_state.last_step_slug,
                )
        return None

    # completed | skipped — PUT /api/onboarding/state semantics.
    new_state, prior_status = await asyncio.to_thread(
        repo.upsert_state,
        user_id=user_id, status=event, last_step_id=step_id,
    )
    if event == "completed" and prior_status != "completed":
        await record_onboarding_completed(
            actor_user_id=user_id,
            auth_principal=principal,
            last_step_slug=new_state.last_step_slug,
        )
    if event == "skipped" and prior_status != "skipped":
        await record_onboarding_skipped(
            actor_user_id=user_id,
            auth_principal=principal,
            last_step_slug=new_state.last_step_slug,
        )
    return None


HANDLERS = {"chrome_tour_event": _handle_tour_event}
