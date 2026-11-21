"""Checks retained assignment text against the host's privacy and injection gates."""

import asyncio

from personalization.phi_gate import get_phi_gate
from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.privacy import content_text, privacy_text


async def safe_text(text: str, urls: tuple[str, ...] = ()) -> None:
    from orchestrator.mas_defense import scan_message
    raw = content_text(text)
    if scan_message(text) or scan_message(raw):
        raise DispatchDenied("assignment_result_quarantined")
    if await asyncio.to_thread(get_phi_gate().contains_phi, privacy_text(raw, urls)):
        raise DispatchDenied("assignment_phi_refused")
