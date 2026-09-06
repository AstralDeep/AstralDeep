"""Deep-owned host adapter for the Personalization Projection surface.

Tabbed surface mirroring the feature-025 REST routers, calling the SAME
service/store internals the endpoints call (never HTTP-to-self):

* ``soul`` (default) — profession / goals / personality-notes form →
  ``chrome_profile_save``. Reuses the exact validation models
  (``ProfileUpdateRequest`` / ``PersonalitySpec``) and PHI gate
  (``get_phi_gate()``) of ``PUT /api/personalization/profile``
  (backend/personalization/api.py).
* ``memory`` — durable memory list with inline edit (``chrome_memory_update``)
  and delete (``chrome_memory_delete``) via ``PersonalizationRepository``.
* ``skills`` — skill catalog (agent tool × scope × availability) with
  ``chrome_skill_toggle`` via ``ToolPermissionManager`` (FR-011 scope-bounding
  preserved: enabling can never exceed the user's granted scope).
* ``schedule`` — scheduled-job list + inline run history with
  ``chrome_job_pause`` / ``chrome_job_resume`` / ``chrome_job_delete`` /
  ``chrome_job_run_now`` via ``ScheduledJobStore`` (delete is the soft
  ``status='disabled'`` the REST endpoint performs). Job creation happens in
  chat (a hint line is rendered).
* ``dreaming`` — consolidation opt-out toggle (``chrome_dreaming_toggle``),
  recent sweeps, and a manual sweep trigger (``chrome_dreaming_trigger``)
  via ``dreaming.consolidation.run_sweep``.

Every mutating handler is explicit-save: it performs the change, emits the
same audit event the REST endpoint emits (``record_generic``), and returns
``(surface_key, params, notice_html)`` so the dispatcher re-renders the tab
with an inline success/error notice (FR-016). Expected failures (PHI
rejection, not-found, scope denial, bad input) never raise. Every dynamic
string is escaped via ``esc()``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime

from audit.hooks import record_generic
from dreaming.consolidation import run_sweep
from personalization.phi_gate import get_phi_gate
from personalization.schemas import PersonalitySpec, ProfileUpdateRequest
from pydantic import ValidationError
from scheduler.store import ScheduleActionError, ScheduledJobStore
from shared.feature_flags import flags
from webrender.chrome import esc, notice_block
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Personalization")

TITLE = "Personalization"
SURFACE_KEY = "personalization"

_TABS = (
    ("soul", "Soul"),
    ("memory", "Memory"),
    ("skills", "Skills"),
    ("schedule", "Schedule"),
    ("dreaming", "Dreaming"),
)
_TAB_KEYS = {key for key, _ in _TABS}

# Shared Tailwind class strings (visual language of webrender/renderer.py).
_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_GHOST = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 text-astral-text "
    "border border-white/10 hover:bg-white/10"
)
_BTN_DANGER = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 text-red-400 "
    "border border-red-500/20 hover:bg-red-500/20"
)
_INPUT_CLS = (
    "w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm "
    "text-astral-text placeholder-astral-muted focus:outline-none "
    "focus:border-astral-primary/50"
)
_LABEL_CLS = "block text-xs font-medium text-astral-muted mb-1"
_CARD_CLS = "bg-white/5 border border-white/10 rounded-lg p-4"

_STATUS_BADGES = {
    "active": "bg-green-500/10 text-green-400 border-green-500/20",
    "paused": "bg-yellow-500/10 text-yellow-400 border-yellow-500/20",
    "completed": "bg-white/5 text-astral-muted border-white/10",
}
_OUTCOME_COLORS = {
    "success": "text-green-400",
    "failure": "text-red-400",
    "skipped_auth": "text-yellow-400",
    "interrupted": "text-yellow-400",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _payload_attr(data) -> str:
    """JSON-encode ``data`` and escape it for a single-quoted HTML attribute."""
    return esc(json.dumps(data))


def _btn(label: str, action: str, payload=None, *, cls: str = _BTN_PRIMARY,
         collect: bool = False) -> str:
    """Render a ``data-ui-action`` button (optionally form-collecting)."""
    collect_attr = ' data-ui-collect="true"' if collect else ""
    return (
        f'<button type="button" class="{cls}" data-ui-action="{esc(action)}"'
        f"{collect_attr} data-ui-payload='{_payload_attr(payload or {})}'>"
        f"{esc(label)}</button>"
    )


def _fmt_ts(ms) -> str:
    """Format an epoch-milliseconds value as a short UTC timestamp."""
    if not ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _claims(orch, websocket, user_id: str) -> dict:
    """JWT claims for audit attribution — session claims when available."""
    try:
        sessions = getattr(orch, "ui_sessions", None) or {}
        claims = sessions.get(websocket)
    except Exception:  # noqa: BLE001 - legacy audit attribution fallback, never assignment authority
        claims = None
    return claims or {"sub": user_id}


def _svc(orch):
    """The orchestrator's PersonalizationService (same as the REST routers)."""
    return getattr(orch, "personalization_service", None)


def _job_store(orch):
    """A ScheduledJobStore over the application Plane runtime, or None."""

    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    injected = getattr(orch, "scheduled_job_store", None)
    if injected is not None:
        return injected
    coordinator = getattr(orch, "work_admission", None)
    if coordinator is None:
        return None
    source = plane_source_from_orchestrator(orch)
    return ScheduledJobStore(
        coordinator=coordinator,
        plane_runtime=source.plane_runtime,
        plane_repositories=source.plane_repositories,
    )


def _params(tab: str, **extra) -> dict:
    """Params dict for a re-render of this surface on ``tab``."""
    out = {"tab": tab}
    out.update(extra)
    return out


def _contains_phi(value) -> bool:
    """PHI-gate check (may lazily load the analyzer; run off the event loop)."""
    return get_phi_gate().contains_phi(value)


def _phi_reject_field(body, notes):
    """First PHI-rejected soul-form field label, or None (run off the loop)."""
    gate = get_phi_gate()
    if body.profession and gate.contains_phi(body.profession):
        return "profession"
    for goal in body.goals or []:
        if gate.contains_phi(goal):
            return "goals"
    if notes and gate.contains_phi(notes):
        return "personality notes"
    return None


def _run_manual_sweep(repo, user_id):
    """Run a manual consolidation sweep (sync + CPU-heavy; run off the loop)."""
    return run_sweep(repo, get_phi_gate(), user_id, trigger="manual")


def _phi_notice(field: str) -> str:
    """Error notice matching the REST PHI-rejection reason text."""
    return notice_block(
        "error",
        f"'{field}' was rejected: it looks like protected health information "
        "and cannot be stored.",
    )


def _unavailable(message: str) -> str:
    """Notice for a missing backend subsystem (mirrors the routers' 503s)."""
    return notice_block("error", message)


def _tab_bar(active: str) -> str:
    """The tab strip — each tab is a ``chrome_open`` button carrying its tab."""
    parts = []
    for key, label in _TABS:
        payload = _payload_attr({"surface": SURFACE_KEY, "params": {"tab": key}})
        if key == active:
            cls = (
                "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
                "text-astral-primary border border-astral-primary/30"
            )
            current = ' aria-current="true"'
        else:
            cls = (
                "px-3 py-1.5 rounded-lg text-xs font-medium text-astral-muted "
                "hover:text-astral-text hover:bg-white/5"
            )
            current = ""
        parts.append(
            f'<button type="button" role="tab" class="{cls}"{current} '
            f"data-ui-action=\"chrome_open\" data-ui-payload='{payload}'>"
            f"{esc(label)}</button>"
        )
    inner = "".join(parts)
    return (
        f'<div class="flex flex-wrap gap-1 border-b border-white/10 pb-2" '
        f'role="tablist" aria-label="Personalization sections">{inner}</div>'
    )


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

