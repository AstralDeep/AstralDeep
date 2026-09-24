"""Renders the agent-info dialog opened from the sidebar directory: description,
examples, and declared tool chips. 'Run' dispatches an example through the ordinary
chat_message gate/audit path; visibility mirrors agents.py's directory rule.
"""

import asyncio
import json
import logging

from webrender.chrome import esc

logger = logging.getLogger("Orchestrator.Surfaces.AgentIntro")

TITLE = "Agent"
SUBTITLE = "What it does, and what to ask it"


async def title(orch, user_id, params) -> str:
    agent_id = str((params or {}).get("agent_id") or "")
    if not agent_id:
        return TITLE
    card, _enabled = await _visible_agent(orch, user_id, agent_id)
    return getattr(card, "name", "") or TITLE

NO_NAV = True

MAX_TOOL_CHIPS = 14

_BTN_RUN = (
    "astral-btn astral-btn-primary px-3 py-1.5 rounded-lg text-xs font-medium"
)
_BTN_LOAD = (
    "astral-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 "
    "border border-white/10 text-astral-text"
)


def _payload(data) -> str:
    return esc(json.dumps(data))


async def _visible_agent(orch, user_id, agent_id: str):
    card = orch.agent_cards.get(agent_id)
    if card is None:
        return None, False
    try:
        from orchestrator.projection_surfaces.agents import _agent_rows, _list_context

        email, ownership, disabled = await _list_context(orch, user_id)
        rows = await asyncio.to_thread(_agent_rows, orch, ownership, disabled)
    except Exception:
        logger.exception("agent_intro: visibility lookup failed for %s", agent_id)
        return None, False
    for row in rows:
        if row["id"] != agent_id:
            continue
        owned = bool(email) and row.get("owner_email") == email
        if owned or row.get("is_public"):
            return card, not row.get("disabled")
        break
    return None, False


def _examples(card):
    raw = (getattr(card, "metadata", None) or {}).get("examples") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        out.append({"title": str(item.get("title") or "Example").strip(), "prompt": prompt})
        if len(out) >= 4:
            break
    return out


def _example_html(example) -> str:
    prompt = example["prompt"]
    return (
        '<div class="astral-agent-example">'
        '<div class="astral-agent-example-copy">'
        f'<div class="astral-agent-example-title">{esc(example["title"])}</div>'
        f'<div class="astral-agent-example-prompt">{esc(prompt)}</div></div>'
        '<div class="astral-agent-example-actions">'
        f'<button type="button" class="{_BTN_RUN}" data-ui-action="chat_message" '
        f'data-ui-payload=\'{_payload({"message": prompt})}\' '
        f'aria-label="Run: {esc(example["title"])}">Run</button>'
        f'<button type="button" class="{_BTN_LOAD}" data-ui-action="compose_prompt" '
        f'data-ui-payload=\'{_payload({"message": prompt})}\' '
        f'aria-label="Load the prompt for: {esc(example["title"])}">Load</button>'
        "</div></div>"
    )


def _tools_html(card) -> str:
    skills = list(getattr(card, "skills", None) or [])
    if not skills:
        return ""
    shown = skills[:MAX_TOOL_CHIPS]
    chips = "".join(
        '<span class="astral-agent-tool-chip" title="{}">{}</span>'.format(
            esc(str(getattr(s, "description", "") or "")), esc(str(s.id or s.name)))
        for s in shown
    )
    rest = len(skills) - len(shown)
    if rest > 0:
        chips += f'<span class="astral-agent-tool-chip">+{rest} more</span>'
    return (
        '<div><div class="astral-agent-intro-section-title">'
        f"Tools it can reach ({len(skills)})</div>"
        f'<div class="astral-agent-tools">{chips}</div></div>'
    )


async def render(orch, user_id, roles, params) -> str:
    from webrender.chrome import chrome_error_block

    agent_id = str((params or {}).get("agent_id") or "")
    card, enabled = await _visible_agent(orch, user_id, agent_id) if agent_id else (None, False)
    if card is None:
        return chrome_error_block("That agent isn't available to your account.")

    state = (
        '<span class="astral-badge astral-badge-accent">Available</span>' if enabled
        else '<span class="astral-badge">Turned off for this account</span>'
    )
    examples = _examples(card)
    if examples:
        examples_html = (
            '<div><div class="astral-agent-intro-section-title">Try one of these</div>'
            '<div class="astral-agent-intro-examples">'
            + "".join(_example_html(e) for e in examples)
            + "</div></div>"
        )
    else:
        examples_html = (
            '<p class="astral-agent-intro-lede">This agent ships no example '
            "prompts yet. Ask it in your own words — the console routes your "
            "message to it when it fits.</p>"
        )

    permissions = (
        f'<button type="button" class="{_BTN_LOAD}" data-ui-action="chrome_open" '
        f'data-ui-payload=\'{_payload({"surface": "agents", "params": {"agent_id": agent_id}})}\''
        ">Permissions for this agent</button>"
    )

    return (
        '<div class="astral-agent-intro">'
        f"<div>{state}</div>"
        f'<p class="astral-agent-intro-lede">{esc(card.description)}</p>'
        f"{examples_html}"
        f"{_tools_html(card)}"
        f'<div class="pt-1">{permissions}</div>'
        "</div>"
    )


async def components(orch, user_id, roles, params):
    from webrender.chrome.surfaces import _sdui

    agent_id = str((params or {}).get("agent_id") or "")
    card, enabled = await _visible_agent(orch, user_id, agent_id) if agent_id else (None, False)
    if card is None:
        return [_sdui.alert("That agent isn't available to your account.", "error")]

    out = [
        _sdui.badge("Available" if enabled else "Turned off for this account",
                    "success" if enabled else "default"),
        _sdui.text(card.description, "body"),
    ]
    examples = _examples(card)
    if examples:
        out.append(_sdui.text("Try one of these", "h3"))
        out.append(_sdui.container(
            [_sdui.button(e["title"], "chat_message", {"message": e["prompt"]})
             for e in examples],
            direction="column"))
    skills = list(getattr(card, "skills", None) or [])
    if skills:
        out.append(_sdui.text(f"Tools it can reach ({len(skills)})", "h3"))
        out.append(_sdui.bullet_list([str(s.id or s.name) for s in skills[:MAX_TOOL_CHIPS]]))
    out.append(_sdui.button("Permissions for this agent", "chrome_open",
                            {"surface": "agents", "params": {"agent_id": agent_id}}))
    return out
