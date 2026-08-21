"""Deep-owned host adapter for the BYO agent Projection surface (key
``agent_authoring``, T018/T019 + the UI half of T024/T026/T028/T030).

One server-driven surface, two views:

* **My agents** — the owner's user agents with their DERIVED running/offline
  status (liveness is socket presence, never a stored column), their in-progress
  authoring sessions, and a "new agent" form. Revise + Delete live here. There is
  **no share/publish/transfer control anywhere on this surface** — user agents are
  private by construction (FR-020, Constitution K), so the affordance simply does
  not exist (a test asserts it).
* **The guided flow** — Specify → Clarify → Plan → Tasks → Analyze → Generate.
  Each phase shows an ASSISTANT-DRAFTED, fully EDITABLE artifact; advancing is
  always an explicit act. Clarify and Analyze are HARD GATES: they decline to
  advance with a plain-language notice, and Generate is only ever reachable from
  a passed Analyze — enforced in :mod:`orchestrator.agent_authoring`, on the
  server, not by hiding a button.

Renders BOTH ``render()`` (web HTML) and ``components()`` (native SDUI) from day
one, so web/Windows/Android/Apple all author + manage with no client work
(contracts/authoring-surface.md; the watch is excluded by ``chrome_events``'s
device list, FR-023).

**Flag**: every entry point here re-checks ``FF_BYO_AGENTS`` and fails closed.
The delivery/tunnel/lifecycle seams underneath are not individually flagged —
they are reachable ONLY from here, so this is the gate that keeps the whole
feature inert when the flag is off (FR-009).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Tuple
import uuid

from orchestrator import agent_authoring as aa
from webrender.chrome import esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.Authoring")

TITLE = "My agents"

SURFACE_KEY = "agent_authoring"

#: FR-024 — the honest, always-shown truth about where these agents run.
HOST_NOTE = ("Your agents run on your desktop host, not on the server. They are offline "
             "while none of your desktop hosts is online.")

_DISABLED = "Personal agents are not enabled on this deployment."

_INPUT_CLS = ("rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm "
              "text-astral-text w-full")
_BTN_PRIMARY = ("px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
                "text-astral-primary border border-astral-primary/30")
_BTN = ("px-3 py-1.5 rounded-lg text-xs bg-white/5 border border-white/10 "
        "text-astral-text")
_BTN_DANGER = "px-3 py-1.5 rounded-lg text-xs text-red-400 hover:bg-red-500/10"

_PHASE_HELP = {
    "specify": "Say what the agent should do, in your own words. The assistant drafts "
               "it; you have the final edit.",
    "clarify": "Answer every open question. This step cannot be skipped — an agent "
               "built on an unresolved ambiguity is a bug waiting to happen.",
    "plan": "One tool per line: name | scope | what it does. Ask for the fewest "
            "permissions that work — extra ones are refused at Analyze.",
    "tasks": "The build steps, one per line. Edit freely.",
    "analyze": "The agent constitution is checked here. Nothing is written until it "
               "passes.",
    "generate": "Analyze passed. Generating sends the agent's code to your desktop "
                "host, which runs it and connects it back.",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _payload(data: Dict[str, Any]) -> str:
    return esc(json.dumps(data))


def _fields(payload: Any) -> Dict[str, str]:
    raw = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {k: ("" if v is None else str(v))
            for k, v in raw.items() if isinstance(k, str) and not isinstance(v, (dict, list))}


def _mutation_fields(payload: Any) -> Dict[str, str]:
    """Merge server-rendered CAS identities with collected editable fields."""

    fields = _fields(payload)
    if isinstance(payload, dict):
        for name in ("expected_revision", "transition_id"):
            value = payload.get(name)
            if value is not None and not isinstance(value, (dict, list, bool)):
                fields[name] = str(value)
    return fields


def _user_agents(orch, user_id: str) -> List[Dict[str, Any]]:
    from orchestrator import user_agents as ua
    return ua.list_user_agents(orch.user_agent_registry, user_id)


def _agent_view(orch, user_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """One agent's display state: durable status + DERIVED liveness."""
    agent_id = row.get("agent_id") or ""
    live = aa.agent_status(orch, user_id, agent_id)
    return {
        "agent_id": agent_id,
        "name": row.get("display_name") or agent_id,
        "status": row.get("status") or "authoring",
        "live": live,
        "revalidation_required": bool(row.get("revalidation_required")),
    }


