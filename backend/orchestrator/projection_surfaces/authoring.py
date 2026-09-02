"""Deep-owned host adapter for the BYO agent Projection surface (key
``agent_authoring``, T018/T019 + the UI half of T024/T026/T028/T030).

One server-driven surface, two views:

* **My agents & skills** (feature 077 home) — whether the owner's desktop client
  is connected (by name when known) and what to do when it is not; the express
  lane ("describe it and it exists": one description → a background pipeline
  through the SAME gated session with a live step checklist, stopping only for
  the assistant's questions); the owner's user agents with their DERIVED
  running/offline status (liveness is socket presence, never a stored column);
  in-progress step-editor sessions; the owner's SKILLS (list / create / edit /
  toggle / delete, `orchestrator/user_skills.py`); and the step-by-step editor
  as the advanced path. Revise + Delete live here. There is **no
  share/publish/transfer control anywhere on this surface** — user agents are
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
from orchestrator import agent_quick_create as qc
from orchestrator import user_skills as us
from webrender.chrome import esc, notice_block

logger = logging.getLogger("Orchestrator.Chrome.Authoring")

TITLE = "My agents & skills"

SURFACE_KEY = "agent_authoring"

#: FR-024 — the honest, always-shown truth about where these agents run.
HOST_NOTE = ("Your agents run on your desktop host, not on the server. They are offline "
             "while none of your desktop hosts is online.")
#: 077 — what a person without a connected desktop client should do (FR-005).
HOST_HOWTO = ("To run agents on your PC: install the AstralDeep desktop client, sign in "
              "with this same account, and keep it open. It shows up here as soon as it "
              "connects.")

_DISABLED = "Personal agents are not enabled on this deployment."
_SKILLS_DISABLED = "Skills are not enabled on this deployment."

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
    "clarify": "Press “Find open questions”, then answer every one. This step cannot be "
               "skipped — an agent built on an unresolved ambiguity is a bug waiting to happen.",
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
    runs = qc.runs_for(user_id)
    run_ids = {r.draft_id for r in runs}
    presence = aa.host_presence(orch, user_id)
    return {
        "agents": [_agent_view(orch, user_id, r) for r in rows],
        # A session whose draft already produced a delivered agent is not "in
        # progress" — it would otherwise linger forever in the list; a session
        # the express lane owns is shown as its run, not as an editor session.
        "sessions": [s for s in sessions
                     if (s["id"] not in live_ids or aa.phase_of(s) != "generate")
                     and s["id"] not in run_ids],
        "runs": runs,
        "host_online": presence["online"],
        "host_label": presence["label"],
        "skills": _skills(orch, user_id),
    }


def _skills(orch, user_id: str) -> List[us.Skill]:
    store = us.store_for(orch)
    if store is None:
        return []
    try:
        return store.list(user_id)
    except Exception:  # noqa: BLE001 — a broken file never hides the surface
        logger.debug("user_skills: list failed", exc_info=True)
        return []


def _commands_attr(skills: List[us.Skill]) -> str:
    """``data-astral-commands`` — the web typeahead's refresh source (FR-012)."""
    items = [{"name": "/" + sk.command, "desc": sk.name}
             for sk in skills if sk.enabled and sk.command]
    return esc(json.dumps(items))


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
    """The step-by-step (advanced) entry — the express lane is the default."""
    return (
        f'<details class="bg-white/5 border border-white/10 rounded-lg p-4">'
        f'<summary class="text-sm font-medium text-astral-text cursor-pointer">'
        f"Advanced: build it step by step</summary>"
        f'<p class="text-xs text-astral-muted mt-2">You edit each artifact yourself — '
        f"Specify, Clarify, Plan, Tasks — then run Analyze and Generate.</p>"
        f'<div class="mt-2" data-ui-form>'
        f'<input name="agent_name" type="text" placeholder="Agent name" '
        f'class="{_INPUT_CLS} mb-2">'
        f'<textarea name="description" rows="2" placeholder="What should it do for you? '
        f'(at least 10 characters)" class="{_INPUT_CLS}"></textarea>'
        f'<button type="button" class="{_BTN} mt-2" '
        f'data-ui-action="chrome_author_start" data-ui-collect="true">Start step by step</button>'
        f"</div></details>"
    )


