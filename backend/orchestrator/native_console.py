"""Attaches the shared console model to negotiated authenticated native chrome.
It reuses web landing visibility and display identity without creating client authority.
"""

from orchestrator import web_landing
from orchestrator.web_auth import identity_from_claims
from rote.console import watch_availability
from webrender.chrome.console_model import CONSOLE_CONTRACT, build_console_model


async def attach_native_console(orch, menu: dict, claims: dict, profile) -> dict:
    kind = getattr(getattr(profile, "device_type", None), "value", None)
    if kind not in {"ios", "macos", "android", "watch", "windows"}:
        return menu
    if getattr(profile, "console_contract", None) != CONSOLE_CONTRACT:
        return menu
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return menu
    catalog = await web_landing.payload(orch, subject)
    console = build_console_model(catalog, menu, identity_from_claims(claims))
    if kind == "watch":
        capabilities = claims.get("_client_capabilities", ())
        for action in console["composer_actions"]:
            target = action.get("action", {})
            action["availability"] = watch_availability(
                target.get("surface"), target.get("params"), profile, capabilities)
        for agent in console["catalog"]["agents"]:
            agent["availability"] = watch_availability("agent_intro", {}, profile, capabilities)
    return {**menu, "console": console}