async def _list_context(orch, user_id: str) -> Dict[str, Any]:
    rows = await asyncio.to_thread(_user_agents, orch, user_id)
    sessions = await asyncio.to_thread(aa.list_sessions, orch, user_id)
    live_ids = {
        row.get("draft_id")
        for row in rows
        if row.get("status") == "live" or row.get("active_revision_id") is not None
    }
    return {
        "agents": [_agent_view(orch, user_id, r) for r in rows],
        # A session whose draft already produced a delivered agent is not "in
        # progress" — it would otherwise linger forever in the list.
        "sessions": [s for s in sessions
                     if s["id"] not in live_ids or aa.phase_of(s) != "generate"],
        "host_online": aa.host_online(orch, user_id),
    }


# ---------------------------------------------------------------------------
# Web render
# ---------------------------------------------------------------------------

def _status_badge(view: Dict[str, Any]) -> str:
    if view["live"] == "running":
        cls, label = "bg-green-500/10 text-green-400 border-green-500/20", "running"
    else:
        cls, label = "bg-white/5 text-astral-muted border-white/10", "offline"
    return (f'<span class="text-[10px] px-2 py-0.5 rounded-full border {cls}">'
            f"{esc(label)}</span>")


def _agent_row(view: Dict[str, Any]) -> str:
    pid = _payload({"agent_id": view["agent_id"]})
    warn = ""
    if view["revalidation_required"]:
        warn = ('<div class="text-xs text-yellow-400 mt-1">The agent rules changed — revise '
                "and re-run Analyze before this can run again.</div>")
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3">'
        f'<div class="flex items-center gap-2">'
        f'<span class="text-sm font-medium text-astral-text">{esc(view["name"])}</span>'
        f"{_status_badge(view)}"
        f'<span class="text-xs text-astral-muted ml-auto">{esc(view["status"])}</span></div>'
        f"{warn}"
        f'<div class="flex gap-2 mt-2">'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_revise" '
        f"data-ui-payload='{pid}'>Revise</button>"
        f'<button type="button" class="{_BTN_DANGER}" data-ui-action="chrome_author_delete" '
        f"data-ui-payload='{pid}'>Delete</button>"
        f"</div></div>"
    )


def _session_row(session: Dict[str, Any]) -> str:
    phase = aa.phase_of(session)
    pid = _payload({"surface": SURFACE_KEY, "params": {"draft_id": session["id"]}})
    return (
        f'<button type="button" class="w-full text-left bg-white/5 hover:bg-white/10 border '
        f'border-white/10 rounded-lg p-3" data-ui-action="chrome_open" '
        f"data-ui-payload='{pid}'>"
        f'<div class="flex items-center gap-2">'
        f'<span class="text-sm font-medium text-astral-text">'
        f'{esc(session.get("agent_name") or "")}</span>'
        f'<span class="text-xs text-astral-muted ml-auto">'
        f"{esc(aa.PHASE_LABELS[phase])}</span></div></button>"
    )


def _new_form() -> str:
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4" data-ui-form>'
        f'<div class="text-sm font-medium text-astral-text mb-2">Create an agent</div>'
        f'<input name="agent_name" type="text" placeholder="Agent name" '
        f'class="{_INPUT_CLS} mb-2">'
        f'<textarea name="description" rows="2" placeholder="What should it do for you? '
        f'(at least 10 characters)" class="{_INPUT_CLS}"></textarea>'
        f'<button type="button" class="{_BTN_PRIMARY} mt-2" '
        f'data-ui-action="chrome_author_start" data-ui-collect="true">Start</button>'
        f"</div>"
    )


