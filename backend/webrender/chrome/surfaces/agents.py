"""Feature 027 — T012: Agents & permissions settings surface.

List view (tabs ``mine`` | ``public`` + a Drafts tab that opens the
``drafts`` surface) and per-agent detail view (per-tool permission matrix,
visibility toggle, credentials, per-user enable/disable). Render + handlers
call the SAME internals the REST routes in ``backend/orchestrator/api.py``
use (``GET /api/agents``, ``GET/PUT /api/agents/{id}/permissions``,
``PUT /api/agents/{id}/visibility``, the credentials routes and
``PUT /api/users/me/agent-enabled``) — never HTTP-to-self.

Tab semantics follow Feature 013 (``agentTabFilters``): *mine* = agents whose
``owner_email`` equals the user's email; *public* = ``is_public`` agents;
owned-and-public agents appear in both tabs.

Every dynamic interpolation goes through ``esc()`` (escape-by-default).
"""
import asyncio
import json
import logging

from webrender.chrome import esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.Agents")

TITLE = "Agents & permissions"

# Permission-kind columns of the matrix — mirrors
# ``orchestrator.tool_permissions.VALID_SCOPES`` (kept literal so the render
# layer does not import orchestrator modules at import time).
PERMISSION_KINDS = ("tools:read", "tools:write", "tools:search", "tools:system", "tools:files")
_KIND_LABELS = {
    "tools:read": "Read",
    "tools:write": "Write",
    "tools:search": "Search",
    "tools:system": "System",
    "tools:files": "Files",
}
_KIND_DESCRIPTIONS = {
    "tools:read": "Read and retrieve data, run analyses, generate visualizations.",
    "tools:write": "Create, modify, or delete data and post to external services.",
    "tools:search": "Query external APIs and databases for information.",
    "tools:system": "Access system resources such as CPU, memory, and disk.",
    "tools:files": "Read uploaded files, documents, and volumes.",
}
# Form-field prefix for a section's master switch (``__scope::<kind>``); the
# ``__`` sentinel can never collide with a tool name field (``<tool>::<kind>``).
SCOPE_FIELD_PREFIX = "__scope::"

