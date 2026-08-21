"""Deep-owned host adapter for the Admin Tools Projection surface.

Two tabs (``params.tab``):

* ``quality`` (default) — the feedback-admin read views: latest
  underperforming-tool quality signals and pending knowledge-update
  proposals, with approve/reject actions that reuse the SAME internals
  (and therefore the same audit emission) as the
  ``feedback_admin_router`` endpoints in ``backend/feedback/api.py``
  (``feedback.proposals.apply_accepted`` / ``reject_proposal``).
* ``tutorial`` — tutorial-step administration: full step list including
  archived rows (``OnboardingRepository.list_all_steps`` — the
  ``GET /api/admin/tutorial/steps`` internals), a per-step edit/create
  form, and archive/restore actions, all mirroring the
  ``onboarding_admin_router`` endpoint bodies in
  ``backend/onboarding/api.py`` including their
  ``record_tutorial_step_edited`` audit calls.

This surface is admin-only. ``ADMIN_ONLY = True`` gates it at the
dispatcher; additionally — defense in depth (FR-014) — ``render`` and
EVERY handler hard-check ``"admin" in roles`` themselves and return an
error notice (plus a best-effort ``settings`` audit event for handler
rejections) when the check fails.

Escape-by-default: every dynamic interpolation goes through ``esc()``.
"""
import asyncio
import json
import logging

from pydantic import ValidationError

from feedback.proposals import (
    InvalidArtifactPath,
    StaleProposalError,
    apply_accepted,
    reject_proposal,
)
from feedback.schemas import RATIONALE_MAX_CHARS
from onboarding.recorder import record_tutorial_step_edited
from onboarding.repository import DuplicateSlug, StepNotFound
from onboarding.schemas import (
    AUDIENCE_VALUES,
    TARGET_KIND_VALUES,
    AdminTutorialStepCreateRequest,
    AdminTutorialStepUpdateRequest,
)
from webrender.chrome import chrome_error_block, esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.AdminTools")

TITLE = "Admin tools"
ADMIN_ONLY = True

SURFACE_KEY = "admin_tools"

_DENIED_MESSAGE = "Admin role required for this action."

# Editable tutorial-step columns (slug is create-only; stable thereafter,
# mirroring AdminTutorialStepUpdateRequest which omits it).
_STEP_UPDATE_FIELDS = ("audience", "display_order", "target_kind", "target_key", "title", "body")
_STEP_CREATE_FIELDS = ("slug",) + _STEP_UPDATE_FIELDS

_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_DANGER = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 "
    "text-red-400 border border-red-500/30 hover:bg-red-500/20"
)
_BTN_NEUTRAL = (
    "px-3 py-1.5 rounded-lg text-xs font-medium text-astral-text "
    "border border-white/10 hover:bg-white/5"
)
_INPUT_CLS = (
    "w-full rounded-lg bg-white/10 border border-white/10 px-2 py-1.5 "
    "text-sm text-astral-text"
)
_LABEL_CLS = "flex flex-col gap-1 text-xs text-astral-muted"


def _is_admin(roles) -> bool:
    """True when the session roles include ``admin``."""
    return "admin" in (roles or [])


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

async def render(orch, user_id, roles, params) -> str:
    """Render the Admin tools surface body (tab bar + active tab).

    Args:
        orch: Orchestrator instance (``feedback_repo`` / ``onboarding_repo``).
        user_id: Acting user id (JWT subject).
        roles: Session roles; must contain ``admin`` (re-checked here).
        params: ``{tab?: quality|tutorial, step_id?, draft?}``.

    Returns:
        Body HTML for the modal shell; an error block for non-admins.
    """
    if not _is_admin(roles):
        logger.warning("admin_tools render denied for non-admin user %s", user_id)
        return chrome_error_block("Admin role required to view this surface.")
    params = params or {}
    tab = params.get("tab") or "quality"
    if tab not in ("quality", "tutorial"):
        tab = "quality"
    if tab == "quality":
        body = await asyncio.to_thread(_render_quality, orch)
    else:
        body = await asyncio.to_thread(_render_tutorial, orch, params)
    return _tab_bar(tab) + body


