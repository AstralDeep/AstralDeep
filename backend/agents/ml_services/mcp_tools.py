#!/usr/bin/env python3
"""Union MCP tool registry for the ML Services agent: merges classify_tools.py,
forecaster_tools.py, and llm_factory_tools.py into one TOOL_REGISTRY and adds
_credentials_check, which probes all three optional credential bundles.
"""
import logging
import os
import sys
from typing import Any, Dict, Set

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from astralprims import Card, Table

from agents.ml_services import _wrapper, classify_tools, forecaster_tools, llm_factory_tools
from agents.ml_services._wrapper import ui as _ui

logger = logging.getLogger("MlServicesAgentMCPTools")

AGENT_ID = "ml-services-1"

LONG_RUNNING_TOOLS: Set[str] = (
    classify_tools.LONG_RUNNING_TOOLS
    | forecaster_tools.LONG_RUNNING_TOOLS
    | llm_factory_tools.LONG_RUNNING_TOOLS
)

_BUNDLE_PROBES = (
    ("classify", "CLASSify", _wrapper.CLASSIFY_BUNDLE, classify_tools._credentials_check),
    ("forecaster", "Forecaster", _wrapper.FORECASTER_BUNDLE, forecaster_tools._credentials_check),
    ("llm_factory", "LLM-Factory", _wrapper.LLM_FACTORY_BUNDLE, llm_factory_tools._credentials_check),
)

# Order is the precedence: auth_failed > unreachable > unexpected
_VERDICT_PRECEDENCE = ("auth_failed", "unreachable", "unexpected")


def _credentials_check(**kwargs) -> Dict[str, Any]:
    credentials = kwargs.get("_credentials", {}) or {}
    bundles: Dict[str, Dict[str, str]] = {}
    rows = []
    for key, label, bundle, probe in _BUNDLE_PROBES:
        if not _wrapper.bundle_configured(credentials, bundle):
            verdict = {
                "credential_test": "not_configured",
                "detail": f"{bundle.display_name} credentials are not saved.",
            }
        else:
            verdict = probe(**kwargs)
        bundles[key] = verdict
        rows.append([label, verdict.get("credential_test", "unexpected"),
                     verdict.get("detail") or "—"])

    configured = {
        key: v for key, v in bundles.items()
        if v.get("credential_test") != "not_configured"
    }
    summary = "; ".join(
        f"{label}: {bundles[key].get('credential_test')}"
        for key, label, _bundle, _probe in _BUNDLE_PROBES
    )
    if not configured:
        overall = "unexpected"
        detail = (
            "No ML Services credentials are configured. Save at least one "
            "service's URL and API key in the agent's settings."
        )
    else:
        overall = "ok"
        for level in _VERDICT_PRECEDENCE:
            if any(v.get("credential_test") == level for v in configured.values()):
                overall = level
                break
        detail = summary

    status_table = Table(headers=["Service", "Status", "Detail"], rows=rows)
    return _ui(
        [Card(title="ML Services credential status", content=[status_table])],
        data={"credential_test": overall, "detail": detail, "bundles": bundles},
    )


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "_credentials_check": {
        "function": _credentials_check,
        "description": (
            "Internal: probe the saved URL + API key of each configured service "
            "bundle (CLASSify, Forecaster, LLM-Factory) with a cheap authenticated "
            "GET and report per-bundle verdicts."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": True},
        "scope": "tools:read",
    },
    **classify_tools.TOOL_REGISTRY,
    **forecaster_tools.TOOL_REGISTRY,
    **llm_factory_tools.TOOL_REGISTRY,
}