async def _render_list(orch, user_id: str) -> str:
    ctx = await _list_context(orch, user_id)
    agents = "".join(_agent_row(a) for a in ctx["agents"]) or (
        '<div class="text-sm text-astral-muted italic">No agents yet — create one below.</div>')
    sessions = "".join(_session_row(s) for s in ctx["sessions"])
    sessions_block = (
        '<div class="text-xs font-semibold uppercase tracking-wider text-astral-muted">'
        f"In progress</div><div class=\"space-y-2\">{sessions}</div>" if sessions else "")
    host = HOST_NOTE if not ctx["host_online"] else (
        "Your agents run on your desktop host, not on the server.")
    return (
        f'<p class="text-xs text-astral-muted">{esc(host)}</p>'
        f'<div class="space-y-2">{agents}</div>'
        f"{sessions_block}"
        f"{_new_form()}"
    )


def _rail(current: str) -> str:
    chips = []
    for phase in aa.PHASES:
        done = aa.PHASES.index(phase) < aa.PHASES.index(current)
        if phase == current:
            cls = "bg-astral-primary/20 text-astral-primary border-astral-primary/30"
        elif done:
            cls = "bg-white/10 text-astral-text border-white/10"
        else:
            cls = "bg-transparent text-astral-muted border-white/10"
        chips.append(f'<span class="text-[10px] px-2 py-0.5 rounded-full border {cls}">'
                     f"{esc(aa.PHASE_LABELS[phase])}</span>")
    return f'<div class="flex flex-wrap gap-1.5">{"".join(chips)}</div>'


def _violations_block(record: Dict[str, Any]) -> str:
    rows = []
    for v in record.get("violations") or []:
        rows.append(
            f'<li class="text-sm text-red-400">{esc(v.get("plain_language") or "")} '
            f'<span class="text-xs text-astral-muted">'
            f'(rule {esc(v.get("principle") or "")} — {esc(v.get("title") or "")}; '
            f'field: {esc(v.get("offending_field") or "")})</span></li>')
    if not rows:
        return ""
    return ('<div class="border border-red-500/20 bg-red-500/10 rounded-lg p-3">'
            '<div class="text-sm text-red-400 font-medium mb-1">This design cannot be built '
            "as written — nothing was generated.</div>"
            f'<ul class="space-y-1 list-disc pl-5">{"".join(rows)}</ul></div>')


def _phase_body(row: Dict[str, Any], phase: str) -> str:
    """The editable artifact for ``phase`` (web)."""
    if phase == "specify":
        return (
            f'<input name="agent_name" type="text" value="{esc(row.get("agent_name") or "")}" '
            f'class="{_INPUT_CLS} mb-2">'
            f'<textarea name="specification" rows="10" class="{_INPUT_CLS}">'
            f'{esc(row.get("description") or "")}</textarea>'
        )
    if phase == "clarify":
        items = aa.clarify_items(row)
        if not items:
            return ('<div class="text-sm text-astral-muted italic">No questions drafted yet — '
                    "use “Ask the assistant”.</div>")
        parts = []
        for idx, item in enumerate(items):
            parts.append(
                f'<label class="flex flex-col gap-1 text-sm mb-2">'
                f'<span class="text-astral-text">{esc(item["question"])}</span>'
                f'<textarea name="q{idx}" rows="2" class="{_INPUT_CLS}">'
                f'{esc(item["answer"])}</textarea></label>')
        return "".join(parts)
    if phase == "plan":
        plan = aa.plan_artifact(row)
        return (
            '<label class="flex flex-col gap-1 text-sm mb-2">'
            '<span class="text-astral-text">Tools — one per line: '
            "name | scope | what it does</span>"
            f'<textarea name="tools" rows="6" class="{_INPUT_CLS}">'
            f'{esc(aa.format_tool_lines(plan.get("tools") or []))}</textarea></label>'
            '<label class="flex flex-col gap-1 text-sm mb-2">'
            '<span class="text-astral-text">Permissions requested (comma separated)</span>'
            f'<input name="scopes" type="text" class="{_INPUT_CLS}" '
            f'value="{esc(", ".join(plan.get("declared_scopes") or []))}"></label>'
            '<label class="flex flex-col gap-1 text-sm">'
            '<span class="text-astral-text">External addresses it may reach '
            "(one per line, optional)</span>"
            f'<textarea name="egress" rows="2" class="{_INPUT_CLS}">'
            f'{esc(chr(10).join(plan.get("declared_egress") or []))}</textarea></label>'
        )
    if phase == "tasks":
        plan = aa.plan_artifact(row)
        return (f'<textarea name="tasks" rows="8" class="{_INPUT_CLS}">'
                f'{esc(chr(10).join(plan.get("tasks") or []))}</textarea>')
    if phase == "analyze":
        record = aa.analyze_record(row)
        if not record:
            return ('<div class="text-sm text-astral-muted italic">Not checked yet.</div>')
        if record.get("passed"):
            return notice_block("success", "Analyze passed — you can generate this agent.")
        return _violations_block(record)
    # generate
    record = aa.analyze_record(row)
    return notice_block("success", f"Analyze passed against the agent rules "
                                   f"(version {record.get('constitution_version') or '?'}).")