def _quick_form(host_online: bool) -> str:
    hint = ("It will be sent to your desktop client as soon as it is built."
            if host_online else
            "It will be built now and sent when your desktop client connects.")
    return (
        f'<div class="bg-astral-primary/10 border border-astral-primary/20 rounded-lg p-4" '
        f"data-ui-form>"
        f'<div class="text-sm font-medium text-astral-text mb-1">Create an agent</div>'
        f'<p class="text-xs text-astral-muted mb-2">Describe what you want it to do, in your '
        f"own words. The assistant designs it, checks it against the agent rules, and builds "
        f"it — you only get asked when something is unclear. {esc(hint)}</p>"
        f'<textarea name="description" rows="3" placeholder="e.g. Every morning, read the '
        f'CSV in my Downloads folder called sales.csv and tell me the three biggest changes '
        f'since yesterday" class="{_INPUT_CLS}"></textarea>'
        f'<input name="agent_name" type="text" placeholder="Name (optional)" '
        f'class="{_INPUT_CLS} mt-2">'
        f'<button type="button" class="{_BTN_PRIMARY} mt-2" '
        f'data-ui-action="chrome_author_quick_create" data-ui-collect="true">Create</button>'
        f"</div>"
    )


def _desktop_status(ctx: Dict[str, Any]) -> str:
    if ctx["host_online"]:
        return (f'<div class="flex items-center gap-2 text-xs">'
                f'<span class="w-2 h-2 rounded-full bg-green-400"></span>'
                f'<span class="text-astral-text">Desktop host connected — '
                f'{esc(ctx["host_label"])}</span>'
                f'<span class="text-astral-muted">· your agents run on that desktop host, '
                f"not on the server</span>"
                f"</div>")
    return (f'<div class="border border-yellow-500/20 bg-yellow-500/10 rounded-lg p-3 text-xs">'
            f'<div class="text-yellow-400 font-medium">No desktop client connected</div>'
            f'<div class="text-astral-muted mt-1">{esc(HOST_HOWTO)}</div></div>')


_STEP_MARK = {"pending": "○", "running": "◌", "done": "✓", "failed": "✗", "waiting": "…"}


def _run_card(run: qc.QuickRun, host_label: str) -> str:
    rows = []
    for step in qc.STEPS:
        state = run.steps.get(step, "pending")
        cls = {"done": "text-green-400", "failed": "text-red-400", "running": "text-astral-primary",
               "waiting": "text-yellow-400"}.get(state, "text-astral-muted")
        rows.append(f'<li class="text-xs {cls}">{_STEP_MARK.get(state, "○")} '
                    f"{esc(qc.STEP_LABELS[step])}</li>")
    pid = _payload({"draft_id": run.draft_id})
    body = ""
    actions = ""
    if run.state == qc.NEEDS_ANSWERS:
        fields = "".join(
            f'<label class="block text-xs text-astral-text mt-2">{esc(q["question"])}'
            f'<textarea name="q{i}" rows="2" class="{_INPUT_CLS} mt-1">'
            f'{esc(q.get("answer") or "")}</textarea></label>'
            for i, q in enumerate(run.questions))
        body = (f'<div data-ui-form><input type="hidden" name="draft_id" '
                f'value="{esc(run.draft_id)}">'
                f'<div class="text-xs text-yellow-400">The assistant needs a few answers '
                f"before it can build this.</div>{fields}"
                f'<button type="button" class="{_BTN_PRIMARY} mt-2" '
                f'data-ui-action="chrome_author_quick_answers" data-ui-collect="true">'
                f"Continue</button></div>")
    elif run.state == qc.FAILED:
        violations = _violations_block({"violations": run.outcome.get("violations") or []})
        body = (f'<div class="text-xs text-red-400 mt-1">{esc(run.message)}</div>{violations}')
        retry = ""
        if run.outcome.get("step") in ("generate", "deliver") and run.steps.get("analyze") == "done":
            # Analyze passed; the model's code (or the delivery) failed — a
            # second generation is a fresh draft of the code, not a new design.
            retry = (f'<button type="button" class="{_BTN_PRIMARY}" '
                     f'data-ui-action="chrome_author_quick_resend" data-ui-payload=\'{pid}\'>'
                     f"Try again</button>")
        actions = (
            retry
            + f'<button type="button" class="{_BTN}" data-ui-action="chrome_open" '
            f"data-ui-payload='{_payload({'surface': SURFACE_KEY, 'params': {'draft_id': run.draft_id}})}'>"
            f"Fix in the editor</button>"
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_quick_dismiss" '
            f"data-ui-payload='{pid}'>Dismiss</button>")
    elif run.state == qc.WAITING_FOR_DESKTOP:
        body = (f'<div class="text-xs text-yellow-400 mt-1">{esc(run.message)} {esc(HOST_HOWTO)}'
                f"</div>")
        actions = (
            f'<button type="button" class="{_BTN_PRIMARY}" '
            f'data-ui-action="chrome_author_quick_resend" data-ui-payload=\'{pid}\'>'
            f"Resend to my desktop</button>"
            f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_quick_dismiss" '
            f"data-ui-payload='{pid}'>Dismiss</button>")
    elif run.state == qc.DONE:
        body = (f'<div class="text-xs text-green-400 mt-1">Running on {esc(host_label)} — '
                f"ask for it in chat.</div>")
        actions = (f'<button type="button" class="{_BTN}" '
                   f'data-ui-action="chrome_author_quick_dismiss" data-ui-payload=\'{pid}\'>'
                   f"Dismiss</button>")
    else:
        body = (f'<div class="text-xs text-astral-muted mt-1">{esc(run.message or "Working…")}'
                f"</div>")
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3" '
        f'data-quick-run="{esc(run.state)}">'
        f'<div class="flex items-center gap-2"><span class="text-sm font-medium '
        f'text-astral-text">{esc(run.agent_name)}</span>'
        f'<span class="text-xs text-astral-muted ml-auto">{esc(run.state.replace("_", " "))}'
        f"</span></div>"
        f'<ul class="mt-2 space-y-0.5">{"".join(rows)}</ul>{body}'
        f'<div class="flex gap-2 mt-2">{actions}</div></div>'
    )


