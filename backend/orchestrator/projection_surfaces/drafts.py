"""Deep-owned host adapter for the draft-agents Projection surface.

One lifecycle, one list (SC-007): drafts created from chat (origin
``auto_chat``), manually from this surface (``manual``), and staged live-agent
revisions (``revision``) all appear here and share the same decision actions
(``draft_approve`` / ``draft_refine`` / ``draft_discard`` / ``revision_*``,
registered by ``orchestrator.agentic_creation``). Manual creation runs the
SAME auto-create + self-test pipeline as chat (``create_capability``).

Renders for the web target only (chrome layer).
"""
import asyncio
import json
import logging

from webrender import esc
from webrender.chrome import notice_block

logger = logging.getLogger("Orchestrator.Chrome")

TITLE = "Drafts & creation"

_ORIGIN_BADGES = {
    "auto_chat": ("from chat", "bg-astral-primary/40 text-white border border-astral-primary/50"),
    "revision": ("revision", "bg-astral-secondary/40 text-white border border-astral-secondary/50"),
    "manual": ("manual", "bg-white/10 text-astral-text border border-white/20"),
}

_TERMINAL_NOTE = {
    "rejected": "rejected — editable (Refine) or discardable",
    "error": "errored — refine with guidance or discard",
    "pending_review": "awaiting admin review",
}


def _user_drafts(orch, user_id):
    """All non-live drafts for the user — including rejected (012 FR-010a)."""
    lifecycle = getattr(orch, "lifecycle_manager", None)
    store = vars(lifecycle).get("draft_store") if hasattr(lifecycle, "__dict__") else None
    if store is None:
        raise RuntimeError("Plane draft persistence is unavailable")
    return store.get_decidable_drafts(user_id)


def _badge(origin):
    label, cls = _ORIGIN_BADGES.get(origin or "manual", _ORIGIN_BADGES["manual"])
    return f'<span class="text-[10px] px-1.5 py-0.5 rounded {cls}">{esc(label)}</span>'


def _self_test_line(draft):
    try:
        st = json.loads(draft.get("self_test") or "{}")
    except (TypeError, json.JSONDecodeError):
        st = {}
    if not st:
        return '<span class="text-xs text-astral-muted italic">not self-tested yet</span>'
    icon = "✓" if st.get("status") == "passed" else "✗"
    cls = "text-green-400" if st.get("status") == "passed" else "text-red-400"
    return (f'<span class="text-xs {cls}">{icon} self-test {esc(st.get("status", ""))}'
            f' — {esc(st.get("summary", ""))}</span>')


def _decision_row(draft):
    is_rev = bool(draft.get("revises_agent_id"))
    approve = ("revision_apply", "Apply to live agent") if is_rev else ("draft_approve", "Approve")
    discard = "revision_discard" if is_rev else "draft_discard"
    pid = json.dumps({"draft_id": draft["id"]})
    return (
        f'<div class="flex gap-2 mt-2">'
        f'<button type="button" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary '
        f'text-white" data-ui-action="{approve[0]}" data-ui-payload=\'{esc(pid)}\'>{esc(approve[1])}</button>'
        f'<button type="button" class="px-3 py-1.5 rounded-lg text-xs bg-white/5 border border-white/10 '
        f'text-astral-text" data-ui-action="chrome_open" '
        f'data-ui-payload=\'{esc(json.dumps({"surface": "drafts", "params": {"draft_id": draft["id"], "refine": True}}))}\'>Refine…</button>'
        f'<button type="button" class="px-3 py-1.5 rounded-lg text-xs text-red-400 hover:bg-red-500/10 '
        f'rounded-lg" data-ui-action="{discard}" data-ui-payload=\'{esc(pid)}\'>Discard</button>'
        f'</div>'
    )


def _detail(orch, user_id, draft, show_refine=False):
    status = draft.get("status", "?")
    note = _TERMINAL_NOTE.get(status, "")
    parts = [
        f'<button type="button" class="text-xs text-astral-muted hover:text-astral-text" '
        f'data-ui-action="chrome_open" data-ui-payload=\'{esc(json.dumps({"surface": "drafts"}))}\'>'
        f"← All drafts</button>",
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4">'
        f'<div class="flex items-center gap-2">'
        f'<h3 class="text-sm font-semibold text-astral-text">{esc(draft.get("agent_name", ""))}</h3>'
        f"{_badge(draft.get('origin'))}"
        f'<span class="text-xs text-astral-muted">status: {esc(status)}</span></div>'
        f'<p class="text-sm text-astral-text/80 mt-1">{esc(draft.get("description", ""))}</p>'
        f'<div class="mt-2">{_self_test_line(draft)}</div>'
        + (f'<div class="text-xs text-yellow-400 mt-1">{esc(note)}</div>' if note else "")
        + (f'<div class="text-xs text-red-400 mt-1">{esc(draft.get("error_message") or "")}</div>'
           if draft.get("error_message") else "")
        + (f'<div class="text-xs text-astral-muted mt-1">revises: {esc(draft.get("revises_agent_id") or "")}</div>'
           if draft.get("revises_agent_id") else "")
        # Live drafts are done — no approve/refine/discard (discarding a live
        # row would orphan the running agent; 027 click-through finding).
        + (_decision_row(draft) if status != "live"
           else '<div class="text-xs text-green-400 mt-2">This draft was approved and is '
                "live — manage it under Agents &amp; permissions.</div>")
        + "</div>",
    ]
    if show_refine:
        pid = esc(json.dumps({"draft_id": draft["id"]}))
        parts.append(
            f'<div class="bg-white/5 border border-white/10 rounded-lg p-4" data-ui-form>'
            f'<div class="text-sm font-medium text-astral-text mb-2">Refine this draft</div>'
            f'<textarea name="message" rows="3" placeholder="Describe what to change or fix…" '
            f'class="w-full rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm '
            f'text-astral-text"></textarea>'
            f'<button type="button" class="mt-2 px-3 py-1.5 rounded-lg text-xs font-medium '
            f'bg-astral-primary text-white" data-ui-action="draft_refine" '
            f"data-ui-payload='{pid}' data-ui-collect=\"true\">Refine</button></div>"
        )
    parts.append(
        '<div class="text-xs text-astral-muted">Test the draft from chat — its tools are '
        "available there while it runs. Approving runs the security gate; revisions re-pass "
        "the gate before the live agent changes.</div>"
    )
    return "".join(parts)