async def render(orch, user_id, roles, params) -> str:
    """Render the personalization surface body for ``params.tab``.

    Args:
        orch: The orchestrator (service/DB internals are read off it).
        user_id: The session user (all data strictly user-scoped).
        roles: Session roles (unused — surface is available to everyone).
        params: Optional dict; ``tab`` selects the section (default ``soul``)
            and ``draft`` (soul only) re-fills the form after a failed save.

    Returns:
        Body HTML (the dispatcher wraps it in the modal shell).
    """
    params = params or {}
    tab = params.get("tab") or "soul"
    if tab not in _TAB_KEYS:
        tab = "soul"
    if tab == "soul":
        body = await asyncio.to_thread(_render_soul, orch, user_id, params)
    elif tab == "memory":
        body = await _render_memory(orch, user_id)
    elif tab == "skills":
        body = await asyncio.to_thread(_render_skills, orch, user_id)
    elif tab == "schedule":
        body = await asyncio.to_thread(_render_schedule, orch, user_id)
        if flags.is_enabled("persistent_agents"):
            from astralprojection.chrome import render_html
            view = await _assignment_view(orch, user_id, params)
            body = '<h3>Ongoing agents</h3>' + render_html(view) + '<h3>Scheduled tasks</h3>' + body
    else:
        body = await asyncio.to_thread(_render_dreaming, orch, user_id)
    return f'<div class="space-y-4">{_tab_bar(tab)}{body}</div>'


def _render_soul(orch, user_id: str, params: dict) -> str:
    """Soul tab: profession / goals / personality-notes explicit-save form."""
    svc = _svc(orch)
    if svc is None:
        return _unavailable("Personalization subsystem is not available.")
    profile = svc.repo.get_profile(user_id) or {}
    draft = params.get("draft") if isinstance(params.get("draft"), dict) else None
    if draft is not None:
        # FR-016: failed saves preserve the submitted field values.
        profession = str(draft.get("profession") or "")
        goals_text = str(draft.get("goals") or "")
        notes = str(draft.get("personality_notes") or "")
    else:
        profession = str(profile.get("profession") or "")
        goals = [str(g) for g in (profile.get("goals") or [])]
        goals_text = "\n".join(goals)
        personality = profile.get("personality") or {}
        notes = str(personality.get("notes") or "")
    save_btn = _btn("Save profile", "chrome_profile_save", {}, collect=True)
    return (
        f'<div data-ui-form class="{_CARD_CLS} space-y-3">'
        f'<div><label class="{_LABEL_CLS}" for="chrome-profession">Profession</label>'
        f'<input id="chrome-profession" name="profession" type="text" class="{_INPUT_CLS}" '
        f'value="{esc(profession)}" placeholder="e.g. clinical researcher"></div>'
        f'<div><label class="{_LABEL_CLS}" for="chrome-goals">Goals (one per line)</label>'
        f'<textarea id="chrome-goals" name="goals" rows="4" class="{_INPUT_CLS}" '
        f'placeholder="One goal per line">{esc(goals_text)}</textarea></div>'
        f'<div><label class="{_LABEL_CLS}" for="chrome-personality-notes">'
        f"Personality notes</label>"
        f'<textarea id="chrome-personality-notes" name="personality_notes" rows="3" '
        f'class="{_INPUT_CLS}" placeholder="How should the assistant sound?">'
        f"{esc(notes)}</textarea></div>"
        f'<div class="flex justify-end">{save_btn}</div></div>'
        # 025 precedence note (FR-015): personality is style-only.
        f'<p class="text-xs text-astral-muted">Personality guides tone and voice only — it '
        f"never overrides the safety, privacy, or HIPAA/compliance rules. Free-text values "
        f"are screened; anything that looks like protected health information is rejected.</p>"
    )


async def _render_memory(orch, user_id: str) -> str:
    """Memory tab: durable items with inline edit + delete actions."""
    svc = _svc(orch)
    if svc is None:
        return _unavailable("Personalization subsystem is not available.")
    items = await asyncio.to_thread(svc.repo.list_memory, user_id)
    # The REST GET records a memory.view event — preserve that here.
    await record_generic(
        claims={"sub": user_id}, event_class="memory", action_type="memory.view",
        description="Viewed durable memory", outputs_meta={"count": len(items)},
    )
    intro = (
        '<p class="text-xs text-astral-muted">Durable, non-PHI personalization facts the '
        "assistant remembers across sessions. Edits are screened by the same PHI gate as "
        "everything else.</p>"
    )
    if not items:
        return intro + (
            f'<div class="{_CARD_CLS} text-sm text-astral-muted">No memory items yet — '
            f"they appear as you chat or when dreaming promotes recurring signals.</div>"
        )
    rows = []
    for item in items:
        mem_id = str(item.get("id") or "")
        category = str(item.get("category") or "")
        value = str(item.get("value") or "")
        created = _fmt_ts(item.get("created_at"))
        save = _btn("Save", "chrome_memory_update", {"id": mem_id}, collect=True)
        delete = _btn("Delete", "chrome_memory_delete", {"id": mem_id}, cls=_BTN_DANGER)
        rows.append(
            f'<div data-ui-form class="bg-white/5 border border-white/10 rounded-lg p-3 '
            f'space-y-2" data-memory-id="{esc(mem_id)}">'
            f'<div class="flex items-center justify-between text-xs text-astral-muted">'
            f'<span class="uppercase tracking-wider">{esc(category)}</span>'
            f"<span>added {esc(created)}</span></div>"
            f'<input name="value" type="text" class="{_INPUT_CLS}" value="{esc(value)}" '
            f'aria-label="Memory value">'
            f'<div class="flex gap-2 justify-end">{save}{delete}</div></div>'
        )
    return intro + f'<div class="space-y-2">{"".join(rows)}</div>'


def _render_skills(orch, user_id: str) -> str:
    """Skills tab: the catalog GET /api/skills builds, with toggles."""
    tp = getattr(orch, "tool_permissions", None)
    if tp is None:
        return _unavailable("Tool permissions are not available.")
    catalog = []
    # Same enumeration as personalization.api.list_skills (FR-009).
    for agent_id in list(getattr(tp, "_tool_scope_map", {}) or {}):
        scope_map = tp.get_tool_scope_map(agent_id)
        for tool_name, scope in scope_map.items():
            catalog.append({
                "agent_id": agent_id,
                "tool_name": tool_name,
                "scope": scope,
                "enabled": tp.is_tool_allowed(user_id, agent_id, tool_name),
                "authorized": tp.is_scope_enabled(user_id, agent_id, scope),
            })
    if not catalog:
        return (
            f'<div class="{_CARD_CLS} text-sm text-astral-muted">No skills are available '
            f"yet.</div>"
        )
    catalog.sort(key=lambda s: (s["agent_id"], s["tool_name"]))
    rows = []
    for entry in catalog:
        scope_badge = (
            f'<span class="px-2 py-0.5 rounded-full text-[10px] font-medium bg-white/5 '
            f'border border-white/10 text-astral-muted">{esc(entry["scope"])}</span>'
        )
        header = (
            f'<div class="flex items-center gap-2 min-w-0">'
            f'<span class="text-sm text-astral-text truncate">{esc(entry["tool_name"])}</span>'
            f'<span class="text-xs text-astral-muted truncate">{esc(entry["agent_id"])}</span>'
            f"{scope_badge}</div>"
        )
        if entry["authorized"]:
            state = "Enabled" if entry["enabled"] else "Disabled"
            state_cls = "text-green-400" if entry["enabled"] else "text-astral-muted"
            toggle = _btn(
                "Disable" if entry["enabled"] else "Enable",
                "chrome_skill_toggle",
                {"agent_id": entry["agent_id"], "tool_name": entry["tool_name"],
                 "enabled": not entry["enabled"]},
                cls=_BTN_GHOST if entry["enabled"] else _BTN_PRIMARY,
            )
            right = (
                f'<div class="flex items-center gap-2">'
                f'<span class="text-xs {state_cls}">{state}</span>{toggle}</div>'
            )
        else:
            # Render unavailable-with-reason; no toggle is offered (FR-011).
            reason = (
                f"Unavailable — requires the '{entry['scope']}' permission, which you "
                f"haven't been granted."
            )
            right = f'<span class="text-xs text-yellow-400">{esc(reason)}</span>'
        rows.append(
            f'<div class="flex items-center justify-between gap-3 bg-white/5 border '
            f'border-white/10 rounded-lg px-3 py-2">{header}{right}</div>'
        )
    return f'<div class="space-y-2">{"".join(rows)}</div>'