_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_GHOST = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 text-astral-text "
    "border border-white/10 hover:bg-white/10"
)
_BTN_DANGER = (
    "px-2.5 py-1 rounded-lg text-xs font-medium bg-red-500/10 text-red-400 "
    "border border-red-500/20 hover:bg-red-500/20"
)
_INPUT_CLS = (
    "w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm "
    "text-astral-text placeholder:text-astral-muted focus:outline-none "
    "focus:border-astral-primary/50"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(data) -> str:
    """JSON-encode an action payload and escape it for an HTML attribute."""
    return esc(json.dumps(data))


def _email_fallback(email, user_id) -> str:
    """Apply the mock-auth fallback: an email-shaped user_id stands in."""
    email = email or ""
    if not email and "@" in str(user_id or ""):
        email = str(user_id)
    return str(email)


def _user_email(orch, user_id) -> str:
    """Resolve the user's email (owner checks mirror the REST JWT email).

    The REST visibility route compares against the JWT ``email`` claim; the
    chrome dispatcher hands us ``user_id``, so we read the profile the
    orchestrator upserts from those same claims (``users`` table). Falls back
    to ``user_id`` itself when it is email-shaped (mock-auth mode).
    """
    email = ""
    try:
        user = orch.history.db.get_user(user_id)
        email = (user or {}).get("email") or ""
    except Exception:  # pragma: no cover — defensive: profile lookup only
        logger.exception("chrome agents: user profile lookup failed for %s", user_id)
    return _email_fallback(email, user_id)


def _disabled_from_preferences(raw) -> set:
    """Disabled agent-id set from a raw ``user_preferences.preferences`` value.

    Mirrors ``Database.get_user_disabled_agents`` (empty set on missing or
    malformed JSON) so the consolidated context query resolves the per-user
    disabled state without a second round trip.
    """
    try:
        prefs = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        prefs = {}
    value = prefs.get("disabled_agents") if isinstance(prefs, dict) else None
    if not isinstance(value, list):
        return set()
    return {str(v) for v in value}


def _snippet(text, limit: int = 110) -> str:
    """Single-line description snippet, ellipsised past ``limit`` chars."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


_USER_CONTEXT_SQL = (
    "SELECT (SELECT email FROM users WHERE id = ?) AS email, "
    "(SELECT preferences FROM user_preferences WHERE user_id = ?) AS preferences"
)


async def _list_context(orch, user_id):
    """``(user_email, ownership_map, disabled_set)`` in ≤2 DB round trips.

    On a real ``Database`` the user email and preferences resolve in one
    consolidated query plus one ownership scan; objects without the async
    facade (test fakes) fall back to the legacy per-method reads off-loop.
    """
    db = orch.history.db
    if hasattr(db, "afetch_one"):
        row = await db.afetch_one(_USER_CONTEXT_SQL, (user_id, user_id)) or {}
        email = _email_fallback(row.get("email"), user_id)
        disabled_set = _disabled_from_preferences(row.get("preferences"))
        ownership_rows = await db.afetch_all(
            "SELECT agent_id, owner_email, is_public FROM agent_ownership")
    else:
        email = await asyncio.to_thread(_user_email, orch, user_id)
        disabled_set = set(await asyncio.to_thread(db.get_user_disabled_agents, user_id))
        ownership_rows = await asyncio.to_thread(db.get_all_agent_ownership)
    ownership_map = {o["agent_id"]: dict(o) for o in ownership_rows}
    return email, ownership_map, disabled_set


def _agent_rows(orch, ownership_map, disabled_set):
    """Agent dicts mirroring ``GET /api/agents`` (api.py ``list_agents``)."""
    rows = []
    for agent_id, card in orch.agent_cards.items():
        if orch._is_draft_agent(agent_id):
            continue  # hide non-live drafts (same rule as list_agents)
        ownership = ownership_map.get(agent_id, {})
        rows.append({
            "id": agent_id,
            "name": card.name,
            "description": card.description,
            "owner_email": ownership.get("owner_email"),
            "is_public": bool(ownership.get("is_public", False)),
            "disabled": agent_id in disabled_set,
        })
    return rows


def _back_button(tab: str) -> str:
    pl = _payload({"surface": "agents", "params": {"tab": tab}})
    return (
        f'<button type="button" class="{_BTN_GHOST}" data-ui-action="chrome_open" '
        f"data-ui-payload='{pl}'>&larr; Back to agents</button>"
    )


def _enable_button(agent_id: str, enabled: bool, extra: dict) -> str:
    """Per-user enable/disable toggle → ``chrome_agent_enabled``."""
    data = {"agent_id": agent_id, "enabled": not enabled}
    data.update(extra)
    label = "Disable" if enabled else "Enable"
    cls = _BTN_GHOST if enabled else _BTN_PRIMARY
    return (
        f'<button type="button" class="astral-agent-enable {cls}" '
        f'data-ui-action="chrome_agent_enabled" '
        f"data-ui-payload='{_payload(data)}'>{esc(label)}</button>"
    )


# ---------------------------------------------------------------------------
# Render — list view
# ---------------------------------------------------------------------------

def _render_tabs(tab: str) -> str:
    parts = ['<div class="flex items-center gap-1.5" role="tablist">']
    for key, label in (("mine", "My agents"), ("public", "Public")):
        active = key == tab
        cls = _BTN_PRIMARY if active else _BTN_GHOST
        pl = _payload({"surface": "agents", "params": {"tab": key}})
        sel = "true" if active else "false"
        parts.append(
            f'<button type="button" role="tab" aria-selected="{sel}" class="{cls}" '
            f"data-ui-action=\"chrome_open\" data-ui-payload='{pl}'>{esc(label)}</button>"
        )
    drafts_pl = _payload({"surface": "drafts", "params": {}})
    parts.append(
        f'<button type="button" role="tab" aria-selected="false" class="{_BTN_GHOST}" '
        f"data-ui-action=\"chrome_open\" data-ui-payload='{drafts_pl}'>Drafts</button>"
    )
    parts.append("</div>")
    return "".join(parts)


def _render_agent_row(agent: dict, tab: str, user_email: str) -> str:
    agent_id = agent["id"]
    badges = []
    if user_email and agent.get("owner_email") == user_email:
        badges.append(
            '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
            'bg-astral-primary/15 text-astral-primary border border-astral-primary/25">Yours</span>'
        )
    if agent.get("is_public"):
        badges.append(
            '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
            'bg-white/5 text-astral-muted border border-white/10">Public</span>'
        )
    if agent.get("disabled"):
        badges.append(
            '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
            'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20">Disabled by you</span>'
        )
    open_pl = _payload({"surface": "agents", "params": {"agent_id": agent_id, "tab": tab}})
    status = (
        '<span class="flex items-center gap-1.5 text-xs text-astral-muted">'
        '<span class="w-1.5 h-1.5 rounded-full bg-green-400 inline-block"></span>'
        "Connected</span>"
    )
    return (
        f'<div class="astral-agent-row bg-white/5 border border-white/10 rounded-lg p-3 '
        f'flex items-center justify-between gap-3" data-agent-id="{esc(agent_id)}">'
        f'<button type="button" class="text-left flex-1 min-w-0 group" '
        f"data-ui-action=\"chrome_open\" data-ui-payload='{open_pl}'>"
        f'<div class="flex items-center gap-2 flex-wrap">'
        f'<span class="text-sm font-medium text-astral-text group-hover:text-astral-primary">'
        f'{esc(agent["name"])}</span>{"".join(badges)}</div>'
        f'<div class="text-xs text-astral-muted mt-0.5">{esc(_snippet(agent.get("description")))}</div>'
        f"</button>"
        f'<div class="flex items-center gap-2 shrink-0">{status}'
        f'{_enable_button(agent_id, not agent.get("disabled", False), {"tab": tab})}'
        f"</div></div>"
    )


async def _render_list(orch, user_id, tab: str) -> str:
    """Render the agent list body for one tab (≤2 DB round trips)."""
    user_email, ownership_map, disabled_set = await _list_context(orch, user_id)
    rows = await asyncio.to_thread(_agent_rows, orch, ownership_map, disabled_set)
    if tab == "public":
        visible = [a for a in rows if a["is_public"]]
        empty_msg = "No public agents are available."
    else:
        tab = "mine"
        visible = [a for a in rows if user_email and a.get("owner_email") == user_email]
        empty_msg = "You don't own any agents yet. Create one from the Drafts tab or from chat."
    if visible:
        body = "".join(_render_agent_row(a, tab, user_email) for a in visible)
    else:
        body = f'<div class="text-sm text-astral-muted py-6 text-center">{esc(empty_msg)}</div>'
    return (
        f'<div class="astral-agents-list space-y-3" data-tab="{esc(tab)}">'
        f"{_render_tabs(tab)}"
        f'<div class="space-y-2">{body}</div></div>'
    )


# ---------------------------------------------------------------------------
# Render — detail view
# ---------------------------------------------------------------------------

def _switch(name: str, checked: bool, extra_cls: str, aria: str, disabled: bool = False) -> str:
    """Toggle-switch checkbox (styled by ``.astral-switch`` in astral.css)."""
    return (
        f'<label class="astral-switch">'
        f'<input type="checkbox" name="{esc(name)}"{" checked" if checked else ""}'
        f'{" disabled" if disabled else ""} class="{extra_cls}" aria-label="{esc(aria)}">'
        f'<span class="astral-switch-track" aria-hidden="true"></span></label>'
    )


def _destructive_badge(classification) -> str:
    """Feature 063 (FR-025): pre-grant destructive marker for one verb.

    The classification rides the card skill metadata straight from the agent's
    ``TOOL_REGISTRY`` ("never" | "always" | "if_exists" | {"by_action": [...]});
    anything other than never/absent renders a marker — unconditional red,
    input-dependent amber. Display only; the confirmation gate enforces.
    """
    if not classification or classification == "never":
        return ""
    if classification == "always":
        label = "Destructive"
        cls = "bg-red-500/10 text-red-400 border-red-500/20"
    else:  # "if_exists" or {"by_action": [...]} — destructive on some inputs
        label = "Sometimes destructive"
        cls = "bg-yellow-500/10 text-yellow-400 border-yellow-500/20"
    return (
        f'<span class="astral-destructive px-1.5 py-0.5 rounded text-[10px] '
        f'font-medium border {cls}">{esc(label)}</span>'
    )


def _render_perm_sections(agent_id: str, tool_scope_map, per_tool, scope_state,
                          tool_descriptions, tab: str = "mine",
                          tool_destructive=None) -> str:
    """Per-kind permission sections: one master switch per permission kind,
    individual tool switches beneath it.

    The master is named ``__scope::<kind>``; tool switches keep the
    ``<tool>::<kind>`` names from the matrix so the save handler validates
    against the same PUT-permissions rules. A section renders only when the
    agent exposes tools of that kind. When the master is off, the tool
    switches render disabled (the client mirrors this on toggle) and the
    save handler forces the whole section off regardless of collected values.
    """
    tool_destructive = tool_destructive or {}
    sections = []
    sectioned = set()
    for kind in PERMISSION_KINDS:
        tools = sorted(t for t, req in tool_scope_map.items() if req == kind)
        if not tools:
            continue
        sectioned.update(tools)
        label = _KIND_LABELS[kind]
        enabled_count = sum(
            1 for t in tools if bool(per_tool.get(t, {}).get(kind, False))
        )
        # Effective per-tool rows win over the scope at resolution time, so a
        # section with any enabled tool must present as "on" even if the
        # agent-wide scope row is stale/false.
        master_on = bool(scope_state.get(kind, False)) or enabled_count > 0
        rows = []
        for tool_name in tools:
            tool_on = bool(per_tool.get(tool_name, {}).get(kind, False))
            desc = tool_descriptions.get(tool_name, "")
            rows.append(
                f'<div class="flex items-center justify-between gap-3 py-2">'
                f'<div class="min-w-0">'
                f'<div class="flex items-center gap-2 flex-wrap">'
                f'<span class="text-sm text-astral-text">{esc(tool_name)}</span>'
                f'{_destructive_badge(tool_destructive.get(tool_name))}</div>'
                f'<div class="text-xs text-astral-muted">{esc(_snippet(desc, 90))}</div></div>'
                f'{_switch(f"{tool_name}::{kind}", tool_on, "astral-perm-tool", f"{tool_name} {label}", disabled=not master_on)}'
                f"</div>"
            )
        count = len(tools)
        plural = "s" if count != 1 else ""
        dim = "" if master_on else " opacity-50"
        sections.append(
            f'<div class="astral-perm-section border border-white/10 rounded-lg overflow-hidden" '
            f'data-perm-section="{esc(kind)}">'
            f'<div class="flex items-center justify-between gap-3 px-3 py-2.5 bg-white/5">'
            f'<div class="min-w-0">'
            f'<div class="text-sm font-semibold text-astral-text">{esc(label)}</div>'
            f'<div class="text-xs text-astral-muted">{esc(_KIND_DESCRIPTIONS[kind])} '
            f"&middot; {count} tool{plural}</div></div>"
            f'{_switch(f"{SCOPE_FIELD_PREFIX}{kind}", master_on, "astral-perm-master", f"Enable all {label} tools")}'
            f"</div>"
            f'<div class="astral-perm-tools divide-y divide-white/5 px-3{dim}">{"".join(rows)}</div>'
            f"</div>"
        )
    # Tools whose required scope is not a known permission kind stay visible
    # (the old matrix listed every tool) but are not user-configurable here.
    others = sorted(set(tool_scope_map) - sectioned)
    if others:
        rows = []
        for tool_name in others:
            desc = tool_descriptions.get(tool_name, "")
            rows.append(
                f'<div class="flex items-center justify-between gap-3 py-2">'
                f'<div class="min-w-0">'
                f'<div class="flex items-center gap-2 flex-wrap">'
                f'<span class="text-sm text-astral-text">{esc(tool_name)}</span>'
                f'{_destructive_badge(tool_destructive.get(tool_name))}</div>'
                f'<div class="text-xs text-astral-muted">{esc(_snippet(desc, 90))}</div></div>'
                f'<span class="text-xs text-astral-muted shrink-0">Not configurable</span>'
                f"</div>"
            )
        count = len(others)
        plural = "s" if count != 1 else ""
        sections.append(
            f'<div class="astral-perm-section border border-white/10 rounded-lg overflow-hidden">'
            f'<div class="px-3 py-2.5 bg-white/5">'
            f'<div class="text-sm font-semibold text-astral-text">Other</div>'
            f'<div class="text-xs text-astral-muted">Tools with a non-standard permission '
            f"type; they can&#x27;t be configured here. &middot; {count} tool{plural}</div></div>"
            f'<div class="divide-y divide-white/5 px-3">{"".join(rows)}</div>'
            f"</div>"
        )
    if sections:
        body = "".join(sections)
        hint = (
            '<p class="text-xs text-astral-muted mb-3">Turn a section on to allow that '
            "permission for all of its tools, then switch off any individual tools you "
            "don&#x27;t want. Turning a section off disables every tool in it.</p>"
        )
    else:
        body = '<div class="py-3 text-sm text-astral-muted">This agent exposes no tools.</div>'
        hint = ""
    save_pl = _payload({"agent_id": agent_id, "tab": tab})
    return (
        f'<div class="astral-perms bg-white/5 border border-white/10 rounded-lg p-4" data-ui-form>'
        f'<h3 class="text-sm font-semibold text-astral-text mb-2">Tool permissions</h3>'
        f'{hint}<div class="space-y-3">{body}</div>'
        f'<div class="mt-3 flex justify-end">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_perms_save" '
        f"data-ui-collect=\"true\" data-ui-payload='{save_pl}'>Save permissions</button>"
        f"</div></div>"
    )


def _render_visibility(agent_id: str, is_public: bool, tab: str = "mine") -> str:
    state = "Public" if is_public else "Private"
    action_label = "Make private" if is_public else "Make public"
    pl = _payload({"agent_id": agent_id, "is_public": not is_public, "tab": tab})
    return (
        f'<div class="astral-visibility bg-white/5 border border-white/10 rounded-lg p-4 '
        f'flex items-center justify-between gap-3">'
        f'<div><h3 class="text-sm font-semibold text-astral-text">Visibility</h3>'
        f'<div class="text-xs text-astral-muted mt-0.5">This agent is currently '
        f"<span class=\"text-astral-text\">{esc(state.lower())}</span>.</div></div>"
        f'<button type="button" class="{_BTN_GHOST}" data-ui-action="chrome_visibility_set" '
        f"data-ui-payload='{pl}'>{esc(action_label)}</button></div>"
    )


def _render_safe(agent_id: str, is_safe: bool, tab: str = "mine") -> str:
    """Feature 040 (US2): owner/admin control to mark an agent 'safe'.

    The handler (``handle_safe_set`` → ``agent_trust.mark_safe``) enforces the
    admin/owner gate server-side; this just renders the toggle + current state.
    """
    state = "owner-approved safe" if is_safe else "not marked safe"
    action_label = "Unmark safe" if is_safe else "Mark safe"
    pl = _payload({"agent_id": agent_id, "is_safe": not is_safe, "tab": tab})
    return (
        f'<div class="astral-safe bg-white/5 border border-white/10 rounded-lg p-4 '
        f'flex items-center justify-between gap-3">'
        f'<div><h3 class="text-sm font-semibold text-astral-text">Trust</h3>'
        f'<div class="text-xs text-astral-muted mt-0.5">This agent is currently '
        f'<span class="text-astral-text">{esc(state)}</span>. Safe agents&#39; tools '
        f"work without per-user enabling; runtime security still applies.</div></div>"
        f'<button type="button" class="{_BTN_GHOST}" data-ui-action="chrome_safe_set" '
        f"data-ui-payload='{pl}'>{esc(action_label)}</button></div>"
    )


def _normalize_credential_entries(raw) -> "tuple[list, dict]":
    """Normalize ``required_credentials`` declarations to (keys, labels, optional).

    Agents declare them either as plain strings or as dicts like
    ``{"key": "MS_GRAPH_CLIENT_ID", "label": ..., "description": ...,
    "required": bool, "type": ...}`` (the generated-agent shape). Anything
    unrecognizable is skipped rather than crashing the surface. ``optional`` is
    the set of keys a declaration explicitly marks ``"required": False`` (e.g.
    web_research's SEARCH_API_* — there is a keyless fallback) so the UI can
    label them Optional instead of Required.
    """
    keys, labels, optional = [], {}, set()
    for entry in raw or []:
        if isinstance(entry, dict):
            key = str(entry.get("key") or entry.get("name") or "").strip()
            if not key:
                continue
            keys.append(key)
            label = entry.get("label") or entry.get("description")
            if label:
                labels[key] = str(label)
            if entry.get("required") is False:
                optional.add(key)
        elif isinstance(entry, str) and entry.strip():
            keys.append(entry.strip())
    return keys, labels, optional


def _render_credentials(keys, agent_id: str, card, tab: str = "mine") -> str:
    """Render the credentials section from already-fetched stored ``keys``."""
    metadata = getattr(card, "metadata", None) or {}
    declared, req_labels, optional_creds = _normalize_credential_entries(
        metadata.get("required_credentials"))
    stored = set(keys)
    all_keys = list(dict.fromkeys(list(declared) + sorted(stored)))
    parts = [
        '<div class="astral-credentials bg-white/5 border border-white/10 rounded-lg p-4 space-y-3">',
        '<h3 class="text-sm font-semibold text-astral-text">Credentials</h3>',
    ]
    if not all_keys:
        parts.append(
            '<div class="text-xs text-astral-muted">This agent declares no credential '
            "requirements and has no stored credentials.</div>"
        )
    else:
        parts.append("<div data-ui-form class=\"space-y-2\">")
        for key in all_keys:
            is_stored = key in stored
            if is_stored:
                badge = (
                    '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
                    'bg-green-500/10 text-green-400 border border-green-500/20">Stored</span>'
                )
                placeholder = "Stored — enter a new value to replace"
            elif key in optional_creds:
                badge = (
                    '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
                    'bg-white/10 text-astral-muted border border-white/10">Optional</span>'
                )
                placeholder = "Optional — leave blank to use the default"
            else:
                badge = (
                    '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium '
                    'bg-yellow-500/10 text-yellow-400 border border-yellow-500/20">Required</span>'
                )
                placeholder = "Enter value"
            delete_btn = ""
            if is_stored:
                del_pl = _payload({"agent_id": agent_id, "key": key, "tab": tab})
                delete_btn = (
                    f'<button type="button" class="{_BTN_DANGER}" '
                    f'data-ui-action="chrome_credential_delete" '
                    f"data-ui-payload='{del_pl}'>Delete</button>"
                )
            label_attr = f' title="{esc(req_labels[key])}"' if key in req_labels else ""
            parts.append(
                f'<div class="flex items-center gap-2">'
                f'<div class="w-44 shrink-0 flex items-center gap-1.5">'
                f'<span class="text-xs text-astral-text font-mono truncate"{label_attr}>{esc(key)}</span>'
                f"{badge}</div>"
                f'<input type="password" name="{esc(key)}" autocomplete="off" '
                f'placeholder="{esc(placeholder)}" class="{_INPUT_CLS}">'
                f"{delete_btn}</div>"
            )
        save_pl = _payload({"agent_id": agent_id, "tab": tab})
        parts.append(
            f'<div class="flex justify-end">'
            f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_credentials_save" '
            f"data-ui-collect=\"true\" data-ui-payload='{save_pl}'>Save credentials</button></div>"
        )
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


_DETAIL_CONTEXT_SQL = (
    "SELECT (SELECT email FROM users WHERE id = ?) AS email, "
    "(SELECT preferences FROM user_preferences WHERE user_id = ?) AS preferences, "
    "(SELECT owner_email FROM agent_ownership WHERE agent_id = ?) AS owner_email, "
    "(SELECT is_public FROM agent_ownership WHERE agent_id = ?) AS is_public, "
    "(SELECT is_safe FROM agent_trust WHERE agent_id = ?) AS is_safe, "
    "(SELECT string_agg(credential_key, chr(10)) FROM user_credentials"
    " WHERE user_id = ? AND agent_id = ?) AS credential_keys, "
    "(SELECT string_agg(scope || '=' || (CASE WHEN enabled THEN '1' ELSE '0' END), ',')"
    " FROM agent_scopes WHERE user_id = ? AND agent_id = ?) AS scope_state"
)


def _parse_scope_state(raw) -> dict:
    """Decode the context query's ``scope=flag`` aggregate into the
    ``get_agent_scopes`` shape (every known kind present, default False)."""
    state = {kind: False for kind in PERMISSION_KINDS}
    for pair in str(raw or "").split(","):
        scope, sep, flag = pair.partition("=")
        if sep and scope in state:
            state[scope] = flag == "1"
    return state


def _detail_context_legacy(orch, user_id, agent_id) -> dict:
    """Detail-view context via the legacy per-method reads (test fakes)."""
    db = orch.history.db
    ownership = db.get_agent_ownership(agent_id) or {}
    try:
        is_safe, safe_known = bool(db.get_agent_is_safe(agent_id)), True
    except Exception:
        logger.debug("safe-marking lookup failed", exc_info=True)
        is_safe, safe_known = False, False
    return {
        "user_email": _user_email(orch, user_id),
        "owner_email": ownership.get("owner_email"),
        "is_public": bool(ownership.get("is_public", False)),
        "is_safe": is_safe,
        "safe_known": safe_known,
        "enabled": not db.is_user_agent_disabled(user_id, agent_id),
        "credential_keys": orch.credential_manager.list_credential_keys(user_id, agent_id),
        "scope_state": dict(orch.tool_permissions.get_agent_scopes(user_id, agent_id)),
    }


async def _detail_context(orch, user_id, agent_id) -> dict:
    """All non-permission detail-view reads in ONE consolidated round trip.

    Covers user email, per-user disabled state, ownership, the feature-040
    safe marker, stored credential keys, and the agent-wide scope state.
    Objects without the async facade fall back to the legacy reads off-loop.
    """
    db = orch.history.db
    if not hasattr(db, "afetch_one"):
        return await asyncio.to_thread(_detail_context_legacy, orch, user_id, agent_id)
    params = (user_id, user_id, agent_id, agent_id, agent_id,
              user_id, agent_id, user_id, agent_id)
    row = await db.afetch_one(_DETAIL_CONTEXT_SQL, params) or {}
    keys_raw = row.get("credential_keys")
    disabled_set = _disabled_from_preferences(row.get("preferences"))
    return {
        "user_email": _email_fallback(row.get("email"), user_id),
        "owner_email": row.get("owner_email"),
        "is_public": bool(row.get("is_public") or False),
        "is_safe": bool(row.get("is_safe") or False),
        "safe_known": True,
        "enabled": agent_id not in disabled_set,
        "credential_keys": str(keys_raw).split("\n") if keys_raw else [],
        "scope_state": _parse_scope_state(row.get("scope_state")),
    }


async def _render_detail(orch, user_id, roles, agent_id: str, tab: str) -> str:
    """Render the per-agent detail body (≤3 DB round trips total)."""
    card = orch.agent_cards.get(agent_id)
    if not card:
        return (
            notice_block("error", f"Agent '{agent_id}' not found.")
            + f'<div class="pt-2">{_back_button(tab)}</div>'
        )
    tp = orch.tool_permissions
    tool_scope_map = tp.get_tool_scope_map(agent_id)
    ctx = await _detail_context(orch, user_id, agent_id)
    scope_state = ctx["scope_state"]
    # Feature 040: a safe + public agent's tools default ALLOW at runtime, so the
    # picker must show them ON rather than contradict the gate. is_safe/is_public
    # come from _detail_context (no extra query); pass the flip in to stay within
    # the detail render's DB round-trip budget.
    from shared.feature_flags import flags
    safe_default = bool(
        flags.is_enabled("safe_agents") and ctx["is_safe"] and ctx["is_public"])
    per_tool = await asyncio.to_thread(
        tp.get_effective_tool_permissions, user_id, agent_id, safe_default)
    tool_descriptions = {s.id: s.description for s in card.skills}
    # Feature 063 (FR-025): destructive classification propagated from the
    # agent's TOOL_REGISTRY via the card skill metadata (base_agent).
    tool_destructive = {
        s.id: (getattr(s, "metadata", None) or {}).get("destructive")
        for s in card.skills
    }

    user_email = ctx["user_email"]
    is_owner = bool(user_email) and ctx["owner_email"] == user_email
    enabled = ctx["enabled"]

    header = (
        f'<div class="flex items-start justify-between gap-3">'
        f'<div class="min-w-0"><h3 class="text-base font-semibold text-astral-text">'
        f"{esc(card.name)}</h3>"
        f'<div class="text-xs text-astral-muted mt-0.5">{esc(card.description)}</div></div>'
        f'<div class="flex items-center gap-2 shrink-0">'
        f'{_enable_button(agent_id, enabled, {"detail": True, "tab": tab})}'
        f"{_back_button(tab)}</div></div>"
    )
    sections = [header, _render_perm_sections(
        agent_id, tool_scope_map, per_tool, scope_state, tool_descriptions, tab,
        tool_destructive)]
    if is_owner:
        sections.append(_render_visibility(agent_id, ctx["is_public"], tab))
    # Feature 040 (US2): owner/admin safe-marking control.
    if is_owner or "admin" in (roles or []):
        if ctx["safe_known"]:
            sections.append(_render_safe(agent_id, ctx["is_safe"], tab))
        else:
            logger.debug("safe-marking control render skipped")
    sections.append(_render_credentials(ctx["credential_keys"], agent_id, card, tab))
    return (
        f'<div class="astral-agent-detail space-y-4" data-agent-id="{esc(agent_id)}">'
        + "".join(sections)
        + "</div>"
    )


async def render(orch, user_id, roles, params) -> str:
    """Render the Agents & permissions surface body (list or detail view).

    Args:
        orch: The orchestrator instance (service internals).
        user_id: The requesting user's id.
        roles: The session roles (unused — surface is available to all users).
        params: ``{tab?: "mine"|"public", agent_id?: str}``.

    Returns:
        Body HTML for the chrome modal (escape-by-default).
    """
    params = params or {}
    tab = str(params.get("tab") or "mine")
    if tab not in ("mine", "public"):
        tab = "mine"
    agent_id = params.get("agent_id")
    if agent_id:
        return await _render_detail(orch, user_id, roles, str(agent_id), tab)
    return await _render_list(orch, user_id, tab)


# ---------------------------------------------------------------------------
# Handlers (explicit save → re-render with notice; FR-016)
# ---------------------------------------------------------------------------

def _detail_params(agent_id: str, payload) -> dict:
    tab = str((payload or {}).get("tab") or "mine")
    if tab not in ("mine", "public"):
        tab = "mine"
    return {"agent_id": agent_id, "tab": tab}


def _list_params(payload) -> dict:
    tab = str((payload or {}).get("tab") or "mine")
    if tab not in ("mine", "public"):
        tab = "mine"
    return {"tab": tab}


def _apply_perm_writes(tp, user_id, agent_id, masters, per_tool_permissions,
                       tool_scope_map) -> None:
    """Persist a validated permission-save payload (runs off the event loop).

    Writes the per-tool rows (an off master forces its whole section off,
    including tools missing from the submitted fields), then mirrors the
    result up to the ``agent_scopes`` layer so the legacy filter path stays
    coherent — masters write straight through, kinds without a master keep
    the any-enabled-tool → scope-true mirror.
    """
    for tool_name, kind_map in per_tool_permissions.items():
        for kind, enabled in kind_map.items():
            if masters.get(kind) is False:
                enabled = False  # section gate wins
            tp.set_tool_permission(user_id, agent_id, tool_name, kind, bool(enabled))
    for kind, master_on in masters.items():
        if master_on:
            continue
        for tool_name, required in tool_scope_map.items():
            if required == kind and kind not in per_tool_permissions.get(tool_name, {}):
                tp.set_tool_permission(user_id, agent_id, tool_name, kind, False)
    scope_state = tp.get_agent_scopes(user_id, agent_id)
    derived = {**scope_state}
    for kind, master_on in masters.items():
        derived[kind] = bool(master_on)
    per_tool = tp.get_effective_tool_permissions(user_id, agent_id)
    for tool_name, kind_map in per_tool.items():
        for kind, enabled in kind_map.items():
            if enabled and kind not in masters:
                derived[kind] = True
    tp.set_agent_scopes(user_id, agent_id, derived)


async def handle_perms_save(orch, websocket, user_id, roles, payload):
    """``chrome_perms_save {agent_id, fields}`` → PUT-permissions internals.

    ``fields`` arrive from the sectioned form as ``{"__scope::<kind>": bool}``
    masters plus ``{"<tool>::<kind>": bool}`` tool switches, translated to the
    ``per_tool_permissions`` shape (``{tool: {kind: bool}}``) the REST route
    builds, with the same validate-whole-payload-then-write semantics (FR-014:
    any mismatch rejects everything so no half-applied state). A master that
    is off forces every tool of that kind off — the section gate wins over the
    individual switches collected under it. Masters write straight through to
    the ``agent_scopes`` layer; kinds without a master keep the legacy mirror
    (scope goes true when any of its tools is enabled).
    """
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    card = orch.agent_cards.get(agent_id)
    if not card:
        return ("agents", _list_params(payload),
                notice_block("error", f"Agent '{agent_id}' not found."))
    params = _detail_params(agent_id, payload)
    fields = payload.get("fields") or {}
    masters = {}
    per_tool_permissions = {}
    for name, value in fields.items():
        name = str(name)
        if name.startswith(SCOPE_FIELD_PREFIX):
            masters[name[len(SCOPE_FIELD_PREFIX):]] = bool(value)
        elif "::" in name:
            tool_name, kind = name.split("::", 1)
            per_tool_permissions.setdefault(tool_name, {})[kind] = bool(value)
    if not per_tool_permissions and not masters:
        return ("agents", params, notice_block("error", "No permission changes submitted."))

    tool_scope_map = orch.tool_permissions.get_tool_scope_map(agent_id)
    # Validate the whole payload before writing anything (api.py rules).
    for kind in masters:
        if kind not in PERMISSION_KINDS:
            return ("agents", params, notice_block(
                "error", f"Unknown permission kind '{kind}'."
            ))
    for tool_name, kind_map in per_tool_permissions.items():
        required = tool_scope_map.get(tool_name)
        if required is None:
            return ("agents", params, notice_block(
                "error", f"Tool '{tool_name}' is not registered for agent '{agent_id}'."
            ))
        for kind in kind_map:
            if kind != required:
                return ("agents", params, notice_block(
                    "error",
                    f"Permission kind '{kind}' does not apply to tool "
                    f"'{tool_name}' (required: '{required}').",
                ))
    await asyncio.to_thread(
        _apply_perm_writes, orch.tool_permissions, user_id, agent_id,
        masters, per_tool_permissions, tool_scope_map,
    )
    logger.info(
        "Agent permissions updated: user=%s agent=%s shape=sections "
        "masters_changed=%d tools_changed=%d",
        user_id, agent_id, len(masters), len(per_tool_permissions),
    )
    return ("agents", params, notice_block("success", "Permissions saved."))


async def handle_visibility_set(orch, websocket, user_id, roles, payload):
    """``chrome_visibility_set {agent_id, is_public}`` — owner-only toggle."""
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    params = _detail_params(agent_id, payload)
    db = orch.history.db
    ownership = await asyncio.to_thread(db.get_agent_ownership, agent_id)
    if not ownership:
        return ("agents", params, notice_block(
            "error", f"No ownership record for agent '{agent_id}'."
        ))
    user_email = await asyncio.to_thread(_user_email, orch, user_id)
    if not user_email or ownership.get("owner_email") != user_email:
        return ("agents", params, notice_block(
            "error", "Only the agent owner can change visibility."
        ))
    is_public = bool(payload.get("is_public"))
    await asyncio.to_thread(db.set_agent_visibility, agent_id, is_public)
    state = "public" if is_public else "private"
    return ("agents", params, notice_block("success", f"Agent is now {state}."))


async def handle_safe_set(orch, websocket, user_id, roles, payload):
    """``chrome_safe_set {agent_id, is_safe}`` — admin/owner-gated safe toggle (feature 040).

    Delegates the gate + audit to ``agent_trust.mark_safe``. The agent's own
    owner may toggle their agent; admins may toggle any. The marker flips the
    permission baseline (deny→allow) for the agent at check time — runtime
    per-call security is unaffected.
    """
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    params = _detail_params(agent_id, payload)
    db = orch.history.db
    from orchestrator import agent_trust
    # Never trust a literal "owner" role from the token — the safe-marking
    # "owner" privilege must derive ONLY from verified ownership of THIS agent
    # (otherwise a Keycloak realm role literally named "owner" would grant
    # blanket safe-marking of any agent).
    eff_roles = [r for r in (roles or []) if r != "owner"]
    ownership = await asyncio.to_thread(db.get_agent_ownership, agent_id) or {}
    user_email = await asyncio.to_thread(_user_email, orch, user_id)
    if user_email and ownership.get("owner_email") == user_email:
        eff_roles.append("owner")
    res = await agent_trust.mark_safe(
        db, agent_id, bool(payload.get("is_safe")), user_id, eff_roles, chat_id=None)
    if not res.get("ok"):
        return ("agents", params, notice_block(
            "error", "Only an admin or the agent owner can change safe status."))
    if res.get("is_safe"):
        msg = ("Agent is now marked safe. Note: this auto-enables ALL of this "
               "agent's tools — including any write/system tools — for every "
               "user who has not set an explicit per-tool preference.")
    else:
        msg = "Agent is now marked not safe."
    return ("agents", params, notice_block("success", msg))


async def handle_credentials_save(orch, websocket, user_id, roles, payload):
    """``chrome_credentials_save {agent_id, fields}`` → credentials internals.

    Blank fields are skipped (passwords render empty; an empty submit must
    not wipe stored values). After saving, runs the same save-time
    ``_credentials_check`` probe as the PUT-credentials route when the agent
    exposes one, and reports the verdict in the notice (FR-008 semantics).
    """
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    card = orch.agent_cards.get(agent_id)
    if not card:
        return ("agents", _list_params(payload),
                notice_block("error", f"Agent '{agent_id}' not found."))
    params = _detail_params(agent_id, payload)
    fields = payload.get("fields") or {}
    credentials = {
        str(k): str(v) for k, v in fields.items()
        if isinstance(v, str) and v.strip()
    }
    if not credentials:
        return ("agents", params, notice_block("error", "No credential values entered."))
    await asyncio.to_thread(
        orch.credential_manager.set_bulk_credentials, user_id, agent_id, credentials)

    # Save-time credential probe (FR-008) — mirrors set_agent_credentials.
    verdict_note = ""
    kind = "success"
    skill_names = {getattr(s, "name", None) for s in getattr(card, "skills", [])}
    if "_credentials_check" in skill_names:
        verdict = "unreachable"
        detail = None
        try:
            creds = await asyncio.to_thread(
                orch.credential_manager.get_agent_credentials_encrypted, user_id, agent_id)
            args = {}
            if creds:
                args["_credentials"] = creds
                args["_credentials_encrypted"] = True
            mcp_resp = await orch._dispatch_tool_call(
                agent_id=agent_id,
                tool_name="_credentials_check",
                args=args,
                timeout=5.0,
                ui_websocket=None,
            )
            if mcp_resp is None:
                verdict, detail = "unreachable", "no response from agent"
            elif mcp_resp.error:
                verdict, detail = "unreachable", mcp_resp.error.get("message")
            elif isinstance(mcp_resp.result, dict):
                verdict = mcp_resp.result.get("credential_test", "unexpected")
                detail = mcp_resp.result.get("detail")
        except Exception as e:
            # A failed probe must not block the credential save.
            verdict, detail = "unreachable", f"Credential probe failed: {e}"
        verdict_note = f" Connection test: {verdict}."
        if detail:
            verdict_note = f" Connection test: {verdict} — {detail}"
        if verdict != "success":
            kind = "info"
    saved = len(credentials)
    plural = "s" if saved != 1 else ""
    return ("agents", params, notice_block(
        kind, f"Saved {saved} credential{plural}.{verdict_note}"
    ))


async def handle_credential_delete(orch, websocket, user_id, roles, payload):
    """``chrome_credential_delete {agent_id, key}`` → delete-credential internals."""
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    card = orch.agent_cards.get(agent_id)
    if not card:
        return ("agents", _list_params(payload),
                notice_block("error", f"Agent '{agent_id}' not found."))
    params = _detail_params(agent_id, payload)
    key = str(payload.get("key") or "")
    if not key:
        return ("agents", params, notice_block("error", "No credential key given."))
    await asyncio.to_thread(orch.credential_manager.delete_credential, user_id, agent_id, key)
    return ("agents", params, notice_block(
        "success", f"Credential '{key}' deleted for agent '{agent_id}'."
    ))


async def handle_agent_enabled(orch, websocket, user_id, roles, payload):
    """``chrome_agent_enabled {agent_id, enabled}`` → per-user enable internals."""
    payload = payload or {}
    agent_id = str(payload.get("agent_id") or "")
    if agent_id not in orch.agent_cards:
        return ("agents", _list_params(payload),
                notice_block("error", f"Agent '{agent_id}' not found."))
    enabled = bool(payload.get("enabled"))
    await asyncio.to_thread(
        orch.history.db.set_user_agent_disabled, user_id, agent_id, not enabled)
    logger.info(
        "Agent enabled state updated: user=%s agent=%s enabled=%s",
        user_id, agent_id, enabled,
    )
    params = _detail_params(agent_id, payload) if payload.get("detail") else _list_params(payload)
    state = "enabled" if enabled else "disabled"
    name = orch.agent_cards[agent_id].name
    return ("agents", params, notice_block("success", f"{name} {state} for your account."))


HANDLERS = {
    "chrome_perms_save": handle_perms_save,
    "chrome_visibility_set": handle_visibility_set,
    "chrome_safe_set": handle_safe_set,
    "chrome_credentials_save": handle_credentials_save,
    "chrome_credential_delete": handle_credential_delete,
    "chrome_agent_enabled": handle_agent_enabled,
}
