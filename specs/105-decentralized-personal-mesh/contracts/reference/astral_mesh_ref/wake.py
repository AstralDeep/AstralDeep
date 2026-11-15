"""Validates device-authored empty-wake delegations and folds each current device's newest route.
The caller supplies verified current-feed attribution; this module neither authenticates a gateway capability nor sends a push."""

from __future__ import annotations

import re
from typing import Any

from .errors import MeshError

MAX_CAPABILITY_BYTES = 2048
MAX_TTL_MS = 86_400_000
MAX_SAFE_EXPIRY = 2**53 - 1
CAPABILITY = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z", re.ASCII)


def validate(body: Any, signed_ms: int) -> dict | None:
    if type(signed_ms) is not int or signed_ms < 0:
        raise MeshError("wake.malformed")
    if not isinstance(body, dict) or set(body) != {"route"}:
        raise MeshError("wake.malformed")
    value = body["route"]
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"publisher", "capability", "expires_at_ms"}:
        raise MeshError("wake.malformed")
    capability, expiry = value["capability"], value["expires_at_ms"]
    if (value["publisher"] != "astral" or not isinstance(capability, str)
            or not capability.isascii() or len(capability) > MAX_CAPABILITY_BYTES
            or CAPABILITY.fullmatch(capability) is None
            or type(expiry) is not int or not 0 < expiry <= MAX_SAFE_EXPIRY
            or not 0 < expiry - signed_ms <= MAX_TTL_MS):
        raise MeshError("wake.malformed")
    return dict(value)


def fold(events: list[dict], current_device_of_feed: dict[str, str], now_ms: int) -> list[dict]:
    latest = {}
    def order(value):
        return value["clock"]["ms"], value["clock"]["n"], value["feed"], value["seq"]
    for value in events:
        if value["type"] != "device.wake_route" or value["labels"] != {"confidentiality": "private", "integrity": "owner"}:
            continue
        device = current_device_of_feed.get(value["feed"])
        if device is None:
            continue
        if device not in latest or order(value) > order(latest[device]):
            latest[device] = value
    routes = []
    for device, value in sorted(latest.items()):
        if value.get("state", "present") != "present":
            continue
        try:
            route = validate(value["body"], value["clock"]["ms"])
        except MeshError:
            continue
        if route is not None and route["expires_at_ms"] > now_ms:
            routes.append({"device_id": device, "route": route})
    return routes


def register(op):
    @op("wake_route_validate")
    def check(value):
        return {"route": validate(value["body"], value["signed_ms"])}

    @op("wake_route_fold")
    def routes(value):
        return {"routes": fold(value["events"], value["current_device_of_feed"], value["now_ms"])}
