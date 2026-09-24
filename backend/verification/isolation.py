"""Namespaced verification principals and teardown (AstralPlane harness_cleanup
repository): every harness identity, chat, attachment, and draft is prefixed so runs
never collide with or pollute real user data.
"""

from __future__ import annotations

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


# No fallback path — missing deps must fail, never leave state behind.
def teardown(
    *,
    plane_runtime: Any,
    plane_repositories: Any,
    run_id: str,
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
