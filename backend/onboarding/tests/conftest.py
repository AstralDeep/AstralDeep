"""Shared pytest fixtures for the onboarding suite: an isolated Plane database,
onboarding_repo/audit_repo builders, and pytest-namespaced row cleanup so tests never
pollute the shared dev database or live tour.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("AUDIT_HMAC_SECRET", "pytest-audit-secret")
os.environ.setdefault("AUDIT_HMAC_KEY_ID", "k1")


@pytest.fixture(scope="session")
def database():
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


# Only slugs prefixed pytest- get swept; others leak in
def _purge_pytest_rows(database):
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
    _purge_pytest_rows(database)
    yield
    _purge_pytest_rows(database)


@pytest.fixture(autouse=True)
def _isolate_onboarding_state(database, request):
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
            transaction.execute(
                "DELETE FROM tutorial_step WHERE slug LIKE %s",
                (f"pytest-{request.node.name}-%",),
            )
    except Exception:
        pass
