"""Allowlist of module:function caller sites still permitted to enter a synchronous
AstralPlane transaction from the asyncio event-loop thread, read by
tests/plugins/event_loop_guard.py.
"""

ALLOWED_SITES: list[str] = []


def allowed_sites() -> set[str]:
    return {entry.split(" -- ", 1)[0].strip() for entry in ALLOWED_SITES}
