"""Host adapter for the federated Search chrome surface: renders owner-scoped,
source-linked results over personalization/source_search.py with filters, cursor
pagination, and per-source opening through existing authenticated client actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from personalization.source_search import (
    SOURCES,
    SOURCE_LABELS,
    SearchOutcome,
    SourceUnavailable,
    encode_cursor,
    federated_search,
    resolve_source,
)
from webrender.chrome import esc, notice_block
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Search")

TITLE = "Search"
SURFACE_KEY = "search"

_INPUT_CLS = (
    "bg-white/5 border border-white/10 rounded-lg px-2 py-1.5 text-sm "
    "text-astral-text focus:outline-none focus:border-astral-primary/50"
)
_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-sm font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_GHOST = (
    "px-3 py-1.5 rounded-lg text-sm text-astral-muted hover:text-astral-text "
    "hover:bg-white/5 border border-white/10"
)
_ROW_CLS = "bg-white/5 border border-white/10 rounded-lg px-3 py-2 space-y-1"


def _open_payload(params) -> str:
    return esc(json.dumps({"surface": SURFACE_KEY, "params": params}, separators=(",", ":")))


def _nav_button(label, params) -> str:
    current = ' aria-current="true"' if params.get("active") else ""
    params = {key: value for key, value in params.items() if key != "active"}
    return (
        f'<button type="button"{current} data-ui-action="chrome_open" '
        f"data-ui-payload='{_open_payload(params)}' class=\"{_BTN_GHOST}\">"
        f"{esc(label)}</button>"
    )


def _filter_bar(params) -> str:
    query = params.get("q", "")
    active = params.get("source", "")
    chips = [_nav_button("All sources", {"q": query, "active": not active})]
    chips.extend(
        _nav_button(SOURCE_LABELS[name], {"q": query, "source": name, "active": active == name})
        for name in SOURCES
    )
    submit_payload = _open_payload({"surface": SURFACE_KEY, "params": {}})
    return (
        '<div data-ui-form class="flex flex-wrap items-end gap-3 bg-white/5 '
        'border border-white/10 rounded-lg p-3">'
        '<label class="flex flex-col gap-1 text-xs text-astral-muted flex-1 min-w-[12rem]">Search'
        f'<input type="search" name="q" maxlength="200" value="{esc(query)}" '
        'placeholder="Search conversations, notes, and memory" '
        f'class="{_INPUT_CLS}"></label>'
        '<button type="button" data-ui-action="chrome_open" data-ui-collect="true" '
        f"data-ui-payload='{submit_payload}' "
        f'class="{_BTN_PRIMARY}">Search</button>'
        + '<div class="flex flex-wrap gap-2 w-full" role="group" aria-label="Filter by source">'
        + "".join(chips) + "</div></div>"
    )


def _conversation_open(hit) -> str:
    payload = esc(json.dumps({"chat_id": hit.ref.get("chat_id", "")}, separators=(",", ":")))
    return (
        f"<button type=\"button\" class=\"astral-action {_BTN_GHOST}\" "
        f"data-action=\"load_chat\" data-payload='{payload}' "
        f"aria-label=\"Open conversation {esc(hit.title)}\">Open conversation</button>"
    )


def _chrome_open_button(label, aria, surface, params) -> str:
    payload = esc(json.dumps({"surface": surface, "params": params}, separators=(",", ":")))
    return (
        f'<button type="button" data-ui-action="chrome_open" data-ui-payload=\'{payload}\' '
        f'class="{_BTN_GHOST}" aria-label="{esc(aria)}">{esc(label)}</button>'
    )


def _open_control(hit) -> str:
    if hit.source == "conversations":
        return _conversation_open(hit)
    if hit.source == "notes":
        return _chrome_open_button(
            "Open note", "Open private note", "guidance",
            {"mode": "edit", "note_id": hit.ref.get("note_id", ""),
             "expected_revision": hit.ref.get("expected_revision", 0)})
    return _chrome_open_button(
        "Open memory", "Open durable memory", "personalization", {"tab": "memory"})


def _hit_row(hit, unavailable_reason) -> str:
    source_label = SOURCE_LABELS.get(hit.source, hit.source)
    stamp = datetime.fromtimestamp(hit.updated_at / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    control = (
        f'<span class="text-xs text-astral-muted" role="status">{esc(unavailable_reason)}</span>'
        if unavailable_reason else _open_control(hit)
    )
    return (
        f'<div class="{_ROW_CLS}" data-search-hit="{esc(hit.source)}">'
        f'<div class="flex items-center gap-2 flex-wrap">'
        f'<span class="text-xs font-medium text-astral-text">{esc(source_label)}</span>'
        f'<span class="text-xs text-astral-muted">{esc(hit.title)}</span>'
        f'<span class="text-xs text-astral-muted ml-auto">{esc(stamp)} (UTC)</span></div>'
        f'<div class="text-sm text-astral-text/90">{esc(hit.snippet)}</div>'
        f'<div>{control}</div></div>'
    )


def _result_list(outcome: SearchOutcome, stale: dict) -> str:
    rows = "".join(_hit_row(hit, stale.get(_hit_key(hit))) for hit in outcome.hits)
    return f'<div class="space-y-2" role="list" aria-label="Search results">{rows}</div>'


def _hit_key(hit) -> str:
    return json.dumps(hit.ref, sort_keys=True, separators=(",", ":"))


def _pager(outcome: SearchOutcome, params) -> str:
    if outcome.next_offset is None:
        return ""
    following = dict(params, cursor=encode_cursor(outcome.next_offset))
    following.pop("error", None)
    return (
        '<nav class="flex gap-2" aria-label="More results">'
        f'<button type="button" data-ui-action="chrome_open" '
        f"data-ui-payload='{_open_payload(following)}' class=\"{_BTN_GHOST}\">"
        "More results</button></nav>"
    )


def _honest_notices(outcome: SearchOutcome) -> list[str]:
    unavailable = [report.source for report in outcome.reports if not report.available]
    if not outcome.any_available:
        return [notice_block("error",
                             "Search is unavailable right now. Retry in a moment; nothing was "
                             "searched, so results may be missing.")]
    notices = []
    if outcome.hits:
        if unavailable:
            names = ", ".join(SOURCE_LABELS[name] for name in unavailable)
            notices.append(notice_block(
                "error", f"{names} could not be searched right now, so results may be "
                         "missing. The sources above were searched."))
        if outcome.partial:
            notices.append(notice_block(
                "info", "The search scan limit was reached before every item was checked, "
                        "so results may be incomplete. Narrow the search or filter by source."))
        return notices
    if unavailable:
        names = ", ".join(SOURCE_LABELS[name] for name in unavailable)
        return [notice_block(
            "error", f"{names} could not be searched right now, so results may be missing. "
                     "Nothing else matched this search.")]
    return [notice_block("info", f"No matches found for “{outcome.query}”. "
                                 "Try different words or clear the source filter.")]


async def _audit_search(orch, user_id: str, outcome: SearchOutcome) -> None:
    try:
        from audit.recorder import get_recorder, make_correlation_id
        from audit.schemas import AuditEventCreate

        recorder = get_recorder()
        if recorder is None:
            return
        await recorder.record(AuditEventCreate(
            actor_user_id=user_id,
            auth_principal=user_id,
            event_class="personalization",
            action_type="personalization.source_search",
            description="Searched conversations, notes, and memory",
            correlation_id=make_correlation_id(),
            outcome="success",
            inputs_meta={
                "sources": list(outcome.source or SOURCES),
                "has_cursor": outcome.next_offset is not None,
                "query_chars": len(outcome.query),
            },
            outputs_meta={"returned_count": len(outcome.hits)},
            started_at=datetime.now(timezone.utc),
        ))
    except Exception:
        logger.debug("source_search: audit record failed", exc_info=True)


async def _stale_reasons(orch, user_id: str, outcome: SearchOutcome) -> dict:
    reasons: dict[str, str] = {}

    async def check(hit):
        try:
            reason = await resolve_source(orch, user_id=user_id, ref=hit.ref)
        except SourceUnavailable as exc:
            reason = exc.reason
        except Exception:
            logger.debug("source_search: staleness check failed", exc_info=True)
            return
        if reason:
            reasons[_hit_key(hit)] = reason

    await asyncio.gather(*(check(hit) for hit in outcome.hits))
    return reasons


async def _search_state(orch, user_id, params):
    params = params if isinstance(params, dict) else {}
    error = params.get("error")
    if isinstance(error, str) and error:
        return {"mode": "error", "params": params}, None, error
    query = params.get("q", "")
    if not isinstance(query, str):
        query = ""
    if not query.strip():
        return {"mode": "intro", "params": params}, None, ""
    try:
        outcome = await federated_search(
            orch, user_id=user_id, query=query, source=params.get("source") or None,
            cursor=params.get("cursor"),
            notes_caller=_current_caller(orch))
    except ValueError as exc:
        return {"mode": "error", "params": params}, None, str(exc)
    stale = await _stale_reasons(orch, user_id, outcome) if outcome.hits else {}
    await _audit_search(orch, user_id, outcome)
    return {"mode": "results", "params": params, "outcome": outcome}, stale, ""


def _current_caller(orch):
    try:
        from orchestrator.human_request_authority import current_human_caller

        return current_human_caller(expected_orchestrator=orch)
    except Exception:
        logger.debug("source_search: human caller unavailable", exc_info=True)
        return None


def form_params(params, fields) -> dict:
    from personalization.source_search import form_params as merge

    return merge(params, fields)


async def render(orch, user_id, roles, params) -> str:
    state, stale, error = await _search_state(orch, user_id, params)
    parts = [_filter_bar(state["params"])]
    if state["mode"] == "intro":
        parts.append(
            '<div class="bg-white/5 border border-white/10 rounded-lg p-4 text-sm '
            'text-astral-muted">Search your conversations, private notes, and durable '
            "memory from one place. Results link to their source.</div>")
        return f'<div class="space-y-4">{"".join(parts)}</div>'
    if state["mode"] == "error":
        parts.append(notice_block("error", error))
        return f'<div class="space-y-4">{"".join(parts)}</div>'
    outcome = state["outcome"]
    parts.extend(_honest_notices(outcome))
    if outcome.hits:
        parts.append(_result_list(outcome, stale))
    parts.append(_pager(outcome, state["params"]))
    return f'<div class="space-y-4">{"".join(parts)}</div>'


def _native_hit_card(hit, reason):
    label = SOURCE_LABELS.get(hit.source, hit.source)
    content = [_sdui.text(hit.snippet, "caption")]
    if reason:
        return _sdui.card(label, content), _sdui.alert(reason, "warning")
    if hit.source == "conversations":
        action = {"label": "Open conversation", "action": "load_chat", "variant": "secondary",
                  "payload": {"chat_id": hit.ref.get("chat_id", "")}}
    elif hit.source == "notes":
        action = {"label": "Open note", "action": "chrome_open", "variant": "secondary",
                  "payload": {"surface": "guidance", "params": {
                      "mode": "edit", "note_id": hit.ref.get("note_id", ""),
                      "expected_revision": hit.ref.get("expected_revision", 0)}}}
    else:
        action = {"label": "Open memory", "action": "chrome_open", "variant": "secondary",
                  "payload": {"surface": "personalization", "params": {"tab": "memory"}}}
    return _sdui.card(label, [*content, _sdui.form([], actions=[action])]), None


async def components(orch, user_id, roles, params):
    state, stale, error = await _search_state(orch, user_id, params)
    params = state["params"]
    query = params.get("q", "")
    out = [_sdui.form(
        [
            _sdui.field("q", "Search", "text", default=query,
                        help="Conversations, private notes, and durable memory"),
            _sdui.field("source", "Source", "select",
                        default=params.get("source", ""),
                        options=["", *SOURCES]),
        ],
        actions=[{"label": "Search", "action": "chrome_open",
                  "payload": {"surface": SURFACE_KEY, "params": {}}, "variant": "primary"}],
        title="Search",
    )]
    for name in SOURCES:
        out.append(_sdui.button(SOURCE_LABELS[name], "chrome_open",
                                {"surface": SURFACE_KEY,
                                 "params": {"q": query, "source": name}}))
    if state["mode"] == "intro":
        out.append(_sdui.alert("Search your conversations, private notes, and durable memory "
                               "from one place. Results link to their source.", "info"))
        return out
    if state["mode"] == "error":
        out.append(_sdui.alert(error, "error"))
        return out
    outcome = state["outcome"]
    unavailable = [report.source for report in outcome.reports if not report.available]
    if not outcome.any_available:
        out.append(_sdui.alert("Search is unavailable right now. Retry in a moment; nothing "
                               "was searched, so results may be missing.", "error"))
        return out
    if unavailable:
        names = ", ".join(SOURCE_LABELS[name] for name in unavailable)
        out.append(_sdui.alert(
            f"{names} could not be searched right now, so results may be missing. "
            + ("The sources above were searched." if outcome.hits
               else "Nothing else matched this search."), "warning"))
    if outcome.hits and outcome.partial:
        out.append(_sdui.alert("The search scan limit was reached before every item was "
                               "checked, so results may be incomplete.", "warning"))
    if not outcome.hits and not unavailable:
        out.append(_sdui.alert(f"No matches found for “{outcome.query}”.", "info"))
    for hit in outcome.hits:
        card, reason_alert = _native_hit_card(hit, stale.get(_hit_key(hit)))
        out.append(card)
        if reason_alert is not None:
            out.append(reason_alert)
    if outcome.next_offset is not None:
        following = dict(params, cursor=encode_cursor(outcome.next_offset))
        following.pop("error", None)
        out.append(_sdui.button("More results", "chrome_open",
                                {"surface": SURFACE_KEY, "params": following}))
    return out