def _tab_bar(active: str) -> str:
    """Tab buttons re-opening this surface with the chosen ``tab`` param."""
    buttons = []
    for key, label in (("quality", "Tool quality"), ("tutorial", "Tutorial admin")):
        payload = json.dumps({"surface": SURFACE_KEY, "params": {"tab": key}})
        if key == active:
            cls = ("bg-astral-primary/20 text-astral-primary "
                   "border-astral-primary/30")
        else:
            cls = "text-astral-muted border-white/10 hover:bg-white/5"
        buttons.append(
            f'<button type="button" role="tab" aria-selected="{esc(str(key == active)).lower()}" '
            f'class="px-3 py-1.5 rounded-lg text-xs font-medium border {cls}" '
            f'data-admin-tab-btn="{esc(key)}" data-ui-action="chrome_open" '
            f"data-ui-payload='{esc(payload)}'>{esc(label)}</button>"
        )
    inner = "".join(buttons)
    return f'<div class="flex items-center gap-2" role="tablist">{inner}</div>'


# ----- Tool quality tab -----------------------------------------------------

def _pct(value) -> str:
    """Format a 0..1 ratio as a percentage string."""
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _iso_short(value) -> str:
    """Trim an ISO timestamp string to a readable minute precision."""
    return (str(value or ""))[:16].replace("T", " ")


def _render_quality(orch) -> str:
    """Tool-quality read views + pending proposals with decide actions.

    Mirrors ``list_flagged`` and ``list_proposals`` in
    ``backend/feedback/api.py`` — the same ``FeedbackRepository`` calls,
    the same per-item enrichment (category breakdown, pending-proposal
    badge via ``to_admin_view``).
    """
    repo = getattr(orch, "feedback_repo", None)
    if repo is None:
        return notice_block("error", "Feedback subsystem not initialized.")
    try:
        snaps, _cursor = repo.list_underperforming(limit=50, cursor=None)
        flagged = []
        for s in snaps:
            cb = repo.category_breakdown(s.agent_id, s.tool_name, s.window_start, s.window_end)
            props, _ = repo.list_proposals(
                status="pending", agent_id=s.agent_id, tool_name=s.tool_name, limit=1,
            )
            flagged.append(s.to_admin_view(
                flagged_at=s.computed_at,
                pending_proposal_id=str(props[0].id) if props else None,
            ) | {"category_breakdown": cb})
        proposals, _ = repo.list_proposals(status="pending", limit=50)
    except Exception:
        logger.exception("admin_tools: failed to load tool-quality data")
        return notice_block("error", "Failed to load tool-quality data.")

    sections = ['<div class="space-y-4" data-admin-tab="quality">']
    sections.append(_flagged_section(flagged))
    sections.append(_proposals_section(proposals))
    sections.append("</div>")
    return "".join(sections)


