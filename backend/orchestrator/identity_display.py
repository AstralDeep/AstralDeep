"""Derives the display name, role label and initials the chrome shows from a registered session's claims.
chrome_events.py and native_console.py call it; it reads no credential and grants nothing.
"""

from typing import Any

ANONYMOUS_IDENTITY = {"name": "Signed in", "role": "Guest", "initials": "A"}


def identity_from_claims(payload: Any, roles=None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    name = str(
        payload.get("name")
        or " ".join(
            part for part in (payload.get("given_name"), payload.get("family_name")) if part
        ).strip()
        or payload.get("preferred_username")
        or ""
    ).strip()
    if not name or "@" in name:
        name = ANONYMOUS_IDENTITY["name"]
    roles = list(roles or [])
    role = "Administrator" if "admin" in roles else ("Member" if roles else "Guest")
    initials = "".join(part[0] for part in name.split()[:2] if part).upper() or "A"
    return {"name": name, "role": role, "initials": initials}
