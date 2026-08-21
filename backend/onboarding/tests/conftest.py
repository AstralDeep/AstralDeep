"""Shared pytest fixtures for the onboarding test suite (feature 005)."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# Ensure the test process can import backend modules
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("AUDIT_HMAC_SECRET", "pytest-audit-secret")
os.environ.setdefault("AUDIT_HMAC_KEY_ID", "k1")


@pytest.fixture(scope="session")
def database():
    """Isolated current AstralPlane database for the onboarding module."""

    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("onboarding_tests") as runtime:
        yield runtime


@pytest.fixture
def audit_repo(database):
    from audit.repository import AuditRepository

    return AuditRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


@pytest.fixture
def onboarding_repo(database):
    from onboarding.repository import OnboardingRepository

    return OnboardingRepository(
        None,
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


@pytest.fixture
def unique_user(request):
    return f"pytest-{request.node.name}-{uuid.uuid4().hex[:8]}"


def _purge_pytest_rows(database):
    """Delete any pytest-namespaced rows from the tutorial tables (best-effort).

    Runs at BOTH session start and end so a previously *interrupted* session
    (whose teardown never ran) cannot leave orphaned ``pytest-%`` tutorial steps
    polluting the shared dev database / live tour. Tests MUST namespace every
    tutorial_step slug with the ``pytest-`` prefix for this to catch them.
    """
    try:
        with database.transaction() as transaction:
            transaction.execute(
                "DELETE FROM onboarding_state WHERE user_id LIKE 'pytest-%'"
            )
            transaction.execute(
                "DELETE FROM tutorial_step_revision "
                "WHERE editor_user_id LIKE 'pytest-%'"
            )
            transaction.execute(
                "DELETE FROM tutorial_step WHERE slug LIKE 'pytest-%'"
            )
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _final_pytest_cleanup(database):
    # Self-heal: purge leftovers from any prior interrupted session first…
    _purge_pytest_rows(database)
    yield
    # …and again on normal completion.
    _purge_pytest_rows(database)


@pytest.fixture(autouse=True)
def _isolate_onboarding_state(database, request):
    """Clean any onboarding rows left behind by a prior test for the same user.

    The fixture-level isolation here is name-spaced by test name so concurrent
    tests do not stomp on each other (test names are part of ``unique_user``).
    """
    yield
    try:
        with database.transaction() as transaction:
            transaction.execute(
                "DELETE FROM onboarding_state WHERE user_id LIKE %s",
                (f"pytest-{request.node.name}-%",),
            )
            transaction.execute(
                "DELETE FROM tutorial_step_revision "
                "WHERE editor_user_id LIKE %s",
                (f"pytest-{request.node.name}-%",),
            )
            # Test-created steps use slugs prefixed with the test name.
            transaction.execute(
                "DELETE FROM tutorial_step WHERE slug LIKE %s",
                (f"pytest-{request.node.name}-%",),
            )
    except Exception:
        pass
