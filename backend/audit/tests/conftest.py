"""Shared pytest fixtures for the audit test suite: an isolated AstralPlane database, a
bound AuditRepository, unique per-test actor ids, and an AuditEventCreate factory.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
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

    with isolated_plane_runtime("audit_tests") as runtime:
        yield runtime


@pytest.fixture
def repo(database):
    from audit.repository import AuditRepository

    return AuditRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


@pytest.fixture
def unique_user(request):
    return f"pytest-{request.node.name}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def make_event():
    from audit.schemas import AuditEventCreate

    def _make(**overrides):
        defaults = dict(
            actor_user_id="pytest-default",
            auth_principal="pytest-default",
            event_class="auth",
            action_type="auth.test",
            description="Test event",
            correlation_id=str(uuid.uuid4()),
            outcome="success",
            inputs_meta={"k": "v"},
            started_at=datetime.now(timezone.utc),
        )
        defaults.update(overrides)
        return AuditEventCreate(**defaults)

    return _make