def _skill_row(skill: us.Skill) -> str:
    pid = _payload({"slug": skill.slug})
    where = "every chat" if skill.always else ", ".join(skill.applies_to)
    cmd = (f'<code class="text-xs text-astral-primary">/{esc(skill.command)}</code>'
           if skill.command else "")
    state = ("" if skill.enabled else
             '<span class="text-[10px] px-2 py-0.5 rounded-full border bg-white/5 '
             'text-astral-muted border-white/10">off</span>')
    toggle_label = "Disable" if skill.enabled else "Enable"
    toggle = _payload({"slug": skill.slug, "enabled": not skill.enabled})
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-3">'
        f'<div class="flex items-center gap-2 flex-wrap">'
        f'<span class="text-sm font-medium text-astral-text">{esc(skill.name)}</span>{cmd}{state}'
        f'<span class="text-xs text-astral-muted ml-auto">applies to {esc(where)}</span></div>'
        f'<div class="text-xs text-astral-muted mt-1 whitespace-pre-line">'
        f'{esc(skill.instructions[:280])}{"…" if len(skill.instructions) > 280 else ""}</div>'
        f'<div class="flex gap-2 mt-2">'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_user_skill_edit" '
        f"data-ui-payload='{pid}'>Edit</button>"
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_user_skill_toggle" '
        f"data-ui-payload='{toggle}'>{toggle_label}</button>"
        f'<button type="button" class="{_BTN_DANGER}" data-ui-action="chrome_user_skill_delete" '
        f"data-ui-payload='{pid}'>Delete</button>"
        f"</div></div>"
    )


def _skill_form(skill: us.Skill | None) -> str:
    editing = skill is not None
    title = "Edit skill" if editing else "Add a skill"
    slug = f'<input type="hidden" name="skill_slug" value="{esc(skill.slug)}">' if editing else ""
    applies = "" if (skill is None or skill.always) else ", ".join(skill.applies_to)
    return (
        f'<div class="bg-white/5 border border-white/10 rounded-lg p-4" data-ui-form>'
        f'<div class="text-sm font-medium text-astral-text mb-1">{title}</div>'
        f'<p class="text-xs text-astral-muted mb-2">A skill is standing guidance in your own '
        f"words — how you like things done, a checklist, a format. It is followed in every "
        f"chat (or only when the agents you name are in play), and a /command turns it into "
        f"a shortcut you can type.</p>{slug}"
        f'<input name="skill_name" type="text" placeholder="Name (e.g. Weekly status format)" '
        f'value="{esc(skill.name) if editing else ""}" class="{_INPUT_CLS} mb-2">'
        f'<div class="flex gap-2 mb-2">'
        f'<input name="skill_command" type="text" placeholder="/command (optional)" '
        f'value="{esc(skill.command) if editing else ""}" class="{_INPUT_CLS}">'
        f'<input name="skill_applies" type="text" placeholder="Applies to: every chat, or agent ids" '
        f'value="{esc(applies)}" class="{_INPUT_CLS}"></div>'
        f'<textarea name="skill_instructions" rows="5" placeholder="Instructions the assistant '
        f'should follow" class="{_INPUT_CLS}">{esc(skill.instructions) if editing else ""}'
        f"</textarea>"
        f'<div class="flex gap-2 mt-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="chrome_user_skill_save" '
        f'data-ui-collect="true">{"Save" if editing else "Add skill"}</button>'
        + (f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_list">'
           f"Cancel</button>" if editing else "")
        + "</div></div>"
    )


