"""Resolves the websockets client's renamed custom-header kwarg (extra_headers pre-14.0,
additional_headers from 14.0) via one signature probe. Used by orchestrator.py when
connecting outbound agent sockets.
"""

from __future__ import annotations

import inspect

import websockets


# Probed here, not try/except — the TypeError surfaces deep in asyncio
def _probe() -> str:
    try:
        params = inspect.signature(websockets.connect).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return "additional_headers"
    return "additional_headers" if "additional_headers" in params else "extra_headers"


WS_HEADER_KWARG = _probe()


def ws_header_kwargs(headers: dict) -> dict:
    if not headers:
        return {}
    return {WS_HEADER_KWARG: dict(headers)}
