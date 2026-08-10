"""Version-safe helpers for the ``websockets`` client API.

``websockets`` renamed the custom-handshake-header kwarg at **14.0** — not by
renaming a parameter, but by repointing the lazy ``websockets.connect`` alias
from ``.legacy.client`` (``extra_headers``) to ``.asyncio.client``
(``additional_headers``). There is no shim in either direction.

The wrong name does NOT fail cleanly: both ``connect`` classes end their
signature with ``**kwargs: Any`` and forward it verbatim to
``loop.create_connection``, so a mistaken kwarg sails through construction and
raises ``TypeError: BaseEventLoop.create_connection() got an unexpected keyword
argument …`` deep inside asyncio at *await* time, after a TCP attempt. A
``try/except TypeError`` around the call therefore does not work — hence the
one-time signature probe below.

This matters because ``backend/requirements.txt`` pins only floors
(``websockets>=12.0``) while the shipped image runs a much newer release, so the
supported range spans the 14.0 flip in both directions.
"""
from __future__ import annotations

import inspect

import websockets


def _probe() -> str:
    """Return the header kwarg name for the installed ``websockets``.

    ``websockets.connect`` is a class in every version 12.0 → 17.x, and
    :func:`inspect.signature` on a class introspects ``__init__`` and drops
    ``self``, so this single probe covers both eras.
    """
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return "additional_headers"
    return "additional_headers" if "additional_headers" in params else "extra_headers"


#: ``"additional_headers"`` on websockets >= 14.0, ``"extra_headers"`` on <= 13.x.
WS_HEADER_KWARG = _probe()


def ws_header_kwargs(headers: dict) -> dict:
    """``**ws_header_kwargs({...})`` — spread into ``websockets.connect``.

    Returns ``{}`` for an empty/absent header map so a caller can pass it
    unconditionally without sending an empty header block.
    """
    if not headers:
        return {}
    return {WS_HEADER_KWARG: dict(headers)}