async def _render_list(orch, user_id: str, edit_skill: str = "") -> str:
    ctx = await _list_context(orch, user_id)
    skills_enabled = us.enabled()
    byo = aa.byo_enabled()
    parts: List[str] = [f'<div class="space-y-4" data-astral-commands="{_commands_attr(ctx["skills"])}">']
    if byo:
        parts.append(_desktop_status(ctx))
        parts.append(_quick_form(ctx["host_online"]))
        for run in ctx["runs"]:
            parts.append(_run_card(run, ctx["host_label"]))
        agents = "".join(_agent_row(a) for a in ctx["agents"]) or (
            '<div class="text-sm text-astral-muted italic">No agents yet — describe one above '
            "and press Create.</div>")
        parts.append('<div class="text-xs font-semibold uppercase tracking-wider '
                     'text-astral-muted">Your agents</div>'
                     f'<div class="space-y-2">{agents}</div>')
        sessions = "".join(_session_row(s) for s in ctx["sessions"])
        if sessions:
            parts.append('<div class="text-xs font-semibold uppercase tracking-wider '
                         'text-astral-muted">In the step editor</div>'
                         f'<div class="space-y-2">{sessions}</div>')
        parts.append(_new_form())
    else:
        parts.append(notice_block("info", _DISABLED))
    if skills_enabled:
        editing = next((sk for sk in ctx["skills"] if sk.slug == edit_skill), None) if edit_skill else None
        rows = "".join(_skill_row(sk) for sk in ctx["skills"])
        parts.append('<div class="text-xs font-semibold uppercase tracking-wider '
                     'text-astral-muted">Your skills</div>')
        parts.append(f'<div class="space-y-2">{rows}</div>' if rows else
                     '<div class="text-sm text-astral-muted italic">No skills yet — add one '
                     "below. They work in every client and need no desktop.</div>")
        parts.append(_skill_form(editing))
    parts.append("</div>")
    return "".join(parts)


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


def _phase_body(row: Dict[str, Any], phase: str, orch=None) -> str:
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
            if row.get("clarify_answers") is not None:
                return ('<div class="text-sm text-astral-muted italic">The assistant found no '
                        "open questions — continue.</div>")
            return ('<div class="text-sm text-astral-muted italic">Not checked yet — press '
                    "“Find open questions”.</div>")
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
    ok, reason = _gate(orch, row)
    if not ok:
        return notice_block("error", f"{reason} Re-run Analyze before generating.")
    return notice_block("success", f"Analyze passed against the agent rules "
                                   f"(version {record.get('constitution_version') or '?'}).")


def _gate(orch, row: Dict[str, Any]) -> Tuple[bool, str]:
    """The structural generation gate, as the surface shows it (never enforces
    it — :func:`agent_authoring.generation_gate` is re-run server-side by the
    handler; this only keeps the person from pressing a button that will fail)."""
    if orch is None:
        return True, ""
    try:
        return aa.generation_gate(orch, row)
    except Exception:  # noqa: BLE001 — a test double without the registry
        return True, ""


def _mutation_payload(draft_id: str, state_revision: int) -> Dict[str, Any]:
    """Return one render-bound, replay-safe authoring mutation identity."""

    return {
        "draft_id": draft_id,
        "expected_revision": state_revision,
        "transition_id": str(uuid.uuid4()),
    }


def _phase_actions(draft_id: str, phase: str, state_revision: int, stale: bool = False) -> str:
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
        if stale:
            act = (f'<button type="button" class="{_BTN_PRIMARY}" '
                   f'data-ui-action="chrome_author_analyze" data-ui-payload=\'{pid}\'>'
                   "Re-run Analyze</button>")
        else:
            act = (f'<button type="button" class="{_BTN_PRIMARY}" '
                   f'data-ui-action="chrome_author_generate" data-ui-payload=\'{pid}\'>'
                   "Generate &amp; send to my desktop</button>"
                   f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_analyze" '
                   f"data-ui-payload='{pid}'>Re-run Analyze</button>")
        return f'<div class="flex flex-wrap gap-2">{act}{back}</div>'
    advance = "chrome_author_clarify" if phase == "clarify" else "chrome_author_advance"
    draft_label = "Find open questions" if phase == "clarify" else "Ask the assistant"
    return (
        '<div class="flex flex-wrap gap-2">'
        f'<button type="button" class="{_BTN_PRIMARY}" data-ui-action="{advance}" '
        f"data-ui-payload='{_payload(_mutation_payload(draft_id, state_revision))}' "
        'data-ui-collect="true">Save &amp; continue</button>'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_edit" '
        f"data-ui-payload='{_payload(_mutation_payload(draft_id, state_revision))}' "
        'data-ui-collect="true">Save</button>'
        f'<button type="button" class="{_BTN}" data-ui-action="chrome_author_draft" '
        f"data-ui-payload='{pid}'>{draft_label}</button>"
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
        f"{_phase_body(row, phase, orch)}</div>"
        f"{_phase_actions(draft_id, phase, int(row.get('state_revision') or 0), stale=(phase == 'generate' and not _gate(orch, row)[0]))}"
        f'<p class="text-xs text-astral-muted">{esc(HOST_NOTE)}</p>'
        f"</div>"
    )


