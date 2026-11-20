"""Renders the Audit Trail surface: a filterable, cursor-paginated event list, optionally
scoped to one chat, and a per-event detail view. Reads reuse the same orch.audit_repo
internals as the GET /api/audit REST routes in backend/audit/api.py, scoped to the
authenticated user, and orch.history names the scoped chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from itertools import groupby

from audit.api import _availability_resolver
from audit.recorder import get_recorder, make_correlation_id, now_utc
from audit.schemas import EVENT_CLASSES, OUTCOMES, AuditEventCreate
from webrender.chrome import chrome_error_block, esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.Audit")

TITLE = "Audit log"

_SURFACE_KEY = "audit"
_PAGE_LIMIT = 50
_SNIPPET_CHARS = 120
_LIST_KEYS = ("conversation_id", "event_class", "outcome", "q", "cursor", "from", "to", "history")
_VALUE_LIMITS = {"conversation_id": 256, "event_class": 64, "outcome": 32, "q": 256,
                 "cursor": 512, "from": 10, "to": 10}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class _FilterError(ValueError):
    pass


def _history(value) -> list[str]:
    if not isinstance(value, str) or len(value) > 16_384:
        return []
    try:
        items = json.loads(value or "[]")
    except (ValueError, RecursionError):
        return []
    if not isinstance(items, list) or len(items) > 100:
        return []
    return items if all(isinstance(item, str) and len(item) <= 512 for item in items) else []


def _list_params(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _LIST_KEYS:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()
        if "\x00" in item:
            raise ValueError("Audit filters cannot contain null characters.")
        if key == "history":
            result[key] = json.dumps(_history(item))
        elif key == "conversation_id" and (
            len(item) > _VALUE_LIMITS[key] or _CONTROL_RE.search(item)
        ):
            raise ValueError("Choose a valid chat for the audit log.")
        elif len(item) > _VALUE_LIMITS[key]:
            raise ValueError(
                "Choose valid dates with From no later than Through." if key in ("from", "to")
                else "Search is limited to 256 characters; use shorter audit filters."
            )
        else:
            result[key] = item
    return result


def _date_bound(value, *, end=False):
    if not value:
        return None
    parsed = date.fromisoformat(str(value))
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    start = datetime.combine(parsed, time.min, tzinfo=timezone.utc)
    return start + timedelta(days=1) if end else start


def _chat_title(orch, user_id, conversation_id):
    if not conversation_id:
        return None
    try:
        record = orch.history.get_conversation_record(conversation_id, user_id)
    except ValueError:
        return None
    return None if record is None else (record.title or "New Chat")


def _nav_button(label, fields) -> str:
    payload = esc(json.dumps({"fields": fields}))
    return (
        f'<button type="button" data-ui-action="chrome_audit_page" data-ui-payload=\'{payload}\' '
        'class="px-3 py-1.5 rounded-lg text-sm bg-white/5 hover:bg-white/10 '
        f'border border-white/10">{esc(label)}</button>'
    )

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


def _outcome_badge(outcome) -> str:
    cls = _OUTCOME_BADGE_STYLES.get(outcome, "border-white/10 bg-white/5 text-astral-muted")
    return (
        f'<span class="inline-block px-2 py-0.5 rounded-full border text-[10px] '
        f'font-medium uppercase tracking-wide {cls}">{esc(outcome)}</span>'
    )


def _fmt_ts(value) -> str:
    if value is None:
        return "-"
    try:
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover
        return str(value)


def _valid_or_none(value, allowed):
    value = str(value or "").strip()
    return value if value in allowed else None


def _select(name: str, label: str, options, current, *, empty_label="All") -> str:
    opts = [f'<option value="">{esc(empty_label)}</option>']
    for option in options:
        value, text = option if isinstance(option, tuple) else (option, option)
        sel = " selected" if value == current else ""
        opts.append(f'<option value="{esc(value)}"{sel}>{esc(text)}</option>')
    return (
        f'<label class="flex flex-col gap-1 text-xs text-astral-muted">{esc(label)}'
        f'<select name="{esc(name)}" class="{_INPUT_CLS}">{"".join(opts)}</select></label>'
    )


def _filter_bar(event_class, outcome, q, params=None, chat_title=None) -> str:
    q_input = (
        f'<label class="flex flex-col gap-1 text-xs text-astral-muted flex-1 min-w-[10rem]">Search'
        f'<input type="text" name="q" maxlength="256" value="{esc(q)}" '
        f'placeholder="Description or action type" class="{_INPUT_CLS}"></label>'
    )
    apply_btn = (
        '<button type="button" data-ui-action="chrome_audit_page" data-ui-collect="true" '
        'class="px-3 py-1.5 rounded-lg text-sm font-medium bg-astral-primary/20 '
        'text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30">'
        "Apply</button>"
    )
    params = params or {}
    chat_id = params.get("conversation_id") or ""
    scope = {"conversation_id": chat_id} if chat_id else {}
    chat_label = chat_title or chat_id
    chat_select = (
        _select("conversation_id", "Chat", [(chat_id, chat_label)], chat_id,
                empty_label="All chats") if chat_id else ""
    )
    heading = (
        f'<h3 class="text-sm font-semibold">Chat: {esc(chat_label)}</h3>' if chat_id else ""
    )
    dates = "".join(
        f'<label class="flex flex-col gap-1 text-xs text-astral-muted">{label} (UTC)'
        f'<input type="date" name="{key}" value="{esc(params.get(key, ""))}" '
        f'class="{_INPUT_CLS}"></label>'
        for key, label in (("from", "From"), ("to", "Through"))
    )
    return (
        heading
        + '<div data-ui-form class="flex flex-wrap items-end gap-3 bg-white/5 '
        'border border-white/10 rounded-lg p-3">'
        + chat_select
        + _select("event_class", "Event class", EVENT_CLASSES, event_class)
        + _select("outcome", "Outcome", OUTCOMES, outcome)
        + q_input
        + dates
        + apply_btn
        + _nav_button("Reset", scope)
        + "</div>"
        + '<div class="flex flex-wrap gap-2" aria-label="Quick audit filters">'
        + _nav_button("All activity", {})
        + _nav_button("Failures", {**scope, "outcome": "failure"})
        + _nav_button("Tool calls", {**scope, "event_class": "agent_tool_call"})
        + _nav_button("Sign ins", {**scope, "event_class": "auth"})
        + "</div>"
    )


def _row(dto, return_to=None) -> str:
    payload = esc(json.dumps(
        {"surface": _SURFACE_KEY, "params": {"event_id": str(dto.event_id),
                                            "return_to": return_to or {}}}
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


def _pager(next_cursor, params) -> str:
    history = _history(params.get("history"))
    buttons = []
    if params.get("cursor"):
        previous = dict(params, cursor=history[-1] if history else "",
                        history=json.dumps(history[:-1]))
        buttons.append(_nav_button("Previous", previous))
        buttons.append(_nav_button("Newest", {k: v for k, v in params.items()
                                               if k not in ("cursor", "history")}))
    if next_cursor:
        following = dict(params, cursor=next_cursor,
                         history=json.dumps((history + [params.get("cursor", "")])[-100:]))
        buttons.append(_nav_button("Next", following))
    return '<nav class="flex flex-wrap gap-2" aria-label="Audit pages">' + "".join(buttons) + "</nav>"


def _back_button(params=None) -> str:
    payload = esc(json.dumps({"surface": _SURFACE_KEY, "params": params or {}}))
    return (
        f"<button type=\"button\" data-ui-action=\"chrome_open\" data-ui-payload='{payload}' "
        f'class="inline-flex items-center gap-1 text-sm text-astral-primary hover:underline">'
        f"&larr; Back to audit log</button>"
    )


def _detail_row(label: str, value_html: str) -> str:
    return (
        f'<div class="flex gap-3 text-sm">'
        f'<div class="w-36 shrink-0 text-astral-muted">{esc(label)}</div>'
        f'<div class="text-astral-text break-all min-w-0">{value_html}</div></div>'
    )


def _meta_block(label: str, data) -> str:
    pretty = json.dumps(data or {}, indent=2, sort_keys=True, default=str)
    return (
        f'<div class="space-y-1"><div class="text-xs font-semibold uppercase '
        f'tracking-wider text-astral-muted">{esc(label)}</div>'
        f'<pre class="text-xs text-astral-text/90 bg-black/30 border border-white/10 '
        f'rounded-lg p-3 overflow-x-auto whitespace-pre-wrap">{esc(pretty)}</pre></div>'
    )


def _pointers_block(pointers) -> str:
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


async def _record_list_view(user_id, event_class, outcome, q, cursor, returned_count,
                            from_ts=None, to_ts=None, conversation_id=None) -> None:
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
                    **({"conversation_id": conversation_id} if conversation_id else {}),
                    "event_class": [event_class] if event_class else [],
                    "outcome": [outcome] if outcome else [],
                    "from": from_ts.isoformat() if from_ts else None,
                    "to": to_ts.isoformat() if to_ts else None,
                    "has_q": bool(q),
                    "has_cursor": bool(cursor),
                },
            },
            outputs_meta={"returned_count": returned_count},
            started_at=now_utc(),
        ))
    except Exception as exc:  # pragma: no cover
        logger.debug("audit_view self-record failed: %s", exc)


async def _record_detail_view(user_id, event_id) -> None:
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
    except Exception as exc:  # pragma: no cover
        logger.debug("audit_view detail self-record failed: %s", exc)


async def _read_list(orch, user_id, params):
    try:
        params = _list_params(params)
    except ValueError as exc:
        raise _FilterError(str(exc)) from exc
    event_class = _valid_or_none(params.get("event_class"), EVENT_CLASSES)
    outcome = _valid_or_none(params.get("outcome"), OUTCOMES)
    q = str(params.get("q") or "").strip()
    cursor = str(params.get("cursor") or "").strip() or None
    conversation_id = params.get("conversation_id") or None
    params = {key: str(params[key]) for key in _LIST_KEYS if params.get(key)}
    params.update(event_class=event_class or "", outcome=outcome or "", q=q,
                  cursor=cursor or "", history=json.dumps(_history(params.get("history"))))
    params = {key: value for key, value in params.items() if value}

    notices = []
    try:
        from_ts = _date_bound(params.get("from"))
        to_ts = _date_bound(params.get("to"), end=True)
        if from_ts and to_ts and from_ts >= to_ts:
            raise ValueError("reversed dates")
    except (ValueError, OverflowError) as exc:
        raise _FilterError("Choose valid dates with From no later than Through.") from exc
    kwargs = {
        "limit": _PAGE_LIMIT,
        "cursor": cursor,
        "event_classes": [event_class] if event_class else None,
        "outcomes": [outcome] if outcome else None,
        "keyword": q or None,
        "conversation_id": conversation_id,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "availability_resolver": _availability_resolver(orch, user_id),
    }
    try:
        items, next_cursor = await asyncio.to_thread(
            orch.audit_repo.list_for_user, user_id, **kwargs)
    except ValueError as exc:
        if not cursor:
            raise
        logger.warning("chrome audit: invalid cursor for user %s: %s", user_id, exc)
        notices.append("Invalid page cursor - showing the first page.")
        cursor = None
        params.pop("cursor", None)
        params["history"] = "[]"
        kwargs["cursor"] = None
        items, next_cursor = await asyncio.to_thread(
            orch.audit_repo.list_for_user, user_id, **kwargs)

    await _record_list_view(user_id, event_class, outcome, q, cursor, len(items), from_ts, to_ts,
                            conversation_id)

    return params, event_class, outcome, q, items, next_cursor, notices


async def _render_list(orch, user_id, params) -> str:
    try:
        params, event_class, outcome, q, items, next_cursor, notices = await _read_list(orch, user_id, params)
    except _FilterError as exc:
        try:
            params = _list_params(params)
        except ValueError:
            params = {}
        chat_title = await asyncio.to_thread(
            _chat_title, orch, user_id, params.get("conversation_id"))
        return (notice_block("error", str(exc)) + _filter_bar(
            _valid_or_none(params.get("event_class"), EVENT_CLASSES),
            _valid_or_none(params.get("outcome"), OUTCOMES), params.get("q", ""), params,
            chat_title,
        ))
    notices = [notice_block("error", message) for message in notices]
    chat_title = await asyncio.to_thread(_chat_title, orch, user_id, params.get("conversation_id"))

    if items:
        groups = []
        for day, entries in groupby(items, key=lambda dto: _fmt_ts(dto.recorded_at)[:10]):
            groups.append(f'<h3 class="text-sm font-semibold mt-3">{esc(day)} (UTC)</h3>')
            for routine, batch in groupby(entries, key=lambda dto: (
                dto.outcome == "success" and dto.action_type in (
                    "ws.chrome_open", "ws.chrome_close", "audit_view.list", "audit_view.detail"
                )
            )):
                rows = list(batch)
                body = "".join(_row(dto, params) for dto in rows)
                if routine and len(rows) > 1:
                    body = ('<details class="astral-collapsible border border-white/10 rounded-lg p-2">'
                            f'<summary class="flex items-center gap-2 cursor-pointer text-sm text-astral-muted">'
                            f'{len(rows)} navigation and audit views</summary>'
                            f'<div class="space-y-2 mt-2">{body}</div></details>')
                groups.append(body)
        rows_html = '<div class="space-y-2">' + "".join(groups) + "</div>"
    else:
        rows_html = (
            '<div class="bg-white/5 border border-white/10 rounded-lg p-4 '
            'text-sm text-astral-muted">No audit entries match the current filters.</div>'
        )

    count_line = (
        f'<div class="text-xs text-astral-muted">Showing {esc(len(items))} '
        f"entr{'y' if len(items) == 1 else 'ies'}</div>"
    )
    pager = _pager(next_cursor, params)
    return ("".join(notices) + _filter_bar(event_class, outcome, q, params, chat_title)
            + count_line + pager + rows_html + pager)


async def _render_detail(orch, user_id, event_id, return_to=None) -> str:
    event_id = str(event_id)
    dto = await asyncio.to_thread(
        orch.audit_repo.get_for_user,
        user_id,
        event_id,
        availability_resolver=_availability_resolver(orch, user_id),
    )
    if dto is None:
        return _back_button(return_to) + chrome_error_block(
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
        _back_button(return_to)
        + card
        + _meta_block("Inputs metadata", dto.inputs_meta)
        + _meta_block("Outputs metadata", dto.outputs_meta)
        + _pointers_block(dto.artifact_pointers)
    )


async def render(orch, user_id, roles, params) -> str:
    params = params if isinstance(params, dict) else {}
    event_id = params.get("event_id")
    if event_id:
        try:
            return_to = _list_params(params.get("return_to"))
        except ValueError:
            return_to = {}
        return await _render_detail(orch, user_id, event_id, return_to)
    return await _render_list(orch, user_id, params)


def _entry_snapshot(dto):
    entry = dto.model_dump(mode="json")
    for key in ("recorded_at", "started_at", "completed_at"):
        entry[key] = _fmt_ts(getattr(dto, key))
    entry["artifacts"] = entry.get("artifact_pointers", [])
    return entry


async def components(orch, user_id, roles, params):
    from astralprojection.chrome.admin import build_audit_view
    from webrender.chrome.surfaces import _sdui

    params = params if isinstance(params, dict) else {}
    if params.get("event_id"):
        try:
            return_to = _list_params(params.get("return_to"))
        except ValueError:
            return_to = {}
        event_id = str(params["event_id"])
        dto = await asyncio.to_thread(
            orch.audit_repo.get_for_user, user_id, event_id,
            availability_resolver=_availability_resolver(orch, user_id),
        )
        if dto is not None:
            await _record_detail_view(user_id, event_id)
        view = build_audit_view(selected=_entry_snapshot(dto) if dto is not None else {}, filters=return_to)
        return [item.to_dict() for item in view.components]
    try:
        active, _, _, _, items, next_cursor, notices = await _read_list(orch, user_id, params)
    except _FilterError as exc:
        try:
            active = _list_params(params)
        except ValueError:
            active = {}
        chat_title = await asyncio.to_thread(
            _chat_title, orch, user_id, active.get("conversation_id"))
        view = build_audit_view(event_classes=EVENT_CLASSES, filters=active, loaded=False,
                                chat_title=chat_title)
        return [_sdui.alert(str(exc), "error"), *[item.to_dict() for item in view.components]]
    chat_title = await asyncio.to_thread(_chat_title, orch, user_id, active.get("conversation_id"))
    view = build_audit_view(
        [_entry_snapshot(dto) for dto in items], filters=active,
        event_classes=EVENT_CLASSES, next_cursor=next_cursor, chat_title=chat_title,
    )
    return [*[_sdui.alert(message, "error") for message in notices],
            *[item.to_dict() for item in view.components]]


async def _handle_audit_page(orch, websocket, user_id, roles, payload):
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    try:
        params = _list_params(fields)
    except ValueError as exc:
        return (_SURFACE_KEY, {}, notice_block("error", str(exc)))
    return (_SURFACE_KEY, params, "")


HANDLERS = {"chrome_audit_page": _handle_audit_page}
