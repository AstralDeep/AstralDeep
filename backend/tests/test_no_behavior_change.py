"""Tests for agents' mcp_tools TOOL_REGISTRY and
backend/orchestrator/agent_validator.py: registry entries keep their
function/description/input_schema/scope shape, and the codegen validator accepts
astralprims imports and warns without one.
"""

import importlib
import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

AGENT_TOOL_MODULES = [
    "agents.general.mcp_tools",
    "agents.weather.mcp_tools",
    "agents.journal_review.mcp_tools",
    "agents.ml_services.mcp_tools",
]


@pytest.mark.parametrize("modname", AGENT_TOOL_MODULES)
def test_agent_tool_module_imports_and_registry_intact(modname):
    try:
        mod = importlib.import_module(modname)
    except ImportError as e:
        msg = str(e).lower()
        assert "astralprims" not in msg and "primitives" not in msg, (
            f"{modname} failed to import due to the primitives migration: {e}")
        pytest.skip(f"{modname} unimportable for an unrelated (env) reason: {e}")
        return

    registry = getattr(mod, "TOOL_REGISTRY", None)
    if registry is None:
        pytest.skip(f"{modname} has no TOOL_REGISTRY")
        return
    assert isinstance(registry, dict) and registry, f"{modname} TOOL_REGISTRY empty"
    for tool_name, spec in registry.items():
        assert callable(spec.get("function")), f"{modname}:{tool_name} missing function"
        assert spec.get("description"), f"{modname}:{tool_name} missing description"
        assert "input_schema" in spec, f"{modname}:{tool_name} missing input_schema"
        assert "scope" in spec, f"{modname}:{tool_name} missing scope (permission surface)"


def test_codegen_validator_accepts_astralprims_and_warns_without():
    from orchestrator.agent_validator import AgentSpecValidator, ValidationReport, ValidationSeverity

    v = AgentSpecValidator()

    def warnings_for(code):
        report = ValidationReport()
        v._validate_imports(code, report)
        return [i for i in report.findings if i.severity == ValidationSeverity.WARNING and i.category == "IMPORT"]

    assert not warnings_for("from astralprims import Card, Text\n"), \
        "validator should accept astralprims imports"
    assert warnings_for("x = 1\n"), "validator should warn when no primitive import is present"