async def render(orch, user_id: str, roles: Any, params: Any) -> str:
    """Web body: the home (agents + skills), or one authoring session."""
    _ = roles
    params = params if isinstance(params, dict) else {}
    if not aa.byo_enabled() and not us.enabled():
        return notice_block("info", _DISABLED)
    draft_id = str(params.get("draft_id") or "")
    if draft_id:
        if not aa.byo_enabled():
            return notice_block("info", _DISABLED)
        return await _render_session(orch, user_id, draft_id)
    return await _render_list(orch, user_id, edit_skill=str(params.get("skill_slug") or ""))


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
    params = params if isinstance(params, dict) else {}
    if not aa.byo_enabled() and not us.enabled():
        return [_sdui.alert(_DISABLED, "info")]
    draft_id = str(params.get("draft_id") or "")

    if draft_id:
        if not aa.byo_enabled():
            return [_sdui.alert(_DISABLED, "info")]
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
            ok, reason = _gate(orch, row)
            if ok:
                out.append(_sdui.alert(
                    f"Analyze passed against the agent rules "
                    f"(version {record.get('constitution_version') or '?'}).", "success"))
                out.append(_sdui.button("Generate & send to my desktop", "chrome_author_generate",
                                        _mutation_payload(draft_id, state_revision),
                                        variant="primary"))
                out.append(_sdui.button("Re-run Analyze", "chrome_author_analyze",
                                        _mutation_payload(draft_id, state_revision)))
            else:
                out.append(_sdui.alert(f"{reason} Re-run Analyze before generating.", "error"))
                out.append(_sdui.button("Re-run Analyze", "chrome_author_analyze",
                                        _mutation_payload(draft_id, state_revision),
                                        variant="primary"))
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
                out.append(_sdui.text(
                    "The assistant found no open questions — continue." if
                    (phase == "clarify" and row.get("clarify_answers") is not None)
                    else "Nothing drafted yet — ask the assistant.", "caption"))
            out.append(_sdui.button(
                "Find open questions" if phase == "clarify" else "Ask the assistant",
                "chrome_author_draft", {"draft_id": draft_id}))
        out.append(_sdui.button("← My agents & skills", "chrome_author_list"))
        out.append(_sdui.text(HOST_NOTE, "caption"))
        return out

    return await _home_components(orch, user_id, params, _sdui)


def _run_components(run: qc.QuickRun, host_label: str, _sdui) -> Dict[str, Any]:
    lines = [f"{_STEP_MARK.get(run.steps.get(step, 'pending'), '○')} {qc.STEP_LABELS[step]}"
             for step in qc.STEPS]
    content: List[Dict[str, Any]] = [_sdui.bullet_list(lines)]
    pid = {"draft_id": run.draft_id}
    if run.state == qc.NEEDS_ANSWERS:
        content.append(_sdui.alert("The assistant needs a few answers before it can build this.",
                                   "warning"))
        fields = [
            _sdui.field(f"q{i}", q["question"], "textarea", default=q.get("answer") or None)
            for i, q in enumerate(run.questions)]
        content.append(_sdui.form(fields, submit_action="chrome_author_quick_answers",
                                  submit_label="Continue", submit_payload=pid))
    elif run.state == qc.FAILED:
        content.append(_sdui.alert(run.message, "error"))
        for v in run.outcome.get("violations") or []:
            content.append(_sdui.alert(
                f"{v.get('plain_language') or ''} (rule {v.get('principle')}, "
                f"field: {v.get('offending_field')})", "error"))
        if run.outcome.get("step") in ("generate", "deliver") and run.steps.get("analyze") == "done":
            content.append(_sdui.button("Try again", "chrome_author_quick_resend", pid,
                                        variant="primary"))
        content.append(_sdui.button("Fix in the editor", "chrome_open",
                                    {"surface": SURFACE_KEY, "params": {"draft_id": run.draft_id}}))
        content.append(_sdui.button("Dismiss", "chrome_author_quick_dismiss", pid))
    elif run.state == qc.WAITING_FOR_DESKTOP:
        content.append(_sdui.alert(f"{run.message} {HOST_HOWTO}", "warning"))
        content.append(_sdui.button("Resend to my desktop", "chrome_author_quick_resend", pid,
                                    variant="primary"))
        content.append(_sdui.button("Dismiss", "chrome_author_quick_dismiss", pid))
    elif run.state == qc.DONE:
        content.append(_sdui.alert(f"Running on {host_label} — ask for it in chat.", "success"))
        content.append(_sdui.button("Dismiss", "chrome_author_quick_dismiss", pid))
    else:
        content.append(_sdui.text(run.message or "Working…", "caption"))
    return _sdui.card(run.agent_name, content)


