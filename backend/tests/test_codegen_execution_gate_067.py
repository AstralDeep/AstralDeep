"""H4: generated code is gated for nefarious activity BEFORE it executes.

Codegen stays available to every user — the change is not a role check. What
changed is the severity floor and the ORDERING:

* the gate blocks HIGH as well as CRITICAL (``blocks_execution``). ``os.environ``
  access parses as an attribute, not a call, so it was classified HIGH and
  sailed past a CRITICAL-only gate straight into ``validator.validate()``,
  which imports the module and calls every tool IN the secret-holding
  orchestrator process;
* the auto-fix loop re-analyzes its refined bytes (it used to syntax-check
  them only, so a second-round payload reached the same exec seam);
* ``approve_agent`` decides on the security report BEFORE running the
  validator, not after;
* the revision path returns ``validation=None`` when the gate refuses, so the
  validator never executes refused code, and it now validates the STAGED
  slug rather than re-importing the unmodified live agent.
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
from shared.database import Database


# Reads a secret straight out of the orchestrator's environment. Classified
# HIGH (BLOCKED_ATTRIBUTE), which a CRITICAL-only gate let through.
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
    # The floor — not just "not passed" — is what the lifecycle gates on.
    assert blocks_execution(report) is True


def test_clean_generated_code_still_passes_the_floor():
    report = CodeSecurityAnalyzer().analyze(CLEAN_TOOLS)
    assert blocks_execution(report) is False


def test_medium_findings_do_not_block_execution():
    # open()/getattr are MEDIUM: a parser that reads its input file must still
    # be generatable by an ordinary user.
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
    """Auto-generated parsers are the main thing that writes byte literals.
    Once HIGH became execution-blocking, the old three-escapes-per-line
    obfuscation heuristic would have refused every correct binary-format
    parser — a legitimate magic-number check must survive the floor.
    """
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
    """A refusal the model cannot avoid would be a dead end, so the prompt has
    to name what the analyzer now blocks."""
    from orchestrator.agent_generator import security_rules_block

    for self_contained in (False, True):
        rules = security_rules_block(self_contained=self_contained)
        assert "os.environ" in rules and "os.getenv" in rules
        assert "globals()" in rules
        # And it must say the consequence, not merely discourage.
        assert "REFUSED" in rules or "refused" in rules


@pytest.fixture()
def lifecycle():
    db = Database()
    db._init_db()
    manager = AgentLifecycleManager(db, orchestrator=None)
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
        db.execute("DELETE FROM draft_agents WHERE id = ?", (draft["id"],))


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
    # And nothing was written into the agent tree for a subprocess to pick up.
    tools_file = os.path.join(
        lifecycle._agents_dir, state["agent_slug"], "mcp_tools.py"
    )
    assert not os.path.exists(tools_file)


@pytest.mark.asyncio
async def test_clean_code_still_generates_for_an_ordinary_user(lifecycle):
    # The posture change must not make codegen unavailable — only nefarious
    # output is refused.
    state = await _generate(lifecycle, CLEAN_TOOLS, name="Gate Clean")
    assert state["status"] == "generated", state.get("error_message")
    assert json.loads(state["validation_report"])["passed"] is True


@pytest.mark.asyncio
async def test_auto_fix_payload_is_re_gated_before_it_executes(lifecycle):
    # Round 1 is clean but fails spec validation; the "fix" smuggles in the
    # env read. The refined bytes used to be syntax-checked only and then fed
    # straight back into validator.validate().
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
        # Registry with a tool whose return shape is wrong → validation fails
        # → auto-fix loop runs.
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
    # approve_agent re-reads the on-disk file. Plant flagged code there and
    # assert the verdict lands without the validator executing it.
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