def _render_schedule(orch, user_id: str) -> str:
    """Schedule tab: job list + inline run history; creation happens in chat."""
    store = _job_store(orch)
    if store is None:
        return _unavailable("The scheduler is not available.")
    hint = (
        '<p class="text-xs text-astral-muted">New jobs are created in chat — ask the '
        "assistant to schedule a task and it will walk you through consent.</p>"
    )
    # 030 FR-005: when unattended execution is gated off (pending the
    # offline-grant security review), say so plainly — jobs can be created but
    # will not fire until an operator enables FF_SCHEDULER_EXECUTION.
    execution_enabled = flags.is_enabled("scheduler_execution")
    if not execution_enabled:
        hint = (
            f'<div class="{_CARD_CLS} text-sm">⚠️ Unattended execution is currently '
            "<strong>unavailable</strong>: scheduled jobs will not run until an "
            "administrator enables it (pending a security review). You can still create "
            "and manage jobs.</div>" + hint
        )
    # 'disabled' is the soft-deleted state the REST delete endpoint sets;
    # '__dreaming__' jobs are internal consolidation, not user-facing.
    jobs = [j for j in store.list_jobs(user_id)
            if (j.get("status") or "") != "disabled"
            and j.get("agent_id") != "__dreaming__"]
    if not jobs:
        return hint + (
            f'<div class="{_CARD_CLS} text-sm text-astral-muted">No scheduled jobs '
            f"yet.</div>"
        )
    cards = []
    for job in jobs:
        job_id = str(job.get("id") or "")
        status = str(job.get("status") or "")
        badge_cls = _STATUS_BADGES.get(status, _STATUS_BADGES["completed"])
        schedule_desc = (
            f'{job.get("schedule_kind") or "?"}: {job.get("schedule_expr") or "?"} '
            f'({job.get("timezone") or "UTC"})'
        )
        actions = []
        if status == "active":
            actions.append(_btn("Pause", "chrome_job_pause", {"job_id": job_id},
                                cls=_BTN_GHOST))
            if execution_enabled:
                actions.append(_btn(
                    "Run now",
                    "chrome_job_run_now",
                    {
                        "job_id": job_id,
                        "submission_id": str(uuid.uuid4()),
                    },
                ))
        elif status == "paused":
            actions.append(_btn("Resume", "chrome_job_resume", {"job_id": job_id}))
        actions.append(_btn("Delete", "chrome_job_delete", {"job_id": job_id},
                            cls=_BTN_DANGER))
        runs = store.list_runs(user_id, job_id)[:5]
        run_lines = []
        for run in runs:
            outcome = str(run.get("outcome") or "")
            color = _OUTCOME_COLORS.get(outcome, "text-astral-muted")
            summary = str(run.get("summary") or "")
            summary_html = f" — {esc(summary)}" if summary else ""
            run_lines.append(
                f'<li class="text-xs text-astral-muted">{esc(_fmt_ts(run.get("started_at")))} '
                f'<span class="{color}">{esc(outcome)}</span>{summary_html}</li>'
            )
        if run_lines:
            history = (
                f'<div class="pt-1 border-t border-white/5">'
                f'<div class="text-[10px] uppercase tracking-wider text-astral-muted '
                f'mb-1">Recent runs</div><ul class="space-y-0.5">{"".join(run_lines)}</ul></div>'
            )
        else:
            history = '<div class="text-xs text-astral-muted">No runs yet.</div>'
        cards.append(
            f'<div class="{_CARD_CLS} space-y-2" data-job-id="{esc(job_id)}">'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="text-sm font-medium text-astral-text truncate">'
            f'{esc(job.get("name") or "")}</span>'
            f'<span class="px-2 py-0.5 rounded-full text-[10px] font-medium border '
            f'{badge_cls}">{esc(status)}</span></div>'
            f'<div class="text-xs text-astral-muted">{esc(schedule_desc)}</div>'
            f'<div class="text-xs text-astral-muted">Next run: '
            f'{esc(_fmt_ts(job.get("next_run_at")))} · Last run: '
            f'{esc(_fmt_ts(job.get("last_run_at")))}</div>'
            f"{history}"
            f'<div class="flex gap-2 justify-end">{"".join(actions)}</div></div>'
        )
    return hint + f'<div class="space-y-3">{"".join(cards)}</div>'


def _render_dreaming(orch, user_id: str) -> str:
    """Dreaming tab: opt-out toggle, manual trigger, and recent sweeps."""
    svc = _svc(orch)
    if svc is None:
        return _unavailable("Personalization subsystem is not available.")
    profile = svc.repo.get_profile(user_id)
    enabled = bool(profile.get("dreaming_enabled", True)) if profile else True
    state = "on" if enabled else "off"
    toggle = _btn(
        "Turn off" if enabled else "Turn on",
        "chrome_dreaming_toggle", {"enabled": not enabled},
        cls=_BTN_GHOST if enabled else _BTN_PRIMARY,
    )
    trigger = _btn("Run a sweep now", "chrome_dreaming_trigger", {})
    status_card = (
        f'<div class="{_CARD_CLS} space-y-2">'
        f'<div class="flex items-center justify-between gap-2">'
        f'<span class="text-sm text-astral-text">Background consolidation is '
        f'<span class="font-semibold">{esc(state)}</span></span>{toggle}</div>'
        f'<p class="text-xs text-astral-muted">Dreaming periodically reviews recent, '
        f"recurring signals and promotes the non-PHI ones into long-term memory. "
        f"In-app only; every sweep is recorded below.</p>"
        f'<div class="flex justify-end">{trigger}</div></div>'
    )
    sweeps = svc.repo.list_sweeps(user_id)
    if not sweeps:
        sweeps_html = (
            f'<div class="{_CARD_CLS} text-sm text-astral-muted">No sweeps yet.</div>'
        )
    else:
        lines = []
        for sweep in sweeps:
            counts = (
                f'considered {sweep.get("candidates_considered", 0)}, '
                f'promoted {sweep.get("promoted_count", 0)}'
            )
            summary = str(sweep.get("summary") or "")
            summary_html = (
                f'<div class="text-xs text-astral-muted">{esc(summary)}</div>'
                if summary else ""
            )
            lines.append(
                f'<li class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 '
                f'space-y-0.5"><div class="flex items-center justify-between gap-2 '
                f'text-xs text-astral-muted"><span>{esc(_fmt_ts(sweep.get("ran_at")))} · '
                f'{esc(sweep.get("trigger") or "")}</span><span>{esc(counts)}</span></div>'
                f"{summary_html}</li>"
            )
        sweeps_html = (
            f'<div><div class="text-[10px] uppercase tracking-wider text-astral-muted '
            f'mb-1">Recent sweeps</div><ul class="space-y-2">{"".join(lines)}</ul></div>'
        )
    return status_card + sweeps_html


# ---------------------------------------------------------------------------
# Feature 043 — the surface as native SDUI components (one tab at a time, so
# the per-tab data reads + audit — e.g. memory.view — match render() exactly).
# ---------------------------------------------------------------------------

async def components(orch, user_id, roles, params):
    """The personalization surface as native SDUI components, per ``params.tab``.

    Mirrors ``render()``: a tab bar of ``chrome_open`` buttons (re-open on a
    tab) + only the selected tab's content, so switching tabs re-reads that
    tab's data (and re-fires its audit) exactly like the web.
    """
    params = params or {}
    tab = params.get("tab") or "soul"
    if tab not in _TAB_KEYS:
        tab = "soul"
    tab_bar = _sdui.container(
        [_sdui.button(label, "chrome_open",
                      {"surface": "personalization", "params": {"tab": key}},
                      variant="primary" if key == tab else "secondary")
         for key, label in _TABS],
        direction="row",
    )
    if tab == "soul":
        body = await asyncio.to_thread(_components_soul, orch, user_id, params)
    elif tab == "memory":
        body = await _components_memory(orch, user_id)
    elif tab == "skills":
        body = await asyncio.to_thread(_components_skills, orch, user_id)
    elif tab == "schedule":
        body = await asyncio.to_thread(_components_schedule, orch, user_id)
        if flags.is_enabled("persistent_agents"):
            view = await _assignment_view(orch, user_id, params)
            body = [_sdui.text("Ongoing agents", "h3"),
                    *[item.to_dict() for item in view.components],
                    _sdui.text("Scheduled tasks", "h3"), *body]
    else:
        body = await asyncio.to_thread(_components_dreaming, orch, user_id)
    return [tab_bar, *body]


