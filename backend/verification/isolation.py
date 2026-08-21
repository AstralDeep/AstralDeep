"""Namespaced principals + teardown (T007 / FR-031 / D14).

Every harness identity, chat, attachment, and draft is namespaced under a
``__verif__`` prefix so runs never collide with — or pollute — real user data.
Qualification teardown delegates the fixed deletion manifest and SQL to
AstralPlane using the composed application's transaction. ``audit_events`` are
append-only by design and remain, but only under namespaced principals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List

logger = logging.getLogger("verification.isolation")

NAMESPACE_PREFIX = "__verif__"

@dataclass
class Principal:
    """A namespaced authenticated identity used by the harness."""

    user_id: str
    roles: List[str] = field(default_factory=lambda: ["user"])

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def claims(self) -> dict:
        """JWT-shaped claims for in-process session registration."""
        return {
            "sub": self.user_id,
            "preferred_username": self.user_id,
            "email": f"{self.user_id}@verif.local",
            "realm_access": {"roles": list(self.roles)},
            "resource_access": {"astral-frontend": {"roles": list(self.roles)}},
        }


def principal_id(run_id: str, persona: str, role: str = "primary") -> str:
    """Deterministic namespaced user id for ``(run, persona, role)``."""
    safe_run = run_id.replace(NAMESPACE_PREFIX, "")
    return f"{NAMESPACE_PREFIX}{safe_run}_{persona}_{role}"


def make_principal(run_id: str, persona: str, role: str = "primary",
                   roles: List[str] | None = None) -> Principal:
    return Principal(user_id=principal_id(run_id, persona, role), roles=roles or ["user"])


def is_harness_principal(user_id: str) -> bool:
    return bool(user_id) and user_id.startswith(NAMESPACE_PREFIX)


def teardown(
    *,
    plane_runtime: Any,
    plane_repositories: Any,
    run_id: str,
) -> int:
    """Atomically purge every namespaced principal for ``run_id``.

    This qualification boundary accepts only the application's composed Plane
    runtime and catalog. It intentionally has no ``Database``/driver fallback:
    missing dependencies, invalid identities, schema drift, or cleanup failure
    must fail the verification run rather than leave state behind silently.
    Returns the number of rows AstralPlane reports deleted.
    """

    # External/API-only verification can still construct principals without a
    # local data-plane package; only in-process cleanup needs this typed enum.
    from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

    if not callable(getattr(plane_runtime, "transaction", None)):
        raise TypeError("verification teardown requires the application Plane runtime")
    cleanup = getattr(plane_repositories, "harness_cleanup", None)
    if not callable(getattr(cleanup, "purge_run", None)):
        raise TypeError(
            "verification teardown requires the application Plane "
            "harness_cleanup repository"
        )
    with plane_runtime.transaction() as transaction:
        report = cleanup.purge_run(
            transaction,
            profile=HarnessCleanupProfile.VERIFICATION,
            run_id=run_id,
        )
        deleted = getattr(report, "total_deleted", None)
        if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
            raise RuntimeError("Plane harness cleanup returned an invalid deletion count")
    logger.info("teardown removed %d row(s) for verification run %s", deleted, run_id)
    return deleted