def _mutation_payload(draft_id: str, state_revision: int) -> Dict[str, Any]:
    """Return one render-bound, replay-safe authoring mutation identity."""

    return {
        "draft_id": draft_id,
        "expected_revision": state_revision,
        "transition_id": str(uuid.uuid4()),
    }


def _phase_actions(draft_id: str, phase: str, state_revision: int) -> str:
    """The phase's action buttons.

    A collecting button (``data-ui-collect``) posts the form's named fields, so
    the session id rides the form's hidden ``draft_id`` input; a non-collecting
    button posts only its ``data-ui-payload``, so it carries the id explicitly.
    Both land on the handler's ``_draft_id`` — miss either and the action would
    silently address no session."""
    pid = _payload(_mutation_payload(draft_id, state_revision))
    back = (f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_list">'
            "← My agents</button>")
    if phase == "analyze":
        act = (f'<button type="button" class="{_BTN_PRIMARY}" '
               f'data-ui-action="chrome_author_analyze" data-ui-payload=\'{pid}\'>'
               "Run Analyze</button>")
        return f'<div class="flex flex-wrap gap-2">{act}{back}</div>'
    if phase == "generate":
        act = (f'<button type="button" class="{_BTN_PRIMARY}" '
               f'data-ui-action="chrome_author_generate" data-ui-payload=\'{pid}\'>'
               "Generate &amp; send to my desktop</button>"
               f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_analyze" '
               f"data-ui-payload='{pid}'>Re-run Analyze</button>")
        return f'<div class="flex flex-wrap gap-2">{act}{back}</div>'
    advance = "chrome_author_clarify" if phase == "clarify" else "chrome_author_advance"
    return (
        '<div class="flex flex-wrap gap-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="{advance}" '
        f"data-ui-payload='{_payload(_mutation_payload(draft_id, state_revision))}' "
        'data-ui-collect="true">Save &amp; continue</button>'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_edit" '
        f"data-ui-payload='{_payload(_mutation_payload(draft_id, state_revision))}' "
        'data-ui-collect="true">Save</button>'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_draft" '
        f"data-ui-payload='{pid}'>Ask the assistant</button>"
        f"{back}</div>"
    )


async def _render_session(orch, user_id: str, draft_id: str) -> str:
    row = await asyncio.to_thread(aa.get_session, orch, user_id, draft_id)
    if row is None:
        return notice_block("error", "That authoring session is not available.")
    phase = aa.phase_of(row)
    return (
        f'<div data-ui-form class="space-y-3">'
        # The session id rides every field-collecting action (client.js collects
        # named inputs from the enclosing [data-ui-form]).
        f'<input type="hidden" name="draft_id" value="{esc(draft_id)}">'
        f"{_rail(phase)}"
        f'<div class="text-sm font-semibold text-astral-text">'
        f"{esc(aa.PHASE_LABELS[phase])} — {esc(row.get('agent_name') or '')}</div>"
        f'<p class="text-xs text-astral-muted">{esc(_PHASE_HELP[phase])}</p>'
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4">'
        f"{_phase_body(row, phase)}</div>"
        f"{_phase_actions(draft_id, phase, int(row.get('state_revision') or 0))}"
        f'<p class="text-xs text-astral-muted">{esc(HOST_NOTE)}</p>'
        f"</div>"
    )