def _components_soul(orch, user_id, params):
    svc = _svc(orch)
    if svc is None:
        return [_sdui.alert("Personalization subsystem is not available.", "warning")]
    profile = svc.repo.get_profile(user_id) or {}
    draft = params.get("draft") if isinstance(params.get("draft"), dict) else None
    if draft is not None:
        profession = str(draft.get("profession") or "")
        goals_text = str(draft.get("goals") or "")
        notes = str(draft.get("personality_notes") or "")
    else:
        profession = str(profile.get("profession") or "")
        goals_text = "\n".join(str(g) for g in (profile.get("goals") or []))
        notes = str((profile.get("personality") or {}).get("notes") or "")
    return [
        _sdui.form(
            [_sdui.field("profession", "Profession", "text", default=profession,
                         help="e.g. clinical researcher"),
             _sdui.field("goals", "Goals (one per line)", "textarea", default=goals_text),
             _sdui.field("personality_notes", "Personality notes", "textarea", default=notes,
                         help="How should the assistant sound?")],
            submit_action="chrome_profile_save", submit_label="Save profile"),
        _sdui.text("Personality guides tone and voice only — it never overrides the safety, "
                   "privacy, or HIPAA rules, and free-text values are PHI-screened.", "caption"),
    ]


async def _components_memory(orch, user_id):
    svc = _svc(orch)
    if svc is None:
        return [_sdui.alert("Personalization subsystem is not available.", "warning")]
    items = await asyncio.to_thread(svc.repo.list_memory, user_id)
    # Preserve the render()-time memory.view audit event.
    await record_generic(
        claims={"sub": user_id}, event_class="memory", action_type="memory.view",
        description="Viewed durable memory", outputs_meta={"count": len(items)},
    )
    out = [_sdui.text("Durable, non-PHI facts the assistant remembers across sessions. Edits "
                      "are screened by the same PHI gate as everything else.", "caption")]
    if not items:
        out.append(_sdui.alert("No memory items yet — they appear as you chat or when dreaming "
                               "promotes recurring signals.", "info"))
        return out
    for item in items:
        mem_id = str(item.get("id") or "")
        out.append(_sdui.card(
            str(item.get("category") or "memory"),
            [_sdui.form(
                [_sdui.field("value", "Value", "text", default=str(item.get("value") or ""))],
                actions=[
                    {"label": "Save", "action": "chrome_memory_update", "variant": "primary",
                     "payload": {"id": mem_id}},
                    {"label": "Delete", "action": "chrome_memory_delete", "variant": "danger",
                     "payload": {"id": mem_id}},
                ])],
        ))
    return out


def _components_skills(orch, user_id):
    tp = getattr(orch, "tool_permissions", None)
    if tp is None:
        return [_sdui.alert("Tool permissions are not available.", "warning")]
    catalog = []
    for agent_id in list(getattr(tp, "_tool_scope_map", {}) or {}):
        for tool_name, scope in tp.get_tool_scope_map(agent_id).items():
            catalog.append({
                "agent_id": agent_id, "tool_name": tool_name, "scope": scope,
                "enabled": tp.is_tool_allowed(user_id, agent_id, tool_name),
                "authorized": tp.is_scope_enabled(user_id, agent_id, scope),
            })
    if not catalog:
        return [_sdui.alert("No skills are available yet.", "info")]
    catalog.sort(key=lambda s: (s["agent_id"], s["tool_name"]))
    out = []
    for e in catalog:
        children = [_sdui.badge(e["scope"], "default")]
        if e["authorized"]:
            children.append(_sdui.badge("Enabled" if e["enabled"] else "Disabled",
                                        "success" if e["enabled"] else "default"))
            children.append(_sdui.button(
                "Disable" if e["enabled"] else "Enable", "chrome_skill_toggle",
                {"agent_id": e["agent_id"], "tool_name": e["tool_name"],
                 "enabled": not e["enabled"]},
                variant="secondary" if e["enabled"] else "primary"))
        else:
            children.append(_sdui.text(
                f"Unavailable — requires the '{e['scope']}' permission, which you haven't "
                f"been granted.", "caption"))
        out.append(_sdui.card(f"{e['tool_name']} · {e['agent_id']}", children))
    return out


def _components_schedule(orch, user_id):
    store = _job_store(orch)
    if store is None:
        return [_sdui.alert("The scheduler is not available.", "warning")]
    out = []
    execution_enabled = flags.is_enabled("scheduler_execution")
    if not execution_enabled:
        out.append(_sdui.alert("Unattended execution is currently unavailable: scheduled jobs "
                               "will not run until an administrator enables it (pending a "
                               "security review). You can still create and manage jobs.", "warning"))
    out.append(_sdui.text("New jobs are created in chat — ask the assistant to schedule a task.",
                          "caption"))
    jobs = [j for j in store.list_jobs(user_id)
            if (j.get("status") or "") != "disabled" and j.get("agent_id") != "__dreaming__"]
    if not jobs:
        out.append(_sdui.alert("No scheduled jobs yet.", "info"))
        return out
    for job in jobs:
        job_id = str(job.get("id") or "")
        status = str(job.get("status") or "")
        actions = []
        if status == "active":
            actions.append(_sdui.button("Pause", "chrome_job_pause", {"job_id": job_id}, "secondary"))
            if execution_enabled:
                actions.append(_sdui.button(
                    "Run now",
                    "chrome_job_run_now",
                    {
                        "job_id": job_id,
                        "submission_id": str(uuid.uuid4()),
                    },
                    "primary",
                ))
        elif status == "paused":
            actions.append(_sdui.button("Resume", "chrome_job_resume", {"job_id": job_id}, "primary"))
        actions.append(_sdui.button("Delete", "chrome_job_delete", {"job_id": job_id}, "danger"))
        card_children = [_sdui.key_value([
            {"label": "Status", "value": status},
            {"label": "Schedule",
             "value": f'{job.get("schedule_kind") or "?"}: {job.get("schedule_expr") or "?"} '
                      f'({job.get("timezone") or "UTC"})'},
            {"label": "Next run", "value": _fmt_ts(job.get("next_run_at"))},
            {"label": "Last run", "value": _fmt_ts(job.get("last_run_at"))},
        ])]
        runs = store.list_runs(user_id, job_id)[:5]
        if runs:
            card_children.append(_sdui.text("Recent runs", "caption"))
            card_children.append(_sdui.bullet_list([
                f'{_fmt_ts(r.get("started_at"))} — {r.get("outcome") or ""}'
                + (f' · {r.get("summary")}' if r.get("summary") else "")
                for r in runs
            ]))
        card_children.append(_sdui.container(actions, direction="row"))
        out.append(_sdui.card(str(job.get("name") or "Job"), card_children))
    return out


def _components_dreaming(orch, user_id):
    svc = _svc(orch)
    if svc is None:
        return [_sdui.alert("Personalization subsystem is not available.", "warning")]
    profile = svc.repo.get_profile(user_id)
    enabled = bool(profile.get("dreaming_enabled", True)) if profile else True
    out = [_sdui.card(
        f"Background consolidation is {'on' if enabled else 'off'}",
        [_sdui.text("Dreaming periodically reviews recent, recurring signals and promotes the "
                    "non-PHI ones into long-term memory. In-app only; every sweep is recorded "
                    "below.", "caption"),
         _sdui.button("Turn off" if enabled else "Turn on", "chrome_dreaming_toggle",
                      {"enabled": not enabled}, variant="secondary" if enabled else "primary"),
         _sdui.button("Run a sweep now", "chrome_dreaming_trigger", {}, "primary")],
    )]
    sweeps = svc.repo.list_sweeps(user_id)
    if not sweeps:
        out.append(_sdui.alert("No sweeps yet.", "info"))
        return out
    lines = []
    for s in sweeps:
        counts = (f'considered {s.get("candidates_considered", 0)}, '
                  f'promoted {s.get("promoted_count", 0)}')
        line = f'{_fmt_ts(s.get("ran_at"))} · {s.get("trigger") or ""} — {counts}'
        if s.get("summary"):
            line += f' · {s.get("summary")}'
        lines.append(line)
    out.append(_sdui.text("Recent sweeps", "caption"))
    out.append(_sdui.bullet_list(lines))
    return out


# ---------------------------------------------------------------------------
# Handlers (explicit-save: change → audit → re-render with notice)
# ---------------------------------------------------------------------------

