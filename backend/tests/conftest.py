"""Shared pytest fixtures for backend/tests: strips ambient feature-flag env vars for
hermeticity and builds/tears down real Orchestrator and
credential/typesafe/data-sharing store fixtures.
"""

from __future__ import annotations

import asyncio
import os

import pytest

_AMBIENT_FLAG_PREFIXES = ("FF_UI_DESIGNER_",)
_AMBIENT_FLAGS = (
    "FF_ADAPTIVE_OBJECTIVES",
    "FF_MOA_DEBATE",
    "FF_HITL_HIGHRISK",
    "FF_HOOK_SYSTEM",
    "FF_RECURSIVE_DELEGATION",
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


from tests.plugins.event_loop_guard import event_loop_guard  # noqa: E402,F401


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