async def render(orch, user_id: str, roles: Any, params: Any) -> str:
    """Web body: the agent list, or one authoring session."""
    _ = roles
    if not aa.byo_enabled():
        return notice_block("info", _DISABLED)
    params = params if isinstance(params, dict) else {}
    draft_id = str(params.get("draft_id") or "")
    if draft_id:
        return await _render_session(orch, user_id, draft_id)
    return await _render_list(orch, user_id)


# ---------------------------------------------------------------------------
# Native SDUI render
# ---------------------------------------------------------------------------

def _sdui_phase_fields(row: Dict[str, Any], phase: str, _sdui) -> List[Dict[str, Any]]:
    if phase == "specify":
        return [
            _sdui.field("agent_name", "Name", "text", default=row.get("agent_name") or ""),
            _sdui.field("specification", "Specification", "textarea",
                        default=row.get("description") or ""),
        ]
    if phase == "clarify":
        return [_sdui.field(f"q{idx}", item["question"], "textarea",
                            default=item["answer"] or None)
                for idx, item in enumerate(aa.clarify_items(row))]
    if phase == "plan":
        plan = aa.plan_artifact(row)
        return [
            _sdui.field("tools", "Tools", "textarea",
                        default=aa.format_tool_lines(plan.get("tools") or []),
                        help="One per line: name | scope | what it does"),
            _sdui.field("scopes", "Permissions requested", "text",
                        default=", ".join(plan.get("declared_scopes") or [])),
            _sdui.field("egress", "External addresses (optional)", "textarea",
                        default="\n".join(plan.get("declared_egress") or [])),
        ]
    if phase == "tasks":
        plan = aa.plan_artifact(row)
        return [_sdui.field("tasks", "Tasks (one per line)", "textarea",
                            default="\n".join(plan.get("tasks") or []))]
    return []