def _flagged_section(flagged) -> str:
    rows = []
    for item in flagged:
        chips = "".join(
            f'<span class="inline-block px-2 py-0.5 rounded-full bg-white/10 '
            f'text-[10px] text-astral-muted mr-1">{esc(cat)}: {esc(n)}</span>'
            for cat, n in sorted((item.get("category_breakdown") or {}).items())
        )
        badge = ""
        if item.get("pending_proposal_id"):
            badge = (
                '<span class="inline-block px-2 py-0.5 rounded-full bg-astral-primary/20 '
                'text-[10px] text-astral-primary">proposal pending</span>'
            )
        stats = (
            f"Failure rate {_pct(item.get('failure_rate'))} · "
            f"negative feedback {_pct(item.get('negative_feedback_rate'))} · "
            f"{item.get('dispatch_count', 0)} dispatches "
            f"({item.get('failure_count', 0)} failures, "
            f"{item.get('negative_feedback_count', 0)} negative)"
        )
        window = (
            f"Window {_iso_short(item.get('window_start'))} → "
            f"{_iso_short(item.get('window_end'))}"
        )
        rows.append(
            f'<div class="bg-white/5 border border-white/10 rounded-lg p-3 space-y-1">'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="text-sm font-medium text-astral-text">{esc(item.get("tool_name"))}</span>'
            f'<span class="text-xs text-astral-muted">{esc(item.get("agent_id"))}</span></div>'
            f'<div class="text-xs text-astral-muted">{esc(stats)}</div>'
            f'<div class="text-xs text-astral-muted">{esc(window)}</div>'
            f"<div>{chips}{badge}</div></div>"
        )
    if not rows:
        rows.append(
            '<div class="text-sm text-astral-muted italic">'
            "No underperforming tools right now.</div>"
        )
    inner = "".join(rows)
    return (
        '<section class="space-y-2"><h3 class="text-sm font-semibold text-astral-text">'
        f"Underperforming tools</h3>{inner}</section>"
    )


def _proposals_section(proposals) -> str:
    rows = []
    for p in proposals:
        pid = str(p.id)
        ev = p.evidence or {}
        n_audit = len(ev.get("audit_event_ids") or [])
        n_fb = len(ev.get("component_feedback_ids") or [])
        accept_payload = json.dumps({"proposal_id": pid, "decision": "accept"})
        reject_payload = json.dumps({"proposal_id": pid, "decision": "reject"})
        meta = (
            f"Evidence: {n_audit} audit events, {n_fb} feedback items · "
            f"generated {_iso_short(p.generated_at.isoformat())}"
        )
        rows.append(
            f'<div class="bg-white/5 border border-white/10 rounded-lg p-3 space-y-2" '
            f'data-ui-form data-proposal-id="{esc(pid)}">'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="text-sm font-medium text-astral-text">{esc(p.tool_name)}</span>'
            f'<span class="text-xs text-astral-muted">{esc(p.agent_id)}</span></div>'
            f'<div class="text-xs text-astral-muted font-mono">{esc(p.artifact_path)}</div>'
            f'<div class="text-xs text-astral-muted">{esc(meta)}</div>'
            f'<details class="text-xs"><summary class="cursor-pointer text-astral-muted '
            f'hover:text-astral-text">Proposed diff</summary>'
            f'<pre class="mt-1 p-2 rounded-lg bg-black/30 border border-white/10 '
            f'overflow-x-auto text-[11px] text-astral-text">{esc(p.diff_payload)}</pre></details>'
            f'<input type="text" name="rationale" class="{_INPUT_CLS}" '
            f'placeholder="Rationale (required to reject)">'
            f'<div class="flex items-center gap-2">'
            f'<button type="button" class="{_BTN_PRIMARY}" '
            f'data-ui-action="chrome_admin_proposal_decide" '
            f"data-ui-payload='{esc(accept_payload)}'>Approve &amp; apply</button>"
            f'<button type="button" class="{_BTN_DANGER}" '
            f'data-ui-action="chrome_admin_proposal_decide" data-ui-collect="true" '
            f"data-ui-payload='{esc(reject_payload)}'>Reject</button>"
            f"</div></div>"
        )
    if not rows:
        rows.append(
            '<div class="text-sm text-astral-muted italic">'
            "No pending knowledge-update proposals.</div>"
        )
    inner = "".join(rows)
    return (
        '<section class="space-y-2"><h3 class="text-sm font-semibold text-astral-text">'
        f"Pending knowledge-update proposals</h3>{inner}</section>"
    )


# ----- Tutorial admin tab ---------------------------------------------------

