"""Generates the caller event vectors by running every refusal code, principal class and consent signal through caller_events.py.
`--check` fails when the committed file differs and writes nothing.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
caller_events = importlib.import_module("caller_events")

VECTORS = HERE.parent / "vectors"
NAME = "caller-events-v2.json"
SPEC = "106/contracts/caller-events.md"
SECRET_EVENT = {
    "signal": "consent.blocked",
    "request": "req-1",
    "action_digest": "sha256:" + "ab" * 32,
    "tool": "send_message",
    "arguments": {"to": "someone"},
    "approver": "sha256:" + "cd" * 32,
    "outcome": "approved",
}


def generate() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def add(name: str, kind: str, case_input: dict[str, Any]) -> None:
        cases.append({"name": name, "kind": kind, "input": case_input, "expect": caller_events.run(kind, case_input)})

    for _, _, codes in caller_events.CLASSES:
        for code in codes:
            add("classify_" + code.replace(".", "_"), "classify", {"code": code})
    for code in ("gate.permission_denied", "gate.mesh_stopped", "gate.anything_new", "caller.unknown_future_code", "", "gate"):
        add("classify_" + (code.replace(".", "_") or "empty"), "classify", {"code": code})
    for principal in caller_events.PRINCIPALS:
        for turns in (False, True):
            suffix = principal.replace("-", "_") + ("_turns" if turns else "_no_turns")
            add("consent_class_" + suffix, "consent_class", {"principal": principal, "turns": turns})
            for signal in caller_events.OWNER_SIGNALS + caller_events.CALLER_SIGNALS:
                add(
                    "visible_" + suffix + "_" + signal.replace(".", "_"),
                    "visible",
                    {"principal": principal, "turns": turns, "signal": signal},
                )
    for principal in caller_events.PRINCIPALS:
        for device in ("requesting-device", "another-device"):
            add(f"node_consent_{principal}_{device}", "node_consent_recipient",
                {"principal": principal, "device": device, "requester": "requesting-device"})
    add("node_consent_missing_requester", "node_consent_recipient",
        {"principal": "owner-device", "device": "requesting-device", "requester": ""})
    add("consent_class_unknown_principal", "consent_class", {"principal": "contact", "turns": False})
    add("consent_class_turns_not_boolean", "consent_class", {"principal": "caller-ticket", "turns": 1})
    add("visible_unknown_signal", "visible", {"principal": "owner-device", "turns": True, "signal": "ConsentApproved"})
    add("redact_drops_everything_but_signal_and_request", "redact", {"event": SECRET_EVENT})
    add("redact_resumed", "redact", {"event": {"signal": "consent.resumed", "request": "req-1", "outcome": "denied"}})
    add("redact_refuses_an_owner_signal", "redact", {"event": {"signal": "ConsentRequested", "request": "req-1"}})
    add("redact_refuses_a_missing_request", "redact", {"event": {"signal": "consent.blocked"}})
    return {
        "spec": SPEC,
        "format": caller_events.FORMAT,
        "notification_types": list(caller_events.NOTIFICATION_TYPES),
        "classes": [
            {"class": name, "status": status, "codes": list(codes)} for name, status, codes in caller_events.CLASSES
        ],
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    text = json.dumps(generate(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    path = VECTORS / NAME
    if arguments.check:
        if not path.exists() or path.read_text() != text:
            print(f"{NAME} differs from the generator output", file=sys.stderr)
            return 1
        return 0
    path.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
