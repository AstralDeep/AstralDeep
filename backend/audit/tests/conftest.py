"""Shared pytest fixtures for the audit test suite."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure the test process can import the backend modules
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("AUDIT_HMAC_SECRET", "pytest-audit-secret")
os.environ.setdefault("AUDIT_HMAC_KEY_ID", "k1")


@pytest.fixture(scope="session")
def database():
    """Isolated current AstralPlane database for the audit module."""

    from tests.helpers.voice_plane_runtime import isolated_plane_runtime

    with isolated_plane_runtime("audit_tests") as runtime:
        yield runtime


@pytest.fixture
def repo(database):
    """Fresh AuditRepository against the isolated Plane runtime."""
    from audit.repository import AuditRepository

    return AuditRepository(
        plane_runtime=database,
        plane_repositories=database.repositories,
    )


@pytest.fixture
def unique_user(request):
    """A unique ``actor_user_id`` for the calling test (avoids cross-test bleed)."""
    return f"pytest-{request.node.name}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def make_event():
    """Factory: returns an ``AuditEventCreate`` with sensible defaults."""
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