async def components(orch, user_id: str, roles: Any, params: Any) -> List[Dict[str, Any]]:
    """Native SDUI body — the same surface, the same ``chrome_author_*`` actions."""
    _ = roles
    from webrender.chrome.surfaces import _sdui
    if not aa.byo_enabled():
        return [_sdui.alert(_DISABLED, "info")]
    params = params if isinstance(params, dict) else {}
    draft_id = str(params.get("draft_id") or "")

    if draft_id:
        row = await asyncio.to_thread(aa.get_session, orch, user_id, draft_id)
        if row is None:
            return [_sdui.alert("That authoring session is not available.", "error")]
        phase = aa.phase_of(row)
        state_revision = int(row.get("state_revision") or 0)
        out: List[Dict[str, Any]] = [
            _sdui.text(f"{aa.PHASE_LABELS[phase]} — {row.get('agent_name') or ''}", "h3"),
            _sdui.text(_PHASE_HELP[phase], "caption"),
        ]
        if phase == "analyze":
            record = aa.analyze_record(row)
            if record.get("passed"):
                out.append(_sdui.alert("Analyze passed — you can generate this agent.",
                                       "success"))
            elif record:
                for v in record.get("violations") or []:
                    out.append(_sdui.alert(
                        f"{v.get('plain_language') or ''} (rule {v.get('principle')}, "
                        f"field: {v.get('offending_field')})", "error"))
            else:
                out.append(_sdui.text("Not checked yet.", "caption"))
            out.append(_sdui.button("Run Analyze", "chrome_author_analyze",
                                    _mutation_payload(draft_id, state_revision),
                                    variant="primary"))
        elif phase == "generate":
            record = aa.analyze_record(row)
            out.append(_sdui.alert(
                f"Analyze passed against the agent rules "
                f"(version {record.get('constitution_version') or '?'}).", "success"))
            out.append(_sdui.button("Generate & send to my desktop", "chrome_author_generate",
                                    _mutation_payload(draft_id, state_revision),
                                    variant="primary"))
            out.append(_sdui.button("Re-run Analyze", "chrome_author_analyze",
                                    _mutation_payload(draft_id, state_revision)))
        else:
            advance = ("chrome_author_clarify" if phase == "clarify"
                       else "chrome_author_advance")
            fields = _sdui_phase_fields(row, phase, _sdui)
            if fields:
                out.append(_sdui.form(fields, actions=[
                    {"label": "Save", "action": "chrome_author_edit",
                     "payload": _mutation_payload(draft_id, state_revision)},
                    {"label": "Save & continue", "action": advance, "variant": "primary",
                     "payload": _mutation_payload(draft_id, state_revision)},
                ]))
            else:
                out.append(_sdui.text("Nothing drafted yet — ask the assistant.", "caption"))
            out.append(_sdui.button("Ask the assistant", "chrome_author_draft",
                                    {"draft_id": draft_id}))
        out.append(_sdui.button("← My agents", "chrome_author_list"))
        out.append(_sdui.text(HOST_NOTE, "caption"))
        return out

    ctx = await _list_context(orch, user_id)
    out = [_sdui.text(HOST_NOTE if not ctx["host_online"]
                      else "Your agents run on your desktop host, not on the server.",
                      "caption")]
    for view in ctx["agents"]:
        content = [
            _sdui.badge(view["live"], "success" if view["live"] == "running" else "default"),
            _sdui.text(f"status: {view['status']}", "caption"),
            _sdui.button("Revise", "chrome_author_revise", {"agent_id": view["agent_id"]}),
            _sdui.button("Delete", "chrome_author_delete", {"agent_id": view["agent_id"]}),
        ]
        if view["revalidation_required"]:
            content.insert(0, _sdui.alert(
                "The agent rules changed — revise and re-run Analyze before this can run "
                "again.", "warning"))
        out.append(_sdui.card(view["name"], content))
    if not ctx["agents"]:
        out.append(_sdui.text("No agents yet — create one below.", "caption"))
    for session in ctx["sessions"]:
        out.append(_sdui.button(
            f"{session.get('agent_name') or ''} — {aa.PHASE_LABELS[aa.phase_of(session)]}",
            "chrome_open",
            {"surface": SURFACE_KEY, "params": {"draft_id": session["id"]}}))
    out.append(_sdui.form(
        [_sdui.field("agent_name", "Agent name", "text"),
         _sdui.field("description", "What should it do for you?", "textarea",
                     help="At least 10 characters.")],
        submit_action="chrome_author_start", submit_label="Start"))
    return out


# ---------------------------------------------------------------------------
# Handlers — EVERY one re-checks the flag and fails closed (FR-009)
# ---------------------------------------------------------------------------

def _refused() -> Tuple[str, Dict[str, Any], str]:
    return (SURFACE_KEY, {}, notice_block("error", _DISABLED))


def _draft_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("draft_id") or _fields(payload).get("draft_id") or "")


async def _autodraft(orch, websocket, user_id: str, draft_id: str) -> str:
    """Assistant-draft the current phase's artifact when it is still empty.

    This is what makes the flow "assistant-drafted, human-editable": landing on a
    phase hands the user something to react to. Fail-open — a missing/erroring
    LLM leaves an empty artifact the user can write themselves, never a dead end.
    """
    row = await asyncio.to_thread(aa.get_session, orch, user_id, draft_id)
    if row is None:
        return ""
    phase = aa.phase_of(row)
    if phase == "clarify" and row.get("clarify_answers") is not None:
        return ""
    if phase == "plan" and (aa.plan_artifact(row).get("tools_used") or []):
        return ""
    if phase == "tasks" and (aa.plan_artifact(row).get("tasks") or []):
        return ""
    if phase in ("analyze", "generate", "specify"):
        return ""
    _drafted, message = await aa.draft_phase(orch, websocket, user_id, draft_id)
    return message