def _skill_components(skill: us.Skill, _sdui) -> Dict[str, Any]:
    where = "every chat" if skill.always else ", ".join(skill.applies_to)
    content: List[Dict[str, Any]] = []
    if skill.command:
        content.append(_sdui.badge(f"/{skill.command}", "info"))
    if not skill.enabled:
        content.append(_sdui.badge("off", "default"))
    content.append(_sdui.text(f"applies to {where}", "caption"))
    content.append(_sdui.text(skill.instructions[:280] + ("…" if len(skill.instructions) > 280 else "")))
    content.append(_sdui.button("Edit", "chrome_user_skill_edit", {"slug": skill.slug}))
    content.append(_sdui.button("Disable" if skill.enabled else "Enable", "chrome_user_skill_toggle",
                                {"slug": skill.slug, "enabled": not skill.enabled}))
    content.append(_sdui.button("Delete", "chrome_user_skill_delete", {"slug": skill.slug}))
    return _sdui.card(skill.name, content)


def _skill_form_components(skill: us.Skill | None, _sdui) -> List[Dict[str, Any]]:
    editing = skill is not None
    fields = []
    fields.extend([
        _sdui.field("skill_name", "Name", "text", default=skill.name if editing else None),
        _sdui.field("skill_command", "/command (optional)", "text",
                    default=skill.command if editing else None),
        _sdui.field("skill_applies", "Applies to", "text",
                    default=("" if (skill is None or skill.always) else ", ".join(skill.applies_to)) or None,
                    help="Leave empty for every chat, or list agent ids."),
        _sdui.field("skill_instructions", "Instructions", "textarea",
                    default=skill.instructions if editing else None),
    ])
    out = [_sdui.text("Edit skill" if editing else "Add a skill", "h3"),
           _sdui.text("Standing guidance in your own words — how you like things done, a "
                      "checklist, a format. Followed in every chat (or only for the agents you "
                      "name); a /command makes it a shortcut you can type.", "caption"),
           _sdui.form(fields, submit_action="chrome_user_skill_save",
                      submit_label="Save" if editing else "Add skill",
                      submit_payload={"skill_slug": skill.slug} if editing else None)]
    if editing:
        out.append(_sdui.button("Cancel", "chrome_author_list"))
    return out


async def _home_components(orch, user_id: str, params: Dict[str, Any], _sdui) -> List[Dict[str, Any]]:
    ctx = await _list_context(orch, user_id)
    out: List[Dict[str, Any]] = []
    if aa.byo_enabled():
        if ctx["host_online"]:
            out.append(_sdui.alert(f"Desktop host connected — {ctx['host_label']}. Your agents run "
                                   "on that desktop host, not on the server.", "success"))
        else:
            out.append(_sdui.alert(f"No desktop client connected. {HOST_HOWTO}", "warning"))
        out.append(_sdui.text("Create an agent", "h3"))
        out.append(_sdui.text("Describe what you want it to do, in your own words. The assistant "
                              "designs it, checks it against the agent rules, and builds it — "
                              "you only get asked when something is unclear.", "caption"))
        out.append(_sdui.form(
            [_sdui.field("description", "What should it do for you?", "textarea"),
             _sdui.field("agent_name", "Name (optional)", "text")],
            submit_action="chrome_author_quick_create", submit_label="Create"))
        for run in ctx["runs"]:
            out.append(_run_components(run, ctx["host_label"], _sdui))
        out.append(_sdui.text("Your agents", "h3"))
        if not ctx["agents"]:
            out.append(_sdui.text("No agents yet — describe one above and press Create.", "caption"))
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
        if ctx["sessions"]:
            out.append(_sdui.text("In the step editor", "h3"))
        for session in ctx["sessions"]:
            out.append(_sdui.button(
                f"{session.get('agent_name') or ''} — {aa.PHASE_LABELS[aa.phase_of(session)]}",
                "chrome_open",
                {"surface": SURFACE_KEY, "params": {"draft_id": session["id"]}}))
        out.append(_sdui.text("Advanced: build it step by step", "h3"))
        out.append(_sdui.form(
            [_sdui.field("agent_name", "Agent name", "text"),
             _sdui.field("description", "What should it do for you?", "textarea",
                         help="At least 10 characters.")],
            submit_action="chrome_author_start", submit_label="Start step by step"))
    else:
        out.append(_sdui.alert(_DISABLED, "info"))
    if us.enabled():
        out.append(_sdui.text("Your skills", "h3"))
        if not ctx["skills"]:
            out.append(_sdui.text("No skills yet — add one below. They work in every client and "
                                  "need no desktop.", "caption"))
        for skill in ctx["skills"]:
            out.append(_skill_components(skill, _sdui))
        edit_slug = str(params.get("skill_slug") or "")
        editing = next((sk for sk in ctx["skills"] if sk.slug == edit_slug), None) if edit_slug else None
        out.extend(_skill_form_components(editing, _sdui))
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
    """``chrome_author_list`` — back to the home view."""
    _ = orch, websocket, user_id, roles, payload
    if not aa.byo_enabled() and not us.enabled():
        return _refused()
    return (SURFACE_KEY, {}, "")


