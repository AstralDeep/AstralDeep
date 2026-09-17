"""Deep-owned host adapter for the owner's saved-results Projection surface.

Feature 088 T044 (FR-023). Lists the owner's committed ``result_publication``
receipts -- feature 088 T043's exact Work "Save result" approvals
(``orchestrator.work_publication.WorkPublicationService``) -- using Plane's
own conservative decoder (``known_publication_action``) over the same
``persistent_assignment_action`` rows ``AssignmentRepository.list_actions``
already reads, owner-scoped instead of one operation at a time (Plane has no
owner-wide publication accessor yet, so this adapter reuses the exact table
and row shape Plane's own repository queries, never guessing at or widening
a receipt). Viewing a saved result never mutates, proposes or re-derives
anything; every value comes straight from the row Plane itself committed at
Save time, or from the operation's own already-proven public result.
"""
from __future__ import annotations

import logging

TITLE = "Saved results"
ADMIN_ONLY = False
HANDLERS: dict = {}
logger = logging.getLogger("Orchestrator.Chrome.SavedResults")

# How many of the owner's most recent one-shot operations are scanned for a
# committed publication. Plane has no owner-wide receipt index yet; bounding
# the scan keeps one owner's long history from making this read unbounded.
_OPERATION_SCAN_LIMIT = 100
_PAGE_SIZE = 50


def _enabled(orch) -> bool:
    try:
        from shared.feature_flags import flags

        from persistent_agents.service import AssignmentService
        return (flags.is_enabled("persistent_agents")
                and type(getattr(orch, "persistent_assignments", None)) is AssignmentService)
    except Exception:
        return False


def _decoded_receipts(tx, repository, owner_id, *, limit):
    """One transaction: scan the owner's own operations for committed Saves.

    Newest-committed-first. Only a structurally exact ``succeeded``
    ``result_publication`` action survives Plane's own decoder
    (``known_publication_action``); anything else -- an unrelated action kind,
    a still-open proposal, a half-written or foreign-shaped row -- is skipped,
    never guessed at or partially rendered.
    """
    from astralplane.repositories.assignments import plain
    from astralplane.repositories.result_publications import known_publication_action

    operations = repository.list_operations(tx, owner_id=owner_id, limit=_OPERATION_SCAN_LIMIT)
    titles = {op.assignment.assignment_id: op.assignment.definition.name for op in operations}
    if not titles:
        return []
    rows = tx.fetch_all(
        "SELECT assignment_id, data FROM persistent_assignment_action "
        "WHERE owner_user_id=%s AND state='succeeded' AND assignment_id = ANY(%s::uuid[]) "
        "ORDER BY id DESC LIMIT %s",
        (owner_id, list(titles), max(1, limit) * 4),
    )
    decoded = []
    for row in rows:
        try:
            action = plain(row["data"])
        except Exception:
            continue
        request = ((action.get("intent") or {}).get("request")) or {}
        if request.get("kind") != "result_publication":
            continue
        if known_publication_action(action) is None:
            continue
        receipt = action.get("publication_receipt") or {}
        if receipt.get("owner_id") != owner_id or receipt.get("assignment_id") != row["assignment_id"]:
            continue
        decoded.append({
            "operation_id": row["assignment_id"],
            "operation_title": titles.get(row["assignment_id"], ""),
            "action_id": action.get("action_id"),
            "publication_id": receipt.get("publication_id"),
            "conversation_id": receipt.get("conversation_id"),
            "component_id": receipt.get("component_id"),
            "committed_render_revision": receipt.get("committed_render_revision"),
            "committed_at": receipt.get("committed_at"),
            "result_digest": receipt.get("result_digest"),
            "content_digest": receipt.get("content_digest"),
            "stage_digest": receipt.get("stage_digest"),
        })
        if len(decoded) >= limit:
            break
    return decoded


def _conversation_title(tx, orch, owner_id, conversation_id):
    if not conversation_id:
        return None
    try:
        conversations = orch.persistent_assignments.store.plane_runtime.repositories.history.conversations
        chat = conversations.get(tx, owner_id=owner_id, conversation_id=conversation_id)
    except Exception:
        return None
    return getattr(chat, "title", None) if chat is not None else None


def _export_link(orch, conversation_id, render_revision):
    """The result's own existing authenticated canvas download, unchanged.

    Reuses ``GET /api/export/canvas/{chat_id}.html`` exactly as the workspace
    timeline and any other canvas already do (feature 028); no new route.
    """
    try:
        from shared.feature_flags import flags
        if not flags.is_enabled("artifact_export"):
            return None
    except Exception:
        return None
    if not conversation_id or not isinstance(render_revision, int):
        return None
    return {"url": f"/api/export/canvas/{conversation_id}.html?render_revision={render_revision}"}


def _list_state(tx, repository, orch, owner_id):
    receipts = _decoded_receipts(tx, repository, owner_id, limit=_PAGE_SIZE)
    for entry in receipts:
        entry["conversation_title"] = _conversation_title(
            tx, orch, owner_id, entry.get("conversation_id"))
    return {"mode": "list", "receipts": receipts}


def _detail_state(tx, repository, orch, owner_id, publication_id):
    from orchestrator.work_result import project_research_result

    receipts = _decoded_receipts(tx, repository, owner_id, limit=_OPERATION_SCAN_LIMIT)
    match = next((entry for entry in receipts if entry.get("publication_id") == publication_id), None)
    if match is None:
        return {"mode": "detail", "receipt": {}}
    match["conversation_title"] = _conversation_title(
        tx, orch, owner_id, match.get("conversation_id"))
    provenance = {
        "source_title": None, "requested_url": None, "final_url": None, "retrieved_at": None,
        "result_digest": match.get("result_digest"), "content_digest": match.get("content_digest"),
        "stage_digest": match.get("stage_digest"),
    }
    try:
        read = repository.get_operation(tx, owner_id=owner_id, assignment_id=match["operation_id"])
        if read is not None:
            result = project_research_result(tx, repository, owner_id=owner_id, read=read)
            if result.get("available") is True:
                source = result["content"]["source"]
                provenance.update(
                    source_title=source.get("title"), requested_url=source.get("requested_url"),
                    final_url=source.get("final_url"), retrieved_at=source.get("retrieved_at"))
    except Exception:
        logger.debug("saved_results: provenance re-derivation failed", exc_info=True)
    match["provenance"] = provenance
    match["export"] = _export_link(orch, match.get("conversation_id"), match.get("committed_render_revision"))
    return {"mode": "detail", "receipt": match}


async def _state(orch, user_id, params):
    params = params if isinstance(params, dict) else {}
    mode = "detail" if params.get("mode") == "detail" else "list"
    store = orch.persistent_assignments.store

    def read(tx, repository):
        if mode == "detail":
            publication_id = str(params.get("publication_id") or "")
            return _detail_state(tx, repository, orch, user_id, publication_id)
        return _list_state(tx, repository, orch, user_id)

    return await store.transaction(read)


async def render(orch, user_id, roles, params) -> str:
    from astralprojection.chrome import render_html
    from astralprojection.chrome.workspace import build_saved_results_view

    if not _enabled(orch):
        view = build_saved_results_view({}, denied=True)
        return render_html(view)
    state = await _state(orch, user_id, params)
    return render_html(build_saved_results_view(state))


async def components(orch, user_id, roles, params):
    from astralprojection.chrome.workspace import build_saved_results_view

    if not _enabled(orch):
        view = build_saved_results_view({}, denied=True)
        return [component.to_dict() for component in view.components]
    state = await _state(orch, user_id, params)
    view = build_saved_results_view(state)
    return [component.to_dict() for component in view.components]
