"""Tests for astral_sdk.tools and astral_sdk.models: they describe the same operation
shape as the conformance fixtures shared with
backend/tests/test_framework_conformance_088.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from astral_sdk.models import Operation
from astral_sdk.tools import ASTRAL_TOOLS, TOOL_NAMES

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "framework_conformance"


def test_fixtures_directory_exists_and_is_shared_with_the_backend_suite():
    assert FIXTURES_DIR.is_dir(), (
        f"{FIXTURES_DIR} is missing — it is generated/maintained alongside "
        "backend/tests/test_framework_conformance_088.py"
    )


def test_operation_fixture_parses_into_the_sdk_model():
    data = json.loads((FIXTURES_DIR / "submit_operation_result.json").read_text(encoding="utf-8"))
    operation = Operation.from_dict(data)
    assert operation.id == data["id"]
    assert operation.disposition == data["disposition"]
    assert operation.created is True


def test_tools_list_fixture_names_exactly_the_sdk_tool_set():
    data = json.loads((FIXTURES_DIR / "tools_list_result.json").read_text(encoding="utf-8"))
    fixture_names = {tool["name"] for tool in data["tools"]}
    assert fixture_names == set(TOOL_NAMES)


def test_human_only_refusal_fixture_names_commands_never_in_the_catalog():
    data = json.loads((FIXTURES_DIR / "human_only_refusal.json").read_text(encoding="utf-8"))
    assert data["safe_error_code"] == "assignment_human_required"
    for name in ("astral_decide_operation", "astral_reconcile_operation", "astral_delete_operation"):
        assert name not in ASTRAL_TOOLS
    facade_methods = {spec["facade_method"] for spec in ASTRAL_TOOLS.values()}
    for command in data["commands"]:
        assert command not in facade_methods
