"""Verification subjects retain their synthetic namespace under issuer-scoped owners.
The AstralPlane harness_cleanup repository removes only the run's scoped fixtures.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, List

logger = logging.getLogger("verification.isolation")

NAMESPACE_PREFIX = "__verif__"

@dataclass
class Principal:
    user_id: str
    roles: List[str] = field(default_factory=lambda: ["user"])

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def claims(self) -> dict:
        return {
            "sub": self.user_id,
            "preferred_username": self.user_id,
            "email": f"{self.user_id}@verif.local",
            "realm_access": {"roles": list(self.roles)},
            "resource_access": {"astral-frontend": {"roles": list(self.roles)}},
        }


def principal_id(run_id: str, persona: str, role: str = "primary") -> str:
    safe_run = run_id.replace(NAMESPACE_PREFIX, "")
    return f"{NAMESPACE_PREFIX}{safe_run}_{persona}_{role}"


def make_principal(run_id: str, persona: str, role: str = "primary",
                   roles: List[str] | None = None) -> Principal:
    return Principal(user_id=principal_id(run_id, persona, role), roles=roles or ["user"])


def is_harness_principal(user_id: str) -> bool:
    return bool(user_id) and user_id.startswith(NAMESPACE_PREFIX)


def require_issuer_scoped_cleanup(plane_repositories: Any) -> None:
    cleanup = getattr(plane_repositories, "harness_cleanup", None)
    purge_run = getattr(cleanup, "purge_run", None)
    if (not callable(purge_run)
            or "owner_issuer_hash" not in inspect.signature(purge_run).parameters):
        raise RuntimeError("Plane does not support issuer-scoped verification cleanup")


# No fallback path — missing deps must fail, never leave state behind.
def teardown(
    *,
    plane_runtime: Any,
    plane_repositories: Any,
    run_id: str,
    owner_issuer_hash: str | None = None,
) -> int:
    from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

    if not callable(getattr(plane_runtime, "transaction", None)):
        raise TypeError("verification teardown requires the application Plane runtime")
    cleanup = getattr(plane_repositories, "harness_cleanup", None)
    if not callable(getattr(cleanup, "purge_run", None)):
        raise TypeError(
            "verification teardown requires the application Plane "
            "harness_cleanup repository"
        )
    scope = {}
    if owner_issuer_hash is not None:
        require_issuer_scoped_cleanup(plane_repositories)
        scope["owner_issuer_hash"] = owner_issuer_hash
    with plane_runtime.transaction() as transaction:
        report = cleanup.purge_run(
            transaction,
            profile=HarnessCleanupProfile.VERIFICATION,
            run_id=run_id,
            **scope,
        )
        deleted = getattr(report, "total_deleted", None)
        if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
            raise RuntimeError("Plane harness cleanup returned an invalid deletion count")
    logger.info("teardown removed %d row(s) for verification run %s", deleted, run_id)
    return deleted