# ---------------------------------------------------------------------------
# 077 — the express lane
# ---------------------------------------------------------------------------

async def _refresh_home(orch, websocket, user_id, roles, run) -> None:
    """Re-render the home view for the socket that started a run — only while
    that socket is still connected and still looking at this surface."""
    _ = run
    from orchestrator import chrome_events
    if websocket not in getattr(orch, "ui_clients", ()):
        return
    if chrome_events.open_surface_for(orch, websocket) != SURFACE_KEY:
        return
    await chrome_events._render_surface(orch, websocket, user_id, roles, SURFACE_KEY, {}, "")


async def _h_quick_create(orch, websocket, user_id, roles, payload):
    """``chrome_author_quick_create {fields: {description, agent_name?}}`` —
    open a session and run the whole pipeline in the background (FR-001)."""
    if not aa.byo_enabled():
        return _refused()
    fields = _fields(payload)
    run, message = await qc.start(
        orch, websocket, user_id, roles,
        description=fields.get("description") or "",
        agent_name=fields.get("agent_name") or "",
        refresh=_refresh_home)
    if run is None:
        return (SURFACE_KEY, {}, notice_block("error", message))
    return (SURFACE_KEY, {}, notice_block("info", message))


async def _h_quick_answers(orch, websocket, user_id, roles, payload):
    """``chrome_author_quick_answers {draft_id, fields: {q0..}}`` — the owner's
    answers go through the Clarify hard gate, then the pipeline resumes (FR-002)."""
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    ok, message = await qc.resume_with_answers(
        orch, websocket, user_id, roles, draft_id, _fields(payload), refresh=_refresh_home)
    return (SURFACE_KEY, {}, notice_block("success" if ok else "error", message))


async def _h_quick_resend(orch, websocket, user_id, roles, payload):
    """``chrome_author_quick_resend {draft_id}`` — send the already verified
    bundle to a now-connected desktop client (no model call; FR-006)."""
    if not aa.byo_enabled():
        return _refused()
    draft_id = _draft_id(payload)
    run = qc.get_run(user_id, draft_id)
    result = await aa.generate_from_session(orch, user_id, draft_id, websocket=websocket)
    status = str(result.get("status") or "")
    if run is not None:
        run.agent_id = str(result.get("agent_id") or run.agent_id)
        if status == "delivered":
            run.state = qc.DONE
            run.steps["generate"], run.steps["deliver"] = "done", "done"
            run.message = "Delivered — your desktop client is starting it."
            run.outcome = dict(result)
        elif status in ("no_host", "delivery_pending"):
            run.state = qc.WAITING_FOR_DESKTOP
            run.steps["generate"], run.steps["deliver"] = "done", "waiting"
            run.message = ("Still no desktop client connected — it will be sent as soon as one "
                           "connects.")
            run.outcome = dict(result)
        elif status == "generation_failed":
            run.state, run.steps["generate"] = qc.FAILED, "failed"
            run.message = f"Code generation failed: {result.get('error') or 'unknown error'}"
            run.outcome = dict(result, step="generate")
        else:
            run.state, run.steps["deliver"] = qc.FAILED, "failed"
            run.message = ("Your desktop refused this build of the agent. Press Revise on it in "
                           "the list to build a new one." if status == "delivery_failed" else
                           f"Could not send it ({result.get('error') or result.get('reason') or status}).")
            run.outcome = dict(result, step="deliver")
        run.note(run.message)
    kind = "success" if status == "delivered" else "info" if status in ("no_host", "delivery_pending") else "error"
    return (SURFACE_KEY, {}, notice_block(kind, (run.message if run else status) or status))