async def _h_start(orch, websocket, user_id, roles, payload):
    """``chrome_author_start {fields}`` — open a session and draft Specify."""
    _ = roles
    if not aa.byo_enabled():
        return _refused()
    fields = _fields(payload)
    name = (fields.get("agent_name") or "").strip()
    description = (fields.get("description") or "").strip()
    if len(name) < 2 or len(description) < 10:
        return (SURFACE_KEY, {}, notice_block(
            "error", "A name and a description of at least 10 characters are required."))
    session = await aa.start_session(orch, user_id=user_id, agent_name=name,
                                     description=description)
    _ok, message = await aa.draft_phase(orch, websocket, user_id, session["id"])
    return (SURFACE_KEY, {"draft_id": session["id"]}, notice_block("info", message))


async def _h_draft(orch, websocket, user_id, roles, payload):
    """``chrome_author_draft {draft_id}`` — (re)draft the current artifact."""
    _ = roles
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    ok, message = await aa.draft_phase(orch, websocket, user_id, draft_id)
    return (SURFACE_KEY, {"draft_id": draft_id},
            notice_block("info" if ok else "error", message))


async def _h_edit(orch, websocket, user_id, roles, payload):
    """``chrome_author_edit {draft_id, fields}`` — persist the human's edit. Never
    advances: editing and advancing are separate, deliberate acts."""
    _ = roles, websocket
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    ok, message = await asyncio.to_thread(
        aa.save_artifact, orch, user_id, draft_id, _mutation_fields(payload))
    return (SURFACE_KEY, {"draft_id": draft_id},
            notice_block("success" if ok else "error", message))


async def _h_advance(orch, websocket, user_id, roles, payload):
    """``chrome_author_advance`` / ``chrome_author_clarify`` — save + advance one
    phase.

    The CLARIFY HARD GATE lives in :func:`agent_authoring.advance`: with an
    unanswered question the session does not move and the notice says which
    question is blocking. Both action names route here so the gate is the same
    code on every path (a client cannot pick a laxer one).
    """
    _ = roles
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    advanced, _phase, message = await asyncio.to_thread(
        aa.advance, orch, user_id, draft_id, _mutation_fields(payload))
    if not advanced:
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block("error", message))
    drafted = await _autodraft(orch, websocket, user_id, draft_id)
    notice = message + ((" " + drafted) if drafted else "")
    return (SURFACE_KEY, {"draft_id": draft_id}, notice_block("success", notice))


async def _h_analyze(orch, websocket, user_id, roles, payload):
    """``chrome_author_analyze {draft_id}`` — the Analyze HARD GATE.

    On violations the session stays at ``analyze`` and each violation is cited in
    plain language with its offending field; NOTHING is generated (FR-003)."""
    _ = roles, websocket
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    mutation = _mutation_fields(payload)
    try:
        expected_revision = int(mutation["expected_revision"])
    except (KeyError, TypeError, ValueError):
        expected_revision = None
    result = await asyncio.to_thread(
        aa.run_analyze,
        orch,
        user_id,
        draft_id,
        expected_revision=expected_revision,
        transition_id=mutation.get("transition_id"),
    )
    status = result.get("status")
    if status == "passed":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "success", "Analyze passed — you can generate this agent now."))
    if status == "analyze_failed":
        count = len(result.get("violations") or [])
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", f"Analyze found {count} problem(s) — nothing was generated. "
                     "Fix the design and run it again."))
    if status == "too_early":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", "Finish the earlier steps first."))
    if status == "conflict":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", "This draft changed elsewhere. Refresh and run Analyze again."))
    return (SURFACE_KEY, {}, notice_block("error", "That authoring session is not available."))


