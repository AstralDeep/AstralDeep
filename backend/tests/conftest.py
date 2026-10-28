"""Shared pytest fixtures for backend/tests: strips ambient feature-flag env vars for
hermeticity, moves each pytest-xdist worker onto its own database (helpers/xdist_database.py),
builds/tears down real Orchestrator and credential/typesafe/data-sharing store fixtures, and
exposes the session-migrated Plane template that the engine `plane` fixture clones.
"""

from __future__ import annotations

import asyncio
import os

import pytest

_AMBIENT_FLAG_PREFIXES = ("FF_",)
_AMBIENT_FLAGS = (
    "DELEGATION_REQUIRED",
)


def _strip_ambient_flags() -> None:
    # Disarms load_dotenv so it can't re-inject stripped flags
    try:
        import dotenv
        dotenv.load_dotenv(override=False)
        dotenv.load_dotenv = lambda *a, **k: False
        dotenv.main.load_dotenv = dotenv.load_dotenv
    except Exception:
        pass
    for name in list(os.environ):
        if name.startswith(_AMBIENT_FLAG_PREFIXES) or name in _AMBIENT_FLAGS:
            del os.environ[name]


_strip_ambient_flags()

from tests.helpers.xdist_database import drop_worker_database, isolate_worker_database  # noqa: E402

WORKER_DATABASE = isolate_worker_database(os.environ)


def pytest_unconfigure(config):
    if WORKER_DATABASE is not None:
        drop_worker_database(*WORKER_DATABASE)


os.environ.setdefault("DB_POOL_MIN", "1")
os.environ.setdefault("DB_POOL_MAX", "1")
os.environ.setdefault("ASTRAL_TEST_PLANE_MAX_CONNECTIONS", "4")


@pytest.fixture
async def orchestrator_factory():
    instances = []

    def build():
        from orchestrator.orchestrator import Orchestrator

        instance = Orchestrator()
        instances.append(instance)
        return instance

    try:
        yield build
    finally:
        for instance in reversed(instances):
            await asyncio.wait_for(
                instance._close_started_services(),
                timeout=30.0,
            )


@pytest.fixture(scope="module")
async def orchestrator_module_factory():
    instances = []

    def build():
        from orchestrator.orchestrator import Orchestrator

        instance = Orchestrator()
        instances.append(instance)
        return instance

    try:
        yield build
    finally:
        for instance in reversed(instances):
            await asyncio.wait_for(
                instance._close_started_services(),
                timeout=30.0,
            )

@pytest.fixture
def user_skills_disabled(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "user_skills", False)


@pytest.fixture(autouse=True)
def _lightweight_default_phi_analyzer(monkeypatch):
    from types import SimpleNamespace

    from personalization import phi_gate
    monkeypatch.setattr(
        phi_gate,
        "_build_presidio_analyzer",
        lambda: SimpleNamespace(analyze=lambda **_kwargs: []),
    )


from tests.plugins.event_loop_guard import event_loop_guard  # noqa: E402,F401
from tests.helpers.plane_template import plane_template  # noqa: E402,F401


@pytest.fixture
def credential_plane_089():
    from llm_config.tests.conftest import CredentialPlaneFixture

    return CredentialPlaneFixture()


@pytest.fixture
def typesafe_store(monkeypatch, credential_plane_089):
    from cryptography.fernet import Fernet

    from llm_config.typesafe_store import TypeSafeCredentialStore

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return TypeSafeCredentialStore(
        plane_runtime=credential_plane_089,
        plane_repositories=credential_plane_089.repositories,
    )


@pytest.fixture
def data_sharing_store(credential_plane_089):
    from llm_config.data_sharing import DataSharingStore

    return DataSharingStore(
        plane_runtime=credential_plane_089,
        plane_repositories=credential_plane_089.repositories,
    )

from tests.helpers.completed_result import completed_seed  # noqa: E402,F401
