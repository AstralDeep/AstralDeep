"""Renders the attachment-library surface for browsing and deleting previously uploaded
files; attaching an existing one to the next message is handled client-side. Delete
routes through orchestrator/attachments/repository.py's soft-delete.
"""

import asyncio
import json
import logging

from webrender import esc
from webrender.chrome import notice_block
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome")

TITLE = "Attachments"

_CATEGORY_LABEL = {
    "document": "document", "spreadsheet": "spreadsheet", "presentation": "presentation",
    "text": "text/code", "image": "image", "medical": "medical",
    "data": "data", "archive": "archive",
}


def _human_size(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _repo(orch):
    from orchestrator.attachments.repository import AttachmentRepository
    from orchestrator.plane_repository_context import plane_source_from_orchestrator

    injected = getattr(orch, "attachment_repository", None)
    if injected is not None:
        return injected
    return AttachmentRepository.from_plane_source(plane_source_from_orchestrator(orch))


def _list_items(orch, user_id):
    items, _ = _repo(orch).list_for_user(user_id, limit=100)
    return items


async def _schedule_delete(orch, user_id, attachment_id):
    from astralplane.errors import PlaneError
    from orchestrator.attachments.purge import purge_coordinator_from_orchestrator

    try:
        return await purge_coordinator_from_orchestrator(orch).aschedule_attachment(
            owner_id=user_id,
            attachment_id=attachment_id,
        )
    except PlaneError as exc:
        if exc.code == "purge_object_not_found":
            return None
        raise


def _row_html(att) -> str:
    cat = _CATEGORY_LABEL.get(att.category, att.category)
    data = (
        f'data-attachment-id="{esc(att.attachment_id)}" '
        f'data-filename="{esc(att.filename)}" '
        f'data-category="{esc(att.category)}"'
    )
    del_payload = esc(json.dumps({"attachment_id": att.attachment_id}))
    return (
        f'<div class="flex items-center gap-2 bg-white/5 border border-white/10 rounded-lg p-3">'
        f'<div class="min-w-0 flex-1">'
        f'<div class="text-sm text-astral-text truncate">{esc(att.filename)}</div>'
        f'<div class="text-xs text-astral-muted">{esc(cat)} · {esc(_human_size(att.size_bytes))}</div>'
        f'</div>'
        f'<button type="button" class="astral-attach-existing px-3 py-1.5 rounded-lg text-xs '
        f'font-medium bg-astral-primary text-white" {data}>Attach</button>'
        f'<button type="button" class="px-3 py-1.5 rounded-lg text-xs text-red-400 '
        f'hover:bg-red-500/10" data-ui-action="chrome_attachment_delete" '
        f"data-ui-payload='{del_payload}'>Delete</button>"
        f'</div>'
    )


async def render(orch, user_id, roles, params) -> str:
    try:
        items = await asyncio.to_thread(_list_items, orch, user_id)
    except Exception:
        logger.exception("attachments surface: list failed")
        return notice_block("error", "Could not load your attachments.")
    if not items:
        return (
            '<div class="text-sm text-astral-muted italic">No uploads yet. Use the '
            "paperclip in the chat box to attach a file — it will appear here so you can "
            "reuse it in other chats.</div>"
        )
    rows = "".join(_row_html(a) for a in items)
    return (
        '<div class="text-xs text-astral-muted mb-2">Attach a previously uploaded file to '
        "your next message (no re-upload), or delete files you no longer need.</div>"
        f'<div class="space-y-2">{rows}</div>'
    )


async def components(orch, user_id, roles, params):
    try:
        items = await asyncio.to_thread(_list_items, orch, user_id)
    except Exception:
        logger.exception("attachments surface: list failed (native)")
        return [_sdui.alert("Could not load your attachments.", "error")]
    if not items:
        return [_sdui.alert("No uploads yet. Use the paperclip in the chat box to attach "
                            "a file — it will appear here so you can reuse it in other "
                            "chats.", "info")]
    out = [_sdui.text("Attach a previously uploaded file to your next message (no "
                      "re-upload), or delete files you no longer need.", "caption")]
    for att in items:
        cat = _CATEGORY_LABEL.get(att.category, att.category)
        out.append(_sdui.card(
            att.filename,
            [_sdui.text(f"{cat} · {_human_size(att.size_bytes)}", "caption"),
             _sdui.container([
                 _sdui.button("Attach", "attach_existing",
                              {"attachment_id": att.attachment_id, "filename": att.filename,
                               "category": att.category}, variant="primary"),
                 _sdui.button("Delete", "chrome_attachment_delete",
                              {"attachment_id": att.attachment_id}, variant="danger"),
             ], direction="row")],
        ))
    return out


async def _h_attachment_delete(orch, websocket, user_id, roles, payload):
    attachment_id = str((payload or {}).get("attachment_id") or "")
    if not attachment_id:
        return ("attachments", {}, notice_block("error", "No attachment specified."))
    try:
        acceptance = await _schedule_delete(orch, user_id, attachment_id)
    except Exception:
        logger.exception("attachments surface: delete failed")
        return ("attachments", {}, notice_block("error", "Delete failed — please try again."))
    if acceptance is None:
        return ("attachments", {}, notice_block("error", "Attachment not found."))
    return (
        "attachments",
        {},
        notice_block("success", "Attachment hidden; secure cleanup is pending."),
    )


HANDLERS = {
    "chrome_attachment_delete": _h_attachment_delete,
}