async def _h_generate(orch, websocket, user_id, roles, payload):
    """``chrome_author_generate {draft_id}`` — generate + deliver.

    Reachable ONLY post-Analyze-pass, and that is enforced HERE (server-side) by
    :func:`agent_authoring.generation_gate`, not by which buttons the surface drew
    — a forged action on a pre-Analyze session is refused."""
    _ = roles
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    mutation = _mutation_fields(payload)
    try:
        expected_revision = int(mutation["expected_revision"])
    except (KeyError, TypeError, ValueError):
        expected_revision = None
    result = await aa.generate_from_session(
        orch,
        user_id,
        draft_id,
        websocket=websocket,
        expected_revision=expected_revision,
        transition_id=mutation.get("transition_id"),
    )
    status = result.get("status")
    if status == "delivered":
        return (SURFACE_KEY, {}, notice_block(
            "success", "Sent to your desktop host — it will start the agent and connect it."))
    if status == "no_host":
        # The server does not queue delivery to an offline host.  Generate again
        # reopens the exact immutable Plane publication; it never invokes the
        # model or stages a second bundle for this completed draft revision.
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "info", "The agent is ready, but no desktop client is connected, so it "
                    "was not delivered. " + HOST_NOTE +
                    " Open your desktop client and run Generate again to resend "
                    "the same verified bundle."))
    if status == "gate_blocked":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", result.get("reason") or "Analyze has not passed for this agent."))
    if status == "analyze_failed":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", "The agent rules refused this design — nothing was generated."))
    if status == "generation_failed":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", f"Code generation failed: {result.get('error') or 'unknown error'}"))
    if status == "delivery_failed":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", "The verified agent bundle was created, but the desktop host "
                     "could not activate this revision. Check the desktop host, "
                     "then start a new revision; Generate will not rerun this "
                     "terminal revision."))
    if status == "delivery_pending":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "warning", "The verified revision is durable, but desktop activation "
                       "still needs recovery. Keep the desktop client connected "
                       "and run Generate again; the server will reuse the exact "
                       "verified revision and continue with a fenced retry."))
    if status == "conflict":
        return (SURFACE_KEY, {"draft_id": draft_id}, notice_block(
            "error", "This draft changed elsewhere. Refresh before generating again."))
    return (SURFACE_KEY, {}, notice_block("error", "That authoring session is not available."))


async def _h_list(orch, websocket, user_id, roles, payload):
    """``chrome_author_list`` — back to the agent list."""
    _ = orch, websocket, user_id, roles, payload
    if not aa.byo_enabled():
        return _refused()
    return (SURFACE_KEY, {}, "")


async def _h_delete(orch, websocket, user_id, roles, payload):
    """``chrome_author_delete {agent_id}`` — soft delete: stop the host agent,
    drop routing, retain the row + audit trail (T028)."""
    _ = roles, websocket
    if not aa.byo_enabled():
        return _refused()
    agent_id = str((payload or {}).get("agent_id") or "")
    deleted = await orch.delete_user_agent(user_id, agent_id)
    if not deleted:
        return (SURFACE_KEY, {}, notice_block("error", "That agent is not available."))
    return (SURFACE_KEY, {}, notice_block("success", "Deleted — it has been stopped on your "
                                                     "desktop host."))


async def _h_revise(orch, websocket, user_id, roles, payload):
    """``chrome_author_revise {agent_id}`` — re-enter authoring for a live agent.

    The revision walks the whole flow again and must pass Analyze on its own
    before it can generate (T027/FR-026); the running version keeps running until
    the revision registers."""
    _ = roles, websocket
    if not aa.byo_enabled():
        return _refused()
    agent_id = str((payload or {}).get("agent_id") or "")
    result = await aa.revise(orch, user_id, agent_id)
    if result.get("status") != "revising":
        return (SURFACE_KEY, {}, notice_block("error", "That agent is not available."))
    return (SURFACE_KEY, {"draft_id": result["draft_id"]}, notice_block(
        "info", "Revising — this has to pass Analyze again before it can replace the "
                "running version."))


HANDLERS = {
    "chrome_author_start": _h_start,
    "chrome_author_draft": _h_draft,
    "chrome_author_edit": _h_edit,
    "chrome_author_advance": _h_advance,
    # The 057 contract names a handler per phase; specify/plan/tasks are the same
    # save-and-advance act, so they route to the same gated implementation rather
    # than three copies that could drift apart.
    "chrome_author_specify": _h_advance,
    "chrome_author_plan": _h_advance,
    "chrome_author_tasks": _h_advance,
    "chrome_author_clarify": _h_advance,
    "chrome_author_analyze": _h_analyze,
    "chrome_author_generate": _h_generate,
    "chrome_author_list": _h_list,
    "chrome_author_delete": _h_delete,
    "chrome_author_revise": _h_revise,
}
