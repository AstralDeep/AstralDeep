"""Pytest plugin wrapping AstralPlane's synchronous transaction boundary to catch
database calls made from the asyncio event-loop thread; reports by default, or raises
under LOOP_GUARD_ENFORCE unless the site is in loop_guard_allowlist.py.
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
    pass


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _caller_site() -> str:
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
    try:
        from astralplane import PlaneRuntime
    except ImportError:
        return

    for name in GUARDED_METHODS:
        current = getattr(PlaneRuntime, name, None)
        if current is None or getattr(current, "_loop_guard_wrapped", False):
            continue
        _originals[name] = current
        setattr(PlaneRuntime, name, _wrap(name, current))


def uninstall() -> None:
    if not _originals:
        return
    from astralplane import PlaneRuntime

    for name, original in _originals.items():
        if getattr(getattr(PlaneRuntime, name, None), "_loop_guard_wrapped", False):
            setattr(PlaneRuntime, name, original)
    _originals.clear()


@pytest.fixture(scope="session", autouse=True)
def event_loop_guard():
    install()
    yield
    uninstall()
