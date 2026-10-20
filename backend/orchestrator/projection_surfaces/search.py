"""Composes Search chrome primitives over the shared owner-scoped federated service.
Projection renders the same filters, result warnings, pagination and current source views
for web and native clients; ordinary chrome_open carries every request.
"""

import logging

from audit.hooks import record_generic
from orchestrator.human_request_authority import current_human_caller
from personalization.federated_search import (
    SOURCES, SOURCE_LABELS, SearchQueryInvalid, get_search_service, known_types,
)
from personalization.source_search import error_message, form_params
from webrender import render as render_components
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Search")
TITLE = "Search"
SURFACE_KEY = "search"
_INTRO = "Search your conversations, private notes, durable memory, and Atlas from one place. Results link to their source."
_OPEN_LABELS = {"notes": "Open note", "memory": "Open memory", "conversations": "View conversation result", "atlas": "Open Atlas page"}


def _navigation(label, params):
    return _sdui.button(label, "chrome_open", {"surface": SURFACE_KEY, "params": params})


def _filters(params):
    return _sdui.form([
        _sdui.field("q", "Search", default=params.get("q", ""), help="Search your authorized sources"),
        _sdui.field("source", "Source", "select", default=params.get("source", ""),
                    options=[{"value": "", "label": "All sources"}, *[
                        {"value": key, "label": SOURCE_LABELS[key]} for key in SOURCES]]),
        _sdui.field("type", "Type", "select", default=params.get("type", ""),
                    options=[{"value": "", "label": "All types"}, *sorted(known_types())]),
        _sdui.field("project_id", "Project", default=params.get("project_id", ""),
                    help="Leave blank for all projects; use - for global memory and Atlas."),
    ], submit_action="chrome_open", submit_label="Search",
        submit_payload={"surface": SURFACE_KEY, "params": {}}, title="Search")


def _honest_notices(outcome):
    unavailable = [SOURCE_LABELS[report.source] for report in outcome.reports if not report.available]
    incomplete = [SOURCE_LABELS[report.source] for report in outcome.reports if report.available and not report.complete]
    notices = []
    if not outcome.any_available:
        notices.append(_sdui.alert("Search is unavailable right now. Retry in a moment; results may be missing.", "error"))
    elif unavailable:
        notices.append(_sdui.alert(", ".join(unavailable) + " could not be searched right now, so results may be missing.", "warning"))
    if incomplete:
        notices.append(_sdui.alert(", ".join(incomplete) + " could not be checked completely or a scan limit was reached, so results may be incomplete. Narrow your search.", "warning"))
    if any(report.stale for report in outcome.reports):
        notices.append(_sdui.alert("Some results changed or are no longer available. Search again for current sources.", "warning"))
    if not outcome.hits and outcome.any_available and not outcome.partial:
        notices.append(_sdui.alert(f"No matches found for “{outcome.query}”. Try different words or clear the filters.", "info"))
    return notices


async def _caller(orch, user_id):
    caller = current_human_caller(expected_orchestrator=orch)
    if caller is None or caller.owner_id != user_id:
        return None
    await caller.verify_delivery()
    return caller


async def _audit(caller, outcome):
    await record_generic(
        claims=caller.claims, event_class="personalization", action_type="personalization.search",
        description="Searched notes, memory, conversations, and Atlas",
        inputs_meta={"sources": list(outcome.sources), "types": list(outcome.types),
                     "project_filter": bool(outcome.project_id), "query_chars": len(outcome.query),
                     "has_cursor": outcome.next_cursor is not None},
        outputs_meta={"count": len(outcome.hits), "partial": outcome.partial})


def _source_components(view, params):
    back = {key: value for key, value in params.items() if key not in {"open", "error"}}
    output = [_navigation("Back to search", back), _sdui.badge(SOURCE_LABELS[view.source]),
              _sdui.card(view.title, [_sdui.text(view.text)])]
    if not view.complete:
        output.append(_sdui.alert("This source preview was shortened to the display limit.", "warning"))
    if view.source == "conversations":
        output.append(_sdui.button("Open conversation", "load_chat",
                                  {"chat_id": view.ref["conversation_id"]}))
    return output


async def components(orch, user_id, roles, params):
    params = form_params(params, None)
    output = [_filters(params)]
    if params.get("error"):
        return [*output, _sdui.alert(params["error"], "error")]
    if not params.get("q") and "open" not in params:
        return [*output, _sdui.alert(_INTRO)]
    caller = await _caller(orch, user_id)
    if caller is None:
        return [*output, _sdui.alert("Search requires a current authenticated session.", "error")]
    try:
        service = get_search_service(orch)
        if "open" in params:
            view = await service.open_source(owner_id=user_id, ref=params["open"])
            await caller.verify_delivery()
            if view is None:
                return [*output, _sdui.alert("That source changed or is no longer available.", "warning")]
            await record_generic(claims=caller.claims, event_class="personalization",
                action_type="personalization.search_open", description="Opened a search source",
                inputs_meta={"source": view.source})
            return [*output, *_source_components(view, params)]
        outcome = await service.search(owner_id=user_id, query=params["q"], sources=params.get("source"),
            types=params.get("type"), project_id=params.get("project_id"), cursor=params.get("cursor"))
        await caller.verify_delivery()
        await _audit(caller, outcome)
    except SearchQueryInvalid as error:
        return [*output, _sdui.alert(error_message(error), "error")]
    except Exception:
        logger.warning("search surface source unavailable", exc_info=True)
        return [*output, _sdui.alert("Search is unavailable right now. Retry in a moment.", "error")]
    output.extend(_honest_notices(outcome))
    cards = []
    for hit in outcome.hits:
        card = _sdui.card(hit.title, [_sdui.badge(SOURCE_LABELS[hit.source]), _sdui.text(hit.snippet),
            _navigation(_OPEN_LABELS[hit.source], dict(params, open=dict(hit.ref)))])
        card["role"] = "listitem"
        cards.append(card)
    if cards:
        results = _sdui.card("", cards)
        results.update({"role": "list", "aria-label": "Search results"})
        output.append(results)
    if outcome.next_cursor:
        output.append(_navigation("More results", dict(params, cursor=outcome.next_cursor)))
    return output


async def render(orch, user_id, roles, params):
    return render_components(await components(orch, user_id, roles, params))
