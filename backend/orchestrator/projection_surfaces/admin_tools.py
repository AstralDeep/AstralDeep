"""Host adapter for the Admin Tools Projection surface's quality and diagnostics tabs,
reusing the same internals and audit emission as feedback/api.py's admin endpoints.
Registered in projection_surfaces via SURFACE_MODULES.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

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

# Excludes slug — matches the update schema's immutability
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
    return "admin" in (roles or [])


async def title(orch, user_id, params) -> str:
    tab = (params or {}).get("tab")
    if tab == "tutorial":
        return "Tutorial admin"
    if tab == "a2a":
        return "A2A publishing"
    return "Tool quality"


async def render(orch, user_id, roles, params) -> str:
    if not _is_admin(roles):
        logger.warning("admin_tools render denied for non-admin user %s", user_id)
        return chrome_error_block("Admin role required to view this surface.")
    params = params or {}
    tab = params.get("tab") or "quality"
    if tab == "tutorial":
        return await asyncio.to_thread(_render_tutorial, orch, params)
    if tab == "a2a":
        body = await asyncio.to_thread(_render_a2a, orch, roles, params)
        return _tab_bar(tab) + body
    if tab not in ("quality", "diagnostics"):
        tab = "quality"
    if tab == "quality":
        body = await asyncio.to_thread(_render_quality, orch)
    else:
        body = await asyncio.to_thread(_render_diagnostics, orch, roles)
    return _tab_bar(tab) + body


def _tab_bar(active: str) -> str:
    buttons = []
    for key, label in (
        ("quality", "Tool quality"),
        ("diagnostics", "Runtime diagnostics"),
        ("a2a", "A2A publishing"),
    ):
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
    return f'<div class="flex flex-wrap items-center gap-2 mb-4" role="tablist">{inner}</div>'


def _render_diagnostics(orch, roles) -> str:
    from astralprojection.chrome import render_html
    from astralprojection.chrome.admin import build_diagnostics_view

    if not _is_admin(roles):
        logger.warning("admin_tools diagnostics denied for non-admin session")
        return render_html(build_diagnostics_view(denied=True))
    collector = getattr(orch, "runtime_observability", None)
    snapshot = getattr(collector, "snapshot", None)
    if not callable(snapshot):
        return render_html(build_diagnostics_view(error="Runtime diagnostics are unavailable."))
    try:
        samples = [{"name": sample.name, "value": sample.value, "labels": dict(sample.labels)}
                   for sample in snapshot()]
    except Exception:
        logger.exception("admin_tools: failed to read the runtime metric snapshot")
        return render_html(build_diagnostics_view(error="Runtime diagnostics are unavailable."))
    return ('<div data-admin-tab="diagnostics">'
            + render_html(build_diagnostics_view(samples)) + "</div>")


def _pct(value) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _iso_short(value) -> str:
    return (str(value or ""))[:16].replace("T", " ")


def _render_quality(orch) -> str:
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


def _parse_step_id(value):
    if value in (None, "", "new"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_tutorial(orch, params) -> str:
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


async def _audit_denied(user_id, action: str) -> None:
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
    if _is_admin(roles):
        return None
    logger.warning("admin_tools: non-admin %s invoked %s — rejected", user_id, action)
    await _audit_denied(user_id, action)
    return (SURFACE_KEY, {}, notice_block("error", _DENIED_MESSAGE))


async def handle_proposal_decide(orch, websocket, user_id, roles, payload):
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
    return await _toggle_archive(orch, user_id, roles, payload, archive=True)


async def handle_step_restore(orch, websocket, user_id, roles, payload):
    return await _toggle_archive(orch, user_id, roles, payload, archive=False)


def _a2a_registry(orch):
    from shared.a2a_publication import A2APublicationRegistry

    registry = getattr(orch, "a2a_publications", None)
    return registry if isinstance(registry, A2APublicationRegistry) else None


def _a2a_server_enabled() -> bool:
    from shared.feature_flags import flags

    return flags.is_enabled("a2a_server")


def _a2a_disabled_notice():
    return notice_block(
        "info",
        "A2A server is disabled on this deployment. Enable FF_A2A_SERVER to publish "
        "capabilities for external A2A clients.",
    )


def _a2a_status_badge(status: str) -> str:
    classes = {
        "active": "border-green-500/20 bg-green-500/10 text-green-400",
        "expired": "border-yellow-500/20 bg-yellow-500/10 text-yellow-400",
        "withdrawn": "border-white/10 bg-white/5 text-astral-muted",
        "eligible": "border-white/10 bg-white/5 text-astral-muted",
        "published": "border-green-500/20 bg-green-500/10 text-green-400",
    }
    cls = classes.get(status, classes["eligible"])
    return (
        f'<span class="px-2 py-0.5 rounded text-xs border {cls}">{esc(status)}</span>'
    )


def _audience_badges(audiences) -> str:
    badges = "".join(
        f'<span class="px-2 py-0.5 rounded text-xs border border-astral-primary/30 '
        f'bg-astral-primary/10 text-astral-primary">{esc(scope)}</span>'
        for scope in audiences
    )
    return f'<div class="flex flex-wrap gap-1">{badges}</div>' if audiences else \
        '<span class="text-xs text-astral-muted">no audiences registered</span>'


def _render_a2a_capability_row(agent_id: str, name: str, description: str,
                               audiences, published: bool) -> str:
    if published:
        state_html = _a2a_status_badge("published")
    elif audiences:
        state_html = (
            f'{_a2a_status_badge("eligible")}'
            f'<div class="text-xs text-astral-muted mt-1">Stage via REST: '
            f'POST /api/a2a/publications/intents with agent_id, audiences and '
            f'lifetime, then confirm the intent.</div>'
        )
    else:
        state_html = (
            f'{_a2a_status_badge("eligible")}'
            f'<div class="text-xs text-astral-muted mt-1">no audiences to publish</div>'
        )
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3 space-y-2" '
        f'data-a2a-capability="{esc(agent_id)}">'
        f'<div class="flex items-center gap-2"><span class="text-sm font-medium text-astral-text">'
        f'{esc(name)}</span>{state_html}</div>'
        f'<div class="text-xs text-astral-muted">{esc(description)}</div>'
        f'{_audience_badges(audiences)}'
        f'</div>'
    )


def _render_a2a_pending_row(entry: dict) -> str:
    expires = _iso_short(
        datetime.fromtimestamp(entry["expires_at"], tz=timezone.utc).isoformat()
        if entry.get("expires_at") else ""
    )
    return (
        f'<div class="bg-white/5 border border-yellow-500/20 rounded-lg p-3 space-y-1" '
        f'data-a2a-intent="{esc(entry["intent_id"])}">'
        f'<div class="text-sm text-astral-text">{esc(entry["agent_id"])} '
        f'<span class="text-xs text-astral-muted">awaiting confirmation</span></div>'
        f'{_audience_badges(entry.get("audiences") or [])}'
        f'<div class="text-xs text-astral-muted">lifetime {esc(entry["lifetime"])} '
        f'&middot; confirmation expires {esc(expires)}</div>'
        f'</div>'
    )


def _render_a2a_publication_row(entry: dict) -> str:
    status = entry.get("status") or "active"
    published = _iso_short(
        datetime.fromtimestamp(entry["published_at"], tz=timezone.utc).isoformat()
        if entry.get("published_at") else ""
    )
    expires = _iso_short(
        datetime.fromtimestamp(entry["expires_at"], tz=timezone.utc).isoformat()
        if entry.get("expires_at") else "open-ended"
    )
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3 space-y-1" '
        f'data-a2a-publication="{esc(entry["publication_id"])}">'
        f'<div class="flex items-center gap-2"><span class="text-sm text-astral-text">'
        f'{esc(entry["agent_id"])}</span>{_a2a_status_badge(status)}</div>'
        f'{_audience_badges(entry.get("audiences") or [])}'
        f'<div class="text-xs text-astral-muted">published {esc(published)} '
        f'&middot; expires {esc(expires)} &middot; lifetime {esc(entry.get("lifetime") or "")}</div>'
        f'</div>'
    )


def _render_a2a(orch, roles, params) -> str:
    if not _is_admin(roles):
        return chrome_error_block("Admin role required to view this surface.")
    if not _a2a_server_enabled() or _a2a_registry(orch) is None:
        return _a2a_disabled_notice()
    registry = _a2a_registry(orch)
    sections = ['<div class="space-y-4" data-admin-tab="a2a">']
    sections.append(notice_block(
        "info",
        "Publication actions run through the admin REST API: stage with "
        "POST /api/a2a/publications/intents, confirm the intent, and withdraw with "
        "DELETE /api/a2a/publications/{publication_id}.",
    ))
    try:
        capabilities = registry.eligible_catalog()
        pending = registry.pending_intents()
        inventory = registry.inventory()
    except Exception:
        logger.exception("admin_tools: failed to load A2A publication state")
        sections.append(notice_block("error", "Failed to load A2A publication state."))
        sections.append("</div>")
        return "".join(sections)
    cards = getattr(orch, "agent_cards", {}) or {}
    sections.append('<h3 class="text-sm font-semibold text-astral-text">Approved capabilities</h3>')
    if capabilities:
        for agent_id, audiences, published in capabilities:
            card = cards.get(agent_id)
            sections.append(_render_a2a_capability_row(
                agent_id,
                str(getattr(card, "name", None) or agent_id),
                str(getattr(card, "description", None) or ""),
                audiences,
                published,
            ))
    else:
        sections.append(notice_block(
            "info",
            "No approved capabilities are eligible for A2A publication yet. Mark a "
            "first-party agent safe to add it to the approved catalog.",
        ))
    sections.append('<h3 class="text-sm font-semibold text-astral-text">Awaiting confirmation</h3>')
    if pending:
        for entry in pending:
            sections.append(_render_a2a_pending_row(entry))
    else:
        sections.append('<div class="text-xs text-astral-muted">No staged publications.</div>')
    sections.append('<h3 class="text-sm font-semibold text-astral-text">Publications</h3>')
    if inventory:
        for entry in inventory:
            sections.append(_render_a2a_publication_row(entry))
    else:
        sections.append('<div class="text-xs text-astral-muted">Nothing published for A2A.</div>')
    sections.append("</div>")
    return "".join(sections)


async def components(orch, user_id, roles, params):
    from webrender.chrome.surfaces import _sdui

    if not _is_admin(roles):
        return [_sdui.alert("Admin role required to view this surface.", "error")]
    params = params or {}
    if (params.get("tab") or "quality") != "a2a":
        return [_sdui.alert(
            "Use the web client for this Admin Tools tab.", "info",
            title="Not available here",
        )]
    if not _a2a_server_enabled() or _a2a_registry(orch) is None:
        return [_sdui.alert(
            "A2A server is disabled on this deployment. Enable FF_A2A_SERVER to publish "
            "capabilities for external A2A clients.", "info",
        )]
    registry = _a2a_registry(orch)
    comps: list = [_sdui.alert(
        "Publishing advertises an approved capability in the public A2A agent card "
        "until it expires or is withdrawn. Publication never grants execution authority. "
        "Stage and confirm publications through the admin REST API "
        "(/api/a2a/publications).",
        "info", title="A2A publishing",
    )]
    cards = getattr(orch, "agent_cards", {}) or {}
    for agent_id, audiences, published in registry.eligible_catalog():
        card = cards.get(agent_id)
        name = str(getattr(card, "name", None) or agent_id)
        state = f"{agent_id}: " + (
            "published" if published else (
                f"eligible audiences: {', '.join(audiences)}" if audiences
                else "no audiences to publish"
            )
        )
        comps.append(_sdui.card(name, [
            _sdui.badge("published" if published else "eligible",
                        "success" if published else "default"),
            _sdui.text(state),
        ]))
    for entry in registry.pending_intents():
        comps.append(_sdui.card(
            f"Awaiting confirmation: {entry['agent_id']}",
            [_sdui.text(f"lifetime {entry['lifetime']} — confirm through the admin REST API")],
        ))
    for entry in registry.inventory():
        status = entry.get("status") or "active"
        comps.append(_sdui.card(entry["agent_id"], [
            _sdui.badge(status, "success" if status == "active" else "default"),
            _sdui.text(f"audiences: {', '.join(entry.get('audiences') or [])} "
                       f"· lifetime {entry.get('lifetime')}")
        ]))
    return comps


HANDLERS = {
    "chrome_admin_proposal_decide": handle_proposal_decide,
    "chrome_admin_step_save": handle_step_save,
    "chrome_admin_step_archive": handle_step_archive,
    "chrome_admin_step_restore": handle_step_restore,
}
