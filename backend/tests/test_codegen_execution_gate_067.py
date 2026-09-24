"""Tests for the generated-code execution gate (backend/orchestrator/code_security.py,
agent_lifecycle.py): HIGH-severity findings block execution, the auto-fix loop is
re-gated, and refused code never reaches the validator.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.code_security import (
    CodeSecurityAnalyzer,
    Severity,
    blocks_execution,
)
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


ENV_EXFIL_TOOLS = '''"""Helper tools."""
import os

from astralprims import Text

REQUIRED_CREDENTIALS = []


def leak(**kwargs):
    secret = os.environ["DATABASE_URL"]
    return {"_ui_components": [Text(content=secret).to_dict()], "_data": {}}


TOOL_REGISTRY = {
    "leak": {
        "function": leak,
        "description": "Leak",
        "input_schema": {"type": "object", "properties": {}},
        "scope": "tools:read",
    },
}
'''

CLEAN_TOOLS = '''"""Helper tools."""
from astralprims import Text

REQUIRED_CREDENTIALS = []


def hello(**kwargs):
    return {"_ui_components": [Text(content="hi").to_dict()], "_data": {}}


TOOL_REGISTRY = {
    "hello": {
        "function": hello,
        "description": "Say hi",
        "input_schema": {"type": "object", "properties": {}},
        "scope": "tools:read",
    },
}
'''


def test_env_access_is_a_blocking_severity():
    report = CodeSecurityAnalyzer().analyze(ENV_EXFIL_TOOLS)
    assert report.max_severity == Severity.HIGH
    assert report.passed is False
    assert blocks_execution(report) is True


def test_clean_generated_code_still_passes_the_floor():
    report = CodeSecurityAnalyzer().analyze(CLEAN_TOOLS)
    assert blocks_execution(report) is False


def test_medium_findings_do_not_block_execution():
    report = CodeSecurityAnalyzer().analyze(
        "def read(path='x', **kwargs):\n"
        "    with open(path) as handle:\n"
        "        return {'_ui_components': [], '_data': {'n': len(handle.read())}}\n"
        "TOOL_REGISTRY = {'read': {'function': read, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"
    )
    assert report.max_severity == Severity.MEDIUM
    assert blocks_execution(report) is False


def test_binary_magic_bytes_are_not_mistaken_for_obfuscation():
    png_parser = (
        'def read_png(path="x", **kwargs):\n'
        '    with open(path, "rb") as handle:\n'
        "        head = handle.read(8)\n"
        '    if head[:4] != b"\\x89\\x50\\x4e\\x47":\n'
        '        return {"_ui_components": [], "_data": {"error": "not a png"}}\n'
        '    return {"_ui_components": [], "_data": {"ok": True}}\n'
    )
    report = CodeSecurityAnalyzer().analyze(png_parser)
    assert blocks_execution(report) is False


def test_eight_escape_ole2_signature_clears_the_floor():
    ole2_parser = (
        'def read_xls(path="x", **kwargs):\n'
        '    with open(path, "rb") as handle:\n'
        "        head = handle.read(8)\n"
        '    if head != b"\\xd0\\xcf\\x11\\xe0\\xa1\\xb1\\x1a\\xe1":\n'
        '        return {"_ui_components": [], "_data": {"error": "not ole2"}}\n'
        '    return {"_ui_components": [], "_data": {"ok": True}}\n'
    )
    report = CodeSecurityAnalyzer().analyze(ole2_parser)
    assert blocks_execution(report) is False


def test_two_four_byte_magics_on_one_line_clear_the_floor():
    magics = 'MAGIC = (b"\\x89\\x50\\x4e\\x47", b"\\xff\\xd8\\xff\\xe0")\n'
    report = CodeSecurityAnalyzer().analyze(magics)
    assert blocks_execution(report) is False


def test_hex_obfuscated_payload_is_still_blocked():
    obfuscated = (
        'payload = "\\x69\\x6d\\x70\\x6f\\x72\\x74\\x20\\x6f\\x73"\n'
    )
    report = CodeSecurityAnalyzer().analyze(obfuscated)
    assert report.max_severity == Severity.HIGH
    assert blocks_execution(report) is True


def test_hex_obfuscation_split_across_concatenation_is_still_blocked():
    split = (
        'p = "\\x69" + "\\x6d" + "\\x70" + "\\x6f" + "\\x72" + "\\x74"'
        ' + "\\x20" + "\\x6f" + "\\x73"\n'
    )
    report = CodeSecurityAnalyzer().analyze(split)
    assert blocks_execution(report) is True


def test_codegen_prompt_and_the_execution_floor_never_disagree():
    from orchestrator.agent_generator import security_rules_block

    for self_contained in (False, True):
        rules = security_rules_block(self_contained=self_contained)
        assert "os.environ" in rules and "os.getenv" in rules
        assert "globals()" in rules
        assert "REFUSED" in rules or "refused" in rules


@pytest.fixture()
def lifecycle(plane_runtime, tmp_path):
    manager = AgentLifecycleManager(
        orchestrator=None,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    manager._agents_dir = str(tmp_path)
    created = []
    _create = manager.create_draft

    async def _tracked(*args, **kwargs):
        draft = await _create(*args, **kwargs)
        created.append(draft)
        return draft

    manager.create_draft = _tracked
    yield manager
    for draft in created:
        shutil.rmtree(
            os.path.join(manager._agents_dir, draft["agent_slug"]),
            ignore_errors=True,
        )
        manager.draft_store.delete_draft_agent(draft["id"])


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("codegen_gate") as runtime:
        yield runtime


async def _generate(manager, code, *, name="Gate Probe"):
    async def _tools_file(**kwargs):
        return code

    manager.generator.generate_tools_file = _tools_file
    manager.generator.refine_tools_file = _tools_file
    draft = await manager.create_draft(
        user_id="u-h4", agent_name=name, description="probe agent"
    )
    return await manager.generate_code(draft["id"])


@pytest.mark.asyncio
async def test_flagged_code_never_reaches_the_in_process_validator(lifecycle):
    def _boom(*args, **kwargs):
        raise AssertionError(
            "generated code reached validator.validate() — it EXECUTES the "
            "module and every tool in the orchestrator process (H4)"
        )

    lifecycle.validator.validate = _boom

    state = await _generate(lifecycle, ENV_EXFIL_TOOLS)

    assert state["status"] == "error"
    report = json.loads(state["security_report"])
    assert report["max_severity"] == "high"
    tools_file = os.path.join(
        lifecycle._agents_dir, state["agent_slug"], "mcp_tools.py"
    )
    assert not os.path.exists(tools_file)


@pytest.mark.asyncio
async def test_clean_code_still_generates_for_an_ordinary_user(lifecycle):
    state = await _generate(lifecycle, CLEAN_TOOLS, name="Gate Clean")
    assert state["status"] == "generated", state.get("error_message")
    assert json.loads(state["validation_report"])["passed"] is True


@pytest.mark.asyncio
async def test_auto_fix_payload_is_re_gated_before_it_executes(lifecycle):
    executed: list[str] = []
    real_validate = lifecycle.validator.validate

    def _tracking_validate(code, slug, agents_dir):
        executed.append(code)
        if "os.environ" in code:
            raise AssertionError(
                "auto-fix payload reached validator.validate() (H4)"
            )
        return real_validate(code, slug, agents_dir)

    lifecycle.validator.validate = _tracking_validate

    async def _first(**kwargs):
        return (
            "def broken(**kwargs):\n"
            "    return 'not a dict'\n"
            "TOOL_REGISTRY = {'broken': {'function': broken, 'description': 'd',\n"
            "  'input_schema': {'type': 'object', 'properties': {}},"
            " 'scope': 'tools:read'}}\n"
        )

    async def _refined(**kwargs):
        return ENV_EXFIL_TOOLS

    lifecycle.generator.generate_tools_file = _first
    lifecycle.generator.refine_tools_file = _refined
    draft = await lifecycle.create_draft(
        user_id="u-h4-fix", agent_name="Gate Fix", description="probe agent"
    )

    await lifecycle.generate_code(draft["id"])

    assert executed, "validator never ran at all — test is not exercising the loop"
    assert not any("os.environ" in code for code in executed)


@pytest.mark.asyncio
async def test_approve_decides_on_security_before_running_the_validator(lifecycle):
    state = await _generate(lifecycle, CLEAN_TOOLS, name="Gate Approve")
    assert state["status"] == "generated"

    tools_file = os.path.join(
        lifecycle._agents_dir, state["agent_slug"], "mcp_tools.py"
    )
    with open(tools_file, "w", encoding="utf-8") as handle:
        handle.write(ENV_EXFIL_TOOLS)

    def _boom(*args, **kwargs):
        raise AssertionError(
            "approve_agent ran the validator on HIGH-severity code (H4)"
        )

    lifecycle.validator.validate = _boom

    approved = await lifecycle.approve_agent(state["id"])
    assert approved["status"] == "pending_review"
    assert json.loads(approved["security_report"])["max_severity"] == "high"


def test_revision_gate_refuses_before_validation(monkeypatch):
    from orchestrator import agentic_creation

    class _Lifecycle:
        _agents_dir = "/nonexistent"

        def __init__(self):
            self.security = CodeSecurityAnalyzer()
            self.validator = _TripwireValidator()

    class _TripwireValidator:
        def validate(self, code, slug, agents_dir):
            raise AssertionError("staged revision executed despite HIGH finding")

    lifecycle = _Lifecycle()
    report, validation = agentic_creation._gate_revision_code(
        lifecycle,
        {"agent_slug": "rev_slug"},
        {"agent_slug": "live_slug"},
        ENV_EXFIL_TOOLS,
    )
    assert validation is None
    assert blocks_execution(report) is True


def test_revision_validator_inspects_the_staged_slug_not_the_live_one():
    from orchestrator import agentic_creation

    seen = {}

    class _Validator:
        def validate(self, code, slug, agents_dir):
            seen["slug"] = slug
            return object()

    class _Lifecycle:
        _agents_dir = "/nonexistent"
        security = CodeSecurityAnalyzer()
        validator = _Validator()

    agentic_creation._gate_revision_code(
        _Lifecycle(),
        {"agent_slug": "rev_slug"},
        {"agent_slug": "live_slug"},
        CLEAN_TOOLS,
    )
    assert seen["slug"] == "rev_slug"
