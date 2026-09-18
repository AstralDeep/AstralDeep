"""The agent dialog — what an agent is for, opened from the directory.

Clicking an agent in the sidebar used to drop an ``@Name`` mention into the
composer. That was easy to misread (a second click landed on a full composer
and silently did nothing) and it answered the wrong question: someone
clicking an unfamiliar agent wants to know what it *does*, not to address it.
So the click opens this instead — the agent's own description, the examples
the agent ships on its card, and the tools it can reach.

Nothing here is a shortcut past anything. "Run" sends the example as an
ordinary ``chat_message``, which routes, gates and audits exactly like a
typed turn; "Load" only fills the composer and never leaves the client. The
tool chips are the agent's declared capability, not a grant — what this
account may actually call is set in *Agents & permissions*, one click away at
the bottom of the dialog.

The surface declares ``NO_NAV``: it is not a settings screen, so it renders
as a plain dialog rather than inside the settings rail.
"""

import asyncio
import json
import logging

from webrender.chrome import esc

logger = logging.getLogger("Orchestrator.Surfaces.AgentIntro")

TITLE = "Agent"
SUBTITLE = "What it does, and what to ask it"


async def title(orch, user_id, params) -> str:
    """The dialog is headed by the agent it is about, not by the word "Agent".

    It runs the SAME visibility check the body does, so an id this account
    may not see is headed "Agent" over the body's refusal rather than having
    its name printed in the header. An unknown id falls back the same way.
    """
    agent_id = str((params or {}).get("agent_id") or "")
    if not agent_id:
        return TITLE
    card, _enabled = await _visible_agent(orch, user_id, agent_id)
    return getattr(card, "name", "") or TITLE

#: This dialog is reached from the agent directory, not from the settings
#: menu, so it renders without the settings rail.
NO_NAV = True

#: The tool chip row is an at-a-glance list, not an inventory screen.
MAX_TOOL_CHIPS = 14

_BTN_RUN = (
    "astral-btn astral-btn-primary px-3 py-1.5 rounded-lg text-xs font-medium"
)
_BTN_LOAD = (
    "astral-btn px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 "
    "border border-white/10 text-astral-text"
)


def _payload(data) -> str:
    """A ``data-ui-payload`` attribute value, escaped for HTML."""
    return esc(json.dumps(data))


async def _visible_agent(orch, user_id, agent_id: str):
    """The card for ``agent_id`` if this account may see it, else ``None``.

    Visibility is the directory's own rule — the agent is yours, or it is
    public — read through the same helpers the directory and the agents
    surface use, so the dialog can never open an agent the sidebar would not
    have listed. A lookup failure is treated as "not visible": failing closed
    costs a person one dialog, failing open leaks an agent's existence.
    """
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
    """The agent's own example prompts, bounded and sanity-checked.

    They come off the agent card, which the agent itself builds, so this is
    the agent describing its own use rather than the console guessing at it.
    """
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
    """One example row: what it shows, the prompt itself, Run and Load."""
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
    """The agent's declared tools as chips — capability, not permission."""
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
    """Render the agent dialog body.

    Args:
        orch: the orchestrator instance.
        user_id: the requesting user's id.
        roles: session roles (unused — every account may read a visible
            agent's description).
        params: ``{"agent_id": str}``.

    Returns:
        Body HTML for the chrome modal (escape-by-default).
    """
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
        # An agent with no examples is not broken, and an empty section would
        # say less than a sentence does.
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

    # The dialog's header already names the agent (see title()), so the body
    # leads with its state and what it does rather than repeating the name.
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
    """The same dialog for native clients, as astralprims components.

    Native targets get the description, the examples as ``chat_message``
    buttons (there is no composer to "load" into, so only Run is offered) and
    the tool list — the same content, expressed in the vocabulary every
    client already renders.
    """
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
