"""Tangible, server-driven UI checks (backend/verification/checks/base.py, common.py):
component presence, file-derived provenance, persistence, re-executability, and
reader dispatch, each with an adversarial counter.
"""

from __future__ import annotations

from typing import Any, Dict, List

from verification.checks.base import Check, CheckResult, no, ok, register, unsure
from verification.checks.common import json_blob, vocabulary_check
from verification.evidence import CapturedEvidence

_NON_INTERACTIVE = {"alert", "divider", "text"}


def _rich_components(components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        c for c in components
        if isinstance(c, dict) and c.get("type") and c.get("type") not in _NON_INTERACTIVE
    ]


def _identity(c: Dict[str, Any]) -> str:
    return str(c.get("component_id") or "")


def _present_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not inputs.get("warrants_ui", True):
        return ok("us1.component_present", "prose acceptable for this query", warranted=False)
    rich = _rich_components(ev.components)
    if rich:
        return ok("us1.component_present", f"{len(rich)} interactive component(s)",
                  types=sorted({c["type"] for c in rich}))
    return no("us1.component_present", "no interactive component delivered for a UI-warranting query",
              delivered=sorted({c.get("type") for c in ev.components}))


def _present_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    comps = [c for c in ev.components if isinstance(c, dict)]
    if comps and all(c.get("type") == "alert" for c in comps):
        return ok("us1.component_present.counter", "all delivered components are alerts")
    return no("us1.component_present.counter", "genuine non-alert components present")


def _from_file_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not inputs.get("warrants_ui", True):
        return ok("us1.component_from_file", "not applicable (prose answer)")
    markers: List[str] = inputs.get("known_markers", []) or []
    if not markers:
        return unsure("us1.component_from_file", "no text markers for this fixture")
    blob = json_blob(ev.components)
    found = [m for m in markers if m in blob]
    if found:
        return ok("us1.component_from_file", f"file markers present in components: {found}",
                  found=found)
    return no("us1.component_from_file", f"no file markers found (expected one of {markers})")


def _from_file_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    markers: List[str] = inputs.get("known_markers", []) or []
    query: str = inputs.get("query", "") or ""
    blob = json_blob(ev.components)
    echoed = [m for m in markers if m in blob and m in query]
    specific = [m for m in markers if m in blob and m not in query]
    if specific:
        return no("us1.component_from_file.counter", f"file-specific markers present: {specific}")
    if echoed:
        return ok("us1.component_from_file.counter",
                  f"only query-echoed markers present: {echoed}")
    return no("us1.component_from_file.counter", "no markers to doubt")


def _persisted_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not inputs.get("warrants_ui", True):
        return ok("us1.persisted_with_identity", "no persistence required for prose")
    persisted = [c for c in ev.workspace_state if _identity(c).startswith(("wc_", "au_"))]
    if persisted:
        return ok("us1.persisted_with_identity",
                  f"{len(persisted)} persisted component(s) with stable identity",
                  ids=[_identity(c) for c in persisted])
    if not _rich_components(ev.components):
        return unsure("us1.persisted_with_identity", "no rich component was produced to persist")
    return no("us1.persisted_with_identity",
              "rich components delivered but none persisted with a stable identity")


def _persisted_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if _rich_components(ev.components) and not ev.workspace_state:
        return ok("us1.persisted_with_identity.counter", "delivered but workspace empty")
    return no("us1.persisted_with_identity.counter", "workspace reflects delivered components")


def _reexec_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not inputs.get("warrants_ui", True):
        return ok("us1.re_executable", "not applicable for prose")
    candidates = [
        c for c in ev.workspace_state
        if c.get("_source_agent") and c.get("_source_tool")
    ]
    if candidates:
        return ok("us1.re_executable",
                  f"{len(candidates)} component(s) carry a re-executable source",
                  sources=sorted({(c.get("_source_tool")) for c in candidates}))
    if not ev.workspace_state:
        return unsure("us1.re_executable", "nothing persisted to re-execute")
    return no("us1.re_executable", "no persisted component carries a source tool for re-execution")


def _reexec_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if ev.workspace_state and not any(
        c.get("_source_agent") and c.get("_source_tool") for c in ev.workspace_state
    ):
        return ok("us1.re_executable.counter", "persisted components lack a source tool")
    return no("us1.re_executable.counter", "a re-executable source is present")


def _reload_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not inputs.get("warrants_ui", True):
        return ok("us1.survives_reload", "not applicable for prose")
    if any(_identity(c).startswith(("wc_", "au_")) for c in ev.workspace_state):
        return ok("us1.survives_reload", "workspace re-read returns identified components")
    if not _rich_components(ev.components):
        return unsure("us1.survives_reload", "no rich component to persist")
    return no("us1.survives_reload", "workspace re-read does not return the components")


def _reader_dispatched_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    succeeded = [
        r for r in ev.audit_rows
        if r.get("event_class") == "agent_tool_call" and r.get("outcome") == "success"
    ]
    if succeeded:
        return ok("us1.reader_dispatched",
                  f"{len(succeeded)} successful tool dispatch(es) recorded",
                  actions=sorted({r.get("action_type") for r in succeeded}))
    return no("us1.reader_dispatched", "no successful agent_tool_call recorded in the audit trail")


def _reader_dispatched_counter(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if not any(r.get("event_class") == "agent_tool_call" for r in ev.audit_rows):
        return ok("us1.reader_dispatched.counter", "no tool dispatch audited at all")
    return no("us1.reader_dispatched.counter", "tool dispatch is audited")


def _autoparse_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    status = (ev.extra or {}).get("parser_status")
    expected = {"pending_admin_approval", "preparing", "unavailable", "covered"}
    if status in expected:
        return ok("us1.autoparse_safe", f"safe auto-parse outcome observed: {status}",
                  status=status)
    return no("us1.autoparse_safe", f"unexpected coverage outcome: {status!r}")


def _phi_run(ev: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    if (ev.extra or {}).get("synthetic_only") is False:
        return no("us1.health_data_synthetic_only", "non-synthetic medical input detected")
    return ok("us1.health_data_synthetic_only",
              "medical scenario processed synthetic-only content")


def build_us1_checks() -> List[Check]:
    checks = [
        Check("us1.component_present", "tangible_ui", _present_run, _present_counter),
        Check("us1.component_from_file", "tangible_ui", _from_file_run, _from_file_counter),
        Check("us1.persisted_with_identity", "tangible_ui", _persisted_run, _persisted_counter),
        Check("us1.re_executable", "tangible_ui", _reexec_run, _reexec_counter),
        Check("us1.survives_reload", "tangible_ui", _reload_run, None),
        Check("us1.reader_dispatched", "tangible_ui", _reader_dispatched_run,
              _reader_dispatched_counter),
        vocabulary_check("tangible_ui"),
    ]
    for c in checks:
        register(c)
    return checks


AUTOPARSE_CHECK = Check("us1.autoparse_safe", "tangible_ui", _autoparse_run, None)
PHI_CHECK = Check("us1.health_data_synthetic_only", "tangible_ui", _phi_run, None)
register(AUTOPARSE_CHECK)
register(PHI_CHECK)
