"""Namespaces every identity/chat/memory row the harness creates under __bench__ so an
adversarial corpus can't pollute real user data; non-synthetic runs are purged
through AstralPlane's harness_cleanup repository.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List

logger = logging.getLogger("security_benchmark.isolation")

NAMESPACE_PREFIX = "__bench__"

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
            "email": f"{self.user_id}@bench.local",
            "realm_access": {"roles": list(self.roles)},
            "resource_access": {"astral-frontend": {"roles": list(self.roles)}},
        }


def principal_id(run_id: str, benchmark: str, role: str = "primary") -> str:
    base = run_id if run_id.startswith(NAMESPACE_PREFIX) else NAMESPACE_PREFIX + run_id
    return f"{base}__{benchmark}__{role}"


def assert_namespaced(user_id: str) -> None:
    if NAMESPACE_PREFIX not in user_id:
        raise ValueError(
            f"refusing to operate on non-namespaced principal {user_id!r}: "
            f"harness principals MUST carry {NAMESPACE_PREFIX!r}"
        )


# Failures must propagate, never hide leftover test data
def teardown(
    *,
    plane_runtime: Any,
    plane_repositories: Any,
    run_id: str,
) -> int:
    from astralplane.repositories.harness_cleanup import HarnessCleanupProfile

    if not callable(getattr(plane_runtime, "transaction", None)):
        raise TypeError("security benchmark teardown requires the application Plane runtime")
    cleanup = getattr(plane_repositories, "harness_cleanup", None)
    if not callable(getattr(cleanup, "purge_run", None)):
        raise TypeError(
            "security benchmark teardown requires the application Plane "
            "harness_cleanup repository"
        )
    with plane_runtime.transaction() as transaction:
        report = cleanup.purge_run(
            transaction,
            profile=HarnessCleanupProfile.SECURITY_BENCHMARK,
            run_id=run_id,
        )
        deleted = getattr(report, "total_deleted", None)
        if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
            raise RuntimeError("Plane harness cleanup returned an invalid deletion count")
    logger.info("teardown removed %d row(s) for benchmark run %s", deleted, run_id)
    return deleted
