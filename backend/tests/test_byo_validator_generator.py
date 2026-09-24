"""Tests for the BYO static validator and generator
(backend/orchestrator/agent_validator.py, agent_generator.py): disallowed-import
detection, registry shape checks, and generation refusal without a configured LLM.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.agent_validator import (  # noqa: E402
    AgentSpecValidator,
    disallowed_imports,
    registry_from_source,
)
from orchestrator.agent_generator import AgentCodeGenerator  # noqa: E402


@pytest.fixture()
def v():
    return AgentSpecValidator()


def _reg(entry_src: str) -> str:
    return (
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        f"TOOL_REGISTRY = {entry_src}\n"
    )


def test_disallowed_imports_on_unparseable_code_returns_empty():
    assert disallowed_imports("def broken(:\n    pass") == []


def test_disallowed_imports_flags_relative_imports_as_unresolvable():
    bad = disallowed_imports("from . import helper\nfrom ..pkg import thing\n")
    assert "." in bad and "..pkg" in bad


def test_disallowed_imports_flags_a_third_party_from_import():
    bad = disallowed_imports("from somelib import Thing\nimport os\n")
    assert bad == ["somelib"]


def test_disallowed_imports_flags_a_third_party_root_once():
    assert disallowed_imports("import requests\nimport requests.adapters\n") == ["requests"]


@pytest.mark.parametrize("src", [
    '__import__("requests")',
    'importlib.import_module("requests")',
    'import_module("requests")',
    '__import__("requests.adapters")',
    'importlib.import_module(name="requests")',
])
def test_disallowed_imports_flags_dynamic_third_party(src):
    assert disallowed_imports(f"import importlib\nx = {src}\n") == ["requests"]


@pytest.mark.parametrize("src", [
    '__import__("json")',
    'importlib.import_module("xml.etree.ElementTree")',
    'importlib.import_module("astralprims")',
])
def test_disallowed_imports_allows_dynamic_stdlib_and_astralprims(src):
    assert disallowed_imports(f"import importlib\nx = {src}\n") == []


def test_disallowed_imports_flags_computed_module_name_as_unresolvable():
    from orchestrator.agent_validator import UNRESOLVABLE_DYNAMIC_IMPORT

    bad = disallowed_imports("import importlib\nm = importlib.import_module(name_var)\n")
    assert bad == [UNRESOLVABLE_DYNAMIC_IMPORT]


def test_disallowed_imports_ignores_unrelated_calls():
    assert disallowed_imports("x = compute('requests')\ny = obj.load('requests')\n") == []


def test_validate_static_rejects_a_dynamic_third_party_import(v):
    code = (
        "import importlib\n"
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    requests = importlib.import_module('requests')\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd', 'input_schema': {}}}\n"
    )
    report = v.validate_static(code, "byo")
    assert not report.passed
    assert any(f.category == "IMPORT" and "requests" in f.message
               for f in report.findings)


def test_validate_static_explains_a_computed_module_name(v):
    code = (
        "import importlib\n"
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    mod = importlib.import_module(kwargs['lib'])\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd', 'input_schema': {}}}\n"
    )
    report = v.validate_static(code, "byo")
    assert not report.passed
    assert any(f.category == "IMPORT" and "computed at runtime" in f.message
               for f in report.findings)


def test_registry_from_source_on_unparseable_code_returns_empty():
    assert registry_from_source("def (:") == {}


def test_validate_static_reports_a_syntax_error(v):
    report = v.validate_static("def broken(:\n    pass", "byo")
    assert not report.passed
    assert any(f.category == "IMPORT" and "Syntax error" in f.message
               for f in report.findings)


def test_validate_static_missing_function_key_is_an_error(v):
    report = v.validate_static(
        _reg("{'t': {'description': 'd', 'input_schema': {}, 'scope': 'tools:read'}}"), "byo")
    assert not report.passed
    assert any(f.category == "REGISTRY" and "Missing 'function' key" in f.message
               for f in report.findings)


def test_validate_static_function_not_defined_in_file_is_an_error(v):
    report = v.validate_static(
        _reg("{'t': {'function': nonexistent, 'description': 'd', "
             "'input_schema': {}, 'scope': 'tools:read'}}"), "byo")
    assert not report.passed
    assert any("not a function defined in this file" in f.message for f in report.findings)


def test_validate_static_warns_on_missing_description_schema_and_scope(v):
    report = v.validate_static(
        _reg("{'t': {'function': t}}"), "byo")
    msgs = " ".join(f.message for f in report.findings if f.severity == "warning")
    assert "Missing 'description'" in msgs
    assert "input_schema" in msgs
    assert "scope" in msgs
    assert report.passed and report.tools_passed == 1


def test_validate_static_registry_must_be_a_dict_literal(v):
    report = v.validate_static(_reg("build_registry()"), "byo")
    assert not report.passed
    assert any("must be a dict literal" in f.message for f in report.findings)


def test_validate_static_non_string_registry_key_is_rejected(v):
    report = v.validate_static(
        _reg("{123: {'function': t, 'input_schema': {}, 'scope': 'tools:read'}}"), "byo")
    assert not report.passed
    assert any("keys must be string literals" in f.message for f in report.findings)


def test_validate_static_entry_must_be_a_dict_literal(v):
    report = v.validate_static(_reg("{'t': 'not a dict'}"), "byo")
    assert not report.passed
    assert any("entry must be a dict literal" in f.message for f in report.findings)


def test_validate_static_no_usable_entries_when_every_key_is_non_string(v):
    report = v.validate_static(_reg("{123: {'function': t}}"), "byo")
    assert not report.passed
    assert any("no usable tool entries" in f.message or "string literals" in f.message
               for f in report.findings)


def test_validate_static_ignores_a_non_string_key_inside_an_entry(v):
    report = v.validate_static(
        _reg("{'t': {99: 'ignored', 'function': t, 'description': 'd', "
             "'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}"),
        "byo")
    assert report.passed and report.tools_passed == 1


def test_validate_static_ignores_non_literal_entry_values(v):
    report = v.validate_static(
        _reg("{'t': {'function': t, 'description': 'd', "
             "'input_schema': build_schema(), 'scope': 'tools:read'}}"), "byo")
    assert any("input_schema" in f.message for f in report.findings)
    assert report.tools_tested == 1


def test_validate_static_accepts_create_ui_response_return(v):
    code = (
        "from astralprims import Text, create_ui_response\n\n"
        "def t(**kwargs):\n"
        "    return create_ui_response([Text(content='x').to_dict()])\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd', "
        "'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"
    )
    report = v.validate_static(code, "byo")
    assert report.passed and report.tools_passed == 1


def test_validate_static_rejects_a_tool_that_never_returns_the_contract(v):
    code = (
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return 'just a string'\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd', "
        "'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"
    )
    report = v.validate_static(code, "byo")
    assert not report.passed
    assert any(f.category == "RETURN_FORMAT" for f in report.findings)


async def test_generate_tools_file_refuses_without_a_configured_llm():
    gen = AgentCodeGenerator()
    with pytest.raises(RuntimeError, match="LLM not configured"):
        await gen.generate_tools_file(
            agent_name="X", description="d",
            tools_spec=[{"name": "t", "description": "d"}], self_contained=True)


async def test_refine_tools_file_refuses_without_a_configured_llm():
    gen = AgentCodeGenerator()
    with pytest.raises(RuntimeError, match="LLM not configured"):
        await gen.refine_tools_file(
            current_code="TOOL_REGISTRY = {}", user_message="fix it",
            agent_name="X", description="d", self_contained=True)


async def test_aresolve_client_degrades_to_none_when_the_resolver_raises():
    def _boom():
        raise RuntimeError("resolver exploded")

    gen = AgentCodeGenerator(config_resolver=_boom)
    assert await gen._aresolve_client() == (None, None)
