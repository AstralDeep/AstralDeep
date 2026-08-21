"""Deep-owned host adapter for the Audit Trail Projection surface.

List view: filter bar (``event_class`` / ``outcome`` selects + keyword
text input) inside a ``data-ui-form`` container whose Apply button fires
``chrome_audit_page`` with collected ``fields``; reverse-chronological
rows (recorded_at, event_class, action_type, outcome badge, description
snippet) that open the detail view via ``chrome_open``; keyset cursor
pagination through a Next button carrying ``fields`` incl. ``cursor``.

Detail view (``params.event_id``): every public event field plus
``correlation_id`` and pretty-printed ``inputs_meta`` / ``outputs_meta``,
with a back link to the list.

Data access reuses the SAME internals as ``GET /api/audit`` and
``GET /api/audit/{id}`` (``backend/audit/api.py``):
``orch.audit_repo.list_for_user`` / ``get_for_user`` scoped to the
authenticated WebSocket user, the endpoints' artifact-availability
resolver, and the same ``audit_view`` self-recording through the
process-wide recorder (AU-2 / AU-12). ``auth_principal`` is the WS
user id — the WS session has no JWT claims dict, and ``require_user_id``
derives the user id from the same ``sub`` claim the REST path echoes.

Never HTTP-to-self. Every dynamic interpolation goes through ``esc()``.
"""
from __future__ import annotations

import asyncio
import json
import logging

from audit.api import _availability_resolver
from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import EVENT_CLASSES, OUTCOMES, AuditEventCreate
from webrender.chrome import chrome_error_block, esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.Audit")

TITLE = "Audit log"

_SURFACE_KEY = "audit"
_PAGE_LIMIT = 50
_SNIPPET_CHARS = 120

_OUTCOME_BADGE_STYLES = {
    "success": "border-green-500/20 bg-green-500/10 text-green-400",
    "failure": "border-red-500/20 bg-red-500/10 text-red-400",
    "in_progress": "border-astral-primary/20 bg-astral-primary/10 text-astral-primary",
    "interrupted": "border-yellow-500/20 bg-yellow-500/10 text-yellow-400",
}

_INPUT_CLS = (
    "bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-sm "
    "text-astral-text focus:outline-none focus:border-astral-primary/50"
)


# ---------------------------------------------------------------------------
# Small render helpers (all output escaped at every interpolation)
# ---------------------------------------------------------------------------

def _outcome_badge(outcome) -> str:
    """Render a small pill badge for an audit outcome value."""
    cls = _OUTCOME_BADGE_STYLES.get(outcome, "border-white/10 bg-white/5 text-astral-muted")
    return (
        f'<span class="inline-block px-2 py-0.5 rounded-full border text-[10px] '
        f'font-medium uppercase tracking-wide {cls}">{esc(outcome)}</span>'
    )


def _fmt_ts(value) -> str:
    """Format a datetime for display; ``-`` for missing values."""
    if value is None:
        return "-"
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover — defensive against odd types
        return str(value)


def _valid_or_none(value, allowed):
    """Return ``value`` if it is a member of ``allowed``, else ``None``.

    Filter params can only take invalid values via a tampered client (the
    rendered selects only offer valid options), so invalid values are
    silently dropped rather than erroring — same effective scope as the
    REST endpoint's 400 (the read stays user-scoped either way).
    """
    value = str(value or "").strip()
    return value if value in allowed else None


def _select(name: str, label: str, options, current) -> str:
    """Render a labeled ``<select name=...>`` with an All-values default."""
    opts = ['<option value="">All</option>']
    for option in options:
        sel = " selected" if option == current else ""
        opts.append(f'<option value="{esc(option)}"{sel}>{esc(option)}</option>')
    return (
        f'<label class="flex flex-col gap-1 text-xs text-astral-muted">{esc(label)}'
        f'<select name="{esc(name)}" class="{_INPUT_CLS}">{"".join(opts)}</select></label>'
    )


