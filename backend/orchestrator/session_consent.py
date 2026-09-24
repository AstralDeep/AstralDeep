"""Resolves the exact signed-cookie session behind a durable offline-access grant,
checked after normal IAM already ran. Used by offline_grant.py and persistent_agents;
never falls back to token search or an owner's latest session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
import re
import time
from uuid import UUID

from astralplane.repositories.history import SessionConsentObservation
from starlette.requests import HTTPConnection

_SID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


@dataclass(frozen=True)
class ConsentSession:
    observation: SessionConsentObservation

    def reference(self, owner_id: str) -> dict:
        try:
            if not isinstance(self.observation, SessionConsentObservation):
                raise ValueError
            fence = self.observation.credential
            identity = UUID(fence.incarnation_id)
            if (fence.owner_id != owner_id or not isinstance(owner_id, str)
                    or not 1 <= len(owner_id) <= 256
                    or type(fence.version) is not int or fence.version != 2
                    or not isinstance(fence.session_id, str)
                    or _SID.fullmatch(fence.session_id) is None
                    or identity.version != 4 or str(identity) != fence.incarnation_id
                    or any(type(value) is not int or value < 0
                           for value in (fence.created_at, fence.interactive_anchor))
                    or self.observation.valid_until.timestamp() <= time.time()):
                raise ValueError
            return {"session_id": fence.session_id, "incarnation_id": fence.incarnation_id,
                    "created_at": fence.created_at, "interactive_anchor": fence.interactive_anchor}
        except (ValueError, TypeError, AttributeError):
            raise ValueError("consenting_session_unavailable") from None


async def select_consent_session(connection: HTTPConnection, *, principal: dict, store) -> ConsentSession | None:
    try:
        if (not isinstance(connection, HTTPConnection) or not isinstance(principal, dict)
                or connection.scope.get("method") == "OPTIONS"
                or principal.get("act") or any(principal.get(key) for key in
                    ("machine_turn_class", "machine_class", "_machine_turn", "delegated"))
                or os.getenv("USE_MOCK_AUTH", "").strip().lower() in {"true", "1", "yes"}):
            return None
        owner = principal.get("sub")
        expiry = principal.get("exp")
        if (not isinstance(owner, str) or not 1 <= len(owner) <= 256
                or type(expiry) not in (int, float) or not math.isfinite(expiry)
                or expiry <= time.time()):
            return None
        from orchestrator import web_auth
        cookies = [part.strip() for header in connection.headers.getlist("cookie")
                   for part in header.split(";")]
        values = [part.split("=", 1)[1] for part in cookies
                  if "=" in part and part.split("=", 1)[0].strip() == web_auth.COOKIE_NAME]
        if len(values) != 1 or len(values[0]) > 289:
            return None
        sid = web_auth._unsign(values[0])
        if not isinstance(sid, str) or _SID.fullmatch(sid) is None:
            return None
        observed = await asyncio.to_thread(
            store.capture_execution_reference, owner_id=owner, session_id=sid)
        state = observed.state
        fence = state.credential
        if (fence.owner_id != owner or fence.session_id != sid
                or type(fence.version) is not int or fence.version != 2):
            return None
        from datetime import datetime, timezone
        selected = ConsentSession(SessionConsentObservation(
            credential=fence, started_at=state.observed_at,
            valid_until=datetime.fromtimestamp(
                min(math.floor(expiry), fence.hard_expires_at, state.observed_at.timestamp() + 15), timezone.utc)))
        selected.reference(owner)
        return selected
    except Exception:
        # Never add diagnostics here: they'd leak into consent UI
        return None