async def _handle_profile_save(orch, websocket, user_id, roles, payload):
    """Save the soul form — same validation/PHI gate/audit as PUT /profile."""
    svc = _svc(orch)
    if svc is None:
        return (SURFACE_KEY, _params("soul"),
                _unavailable("Personalization subsystem is not available."))
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else None
    if fields is None:
        return (SURFACE_KEY, _params("soul"),
                notice_block("error", "No form data received — nothing was saved."))
    profession = str(fields.get("profession") or "").strip()
    goals_text = str(fields.get("goals") or "")
    goals = [line.strip() for line in goals_text.splitlines() if line.strip()]
    notes = str(fields.get("personality_notes") or "").strip()
    draft = {"profession": profession, "goals": goals_text, "personality_notes": notes}
    fail_params = _params("soul", draft=draft)

    # Same Pydantic validation models as the REST endpoint.
    try:
        body = ProfileUpdateRequest(
            profession=profession, goals=goals,
            personality=PersonalitySpec(notes=notes or None),
        )
    except ValidationError as ve:
        errors = ve.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        msg = str(first.get("msg") or "invalid value")
        return (SURFACE_KEY, fail_params,
                notice_block("error", f"Couldn't save — {loc}: {msg}"))

    # PHI gate on every free-text value before anything persists (FR-017).
    rejected = await asyncio.to_thread(_phi_reject_field, body, notes)
    if rejected:
        return (SURFACE_KEY, fail_params, _phi_notice(rejected))

    # Merge notes into the existing personality so chat-set traits
    # (tone/directness/humor/verbosity) are preserved by this form.
    existing = await asyncio.to_thread(svc.repo.get_profile, user_id) or {}
    existing_personality = dict(existing.get("personality") or {})
    personality_dict = None
    if notes != str(existing_personality.get("notes") or ""):
        merged = dict(existing_personality)
        if notes:
            merged["notes"] = notes
        else:
            merged.pop("notes", None)
        personality_dict = merged

    await asyncio.to_thread(
        svc.repo.upsert_profile,
        user_id, profession=profession, goals=goals, personality=personality_dict,
    )

    changed_personality = personality_dict is not None
    changed = ["profession", "goals"] + (["personality"] if changed_personality else [])
    await record_generic(
        claims=_claims(orch, websocket, user_id),
        event_class="personalization",
        action_type="personalization.personality_update" if changed_personality
        else "personalization.profile_update",
        description="Updated assistant personality" if changed_personality
        else "Updated personalization profile",
        outputs_meta={"changed": changed},
    )
    return (SURFACE_KEY, _params("soul"), notice_block("success", "Profile saved."))


async def _handle_memory_update(orch, websocket, user_id, roles, payload):
    """Edit a memory item's value — PHI-gated, mirrors PUT /api/memory/{id}."""
    svc = _svc(orch)
    if svc is None:
        return (SURFACE_KEY, _params("memory"),
                _unavailable("Personalization subsystem is not available."))
    mem_id = str(payload.get("id") or "")
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    raw_value = fields.get("value") if "value" in fields else payload.get("value")
    value = str(raw_value or "").strip()
    if not mem_id:
        return (SURFACE_KEY, _params("memory"),
                notice_block("error", "Missing memory item id."))
    if not value:
        return (SURFACE_KEY, _params("memory"),
                notice_block("error", "Memory value cannot be empty."))
    if await asyncio.to_thread(_contains_phi, value):
        return (SURFACE_KEY, _params("memory"), _phi_notice("value"))
    if not await asyncio.to_thread(svc.repo.update_memory_value, user_id, mem_id, value):
        return (SURFACE_KEY, _params("memory"),
                notice_block("error", "Memory item not found."))
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="memory",
        action_type="memory.update", description="Updated a memory item",
        outputs_meta={"id": mem_id},
    )
    return (SURFACE_KEY, _params("memory"), notice_block("success", "Memory updated."))


async def _handle_memory_delete(orch, websocket, user_id, roles, payload):
    """Delete a memory item — mirrors DELETE /api/memory/{id}."""
    svc = _svc(orch)
    if svc is None:
        return (SURFACE_KEY, _params("memory"),
                _unavailable("Personalization subsystem is not available."))
    mem_id = str(payload.get("id") or "")
    if not mem_id:
        return (SURFACE_KEY, _params("memory"),
                notice_block("error", "Missing memory item id."))
    if not await asyncio.to_thread(svc.repo.delete_memory, user_id, mem_id):
        return (SURFACE_KEY, _params("memory"),
                notice_block("error", "Memory item not found."))
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="memory",
        action_type="memory.delete", description="Deleted a memory item",
        outputs_meta={"id": mem_id},
    )
    return (SURFACE_KEY, _params("memory"), notice_block("success", "Memory deleted."))


async def _handle_skill_toggle(orch, websocket, user_id, roles, payload):
    """Enable/disable a skill — scope-bounded exactly like PUT /api/skills."""
    tp = getattr(orch, "tool_permissions", None)
    if tp is None:
        return (SURFACE_KEY, _params("skills"),
                _unavailable("Tool permissions are not available."))
    agent_id = str(payload.get("agent_id") or "")
    tool_name = str(payload.get("tool_name") or "")
    enabled = bool(payload.get("enabled"))
    if not agent_id or not tool_name:
        return (SURFACE_KEY, _params("skills"),
                notice_block("error", "Missing skill identifier."))
    required_scope = tp.get_tool_scope(agent_id, tool_name)
    # FR-011: enabling a skill can never exceed the user's granted scope.
    if enabled and not await asyncio.to_thread(
            tp.is_scope_enabled, user_id, agent_id, required_scope):
        return (SURFACE_KEY, _params("skills"), notice_block(
            "error",
            f"This skill needs the '{required_scope}' permission, which you haven't "
            "been granted.",
        ))
    # 027 fix: write the per-(tool, kind) row that is_tool_allowed actually
    # honors (the legacy NULL-kind row is outranked whenever a kind row exists).
    await asyncio.to_thread(tp.set_skill_enabled, user_id, agent_id, tool_name, enabled)
    verb = "Enabled" if enabled else "Disabled"
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="skill",
        action_type="skill.enable" if enabled else "skill.disable",
        description=f"{verb} skill {agent_id}:{tool_name}",
        outputs_meta={"agent_id": agent_id, "tool_name": tool_name, "enabled": enabled},
    )
    return (SURFACE_KEY, _params("skills"),
            notice_block("success", f"{verb} '{tool_name}'."))


async def _job_set_status(orch, websocket, user_id, payload, *, status, action_type,
                          description, success_msg):
    """Shared pause/resume/delete path — mirrors the /api/schedule endpoints."""
    store = _job_store(orch)
    if store is None:
        return (SURFACE_KEY, _params("schedule"),
                _unavailable("The scheduler is not available."))
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        return (SURFACE_KEY, _params("schedule"),
                notice_block("error", "Missing job id."))
    if status in {"paused", "disabled"}:
        changed = await asyncio.to_thread(
            store.set_status_and_cancel_unstarted,
            user_id=user_id,
            job_id=job_id,
            status=status,
            terminal_code=(
                "cancelled_job_paused"
                if status == "paused"
                else "cancelled_job_deleted"
            ),
        )
    else:
        changed = await asyncio.to_thread(store.set_status, user_id, job_id, status)
    if not changed:
        return (SURFACE_KEY, _params("schedule"),
                notice_block("error", "Job not found."))
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="schedule",
        action_type=action_type, description=description,
        outputs_meta={"job_id": job_id},
    )
    return (SURFACE_KEY, _params("schedule"), notice_block("success", success_msg))


async def _handle_job_pause(orch, websocket, user_id, roles, payload):
    """Pause a scheduled job (POST /api/schedule/{id}/pause internals)."""
    return await _job_set_status(
        orch, websocket, user_id, payload, status="paused",
        action_type="schedule.pause", description="Paused scheduled job",
        success_msg="Job paused.",
    )


async def _handle_job_resume(orch, websocket, user_id, roles, payload):
    """Resume a paused job (POST /api/schedule/{id}/resume internals)."""
    return await _job_set_status(
        orch, websocket, user_id, payload, status="active",
        action_type="schedule.resume", description="Resumed scheduled job",
        success_msg="Job resumed.",
    )


async def _handle_job_delete(orch, websocket, user_id, roles, payload):
    """Delete (soft-disable) a job (DELETE /api/schedule/{id} internals)."""
    return await _job_set_status(
        orch, websocket, user_id, payload, status="disabled",
        action_type="schedule.delete", description="Deleted scheduled job",
        success_msg="Job deleted.",
    )


