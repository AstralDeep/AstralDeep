"""Backend-only-UI / thin-client check (backend/verification/checks/base.py,
astralprojection.resources): asserts client.js has no per-component construction
logic or rendering framework, only server markup and backend-defined actions.
"""

from __future__ import annotations

import re
from typing import Any, Dict

from astralprojection.resources import static_path

from verification.checks.base import Check, CheckResult, no, ok, register, unsure
from verification.evidence import CapturedEvidence

_CLIENT_JS = str(static_path("client.js"))

_FRAMEWORK_PATTERNS = (
    re.compile(r"""\bfrom\s+['"]react['"]""", re.IGNORECASE),
    re.compile(r"""\bfrom\s+['"]vue['"]""", re.IGNORECASE),
    re.compile(r"""\bfrom\s+['"]@angular""", re.IGNORECASE),
    re.compile(r"\breact-dom\b", re.IGNORECASE),
    re.compile(r"\bReactDOM\b"),
    re.compile(r"\bReact\.createElement\b"),
)
_TYPE_SWITCH_PATTERNS = (
    re.compile(r"switch\s*\(\s*(?:component|comp|c|node|item|el)\.type\s*\)"),
    re.compile(r"componentRenderers|COMPONENT_RENDERERS|buildComponent|renderComponentByType"),
)
_INJECTION_PATTERNS = (
    re.compile(r"\.innerHTML\s*="),
    re.compile(r"data-component-id"),
)


def inspect_client_surface(path: str = _CLIENT_JS) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return {"readable": False, "error": str(exc), "path": path}
    framework = any(p.search(src) for p in _FRAMEWORK_PATTERNS)
    type_switch = any(p.search(src) for p in _TYPE_SWITCH_PATTERNS)
    injects = all(p.search(src) for p in _INJECTION_PATTERNS)
    forwards = bool(re.search(r"ui_event", src))
    return {
        "readable": True,
        "path": path,
        "lines": src.count("\n") + 1,
        "framework_import": framework,
        "type_switch": type_switch,
        "injects_server_html": injects,
        "forwards_actions": forwards,
    }


def _no_construction_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    insp = ev.client_inspection or inspect_client_surface()
    if not insp.get("readable"):
        return unsure("us3.no_client_construction", f"client surface unreadable: {insp.get('error')}")
    if insp["framework_import"] or insp["type_switch"]:
        return no("us3.no_client_construction",
                  "client contains a rendering framework or per-type construction logic",
                  measurement=insp)
    if insp["injects_server_html"] and insp["forwards_actions"]:
        return ok("us3.no_client_construction",
                  "client only injects server HTML and forwards actions", measurement=insp)
    return unsure("us3.no_client_construction",
                  "client lacks the expected generic-injection markers", measurement=insp)


def _no_construction_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    insp = ev.client_inspection or inspect_client_surface()
    if insp.get("framework_import") or insp.get("type_switch"):
        return ok("us3.no_client_construction.counter", "found framework/construction logic")
    return no("us3.no_client_construction.counter", "no framework/construction logic found")


NO_CLIENT_CONSTRUCTION = register(Check(
    "us3.no_client_construction", "backend_only_ui",
    _no_construction_run, _no_construction_counter,
))
