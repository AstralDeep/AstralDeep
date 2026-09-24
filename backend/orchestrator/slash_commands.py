"""Expands a leading /command typed in chat into an ordinary prompt before any
permission/audit gate runs, so slash commands never bypass them. Curated names take
precedence over user_skill_catalog's; unrecognized tokens pass through as text.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_COMMAND_NAME = re.compile(r"[a-z][a-z0-9_-]*")


def _t_help(_args: str, user_skills: Optional[Dict[str, Any]] = None) -> str:
    listing = "; ".join(f"{c['usage']} — {c['description']}" for c in _ordered())
    text = ("The user asked for help with slash commands. Briefly tell them the "
            f"available commands are: {listing}.")
    if user_skills:
        mine = "; ".join(f"/{name} — {skill.name}" for name, skill in sorted(user_skills.items()))
        text += (f" They also have their own skill commands: {mine}. Mention that they can "
                 "add or edit these under Settings → My agents & skills.")
    return text


def _t_agents(_args: str) -> str:
    return ("List the agents currently enabled for me and, in one line each, what "
            "they can help with. If none are enabled, say so and explain how to enable one.")


def _t_summarize(args: str) -> str:
    target = args or "the content I will provide next"
    return f"Please summarize the following clearly and concisely: {target}"


def _t_research(args: str) -> str:
    topic = args or "the topic I will provide next"
    return (f"Research the following and give me a concise, cited brief: {topic}. "
            "Use web research tools and do not fabricate sources.")


def _t_weather(args: str) -> str:
    where = args or "my location"
    return f"What's the current weather and short-term forecast for: {where}?"


def _t_download(_args: str) -> str:
    return ("The user wants to install the AstralDeep desktop app for Windows. "
            "Offer them the verified download card for the latest released "
            "version and briefly explain the install + sign-in steps. Never "
            "paste a download URL from memory or fabricate one.")


COMMANDS: Dict[str, Dict] = {
    "help": {"usage": "/help", "description": "show available commands", "template": _t_help},
    "agents": {"usage": "/agents", "description": "list your enabled agents", "template": _t_agents},
    "summarize": {"usage": "/summarize <url|text>", "description": "summarize a link or text", "template": _t_summarize},
    "research": {"usage": "/research <topic>", "description": "research + cited brief", "template": _t_research},
    "weather": {"usage": "/weather <location>", "description": "weather + forecast", "template": _t_weather},
    "download": {"usage": "/download", "description": "get the Windows desktop app", "template": _t_download},
}


def _ordered() -> List[Dict]:
    return [COMMANDS[n] for n in COMMANDS]


def command_list() -> List[Dict]:
    return [{"name": n, "usage": c["usage"], "description": c["description"]}
            for n, c in COMMANDS.items()]


def parse(message: str):
    if not message:
        return None
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None
    name = parts[0].lower()
    if not _COMMAND_NAME.fullmatch(name):
        return None
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


def expand_skill(skill: Any, args: str) -> str:
    body = str(getattr(skill, "instructions", "") or "").strip()
    name = str(getattr(skill, "name", "") or "your skill")
    request = args.strip() if args else ""
    text = (f'The user invoked their own skill "{name}". Follow these instructions of '
            f"theirs for this request:\n\n{body}\n\n")
    if request:
        text += f"Their request: {request}"
    else:
        text += ("They gave no further text — carry the skill out as written, asking for "
                 "any input it needs.")
    return text


def expand_message(message: str, user_skills: Optional[Dict[str, Any]] = None) -> str:
    parsed = parse(message)
    if parsed is None:
        return message
    name, args = parsed
    cmd = COMMANDS.get(name)
    if cmd is None and user_skills and name in user_skills:
        return expand_skill(user_skills[name], args)
    if cmd is None:
        listing = ", ".join(f"/{n}" for n in COMMANDS)
        if user_skills:
            listing += ", " + ", ".join(f"/{n}" for n in sorted(user_skills))
        return (f'The user typed an unrecognized command "/{name}". Briefly tell '
                f"them it isn't a known command and list the available ones: {listing}.")
    if name == "help":
        return _t_help(args, user_skills)
    return cmd["template"](args)


def reserved_names() -> List[str]:
    return list(COMMANDS)