async def _handle_job_run_now(orch, websocket, user_id, roles, payload):
    """Materialize one idempotent manual occurrence for the scheduler loop."""
    if not flags.is_enabled("scheduler_execution"):
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", "Scheduled execution is currently unavailable."))
    store = _job_store(orch)
    if store is None:
        return (SURFACE_KEY, _params("schedule"),
                _unavailable("The scheduler is not available."))
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        return (SURFACE_KEY, _params("schedule"),
                notice_block("error", "Missing job id."))
    submission_value = payload.get("submission_id")
    try:
        submission_id = uuid.UUID(str(submission_value))
    except (TypeError, ValueError, AttributeError):
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", "Missing or invalid run-now submission id."))
    if submission_id.version != 4:
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", "Missing or invalid run-now submission id."))
    job = await asyncio.to_thread(store.get_job, user_id, job_id)
    if not job:
        return (SURFACE_KEY, _params("schedule"),
                notice_block("error", "Job not found."))
    if (job.get("status") or "") != "active":
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", "Job is not active — resume it before running it."))
    loop = getattr(orch, "_scheduler_loop", None)
    runner = getattr(loop, "runner", None)
    eligibility = getattr(runner, "assess_job", None)
    if not callable(eligibility):
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", "Scheduled execution is currently unavailable."))
    try:
        result = await asyncio.to_thread(
            store.materialize_run_now,
            user_id=user_id,
            job_id=job_id,
            submission_id=submission_id,
            eligibility=eligibility,
        )
    except ScheduleActionError as exc:
        messages = {
            "job_not_found": "Job not found.",
            "job_not_active": "Job is not active — resume it before running it.",
            "idempotency_conflict": (
                "That run-now submission was already used for another job."
            ),
            "handler_not_idempotent": (
                "This job cannot run unattended because its handler is not idempotent."
            ),
            "handler_downstream_idempotency_unreviewed": (
                "This job uses an effect that is not approved for unattended runs."
            ),
        }
        return (SURFACE_KEY, _params("schedule"), notice_block(
            "error", messages.get(exc.code, "The job could not be queued.")))
    if result.created:
        await record_generic(
            claims=_claims(orch, websocket, user_id), event_class="schedule",
            action_type="schedule.run_now",
            description="Queued scheduled job to run now",
            outputs_meta={
                "job_id": job_id,
                "occurrence_id": str(result.occurrence_id),
                "submission_id": str(submission_id),
            },
        )
    message = (
        "Job queued — it will run at the next scheduler tick."
        if result.created
        else "Job is already queued for this run-now request."
    )
    return (SURFACE_KEY, _params("schedule"), notice_block(
        "success", message))


async def _handle_dreaming_toggle(orch, websocket, user_id, roles, payload):
    """Enable/disable dreaming (POST /api/dreaming/{enable,disable} internals)."""
    svc = _svc(orch)
    if svc is None:
        return (SURFACE_KEY, _params("dreaming"),
                _unavailable("Personalization subsystem is not available."))
    enabled = bool(payload.get("enabled"))
    await asyncio.to_thread(svc.repo.set_dreaming_enabled, user_id, enabled)
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="dreaming",
        action_type="dreaming.enable" if enabled else "dreaming.disable",
        description="Enabled background consolidation" if enabled
        else "Disabled background consolidation",
    )
    msg = "Dreaming enabled." if enabled else "Dreaming disabled."
    return (SURFACE_KEY, _params("dreaming"), notice_block("success", msg))


async def _handle_dreaming_trigger(orch, websocket, user_id, roles, payload):
    """Run a manual sweep (POST /api/dreaming/trigger internals)."""
    svc = _svc(orch)
    if svc is None:
        return (SURFACE_KEY, _params("dreaming"),
                _unavailable("Personalization subsystem is not available."))
    sweep = await asyncio.to_thread(_run_manual_sweep, svc.repo, user_id)
    await record_generic(
        claims=_claims(orch, websocket, user_id), event_class="dreaming",
        action_type="dreaming.sweep", description="Ran a manual consolidation sweep",
        outputs_meta={"promoted": sweep["promoted_count"],
                      "considered": sweep["candidates_considered"]},
    )
    msg = (
        f"Sweep complete — considered {sweep['candidates_considered']} signal(s), "
        f"promoted {sweep['promoted_count']}."
    )
    return (SURFACE_KEY, _params("dreaming"), notice_block("success", msg))


_ASSIGNMENT_CONTROLS = ("create", "revise", "pause", "resume", "stop", "revoke", "run_now", "approval_decide")
_ASSIGNMENT_ACTIONS = tuple(f"chrome_assignment_{suffix}" for suffix in _ASSIGNMENT_CONTROLS)
_ASSIGNMENT_METRICS = ("model_calls", "tool_calls", "tokens", "elapsed_ms")
_ASSIGNMENT_LIMITS = ("cadence_seconds", "max_retries", "max_concurrent_tasks", "max_depth", "max_tasks", "step_timeout_ms")
_ASSIGNMENT_LABELS = {
    "cadence_seconds": "Check interval (seconds)", "max_retries": "Retries",
    "max_concurrent_tasks": "Concurrent tasks", "max_depth": "Delegation depth",
    "max_tasks": "Task count", "step_timeout_ms": "Step timeout (milliseconds)",
    "model_calls": "Model calls", "tool_calls": "Tool calls", "tokens": "Tokens",
    "elapsed_ms": "Elapsed time (milliseconds)", "spend_micro_units": "Spending (millionths of currency unit)",
}


def _assignment_access(orch, websocket, user_id):
    """Require the verified human socket; never synthesize claims for controls."""
    from persistent_agents.models import AssignmentError
    if not flags.is_enabled("persistent_agents"):
        raise AssignmentError("assignment_feature_disabled", 503)
    service = getattr(orch, "persistent_assignments", None)
    if service is None:
        raise AssignmentError("assignment_service_unavailable", 503)
    claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket)
    if (websocket is None or getattr(websocket, "closed", False)
            or not isinstance(claims, dict) or claims.get("sub") != user_id
            or claims.get("act") or any(claims.get(key) for key in
                ("machine_class", "machine_turn_class", "_machine_turn", "delegated"))):
        raise AssignmentError("assignment_owner_required", 403)
    return service, claims


def _assignment_tool_options(orch, service, user_id, claims):
    """Use the same live catalog as dispatch, including finite operation bounds."""
    from persistent_agents.models import AssignmentError

    from orchestrator.tool_visibility import eligible_tool_pairs
    permissions = orch.tool_permissions
    pairs = eligible_tool_pairs(orch, user_id,
        disabled_agents=permissions.list_disabled_agents(user_id), identity_claims=claims)
    tools, sources = [], []
    for agent, skill in pairs:
        identity = f"{agent}:{skill.id}"
        try:
            service.tool_bound(identity)
        except AssignmentError:
            continue
        tools.append(identity)
        if permissions.get_tool_scope(agent, skill.id) in ("tools:read", "tools:search"):
            sources.append("public_page" if identity == "web-research-1:fetch_page" else identity)
    return sources[:32], tools[:32]


def _assignment_limits(flat=None):
    from persistent_agents.models import AssignmentLimits
    values = AssignmentLimits().model_dump()
    if flat is None:
        return values
    for name in _ASSIGNMENT_LIMITS:
        values[name] = flat[name]
    for period, prefix in (("daily", "daily_"), ("lifetime", "")):
        for name in (*_ASSIGNMENT_METRICS, "spend_micro_units"):
            values[period][name] = flat.get(prefix + name)
        values[period]["currency"] = flat.get("currency")
    return AssignmentLimits.model_validate(values).model_dump()


def _assignment_limit_fields(limits):
    result = [{"name": key, "label": _ASSIGNMENT_LABELS[key], "value": limits[key]}
              for key in _ASSIGNMENT_LIMITS]
    for period in ("daily", "lifetime"):
        result.extend({"name": f"{period}.{key}", "label": f"{period.title()} {_ASSIGNMENT_LABELS[key]}",
                       "value": limits[period][key],
                       "help": "Optional; only used with a currency cap." if key == "spend_micro_units" else "Finite hard limit."}
                      for key in (*_ASSIGNMENT_METRICS, "spend_micro_units"))
    return result


