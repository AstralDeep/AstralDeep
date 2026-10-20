"""Normalizes Search surface form input against the shared federated-search contract.
All scanning, ranking, source reads, and cursor authority remain in federated_search.py.
"""

from personalization.federated_search import (
    MAX_CURSOR_BYTES, SearchQueryInvalid, normalize_project_filter, normalize_query,
    normalize_sources, normalize_types,
)

ERRORS = {
    "search_query_required": "Enter a search term.",
    "search_query_too_long": "Search is limited to 200 characters; use a shorter term.",
    "search_query_control_characters": "Search terms cannot contain control characters.",
    "search_source_invalid": "Choose a valid source filter.",
    "search_type_invalid": "Choose a valid type filter.",
    "search_project_invalid": "Choose a valid project filter.",
    "search_cursor_invalid": "That result page is no longer valid; start a new search.",
    "search_cursor_mismatch": "That result page belongs to a different search; start a new search.",
    "search_source_reference_invalid": "That source reference is no longer valid.",
}


def error_message(error):
    return ERRORS.get(error.code, "The search request is invalid; start a new search.")


def form_params(params, fields):
    params = params if isinstance(params, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    merged = {key: params[key] for key in ("q", "source", "type", "project_id", "cursor", "open") if key in params}
    merged.update({key: fields[key] for key in ("q", "source", "type", "project_id") if key in fields})
    if fields:
        merged.pop("cursor", None)
        merged.pop("open", None)
    try:
        raw = merged.get("q", "")
        if "q" in fields or raw:
            merged["q"] = normalize_query(raw)
        sources = normalize_sources(merged.get("source"))
        if merged.get("source"):
            merged["source"] = ",".join(sources)
        else:
            merged.pop("source", None)
        types = normalize_types(merged.get("type"))
        if types:
            merged["type"] = ",".join(types)
        else:
            merged.pop("type", None)
        if merged.get("project_id") == "":
            merged.pop("project_id")
        if "project_id" in merged:
            merged["project_id"] = normalize_project_filter(merged["project_id"])
        cursor = merged.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > MAX_CURSOR_BYTES):
            raise SearchQueryInvalid("search_cursor_invalid")
    except SearchQueryInvalid as error:
        merged.pop("cursor", None)
        merged["error"] = error_message(error)
    if not isinstance(merged.get("q", ""), str):
        merged["q"] = ""
    return merged
