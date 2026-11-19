"""Reference for caller-turn-door.md: validates the door declaration an assembly manifest carries
for the caller turn route, and the turn admission fields a request must satisfy before the bridge runs it."""

from __future__ import annotations

from typing import Any

TURN_TEXT_MIN = 1
TURN_TEXT_MAX = 65536
TURN_IDS = 1


def validate_door(door: dict[str, Any], controls: frozenset[str]) -> dict[str, Any]:
    if not isinstance(door, dict):
        raise ValueError("door.malformed")
    if door.get("principal") != "caller-ticket":
        raise ValueError("door.principal")
    if door.get("turns") is not True:
        raise ValueError("door.turns")
    if door.get("consent") != "own-redacted":
        raise ValueError("door.consent")
    if door.get("notification_types") != []:
        raise ValueError("door.notifications")
    required = {"consent-gate", "ticket-verification", "egress-allowlist"}
    missing = sorted(required - controls)
    if missing:
        raise ValueError("door.controls")
    return {"accepted": True, "required_controls": sorted(required)}


def admit_turn(text: Any, in_flight: int, cap: int, deadline_seconds: float) -> dict[str, Any]:
    if not isinstance(text, str) or not TURN_TEXT_MIN <= len(text.encode("utf-8")) <= TURN_TEXT_MAX:
        raise ValueError("caller.malformed")
    if in_flight >= cap:
        raise ValueError("caller.turn_limit")
    if deadline_seconds <= 0:
        raise ValueError("caller.turn_deadline")
    return {"admitted": True}


def run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    try:
        if kind == "validate_door":
            return validate_door(case["door"], frozenset(case["controls"]))
        if kind == "admit_turn":
            return admit_turn(case["text"], case["in_flight"], case["cap"], case["deadline_seconds"])
    except ValueError as error:
        return {"code": str(error)}
    raise ValueError(kind)
