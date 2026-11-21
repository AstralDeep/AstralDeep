"""Fixture owner namespaces for the in-process harness: one active issuer-scoped namespace per verification run, with no token minting.
verification/drivers/in_process.py builds node-session claims for the owners it derives here.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from urllib.parse import urlsplit


class FixtureIdentity:
    _active = None

    def __init__(self, run_id: str, *, authority: str | None = None) -> None:
        if os.environ.get("ASTRAL_ENV") != "development":
            raise ValueError("synthetic verification IAM requires explicit development mode")
        if not run_id.startswith("__verif__"):
            raise ValueError("synthetic verification IAM requires a namespaced run")
        if type(self)._active is not None:
            raise RuntimeError("synthetic verification IAM already has an active owner")
        if authority is not None:
            parsed = urlsplit(authority)
            if (parsed.scheme != "https" or parsed.netloc != "verification.invalid"
                    or not parsed.path.startswith("/realms/") or parsed.query or parsed.fragment):
                raise ValueError("synthetic IAM issuer must remain on verification.invalid")
        self.run_id = run_id
        self.authority = authority or f"https://verification.invalid/realms/{uuid.uuid4().hex}"
        self.closed = False
        type(self)._active = self

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        type(self)._active = None



def fixture_issuer_digest(issuer: str) -> str:
    return hashlib.sha256(issuer.strip().rstrip("/").encode()).hexdigest()[:16]


def fixture_owner_id(issuer: str, subject: str) -> str:
    return f"fixture.{fixture_issuer_digest(issuer)}.{subject}"