def _filter_bar(event_class, outcome, q) -> str:
    """Render the ``data-ui-form`` filter bar with the Apply button."""
    q_input = (
        f'<label class="flex flex-col gap-1 text-xs text-astral-muted flex-1 min-w-[10rem]">Search'
        f'<input type="text" name="q" value="{esc(q)}" '
        f'placeholder="Description or action type" class="{_INPUT_CLS}"></label>'
    )
    apply_btn = (
        '<button type="button" data-ui-action="chrome_audit_page" data-ui-collect="true" '
        'class="px-3 py-1.5 rounded-lg text-sm font-medium bg-astral-primary/20 '
        'text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30">'
        "Apply</button>"
    )
    return (
        '<div data-ui-form class="flex flex-wrap items-end gap-3 bg-white/5 '
        'border border-white/10 rounded-lg p-3">'
        + _select("event_class", "Event class", EVENT_CLASSES, event_class)
        + _select("outcome", "Outcome", OUTCOMES, outcome)
        + q_input
        + apply_btn
        + "</div>"
    )


def _row(dto) -> str:
    """Render one clickable list row that opens the detail view."""
    payload = esc(json.dumps(
        {"surface": _SURFACE_KEY, "params": {"event_id": str(dto.event_id)}}
    ))
    snippet = dto.description or ""
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[:_SNIPPET_CHARS].rstrip() + "..."
    return (
        f"<button type=\"button\" data-ui-action=\"chrome_open\" data-ui-payload='{payload}' "
        f'class="astral-audit-row w-full text-left bg-white/5 hover:bg-white/10 '
        f'border border-white/10 rounded-lg px-3 py-2 space-y-1">'
        f'<div class="flex items-center gap-2 flex-wrap">'
        f'<span class="text-xs text-astral-muted">{esc(_fmt_ts(dto.recorded_at))}</span>'
        f'<span class="text-xs font-medium text-astral-text">{esc(dto.event_class)}</span>'
        f'<span class="text-xs text-astral-muted">{esc(dto.action_type)}</span>'
        f'<span class="ml-auto">{_outcome_badge(dto.outcome)}</span></div>'
        f'<div class="text-sm text-astral-text/90">{esc(snippet)}</div></button>'
    )


def _next_button(next_cursor, event_class, outcome, q) -> str:
    """Render the Next pagination button (fields incl. the cursor)."""
    fields = {"cursor": next_cursor}
    if event_class:
        fields["event_class"] = event_class
    if outcome:
        fields["outcome"] = outcome
    if q:
        fields["q"] = q
    payload = esc(json.dumps({"fields": fields}))
    return (
        f'<div class="flex justify-end">'
        f"<button type=\"button\" data-ui-action=\"chrome_audit_page\" data-ui-payload='{payload}' "
        f'class="px-3 py-1.5 rounded-lg text-sm font-medium bg-white/5 hover:bg-white/10 '
        f'text-astral-text border border-white/10">Next</button></div>'
    )


def _back_button() -> str:
    """Render the detail view's back link to the audit list."""
    payload = esc(json.dumps({"surface": _SURFACE_KEY, "params": {}}))
    return (
        f"<button type=\"button\" data-ui-action=\"chrome_open\" data-ui-payload='{payload}' "
        f'class="inline-flex items-center gap-1 text-sm text-astral-primary hover:underline">'
        f"&larr; Back to audit log</button>"
    )


def _detail_row(label: str, value_html: str) -> str:
    """Render one label/value detail row. ``value_html`` is pre-escaped."""
    return (
        f'<div class="flex gap-3 text-sm">'
        f'<div class="w-36 shrink-0 text-astral-muted">{esc(label)}</div>'
        f'<div class="text-astral-text break-all min-w-0">{value_html}</div></div>'
    )


def _meta_block(label: str, data) -> str:
    """Pretty-print an inputs/outputs metadata dict inside an escaped pre."""
    pretty = json.dumps(data or {}, indent=2, sort_keys=True, default=str)
    return (
        f'<div class="space-y-1"><div class="text-xs font-semibold uppercase '
        f'tracking-wider text-astral-muted">{esc(label)}</div>'
        f'<pre class="text-xs text-astral-text/90 bg-black/30 border border-white/10 '
        f'rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{esc(pretty)}</pre></div>'
    )


