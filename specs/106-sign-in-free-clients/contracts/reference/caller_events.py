"""Reference for caller-events.md: the class and status of every caller refusal code, the empty notification list, and which consent signal each principal class may see.
generate_caller_event_vectors.py runs every case through run(), and tests/test_caller_events.py holds the node's caller_tickets.py to the same code table.
"""

from __future__ import annotations

from typing import Any

FORMAT = "astral.caller-events/v1"
UNAUTHORIZED = (
    "node.bearer_refused",
    "caller.malformed",
    "caller.proof_malformed",
    "caller.bad_signature",
    "caller.proof_bad_signature",
)
FORBIDDEN = (
    "caller.mesh_mismatch",
    "caller.wrong_node",
    "caller.device_removed",
    "caller.wrong_minter",
    "caller.stopped",
    "caller.expired",
    "caller.not_yet_valid",
    "caller.revoked",
    "caller.proof_mismatch",
    "caller.proof_stale",
    "caller.replayed",
    "caller.tool_not_covered",
    "caller.tool_changed",
    "caller.tool_unavailable",
)
UNAVAILABLE = (
    "caller.replay_full",
    "mesh.not_enrolled",
    "node.development_mesh",
    "node.mesh_unavailable",
    "node.owner_unbound",
)
CLASSES = (("unauthorized", 401, UNAUTHORIZED), ("forbidden", 403, FORBIDDEN), ("unavailable", 503, UNAVAILABLE))
NOTIFICATION_TYPES: tuple[str, ...] = ()
PRINCIPALS = ("owner-device", "caller-ticket", "outside-service")
OWNER_SIGNALS = ("ConsentRequested", "ConsentResolved")
CALLER_SIGNALS = ("consent.blocked", "consent.resumed")
REDACTED_FIELDS = ("signal", "request")


class EventsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def classify(code: str) -> dict[str, Any]:
    for name, status, codes in CLASSES:
        if code in codes:
            return {"class": name, "status": status}
    if code.startswith("gate."):
        return {"class": "gate", "status": 503}
    return {"class": "opaque", "status": 500}


def _principal(principal: Any, turns: Any) -> None:
    if principal not in PRINCIPALS or not isinstance(turns, bool):
        raise EventsError("events.malformed")


def consent_class(principal: Any, turns: Any) -> str:
    _principal(principal, turns)
    if principal == "owner-device":
        return "full"
    if principal == "caller-ticket" and turns:
        return "own-redacted"
    return "none"


def visible(principal: Any, turns: Any, signal: Any) -> bool:
    if signal not in OWNER_SIGNALS + CALLER_SIGNALS:
        raise EventsError("events.malformed")
    visibility = consent_class(principal, turns)
    if visibility == "full":
        return signal in OWNER_SIGNALS
    if visibility == "own-redacted":
        return signal in CALLER_SIGNALS
    return False


def redact(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or event.get("signal") not in CALLER_SIGNALS or not isinstance(event.get("request"), str):
        raise EventsError("events.malformed")
    return {field: event[field] for field in REDACTED_FIELDS}


def run(kind: str, case: dict[str, Any]) -> dict[str, Any]:
    try:
        if kind == "classify":
            return classify(case["code"])
        if kind == "consent_class":
            return {"consent": consent_class(case["principal"], case["turns"])}
        if kind == "visible":
            return {"visible": visible(case["principal"], case["turns"], case["signal"])}
        if kind == "redact":
            return {"event": redact(case["event"])}
    except EventsError as error:
        return {"code": error.code}
    raise ValueError(kind)
