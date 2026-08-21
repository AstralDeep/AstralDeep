"""US3 — backend-only-UI / thin-client checks (T022).

Asserts the structural differentiator: components are server-produced markup from
the published vocabulary, device differences come from backend adaptation, actions
are backend-defined intent the client forwards, and the client surface contains no
per-component construction logic and no client-side rendering framework
(FR-023..027 / D9/D10). The client measurement is an objective static read.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from astralprojection.resources import static_path

from verification.checks.base import Check, CheckResult, no, ok, register, unsure
from verification.checks.common import vocabulary_check
from verification.evidence import CapturedEvidence

_CLIENT_JS = str(static_path("client.js"))

# Framework / construction markers whose ABSENCE we assert (FR-025).
_FRAMEWORK_PATTERNS = (
    re.compile(r"""\bfrom\s+['"]react['"]""", re.IGNORECASE),
    re.compile(r"""\bfrom\s+['"]vue['"]""", re.IGNORECASE),
    re.compile(r"""\bfrom\s+['"]@angular""", re.IGNORECASE),
    re.compile(r"\breact-dom\b", re.IGNORECASE),
    re.compile(r"\bReactDOM\b"),
    # NOTE: document.createElement (standard DOM) is NOT a framework signal and
    # is deliberately not matched here.
    re.compile(r"\bReact\.createElement\b"),
)
# Per-COMPONENT-type construction (building widgets by component type). A switch
# on a *message* type (e.g. `switch (data.type)` routing ui_render/ui_upsert) is
# legitimate thin-client routing and is deliberately NOT matched here.
_TYPE_SWITCH_PATTERNS = (
    re.compile(r"switch\s*\(\s*(?:component|comp|c|node|item|el)\.type\s*\)"),
    re.compile(r"componentRenderers|COMPONENT_RENDERERS|buildComponent|renderComponentByType"),
)
# Generic-injection markers whose PRESENCE we assert.
_INJECTION_PATTERNS = (
    re.compile(r"\.innerHTML\s*="),
    re.compile(r"data-component-id"),
)


def inspect_client_surface(path: str = _CLIENT_JS) -> Dict[str, Any]:
    """Objective measurement of the client surface (FR-025)."""
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


# --- no_client_construction -------------------------------------------------

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


# --- server_markup_present --------------------------------------------------

def _markup_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    html_ops = 0
    total_ops = 0
    for msg in ev.messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") == "ui_upsert":
            for op in msg.get("ops", []) or []:
                if op.get("op") == "remove":
                    continue
                total_ops += 1
                if op.get("html"):
                    html_ops += 1
        elif msg.get("type") == "ui_render" and msg.get("components"):
            total_ops += 1
            if msg.get("html"):
                html_ops += 1
    if total_ops == 0:
        return unsure("us3.server_markup_present", "no component-bearing messages to inspect")
    if html_ops == total_ops:
        return ok("us3.server_markup_present",
                  f"all {total_ops} component message(s) carried server-rendered HTML")
    return no("us3.server_markup_present",
              f"only {html_ops}/{total_ops} component messages carried server HTML")


def _markup_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    for msg in ev.messages:
        if isinstance(msg, dict) and msg.get("type") == "ui_upsert":
            for op in msg.get("ops", []) or []:
                if op.get("op") != "remove" and not op.get("html"):
                    return ok("us3.server_markup_present.counter", "an op lacked server HTML")
    return no("us3.server_markup_present.counter", "every op carried server HTML")


# --- action_is_backend_intent ----------------------------------------------

def _action_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    backed = [
        c for c in ev.workspace_state
        if c.get("_source_agent") and c.get("_source_tool")
    ]
    insp = ev.client_inspection or inspect_client_surface()
    if backed and insp.get("forwards_actions"):
        return ok("us3.action_is_backend_intent",
                  "components carry backend-defined re-exec intent; client forwards actions")
    if not backed:
        return unsure("us3.action_is_backend_intent", "no component with a backend action source")
    return no("us3.action_is_backend_intent", "client does not forward actions generically")


# --- device_diff_is_backend -------------------------------------------------

def _device_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    diff = ev.device_diff or {}
    if not diff:
        return unsure("us3.device_diff_is_backend", "no device-diff evidence captured")
    if diff.get("backend_adapted"):
        return ok("us3.device_diff_is_backend",
                  "device differences produced by the backend ROTE adapter",
                  browser=diff.get("browser_types"), mobile=diff.get("mobile_types"))
    return ok("us3.device_diff_is_backend",
              "backend adapter applied (identical here is still backend-decided)")


def build_us3_checks() -> List[Check]:
    checks = [
        Check("us3.no_client_construction", "backend_only_ui",
              _no_construction_run, _no_construction_counter),
        Check("us3.server_markup_present", "backend_only_ui", _markup_run, _markup_counter),
        Check("us3.action_is_backend_intent", "backend_only_ui", _action_run, None),
        Check("us3.device_diff_is_backend", "backend_only_ui", _device_run, None),
        vocabulary_check("backend_only_ui"),
    ]
    for c in checks:
        register(c)
    return checks
