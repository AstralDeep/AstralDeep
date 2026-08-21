"""Shared fixtures for backend/tests.

Hermeticity guard: a dev container's ``.env`` may export feature flags
globally (FF_UI_DESIGNER_* pass flags, FF_MOA_DEBATE, FF_HOOK_SYSTEM, ...),
but these suites assume the in-code defaults and monkeypatch only the flag
under test. CI sets none of them, so ambient values are stripped for the
whole session to make in-container runs behave identically to CI. Flag
values can also be cached by module-level state on first read, so the strip
must happen once up front, not per-test. Note the ui_designer prefix is
``FF_UI_DESIGNER_`` (trailing underscore): the master ``FF_UI_DESIGNER``
kill-switch is left alone.
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
    # Host runs load the repo ``.env`` lazily: ``orchestrator/orchestrator.py``
    # calls ``load_dotenv(override=False)`` on FIRST import, which happens
    # inside a test module — i.e. AFTER this strip. Worse, ``override=False``
    # only protects keys that still EXIST, so a stripped flag would be
    # re-injected by that later load. Load ``.env`` once NOW (so host runs
    # keep their DB coordinates etc.), strip, then disarm later loads —
    # exactly matching CI, where no ``.env`` file exists at all.
    # (In-container runs already have the flags as ambient process env, so
    # the early load is a no-op there and the strip behaves as before.)
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


# Run at collection import time (before any test module import can cache a
# flag read); tests that need a flag set it explicitly via monkeypatch.
_strip_ambient_flags()


@pytest.fixture
async def orchestrator_factory():
    """Construct real Orchestrators and release their application graph.

    The production runtime intentionally permits one application-scoped Plane
    binding.  Tests that historically constructed a fresh Orchestrator for
    every parameter case must therefore close the exact graph at fixture
    teardown instead of leaking process bindings and connection pools into the
    next case.
    """

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
    """Module-scoped counterpart for intentionally shared Orchestrators."""

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

from tests.plugins.event_loop_guard import event_loop_guard  # noqa: E402,F401