def _pointers_block(pointers) -> str:
    """Render artifact pointers (id, store, extension, size, availability)."""
    if not pointers:
        return ""
    rows = []
    for p in pointers:
        availability = "available" if p.available else "no longer available"
        ext = p.extension or "unknown type"
        size = f"{p.size_bytes} bytes" if p.size_bytes is not None else "size unknown"
        rows.append(
            f'<li class="text-xs text-astral-muted">{esc(p.store)} / {esc(p.artifact_id)} '
            f"({esc(ext)}, {esc(size)}) - {esc(availability)}</li>"
        )
    return (
        f'<div class="space-y-1"><div class="text-xs font-semibold uppercase '
        f'tracking-wider text-astral-muted">Artifacts</div>'
        f'<ul class="space-y-0.5 list-disc list-inside">{"".join(rows)}</ul></div>'
    )


# ---------------------------------------------------------------------------
# audit_view self-recording (same shape as backend/audit/api.py)
# ---------------------------------------------------------------------------

async def _record_list_view(user_id, event_class, outcome, q, cursor, returned_count) -> None:
    """Self-record the list read exactly like ``GET /api/audit`` does.

    Never lets a recording failure break the read itself (AU-2 / AU-12).
    """
    recorder = get_recorder()
    if recorder is None:
        return
    try:
        await recorder.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=user_id,
            event_class="audit_view",
            action_type="audit_view.list",
            description="Viewed audit log list",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "limit": _PAGE_LIMIT,
                "filters": {
                    "event_class": [event_class] if event_class else [],
                    "outcome": [outcome] if outcome else [],
                    "from": None,
                    "to": None,
                    "has_q": bool(q),
                    "has_cursor": bool(cursor),
                },
            },
            outputs_meta={"returned_count": returned_count},
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover — never block the read
        logger.debug("audit_view self-record failed: %s", exc)


async def _record_detail_view(user_id, event_id) -> None:
    """Self-record the detail read exactly like ``GET /api/audit/{id}``."""
    recorder = get_recorder()
    if recorder is None:
        return
    try:
        await recorder.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=user_id,
            event_class="audit_view",
            action_type="audit_view.detail",
            description=f"Viewed audit detail {event_id}",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={"event_id": event_id},
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover — never block the read
        logger.debug("audit_view detail self-record failed: %s", exc)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

async def _render_list(orch, user_id, params) -> str:
    """Render the filterable, cursor-paginated audit list body."""
    event_class = _valid_or_none(params.get("event_class"), EVENT_CLASSES)
    outcome = _valid_or_none(params.get("outcome"), OUTCOMES)
    q = str(params.get("q") or "").strip()
    cursor = str(params.get("cursor") or "").strip() or None

    notices = []
    kwargs = {
        "limit": _PAGE_LIMIT,
        "cursor": cursor,
        "event_classes": [event_class] if event_class else None,
        "outcomes": [outcome] if outcome else None,
        "keyword": q or None,
        "availability_resolver": _availability_resolver(orch, user_id),
    }
    try:
        items, next_cursor = await asyncio.to_thread(
            orch.audit_repo.list_for_user, user_id, **kwargs)
    except ValueError as exc:
        # Expected failure: a stale/corrupt cursor. Fall back to page one.
        logger.warning("chrome audit: invalid cursor for user %s: %s", user_id, exc)
        notices.append(notice_block("error", "Invalid page cursor - showing the first page."))
        cursor = None
        kwargs["cursor"] = None
        items, next_cursor = await asyncio.to_thread(
            orch.audit_repo.list_for_user, user_id, **kwargs)

    await _record_list_view(user_id, event_class, outcome, q, cursor, len(items))

    if items:
        rows_html = (
            f'<div class="space-y-2">{"".join(_row(dto) for dto in items)}</div>'
        )
    else:
        rows_html = (
            '<div class="bg-white/5 border border-white/10 rounded-lg p-4 '
            'text-sm text-astral-muted">No audit entries match the current filters.</div>'
        )

    count_line = (
        f'<div class="text-xs text-astral-muted">Showing {esc(len(items))} '
        f"entr{'y' if len(items) == 1 else 'ies'}</div>"
    )
    pager = _next_button(next_cursor, event_class, outcome, q) if next_cursor else ""
    return "".join(notices) + _filter_bar(event_class, outcome, q) + count_line + rows_html + pager