def _parse_step_id(value):
    """Coerce a payload/params step id to int; None when absent/invalid."""
    if value in (None, "", "new"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_tutorial(orch, params) -> str:
    """Tutorial-step admin: full list (incl. archived) + edit/create form.

    Step list uses ``list_all_steps(include_archived=True)`` — exactly the
    ``GET /api/admin/tutorial/steps`` internals (onboarding/api.py:255-268).
    """
    repo = getattr(orch, "onboarding_repo", None)
    if repo is None:
        return notice_block("error", "Onboarding subsystem not initialized.")
    try:
        steps = repo.list_all_steps(include_archived=True)
    except Exception:
        logger.exception("admin_tools: failed to load tutorial steps")
        return notice_block("error", "Failed to load tutorial steps.")

    selected_raw = params.get("step_id")
    draft = params.get("draft") if isinstance(params.get("draft"), dict) else None

    new_payload = json.dumps(
        {"surface": SURFACE_KEY, "params": {"tab": "tutorial", "step_id": "new"}}
    )
    parts = [
        '<div class="space-y-4" data-admin-tab="tutorial">',
        '<div class="flex items-center justify-between">'
        '<h3 class="text-sm font-semibold text-astral-text">Tutorial steps</h3>'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_open" '
        f"data-ui-payload='{esc(new_payload)}'>New step</button></div>",
    ]
    if steps:
        parts.extend(_step_row(s) for s in steps)
    else:
        parts.append(
            '<div class="text-sm text-astral-muted italic">No tutorial steps yet.</div>'
        )

    if selected_raw == "new":
        parts.append(_step_form(None, draft, max((s.display_order for s in steps), default=0) + 1))
    else:
        selected = _parse_step_id(selected_raw)
        if selected is not None:
            step = repo.get_step(selected)
            if step is None:
                parts.append(notice_block("error", f"Step {selected} not found."))
            else:
                parts.append(_step_form(step, draft, step.display_order))
    parts.append("</div>")
    return "".join(parts)


def _step_row(step) -> str:
    archived = step.archived_at is not None
    badge = ""
    if archived:
        badge = (
            '<span class="inline-block ml-2 px-2 py-0.5 rounded-full bg-white/10 '
            'text-[10px] text-astral-muted">Archived</span>'
        )
    target = step.target_kind
    if step.target_key:
        target = f"{step.target_kind}:{step.target_key}"
    meta = f"#{step.display_order} · audience {step.audience} · target {target}"
    edit_payload = json.dumps(
        {"surface": SURFACE_KEY, "params": {"tab": "tutorial", "step_id": step.id}}
    )
    toggle_action = "chrome_admin_step_restore" if archived else "chrome_admin_step_archive"
    toggle_label = "Restore" if archived else "Archive"
    toggle_cls = _BTN_NEUTRAL if archived else _BTN_DANGER
    toggle_payload = json.dumps({"step_id": step.id})
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3 flex items-center '
        f'justify-between gap-3" data-step-id="{esc(step.id)}">'
        f'<div class="min-w-0"><div class="text-sm font-medium text-astral-text truncate">'
        f'{esc(step.title)} <span class="text-xs text-astral-muted font-mono">'
        f"({esc(step.slug)})</span>{badge}</div>"
        f'<div class="text-xs text-astral-muted truncate">{esc(meta)}</div></div>'
        f'<div class="flex items-center gap-2 shrink-0">'
        f'<button type="button" class="{_BTN_NEUTRAL}" data-ui-action="chrome_open" '
        f"data-ui-payload='{esc(edit_payload)}'>Edit</button>"
        f'<button type="button" class="{toggle_cls}" data-ui-action="{esc(toggle_action)}" '
        f"data-ui-payload='{esc(toggle_payload)}'>{esc(toggle_label)}</button>"
        f"</div></div>"
    )


def _select(name: str, options, current) -> str:
    opts = []
    for opt in options:
        sel = " selected" if str(current) == str(opt) else ""
        opts.append(f'<option value="{esc(opt)}"{sel}>{esc(opt)}</option>')
    inner = "".join(opts)
    return f'<select name="{esc(name)}" class="{_INPUT_CLS}">{inner}</select>'


