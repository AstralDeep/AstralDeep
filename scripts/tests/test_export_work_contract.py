"""Tests for scripts/export_work_contract.py (feature 088 T050).

Pins that the checked-in ``sdk/astral_sdk/work_contract.json`` is exactly what
running the exporter against the CURRENT backend produces (drift guard for
the one shared source between the server's MCP projection and the SDK), and
that the exporter's own shape (keys, tool list, scope set) matches what
``orchestrator.mcp_projection``/``orchestrator.work_operations`` actually
define today.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_work_contract.py"
CHECKED_IN = REPO_ROOT / "sdk" / "astral_sdk" / "work_contract.json"

spec = importlib.util.spec_from_file_location("export_work_contract", SCRIPT)
assert spec and spec.loader
export_work_contract = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = export_work_contract
spec.loader.exec_module(export_work_contract)  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _backend_on_path():
    backend = str(REPO_ROOT / "backend")
    added = backend not in sys.path
    if added:
        sys.path.insert(0, backend)
    yield
    if added:
        sys.path.remove(backend)


def test_checked_in_contract_is_not_stale():
    """The committed JSON must equal a fresh export byte-for-byte (drift pin)."""
    contract = export_work_contract.build_contract()
    fresh_text = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    assert CHECKED_IN.exists(), "sdk/astral_sdk/work_contract.json is missing; run the exporter"
    assert CHECKED_IN.read_text(encoding="utf-8") == fresh_text


def test_check_mode_exits_zero_against_the_checked_in_file():
    assert export_work_contract.main(["--check", "--out", str(CHECKED_IN)]) == 0


def test_check_mode_reports_drift(tmp_path):
    stale = tmp_path / "stale.json"
    stale.write_text("{}\n", encoding="utf-8")
    assert export_work_contract.main(["--check", "--out", str(stale)]) == 1
    # --check must never write, even on drift.
    assert stale.read_text(encoding="utf-8") == "{}\n"


def test_writing_to_a_fresh_path_round_trips(tmp_path):
    out = tmp_path / "nested" / "contract.json"
    assert export_work_contract.main(["--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written == export_work_contract.build_contract()


def test_contract_names_exactly_the_dispatchable_work_tools():
    from orchestrator.work_operations import DISPATCHABLE_TOOL_NAMES

    contract = export_work_contract.build_contract()
    assert set(contract["tools"]) == set(DISPATCHABLE_TOOL_NAMES)
    assert contract["dispatchable_tool_names"] == list(DISPATCHABLE_TOOL_NAMES)
    for name, spec_ in contract["tools"].items():
        assert spec_["scope"] in contract["framework_credential_scopes"]
        assert isinstance(spec_["input_schema"], dict)
        assert spec_["facade_method"]


def test_contract_never_names_a_human_only_or_unwired_command():
    contract = export_work_contract.build_contract()
    for forbidden in ("decide", "reconcile", "delete", "resume", "wait", "wake"):
        assert forbidden not in contract["tools"]
        assert not any(t["facade_method"] == forbidden for t in contract["tools"].values())


def test_contract_scope_set_matches_the_plane_framework_credential_scopes():
    from astralplane.repositories.framework_credentials import FRAMEWORK_CREDENTIAL_SCOPES

    contract = export_work_contract.build_contract()
    assert contract["framework_credential_scopes"] == sorted(FRAMEWORK_CREDENTIAL_SCOPES)


def test_contract_mcp_section_matches_live_constants():
    from orchestrator.mcp_authz import MCP_AUDIENCE, MCP_SCOPES
    from shared.protocol import MCP_PROTOCOL_VERSION

    contract = export_work_contract.build_contract()
    assert contract["mcp"]["protocol_version"] == MCP_PROTOCOL_VERSION
    assert contract["mcp"]["audience"] == MCP_AUDIENCE
    assert contract["mcp"]["entry_scopes"] == list(MCP_SCOPES)


def test_output_is_deterministic_across_two_runs():
    first = json.dumps(export_work_contract.build_contract(), sort_keys=True)
    second = json.dumps(export_work_contract.build_contract(), sort_keys=True)
    assert first == second
