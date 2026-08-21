"""Event-loop blocking detector for the test suite (feature 052, FR-017/SC-005).

Wraps the synchronous AstralPlane transaction boundary for the whole pytest
session so any durable call made from the asyncio event-loop thread is caught. Default is
report mode: the offending caller site and stack are recorded in ``OFFENDERS``
and a warning is logged once per unique site — the call still proceeds, so the
existing suite is unaffected. With ``LOOP_GUARD_ENFORCE=1`` in the environment
the guard raises ``BlockingDBOnEventLoop`` instead, unless the caller site
appears in ``tests.loop_guard_allowlist.ALLOWED_SITES``.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import os
import sys
import traceback

import pytest

from tests.loop_guard_allowlist import allowed_sites

logger = logging.getLogger("tests.event_loop_guard")

GUARDED_METHODS = ("transaction",)

OFFENDERS: list[dict] = []
_reported_sites: set[str] = set()
_originals: dict = {}


class BlockingDBOnEventLoop(Exception):
    """A synchronous Plane transaction entered on the asyncio event-loop thread."""


def _on_event_loop_thread() -> bool:
    """True when the current thread is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _caller_site() -> str:
    """``module:function`` of the nearest caller outside the DB layer and this guard."""
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not (
            module == __name__
            or module == "orchestrator.plane_repository_context"
            or module.startswith("astralplane.")
        ):
            return f"{module}:{frame.f_code.co_name}"
        frame = frame.f_back
    return "<unknown>:<unknown>"


def _flag_blocking_call(method_name: str) -> None:
    """Record or raise for a sync DB call detected on the event-loop thread."""
    site = _caller_site()
    if site in allowed_sites():
        return
    stack = "".join(traceback.format_stack(limit=30))
    if os.getenv("LOOP_GUARD_ENFORCE") == "1":
        raise BlockingDBOnEventLoop(
            f"synchronous PlaneRuntime.{method_name} called on the event-loop thread "
            f"at {site} (add to tests/loop_guard_allowlist.py only as a "
            f"transitional exemption)\n{stack}"
        )
    key = f"{method_name}@{site}"
    if key not in _reported_sites:
        _reported_sites.add(key)
        OFFENDERS.append({"method": method_name, "site": site, "stack": stack})
        logger.warning(
            "event-loop guard: synchronous PlaneRuntime.%s called on the event-loop "
            "thread at %s (report mode; set LOOP_GUARD_ENFORCE=1 to fail)",
            method_name, site,
        )


def _wrap(method_name: str, original):
    """Wrap a sync Plane context manager with the loop-thread check."""
    @contextmanager
    def wrapper(self, *args, **kwargs):
        if _on_event_loop_thread():
            _flag_blocking_call(method_name)
        with original(self, *args, **kwargs) as transaction:
            yield transaction
    wrapper.__name__ = getattr(original, "__name__", method_name)
    wrapper.__doc__ = getattr(original, "__doc__", None)
    wrapper._loop_guard_wrapped = True
    return wrapper


def install() -> None:
    """Idempotently install the guard wrapper on ``PlaneRuntime``."""
    from astralplane import PlaneRuntime

    for name in GUARDED_METHODS:
        current = getattr(PlaneRuntime, name, None)
        if current is None or getattr(current, "_loop_guard_wrapped", False):
            continue
        _originals[name] = current
        setattr(PlaneRuntime, name, _wrap(name, current))


def uninstall() -> None:
    """Restore the original ``PlaneRuntime`` method."""
    from astralplane import PlaneRuntime

    for name, original in _originals.items():
        if getattr(getattr(PlaneRuntime, name, None), "_loop_guard_wrapped", False):
            setattr(PlaneRuntime, name, original)
    _originals.clear()


@pytest.fixture(scope="session", autouse=True)
def event_loop_guard():
    """Session-wide autouse fixture installing the event-loop blocking detector."""
    install()
    yield
    uninstall()