def _step_form(step, draft, default_order) -> str:
    """Edit (``step`` set) or create (``step`` None) form for one step.

    ``draft`` — submitted values preserved across a failed save (FR-016).
    Slug is editable on create only (mirrors the PUT contract: slugs are
    stable identifiers), so the edit form shows it read-only and unnamed.
    """
    values = {}
    if step is not None:
        values = {
            "slug": step.slug, "audience": step.audience,
            "display_order": step.display_order, "target_kind": step.target_kind,
            "target_key": step.target_key or "", "title": step.title, "body": step.body,
        }
    else:
        values = {
            "slug": "", "audience": "user", "display_order": default_order,
            "target_kind": "none", "target_key": "", "title": "", "body": "",
        }
    if draft:
        for key in _STEP_CREATE_FIELDS:
            if key in draft and draft[key] is not None:
                values[key] = draft[key]

    if step is None:
        heading = "New tutorial step"
        save_payload = json.dumps({})
        slug_field = (
            f'<label class="{_LABEL_CLS}">Slug'
            f'<input type="text" name="slug" value="{esc(values["slug"])}" '
            f'class="{_INPUT_CLS}" placeholder="unique-slug"></label>'
        )
        form_id = "new"
    else:
        heading = f"Edit step ({step.slug})"
        save_payload = json.dumps({"step_id": step.id})
        slug_field = (
            f'<label class="{_LABEL_CLS}">Slug (stable)'
            f'<input type="text" value="{esc(step.slug)}" readonly disabled '
            f'class="{_INPUT_CLS} opacity-60"></label>'
        )
        form_id = str(step.id)

    cancel_payload = json.dumps({"surface": SURFACE_KEY, "params": {"tab": "tutorial"}})
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-3" '
        f'data-ui-form data-step-form="{esc(form_id)}">'
        f'<h3 class="text-sm font-semibold text-astral-text">{esc(heading)}</h3>'
        f"{slug_field}"
        f'<div class="grid grid-cols-2 gap-3">'
        f'<label class="{_LABEL_CLS}">Audience{_select("audience", AUDIENCE_VALUES, values["audience"])}</label>'
        f'<label class="{_LABEL_CLS}">Display order'
        f'<input type="number" name="display_order" value="{esc(values["display_order"])}" '
        f'class="{_INPUT_CLS}"></label>'
        f'<label class="{_LABEL_CLS}">Target kind{_select("target_kind", TARGET_KIND_VALUES, values["target_kind"])}</label>'
        f'<label class="{_LABEL_CLS}">Target key'
        f'<input type="text" name="target_key" value="{esc(values["target_key"])}" '
        f'class="{_INPUT_CLS}" placeholder="empty for target kind none"></label></div>'
        f'<label class="{_LABEL_CLS}">Title'
        f'<input type="text" name="title" value="{esc(values["title"])}" class="{_INPUT_CLS}"></label>'
        f'<label class="{_LABEL_CLS}">Body'
        f'<textarea name="body" rows="4" class="{_INPUT_CLS}">{esc(values["body"])}</textarea></label>'
        f'<div class="flex items-center gap-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_admin_step_save" '
        f"data-ui-collect=\"true\" data-ui-payload='{esc(save_payload)}'>Save</button>"
        f'<button type="button" class="{_BTN_NEUTRAL}" data-ui-action="chrome_open" '
        f"data-ui-payload='{esc(cancel_payload)}'>Cancel</button>"
        f"</div></div>"
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _audit_denied(user_id, action: str) -> None:
    """Best-effort ``settings`` audit event for a rejected non-admin call."""
    try:
        from audit.recorder import get_recorder, make_correlation_id, now_utc
        from audit.schemas import AuditEventCreate
        rec = get_recorder()
        if rec is None:
            return
        started = now_utc()
        await rec.record(AuditEventCreate(
            actor_user_id=user_id or "unknown",
            auth_principal=user_id or "unknown",
            event_class="settings",
            action_type="settings.admin_tools.denied",
            description=f"Non-admin invocation of admin action {action!r} rejected.",
            correlation_id=make_correlation_id(),
            outcome="failure",
            outcome_detail="missing admin role",
            started_at=started,
            completed_at=started,
        ))
    except Exception:
        logger.exception("admin_tools: failed to audit denied invocation")


