"""Namespaced harness principals + teardown (spec 047 FR-008, SC-007).

Every identity/chat/memory row the harness creates via the product path is
namespaced under ``__bench__`` so an adversarial corpus can never pollute — or
be confused with — real user data. Non-synthetic qualification runs purge their
namespace through AstralPlane's fixed-manifest cleanup repository and the
caller's application-scoped transaction. Synthetic mode creates no rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List

logger = logging.getLogger("security_benchmark.isolation")

NAMESPACE_PREFIX = "__bench__"

@dataclass
class Principal:
    """A namespaced authenticated identity used by the harness."""

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
    """Guard: refuse to operate on a non-namespaced principal (never touch real users)."""
    if NAMESPACE_PREFIX not in user_id:
        raise ValueError(
            f"refusing to operate on non-namespaced principal {user_id!r}: "
            f"harness principals MUST carry {NAMESPACE_PREFIX!r}"
        )


def teardown(
    *,
    plane_runtime: Any,
    plane_repositories: Any,
    run_id: str,
) -> int:
    """Atomically purge this qualification run through AstralPlane.

    The benchmark harness never borrows a driver connection or owns a second
    pool. Both the runtime and repository catalog must come from the composed
    application. Missing dependencies, invalid run identities, schema drift,
    and cleanup failures propagate so qualification cannot report success while
    leaving adversarial state behind. ``audit_events`` remain append-only.
    """

    # Keep corpus-only/synthetic benchmark runs independent of the data plane;
    # only the live cleanup boundary needs Plane's typed profile contract.
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