async def _render_detail(orch, user_id, event_id) -> str:
    """Render the detail body for one audit event (user-scoped fetch)."""
    event_id = str(event_id)
    dto = await asyncio.to_thread(
        orch.audit_repo.get_for_user,
        user_id,
        event_id,
        availability_resolver=_availability_resolver(orch, user_id),
    )
    if dto is None:
        # Non-existence and cross-user access are indistinguishable
        # (FR-007 / FR-019) — same posture as the REST 404.
        return _back_button() + chrome_error_block(
            "Audit event not found.", retry_surface=_SURFACE_KEY
        )

    await _record_detail_view(user_id, event_id)

    rows = [
        _detail_row("Recorded at", esc(_fmt_ts(dto.recorded_at))),
        _detail_row("Event class", esc(dto.event_class)),
        _detail_row("Action type", esc(dto.action_type)),
        _detail_row("Outcome", _outcome_badge(dto.outcome)),
        _detail_row("Description", esc(dto.description)),
        _detail_row("Event id", esc(dto.event_id)),
        _detail_row("Correlation id", esc(dto.correlation_id)),
        _detail_row("Agent", esc(dto.agent_id or "-")),
        _detail_row("Conversation", esc(dto.conversation_id or "-")),
        _detail_row("Started at", esc(_fmt_ts(dto.started_at))),
        _detail_row("Completed at", esc(_fmt_ts(dto.completed_at))),
    ]
    if dto.outcome_detail:
        rows.append(_detail_row("Outcome detail", esc(dto.outcome_detail)))

    card = (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4 space-y-2">'
        f'{"".join(rows)}</div>'
    )
    return (
        _back_button()
        + card
        + _meta_block("Inputs metadata", dto.inputs_meta)
        + _meta_block("Outputs metadata", dto.outputs_meta)
        + _pointers_block(dto.artifact_pointers)
    )


async def render(orch, user_id, roles, params) -> str:
    """Render the audit surface body (list, or detail when ``event_id`` set).

    Args:
        orch: Orchestrator instance (``orch.audit_repo`` is used directly —
            the same repository the REST endpoints call).
        user_id: Authenticated WebSocket user id; all reads are scoped to it.
        roles: Session roles (unused — the audit log is per-user, not gated).
        params: ``{event_id?}`` for detail, else ``{cursor?, event_class?,
            outcome?, q?}`` list filters.

    Returns:
        Body HTML for the chrome modal (escape-by-default via ``esc()``).
    """
    params = params or {}
    event_id = params.get("event_id")
    if event_id:
        return await _render_detail(orch, user_id, event_id)
    return await _render_list(orch, user_id, params)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _handle_audit_page(orch, websocket, user_id, roles, payload):
    """Apply list filters / pagination (``chrome_audit_page {fields}``).

    Pure navigation — no mutation. Builds the list params from the
    collected form ``fields`` (empty values dropped, whitespace trimmed)
    and asks the dispatcher to re-render the ``audit`` surface with them.

    Returns:
        ``(surface_key, params, notice_html)`` per the surface-module
        handler contract (empty notice — nothing was saved).
    """
    fields = (payload or {}).get("fields") or {}
    params = {}
    for key in ("event_class", "outcome", "q", "cursor"):
        value = fields.get(key)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            params[key] = value
    return (_SURFACE_KEY, params, "")


HANDLERS = {"chrome_audit_page": _handle_audit_page}