async def _deny_non_admin(user_id, roles, action: str):
    """Return the error-path tuple for non-admins, None for admins (FR-014)."""
    if _is_admin(roles):
        return None
    logger.warning("admin_tools: non-admin %s invoked %s — rejected", user_id, action)
    await _audit_denied(user_id, action)
    return (SURFACE_KEY, {}, notice_block("error", _DENIED_MESSAGE))


async def handle_proposal_decide(orch, websocket, user_id, roles, payload):
    """``chrome_admin_proposal_decide {proposal_id, decision, fields?}``.

    accept → ``feedback.proposals.apply_accepted`` (the POST
    ``/proposals/{id}/accept`` internals); reject →
    ``feedback.proposals.reject_proposal`` with the collected rationale
    (the POST ``/proposals/{id}/reject`` internals). Both emit their own
    ``proposal_review`` audit events.
    """
    denied = await _deny_non_admin(user_id, roles, "chrome_admin_proposal_decide")
    if denied:
        return denied
    qparams = {"tab": "quality"}
    payload = payload or {}
    proposal_id = str(payload.get("proposal_id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    if not proposal_id:
        return (SURFACE_KEY, qparams, notice_block("error", "Missing proposal_id."))
    repo = getattr(orch, "feedback_repo", None)
    if repo is None:
        return (SURFACE_KEY, qparams, notice_block("error", "Feedback subsystem not initialized."))

    if decision == "accept":
        try:
            applied = await apply_accepted(
                repo, proposal_id,
                reviewer_user_id=user_id, auth_principal=user_id, edited_diff=None,
            )
        except FileNotFoundError:
            return (SURFACE_KEY, qparams, notice_block("error", "Proposal not found."))
        except StaleProposalError:
            return (SURFACE_KEY, qparams, notice_block(
                "error", "Artifact changed since the proposal was generated (stale proposal)."))
        except InvalidArtifactPath:
            return (SURFACE_KEY, qparams, notice_block("error", "Invalid artifact path."))
        except ValueError as exc:
            return (SURFACE_KEY, qparams, notice_block("error", f"Invalid input: {exc}"))
        return (SURFACE_KEY, qparams, notice_block(
            "success", f"Proposal accepted and applied (status: {applied.status})."))

    if decision == "reject":
        fields = payload.get("fields") or {}
        rationale = str(fields.get("rationale") or payload.get("rationale") or "").strip()
        if not rationale:
            return (SURFACE_KEY, qparams, notice_block(
                "error", "A rationale is required to reject a proposal."))
        if len(rationale) > RATIONALE_MAX_CHARS:
            return (SURFACE_KEY, qparams, notice_block(
                "error", f"Rationale exceeds {RATIONALE_MAX_CHARS} characters."))
        try:
            await reject_proposal(
                repo, proposal_id,
                reviewer_user_id=user_id, auth_principal=user_id, rationale=rationale,
            )
        except FileNotFoundError:
            return (SURFACE_KEY, qparams, notice_block("error", "Proposal not found."))
        except ValueError as exc:
            return (SURFACE_KEY, qparams, notice_block("error", f"Invalid input: {exc}"))
        return (SURFACE_KEY, qparams, notice_block("success", "Proposal rejected."))

    return (SURFACE_KEY, qparams, notice_block(
        "error", f"Unknown decision {decision!r} (expected accept or reject)."))


def _normalize_step_fields(fields: dict) -> dict:
    """Coerce collected form values into repo/schema shapes.

    Raises:
        ValueError: ``display_order`` is not an integer.
    """
    out = {}
    for key in _STEP_CREATE_FIELDS:
        if key not in fields:
            continue
        if key == "display_order":
            continue
        val = fields.get(key)
        if isinstance(val, str) and key in ("slug", "audience", "target_kind", "target_key"):
            val = val.strip()
        out[key] = val
    if "display_order" in fields:
        try:
            out["display_order"] = int(fields["display_order"])
        except (TypeError, ValueError):
            raise ValueError("display_order must be an integer")
    if "target_key" in out and not (out.get("target_key") or "").strip():
        out["target_key"] = None
    return out


def _validation_message(exc: ValidationError) -> str:
    """Condense a pydantic ValidationError into a one-line notice message."""
    try:
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = str(err.get("msg", "invalid"))
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts) or "Invalid input."
    except Exception:
        return str(exc)


async def handle_step_save(orch, websocket, user_id, roles, payload):
    """``chrome_admin_step_save {step_id?, fields}`` — create or update.

    No ``step_id`` → create (the POST ``/api/admin/tutorial/steps``
    internals: ``AdminTutorialStepCreateRequest`` validation,
    ``create_step``, ``record_tutorial_step_edited('create')``).
    With ``step_id`` → partial update (the PUT internals: field
    validation, merged target consistency check, ``update_step``,
    ``record_tutorial_step_edited('update')`` when fields changed).
    Failed saves preserve submitted values via ``params.draft``.
    """
    denied = await _deny_non_admin(user_id, roles, "chrome_admin_step_save")
    if denied:
        return denied
    payload = payload or {}
    fields = payload.get("fields")
    step_id_raw = payload.get("step_id")
    is_create = step_id_raw in (None, "", "new")
    form_key = "new" if is_create else step_id_raw
    err_params = {"tab": "tutorial", "step_id": form_key,
                  "draft": fields if isinstance(fields, dict) else {}}
    if not isinstance(fields, dict) or not fields:
        return (SURFACE_KEY, {"tab": "tutorial"},
                notice_block("error", "No form fields received."))
    repo = getattr(orch, "onboarding_repo", None)
    if repo is None:
        return (SURFACE_KEY, {"tab": "tutorial"},
                notice_block("error", "Onboarding subsystem not initialized."))

    try:
        normalized = _normalize_step_fields(fields)
    except ValueError as exc:
        return (SURFACE_KEY, err_params, notice_block("error", str(exc)))

    if is_create:
        try:
            req = AdminTutorialStepCreateRequest(
                slug=normalized.get("slug") or "",
                audience=normalized.get("audience") or "",
                display_order=normalized.get("display_order", 0),
                target_kind=normalized.get("target_kind") or "",
                target_key=normalized.get("target_key"),
                title=normalized.get("title") or "",
                body=normalized.get("body") or "",
            )
        except ValidationError as exc:
            return (SURFACE_KEY, err_params, notice_block("error", _validation_message(exc)))
        try:
            dto = await asyncio.to_thread(
                repo.create_step,
                editor_user_id=user_id,
                slug=req.slug, audience=req.audience, display_order=req.display_order,
                target_kind=req.target_kind, target_key=req.target_key,
                title=req.title, body=req.body,
            )
        except DuplicateSlug:
            return (SURFACE_KEY, err_params,
                    notice_block("error", "A step with this slug already exists."))
        await record_tutorial_step_edited(
            actor_user_id=user_id, auth_principal=user_id,
            step_id=dto.id, step_slug=dto.slug, change_kind="create",
            changed_fields=list(_STEP_CREATE_FIELDS),
        )
        return (SURFACE_KEY, {"tab": "tutorial"},
                notice_block("success", f"Tutorial step '{dto.slug}' created."))

    step_id = _parse_step_id(step_id_raw)
    if step_id is None:
        return (SURFACE_KEY, {"tab": "tutorial"}, notice_block("error", "Invalid step_id."))
    patch = {k: normalized[k] for k in _STEP_UPDATE_FIELDS if k in normalized}
    try:
        AdminTutorialStepUpdateRequest(**patch)
    except ValidationError as exc:
        return (SURFACE_KEY, err_params, notice_block("error", _validation_message(exc)))
    current = await asyncio.to_thread(repo.get_step, step_id)
    if current is None:
        return (SURFACE_KEY, {"tab": "tutorial"}, notice_block("error", "Step not found."))
    merged_kind = patch.get("target_kind", current.target_kind)
    merged_key = patch.get("target_key", current.target_key)
    if merged_kind == "none" and merged_key is not None:
        return (SURFACE_KEY, err_params, notice_block(
            "error", "Target kind 'none' requires an empty target key."))
    if merged_kind in ("static", "sdui") and not (merged_key or "").strip():
        return (SURFACE_KEY, err_params, notice_block(
            "error", f"Target kind '{merged_kind}' requires a non-empty target key."))
    try:
        dto, changed = await asyncio.to_thread(
            repo.update_step,
            step_id=step_id, editor_user_id=user_id, partial=patch,
        )
    except StepNotFound:
        return (SURFACE_KEY, {"tab": "tutorial"}, notice_block("error", "Step not found."))
    if changed:
        await record_tutorial_step_edited(
            actor_user_id=user_id, auth_principal=user_id,
            step_id=dto.id, step_slug=dto.slug, change_kind="update",
            changed_fields=changed,
        )
        return (SURFACE_KEY, {"tab": "tutorial"},
                notice_block("success", f"Tutorial step '{dto.slug}' saved."))
    return (SURFACE_KEY, {"tab": "tutorial"},
            notice_block("info", "No changes to save."))


async def _toggle_archive(orch, user_id, roles, payload, *, archive: bool):
    """Shared body for archive/restore — the POST archive/restore internals."""
    action = "chrome_admin_step_archive" if archive else "chrome_admin_step_restore"
    denied = await _deny_non_admin(user_id, roles, action)
    if denied:
        return denied
    tparams = {"tab": "tutorial"}
    step_id = _parse_step_id((payload or {}).get("step_id"))
    if step_id is None:
        return (SURFACE_KEY, tparams, notice_block("error", "Invalid step_id."))
    repo = getattr(orch, "onboarding_repo", None)
    if repo is None:
        return (SURFACE_KEY, tparams,
                notice_block("error", "Onboarding subsystem not initialized."))
    try:
        if archive:
            dto = await asyncio.to_thread(
                repo.archive_step, step_id=step_id, editor_user_id=user_id)
        else:
            dto = await asyncio.to_thread(
                repo.restore_step, step_id=step_id, editor_user_id=user_id)
    except StepNotFound:
        return (SURFACE_KEY, tparams, notice_block("error", "Step not found."))
    change_kind = "archive" if archive else "restore"
    await record_tutorial_step_edited(
        actor_user_id=user_id, auth_principal=user_id,
        step_id=dto.id, step_slug=dto.slug, change_kind=change_kind,
        changed_fields=["archived_at"],
    )
    verb = "archived" if archive else "restored"
    return (SURFACE_KEY, tparams,
            notice_block("success", f"Tutorial step '{dto.slug}' {verb}."))


async def handle_step_archive(orch, websocket, user_id, roles, payload):
    """``chrome_admin_step_archive {step_id}`` — soft-delete a step."""
    return await _toggle_archive(orch, user_id, roles, payload, archive=True)


async def handle_step_restore(orch, websocket, user_id, roles, payload):
    """``chrome_admin_step_restore {step_id}`` — restore an archived step."""
    return await _toggle_archive(orch, user_id, roles, payload, archive=False)


HANDLERS = {
    "chrome_admin_proposal_decide": handle_proposal_decide,
    "chrome_admin_step_save": handle_step_save,
    "chrome_admin_step_archive": handle_step_archive,
    "chrome_admin_step_restore": handle_step_restore,
}
