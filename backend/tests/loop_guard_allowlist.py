"""Transitional allowlist for the event-loop blocking detector (feature 052).

Each entry is a ``"module:function"`` caller site temporarily permitted to
enter a synchronous AstralPlane transaction from the asyncio event-loop thread
during the migration window, optionally followed by `` -- <justification>``
(data, not a comment). This list MUST be empty at feature completion
(SC-005).
"""

ALLOWED_SITES: list[str] = []


def allowed_sites() -> set[str]:
    """The bare ``module:function`` sites, with any justification suffix stripped."""
    return {entry.split(" -- ", 1)[0].strip() for entry in ALLOWED_SITES}