def _assignment_row(record):
    """Project allowlisted durable owner state; heartbeat versions are private."""
    from persistent_agents.service import public_record
    data = public_record(record)
    definition, usage = data["definition"], data.get("usage", {})
    source = definition["source"]
    limits = _assignment_limits(definition["limits"])
    currency = limits["lifetime"]["currency"]
    source_key = "public_page" if source["profile"] == "public_page" else f"{source['agent_id']}:{source['tool_name']}"
    row = {key: data.get(key) for key in ("assignment_id", "instruction_revision", "control_epoch", "lifecycle", "phase", "next_wake_at", "wake_reason", "last_check_at", "latest_result")}
    if row["lifecycle"] in ("stopped", "completed"):
        row["wake_reason"] = ""
    row["definition"] = {key: definition.get(key) for key in ("name", "instructions", "allowed_tools", "completion_condition", "conversation_id")}
    row["definition"].update(source_key=source_key, source_url=source["arguments"].get("url", ""),
        source_arguments=json.dumps(source["arguments"], ensure_ascii=False) if source_key != "public_page" else "",
        linked_document_urls="\n".join(source.get("linked_document_urls", [])),
        currency_cap_enabled=currency is not None, currency=currency or "")
    row["grant_summary"] = ("Revocable unattended authorization is bound; current permissions are rechecked for every action."
        if data.get("authority", {}).get("grant_bound") else "Authorization required. Revise and approve to bind a current grant.")
    row["currency_cap_label"] = f"{limits['lifetime']['spend_micro_units']} millionths of {currency}" if currency else "No currency cap"
    row["monetary_cost_label"] = (f"{usage.get('spent', {}).get('spend_micro_units', 0)} millionths of {currency}"
        if currency else "Unpriced/unknown")
    row["limit_summary"] = [{"label": _ASSIGNMENT_LABELS[key], "value": str(limits[key])} for key in _ASSIGNMENT_LIMITS]
    row["usage_summary"] = []
    for key in _ASSIGNMENT_METRICS:
        spent, outstanding, daily = (usage.get(bucket, {}).get(key, 0) for bucket in ("spent", "outstanding", "daily"))
        row["usage_summary"].append({"label": _ASSIGNMENT_LABELS[key],
            "value": f"Lifetime {spent}/{limits['lifetime'][key]}; today {daily}/{limits['daily'][key]}; reserved {outstanding}; remaining {max(0, min(limits['lifetime'][key] - spent - outstanding, limits['daily'][key] - daily - outstanding))}"})
    error = data.get("safe_error_code")
    row["safe_error"] = error or ""
    if error and row["lifecycle"] in ("active", "paused"):
        row["safe_error"] += ". Review activity and restore authorization or revise the assignment before resuming."
    row["available_actions"] = list(_ASSIGNMENT_ACTIONS) if row["lifecycle"] in ("active", "paused") else []
    row["submission_ids"] = {action: str(uuid.uuid4()) for action in row["available_actions"]}
    row["tasks"] = [{"title": task.get("title"), "state": task.get("state"), "result": task.get("bounded_result"),
        "incorporated": bool(task.get("incorporated_by")),
        "dependency_summary": ", ".join(task.get("depends_on", [])),
        "provenance_summary": "Recorded result " + task["result_digest"] if task.get("result_digest") else "No accepted result receipt yet."}
        for task in data.get("tasks", [])[:32]]
    return row, limits


def _assignment_approval(action):
    from persistent_agents.service import public_action
    data = public_action(action)
    intent = data["intent"]
    request = intent["request"]
    expiry = intent.get("approval_expires_at")
    try:
        parsed_expiry = datetime.fromisoformat(expiry)
        expired = parsed_expiry.tzinfo is None or parsed_expiry.astimezone(UTC) <= datetime.now(UTC)
    except (ValueError, TypeError):
        expired = True
    # Never expose credential-bearing fields, even from a malformed stored
    # proposal. An incomplete review cannot be approved through this surface.
    visible = _assignment_review_text(request)
    if visible is None:
        expired = True
    # JSON is text escaped by Projection and never replacement tool arguments.
    return {key: data.get(key) for key in ("action_id", "instruction_revision", "control_epoch", "state")} | {
        "request_digest": intent["request_digest"], "expires_at": expiry, "expired": expired,
        "tool_label": f"{request.get('agent_id', 'model')}:{request.get('tool_name', request.get('kind', 'operation'))}",
        "target_label": "See exact parameters",
        "arguments_summary": visible or "This operation contains protected or oversized fields and cannot be reviewed here.",
        "consequence": intent.get("sensitivity", "Review the exact operation"),
        "preconditions_summary": "Bound precondition " + intent["precondition_digest"],
        "interactive_only": intent.get("interactive_only", False),
        "submission_ids": {decision: str(uuid.uuid4()) for decision in ("approve", "decline")}}


def _assignment_review_text(request):
    from urllib.parse import parse_qsl, urlsplit

    from persistent_agents.models import SourceSelection
    protected = {"password", "secret", "token", "access_token", "refresh_token", "api_key", "apikey",
                 "authorization", "cookie", "credentials", "private_key", "client_secret", "offline_grant_id"}

    def safe(value, depth=0):
        if depth > 8:
            return False
        if isinstance(value, dict):
            return all(isinstance(key, str) and key.lower().replace("-", "_") not in protected and safe(child, depth + 1) for key, child in value.items())
        if isinstance(value, (list, tuple)):
            return all(safe(child, depth + 1) for child in value)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            return not (parsed.username or parsed.password or any(key.lower().replace("-", "_") in protected for key, _ in parse_qsl(parsed.query)))
        return value is None or type(value) in (str, int, float, bool)

    if not safe(request):
        return None
    try:
        # Reuse bounded secret-aware JSON validation, including finite numbers.
        SourceSelection.bounded_arguments(request)
        return json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        return None


def _assignment_draft(params, definition):
    """A chat proposal is only a validated form prefill, never consent."""
    from persistent_agents.models import AssignmentError, SourceSelection
    draft = params.get("assignment_draft", {})
    if not isinstance(draft, dict) or set(draft) - {"name", "instructions", "source_url"}:
        raise AssignmentError("assignment_draft_invalid", 422)
    for key, maximum in (("name", 120), ("instructions", 4096)):
        if key in draft and (not isinstance(draft[key], str) or not 1 <= len(draft[key]) <= maximum or not draft[key].strip() or "\x00" in draft[key]):
            raise AssignmentError("assignment_draft_invalid", 422)
    if "source_url" in draft:
        SourceSelection(agent_id="web-research-1", tool_name="fetch_page", arguments={"url": draft["source_url"]})
    definition.update(draft)
    conversation = params.get("conversation_id")
    if conversation is not None:
        if not isinstance(conversation, str) or not 1 <= len(conversation) <= 128 or not conversation.strip() or "\x00" in conversation:
            raise AssignmentError("assignment_destination_invalid", 422)
        definition["conversation_id"] = conversation


async def _assignment_state(orch, websocket, user_id, params):
    from persistent_agents.models import AssignmentError
    from persistent_agents.service import thaw
    state = {"enabled": flags.is_enabled("persistent_agents"),
             "execution_enabled": getattr(orch, "persistent_assignment_runner", None) is not None}
    if not state["enabled"]:
        return state
    try:
        service, claims = _assignment_access(orch, websocket, user_id)
        identity = params.get("assignment_id")
        mode = params.get("assignment_mode") or ("detail" if identity else "list")
        if mode not in ("list", "detail", "create", "revise"):
            raise AssignmentError("assignment_view_invalid", 422)
        state.update(mode=mode, available_actions=["chrome_assignment_create"],
            submission_ids={"chrome_assignment_create": str(uuid.uuid4())})
        if mode == "list":
            records = await service.list(user_id, claims, limit=50, after_id=params.get("assignment_cursor"))
            state["assignments"] = [_assignment_row(item)[0] for item in records]
            if len(records) == 50:
                state["next_cursor"] = state["assignments"][-1]["assignment_id"]
            return state
        if mode == "create":
            row, limits = {"definition": {"source_key": "public_page", "allowed_tools": ["web-research-1:fetch_page"], "currency_cap_enabled": False}}, _assignment_limits()
        else:
            row, limits = _assignment_row(await service.get(user_id, claims, identity))
        state["assignment"] = row
        if mode in ("create", "revise"):
            state["source_options"], state["tool_options"] = await asyncio.to_thread(_assignment_tool_options, orch, service, user_id, claims)
            state["registered_reader_enabled"] = any(source != "public_page" for source in state["source_options"])
            state["limit_fields"] = _assignment_limit_fields(limits)
            _assignment_draft(params, row["definition"])
        else:
            cursor = params.get("activity_cursor", "0")
            if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,19}", cursor):
                raise AssignmentError("assignment_cursor_invalid", 422)
            activity = await service.activity(user_id, claims, identity, after_sequence=int(cursor), limit=50)
            row["activity"] = [{key: thaw(item).get(key) for key in ("title", "summary", "created_at")} for item in activity]
            if len(activity) == 50:
                row["activity_cursor"] = str(thaw(activity[-1])["sequence"])
            row["approvals"] = [_assignment_approval(item) for item in await service.proposals(user_id, claims, identity)]
        return state
    except (AssignmentError, ValidationError, ValueError, TypeError):
        return {**state, "error": "This assignment view is unavailable or invalid. Reload Schedule and check your authorization."}
    except Exception:  # noqa: BLE001 - fail closed without disclosing backend diagnostics
        logger.error("assignment_surface_query_failed")
        return {**state, "error": "The assignment state could not be loaded. Reload Schedule."}