def _create_form():
    return (
        '<div class="bg-white/5 border border-white/10 rounded-lg p-4" data-ui-form>'
        '<div class="text-sm font-medium text-astral-text mb-2">Create a new agent</div>'
        '<input name="agent_name" type="text" placeholder="Agent name" '
        'class="w-full rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm text-astral-text mb-2">'
        '<textarea name="description" rows="2" placeholder="What should it do? (≥10 characters)" '
        'class="w-full rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm text-astral-text mb-2"></textarea>'
        '<textarea name="tools" rows="2" placeholder="Tools, one per line: name: what it does (optional)" '
        'class="w-full rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm text-astral-text"></textarea>'
        '<button type="button" class="mt-2 px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary '
        'text-white" data-ui-action="chrome_draft_create" data-ui-collect="true">'
        "Generate &amp; self-test</button>"
        '<div class="text-xs text-astral-muted mt-1">Generation + self-test usually takes a couple '
        "of minutes; the draft appears below when staged.</div></div>"
    )


async def render(orch, user_id, roles, params) -> str:
    """Drafts list / detail / create — unified across entry points (SC-007)."""
    draft_id = (params or {}).get("draft_id")
    if draft_id:
        lifecycle = getattr(orch, "lifecycle_manager", None)
        store = (
            vars(lifecycle).get("draft_store")
            if hasattr(lifecycle, "__dict__")
            else None
        )
        if store is None:
            raise RuntimeError("Plane draft persistence is unavailable")
        draft = await asyncio.to_thread(
            store.get_owned_draft_agent,
            user_id,
            str(draft_id),
        )
        if draft:
            return _detail(orch, user_id, draft, show_refine=bool((params or {}).get("refine")))
        return notice_block("error", "Draft not found (it may have been discarded).")

    drafts = await asyncio.to_thread(_user_drafts, orch, user_id)
    rows = []
    for d in drafts:
        open_payload = esc(json.dumps({"surface": "drafts", "params": {"draft_id": d["id"]}}))
        rows.append(
            f'<button type="button" class="w-full text-left bg-white/5 hover:bg-white/10 border '
            f'border-white/10 rounded-lg p-3" data-ui-action="chrome_open" '
            f"data-ui-payload='{open_payload}'>"
            f'<div class="flex items-center gap-2">'
            f'<span class="text-sm font-medium text-astral-text">{esc(d.get("agent_name", ""))}</span>'
            f"{_badge(d.get('origin'))}"
            f'<span class="text-xs text-astral-muted ml-auto">{esc(d.get("status", ""))}</span></div>'
            f'<div class="mt-1">{_self_test_line(d)}</div></button>'
        )
    listing = ("".join(rows) if rows else
               '<div class="text-sm text-astral-muted italic">No drafts yet — create one below or '
               "ask for a missing capability in chat.</div>")
    return (
        f'<div class="space-y-2">{listing}</div>'
        f"{_create_form()}"
    )


async def _h_draft_create(orch, websocket, user_id, roles, payload):
    """Manual creation — the same pipeline as chat (US3 scenario 1)."""
    from orchestrator import agentic_creation

    fields = payload.get("fields") or {}
    agent_name = str(fields.get("agent_name") or "").strip()
    description = str(fields.get("description") or "").strip()
    tools_lines = [ln.strip() for ln in str(fields.get("tools") or "").splitlines() if ln.strip()]
    tools_spec = []
    for line in tools_lines[:4]:
        name, _, desc = line.partition(":")
        tools_spec.append({"name": name.strip() or "tool", "description": desc.strip() or name.strip()})
    if not tools_spec:
        tools_spec = [{"name": "main_tool", "description": description[:200] or agent_name}]
    if not agent_name or len(description) < 10:
        return ("drafts", {}, notice_block(
            "error", "An agent name and a description of at least 10 characters are required."))

    res = await agentic_creation.handle_meta_tool(
        orch, "create_capability",
        {"agent_name": agent_name, "description": description,
         "tools_spec": tools_spec, "user_request": description},
        user_id=user_id, chat_id="", websocket=websocket)
    if res.error:
        return ("drafts", {}, notice_block("error", res.error.get("message", "creation failed")))
    status = (res.result or {}).get("status")
    draft_id = (res.result or {}).get("draft_id")
    if status in ("created", "duplicate") and draft_id:
        kind = "Staged and self-tested" if status == "created" else "Already staged"
        return ("drafts", {"draft_id": draft_id}, notice_block("success", f"{kind} — decide below."))
    return ("drafts", {}, notice_block("error", f"Creation did not complete ({status})."))


HANDLERS = {
    "chrome_draft_create": _h_draft_create,
}