async def _h_quick_dismiss(orch, websocket, user_id, roles, payload):
    """``chrome_author_quick_dismiss {draft_id}`` — drop a finished run card
    (the session and any delivered agent stay)."""
    _ = orch, websocket, roles
    if not aa.byo_enabled():
        return _refused()
    qc.forget(user_id, _draft_id(payload))
    return (SURFACE_KEY, {}, "")


# ---------------------------------------------------------------------------
# 077 — skills
# ---------------------------------------------------------------------------

def _skills_refused() -> Tuple[str, Dict[str, Any], str]:
    return (SURFACE_KEY, {}, notice_block("error", _SKILLS_DISABLED))


async def _h_skill_save(orch, websocket, user_id, roles, payload):
    """``chrome_user_skill_save {skill_slug?, fields: {skill_name, skill_command,
    skill_applies, skill_instructions}}`` — create or replace one skill."""
    _ = websocket, roles
    store = us.store_for(orch)
    if store is None:
        return _skills_refused()
    fields = _fields(payload)
    slug = str((payload or {}).get("skill_slug") or fields.get("skill_slug") or "")
    from orchestrator import slash_commands
    try:
        skill = await asyncio.to_thread(
            store.save, user_id,
            name=fields.get("skill_name") or "",
            instructions=fields.get("skill_instructions") or "",
            applies_to=fields.get("skill_applies") or "",
            command=fields.get("skill_command") or "",
            enabled=True if not slug else (store.get(user_id, slug) or us.Skill("", "", "", ())).enabled,
            slug=slug,
            reserved_commands=slash_commands.reserved_names())
    except us.SkillValidationError as exc:
        params = {"skill_slug": slug} if slug else {}
        return (SURFACE_KEY, params, notice_block("error", str(exc)))
    hint = f" Type /{skill.command} in chat to use it." if skill.command else ""
    return (SURFACE_KEY, {}, notice_block("success", f"Saved “{skill.name}”.{hint}"))


async def _h_skill_edit(orch, websocket, user_id, roles, payload):
    """``chrome_user_skill_edit {slug}`` — open the form prefilled."""
    _ = orch, websocket, user_id, roles
    if not us.enabled():
        return _skills_refused()
    return (SURFACE_KEY, {"skill_slug": str((payload or {}).get("slug") or "")}, "")


async def _h_skill_toggle(orch, websocket, user_id, roles, payload):
    """``chrome_user_skill_toggle {slug, enabled}``."""
    _ = websocket, roles
    store = us.store_for(orch)
    if store is None:
        return _skills_refused()
    slug = str((payload or {}).get("slug") or "")
    enabled = bool((payload or {}).get("enabled"))
    try:
        skill = await asyncio.to_thread(store.set_enabled, user_id, slug, enabled)
    except us.SkillValidationError as exc:
        return (SURFACE_KEY, {}, notice_block("error", str(exc)))
    if skill is None:
        return (SURFACE_KEY, {}, notice_block("error", "That skill no longer exists."))
    return (SURFACE_KEY, {}, notice_block(
        "success", f"“{skill.name}” is now {'on' if enabled else 'off'}."))


async def _h_skill_delete(orch, websocket, user_id, roles, payload):
    """``chrome_user_skill_delete {slug}``."""
    _ = websocket, roles
    store = us.store_for(orch)
    if store is None:
        return _skills_refused()
    slug = str((payload or {}).get("slug") or "")
    deleted = await asyncio.to_thread(store.delete, user_id, slug)
    if not deleted:
        return (SURFACE_KEY, {}, notice_block("error", "That skill no longer exists."))
    return (SURFACE_KEY, {}, notice_block("success", "Skill deleted."))


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
    # 077 — the express lane
    "chrome_author_quick_create": _h_quick_create,
    "chrome_author_quick_answers": _h_quick_answers,
    "chrome_author_quick_resend": _h_quick_resend,
    "chrome_author_quick_dismiss": _h_quick_dismiss,
    # 077 — skills
    "chrome_user_skill_save": _h_skill_save,
    "chrome_user_skill_edit": _h_skill_edit,
    "chrome_user_skill_toggle": _h_skill_toggle,
    "chrome_user_skill_delete": _h_skill_delete,
}