async def _assignment_view(orch, user_id, params):
    from astralprojection.chrome.assignments import build_assignments_view

    from orchestrator.chrome_events import current_surface_socket
    return build_assignments_view(await _assignment_state(orch, current_surface_socket.get(), user_id, params))


def _assignment_integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        return int(value)
    if type(value) is int:
        return value
    raise ValueError("invalid integer")


def _assignment_form(fields):
    """Strictly convert native/web form values to the one service request model."""
    from persistent_agents.models import AssignmentError
    if not isinstance(fields, dict) or fields.get("consent") is not True:
        raise AssignmentError("assignment_consent_required", 422)
    allowed = {"name", "instructions", "source_key", "source_url", "source_arguments", "linked_document_urls",
        "allowed_tools", "conversation_id", "completion_condition", "consent", "currency_cap_enabled", "currency"}
    limit_names = set(_ASSIGNMENT_LIMITS) | {f"{period}.{metric}" for period in ("daily", "lifetime") for metric in (*_ASSIGNMENT_METRICS, "spend_micro_units")}
    if set(fields) - allowed - {f"limits.{name}" for name in limit_names}:
        raise ValueError("invalid fields")
    limits = {"daily": {}, "lifetime": {}}
    capped = fields.get("currency_cap_enabled", False)
    if type(capped) is not bool:
        raise ValueError("invalid currency selection")
    for name in limit_names:
        value = fields.get(f"limits.{name}")
        period, separator, metric = name.partition(".")
        target = limits[period] if separator else limits
        if metric == "spend_micro_units" and not capped:
            target[metric] = None
        else:
            target[metric if separator else name] = _assignment_integer(value)
    for period in ("daily", "lifetime"):
        limits[period]["currency"] = fields.get("currency") if capped else None
    source_key = fields.get("source_key")
    if source_key == "public_page":
        source = {"profile": "public_page", "agent_id": "web-research-1", "tool_name": "fetch_page", "arguments": {"url": fields.get("source_url")}}
    else:
        agent, tool = source_key.split(":", 1)
        raw = fields.get("source_arguments", "")
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("invalid reader selection")
        source = {"profile": "registered_reader", "agent_id": agent, "tool_name": tool, "arguments": json.loads(raw)}
    linked = fields.get("linked_document_urls", "")
    if not isinstance(linked, str) or len(linked) > 8 * 2049:
        raise ValueError("invalid linked URLs")
    source["linked_document_urls"] = [url.strip() for url in linked.splitlines() if url.strip()]
    tools = fields.get("allowed_tools")
    if not isinstance(tools, list) or len(tools) > 32:
        raise ValueError("invalid tools")
    references = []
    for identity in tools:
        agent, tool = identity.split(":", 1)
        references.append({"agent_id": agent, "tool_name": tool})
    return {"name": fields.get("name"), "instructions": fields.get("instructions"), "source": source,
        "allowed_tools": references, "limits": limits, "consent": True,
        "conversation_id": fields.get("conversation_id") or None,
        "completion_condition": fields.get("completion_condition") or None}


def _assignment_handler(command):
    async def handle(orch, websocket, user_id, roles, payload):
        from persistent_agents.models import (
            ApprovalDecisionRequest,
            AssignmentError,
            ControlRequest,
            CreateAssignmentRequest,
            ReviseAssignmentRequest,
            validate_id,
        )
        params = _params("schedule")
        try:
            service, claims = _assignment_access(orch, websocket, user_id)
            if not isinstance(payload, dict):
                raise TypeError("invalid payload")
            body = dict(payload)
            # Clients echo their transport generation inside chrome payloads.
            # Validate and discard only that envelope field; it grants no
            # assignment authority and is not part of a durable owner command.
            if "request_generation" in body:
                validate_id(body.pop("request_generation"))
            if command != "create":
                identity = validate_id(body.pop("assignment_id"))
                params["assignment_id"] = identity
            if command in ("create", "revise"):
                fields = _assignment_form(body.pop("fields", None))
                if command == "create":
                    request = CreateAssignmentRequest.model_validate({**fields, **body})
                    # Unknown top-level fields are rejected; none can override reviewed fields.
                    if set(body) != {"submission_id"}:
                        raise ValueError("invalid payload")
                    result = await service.create(user_id, claims, request)
                    params["assignment_id"] = result.assignment_id
                else:
                    if set(body) != {"submission_id", "expected_instruction_revision", "expected_control_epoch"}:
                        raise ValueError("invalid payload")
                    request = ReviseAssignmentRequest.model_validate({**fields, **body})
                    result = await service.revise(user_id, claims, identity, request)
                message = "Assignment instructions and consent recorded."
            elif command == "approval_decide":
                action_id = validate_id(body.pop("action_id"))
                request = ApprovalDecisionRequest.model_validate(body)
                result = await service.decide(user_id, claims, identity, action_id, request, interaction=websocket)
                outcome = getattr(result, "state", None) or getattr(result, "outcome", None)
                message = ("Action outcome recorded. Reload activity for the durable result." if outcome in ("completed", "declined", "succeeded", "failed", "uncertain")
                    else "Decision recorded. The attended security review may require another approval; inspect activity before continuing.")
            else:
                request = ControlRequest.model_validate(body)
                result = await service.control(user_id, claims, identity, command.replace("_", "-"), request)
                message = "Check requested within cadence and resource limits." if command == "run_now" else f"Assignment {command} recorded."
            if getattr(result, "begun_action_ids", ()):
                message += " An already started external request remains in flight; this control cannot undo it."
            runner = getattr(orch, "persistent_assignment_runner", None)
            if runner is not None:
                runner.notify(params.get("assignment_id"))
            return SURFACE_KEY, params, notice_block("success", message)
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
            message = "Invalid assignment request. Reload the current form and review every field."
        except AssignmentError as exc:
            message = f"Assignment request refused ({exc.code}). Reload the current assignment and review your authorization."
        except Exception:  # noqa: BLE001 - ambiguous outcomes require durable reconciliation
            logger.error("assignment_surface_command_failed")
            message = "The assignment outcome could not be confirmed. Reload activity before retrying; reuse the original submission for a transport retry."
        return SURFACE_KEY, params, notice_block("error", message)
    return handle


HANDLERS = {
    "chrome_profile_save": _handle_profile_save,
    "chrome_memory_update": _handle_memory_update,
    "chrome_memory_delete": _handle_memory_delete,
    "chrome_skill_toggle": _handle_skill_toggle,
    "chrome_job_pause": _handle_job_pause,
    "chrome_job_resume": _handle_job_resume,
    "chrome_job_delete": _handle_job_delete,
    "chrome_job_run_now": _handle_job_run_now,
    "chrome_assignment_create": _assignment_handler("create"),
    "chrome_assignment_revise": _assignment_handler("revise"),
    "chrome_assignment_pause": _assignment_handler("pause"),
    "chrome_assignment_resume": _assignment_handler("resume"),
    "chrome_assignment_stop": _assignment_handler("stop"),
    "chrome_assignment_revoke": _assignment_handler("revoke"),
    "chrome_assignment_run_now": _assignment_handler("run_now"),
    "chrome_assignment_approval_decide": _assignment_handler("approval_decide"),
    "chrome_dreaming_toggle": _handle_dreaming_toggle,
    "chrome_dreaming_trigger": _handle_dreaming_trigger,
}
