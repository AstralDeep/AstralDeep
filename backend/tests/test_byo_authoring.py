"""Feature 058 — BYO authoring orchestration: the Analyze gate is structurally
pre-generation (a violating draft produces NO code) and a passing draft generates
+ delivers a REAL self-contained bundle to the host (never Popen'd).

The LLM call is stubbed (codegen needs a configured system LLM) but everything
downstream of it is the real generator + lifecycle: the earlier all-mocked draft
hid a defect where the delivered bundle was always ``{}``."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.database import Database  # noqa: E402
from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import user_agents as ua  # noqa: E402
from orchestrator.agent_lifecycle import AgentLifecycleManager, BYO_ORIGIN  # noqa: E402


async def _t(fn, *a, **k):
    """Run a synchronous (DB-touching) helper off the event loop (052)."""
    return await asyncio.to_thread(fn, *a, **k)

# A plausible LLM output: self-contained, astralprims-only, correct return shape.
CANNED_TOOLS = '''"""Greeter tools."""
from astralprims import Card, Text

REQUIRED_CREDENTIALS = []


def greet(name="world", **kwargs):
    card = Card(title="Greeting", content=[Text(content=f"Hello, {name}!")])
    return {"_ui_components": [card.to_dict()], "_data": {"greeted": name}}


TOOL_REGISTRY = {
    "greet": {
        "function": greet,
        "description": "Greet someone by name",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"}}},
        "scope": "tools:read",
    },
}
'''


def _fake_orch():
    o = MagicMock()
    o.history.db = Database()
    o.history.db._init_db()
    o.lifecycle_manager = MagicMock()
    o.lifecycle_manager.create_draft = AsyncMock(return_value={"id": "d1", "agent_slug": "greeter"})
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "ok", "files": {"greeter_agent.py": "print('hi')"}})
    o.deliver_agent_bundle = AsyncMock(return_value=1)
    return o


@pytest.fixture()
def real_lifecycle():
    """A real AgentLifecycleManager whose only stub is the LLM tools call."""
    db = Database()
    db._init_db()
    lm = AgentLifecycleManager(db, orchestrator=None)
    lm.generator.generate_tools_file = AsyncMock(return_value=CANNED_TOOLS)
    lm.generator.refine_tools_file = AsyncMock(return_value=CANNED_TOOLS)
    created = []
    _create = lm.create_draft

    async def _tracked(*a, **kw):
        d = await _create(*a, **kw)
        created.append(d)
        return d

    lm.create_draft = _tracked
    yield lm
    for d in created:
        shutil.rmtree(os.path.join(lm._agents_dir, d["agent_slug"]), ignore_errors=True)
        db.execute("DELETE FROM draft_agents WHERE id = ?", (d["id"],))


async def test_analyze_violation_blocks_generation():
    o = _fake_orch()
    res = await aa.author_and_deliver(
        o, user_id="u-block", agent_name="Sharer",
        description="publishes and shares the agent with another user",
        declared_tools=["share_agent"], declared_scopes=["tools:read"])
    assert res["status"] == "analyze_failed"
    principles = {v["principle"] for v in res["violations"]}
    assert principles & {"K", "D"}                       # share/cross-user caught
    o.lifecycle_manager.create_draft.assert_not_awaited()  # NO draft
    o.lifecycle_manager.generate_code.assert_not_awaited() # NO code (FR-003)
    o.deliver_agent_bundle.assert_not_awaited()


async def test_analyze_pass_generates_validates_delivers():
    o = _fake_orch()
    res = await aa.author_and_deliver(
        o, user_id="u-ok", agent_name="Greeter",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    try:
        assert res["status"] == "delivered" and res["delivered_to"] == 1
        o.lifecycle_manager.generate_code.assert_awaited_once()
        o.deliver_agent_bundle.assert_awaited_once()
        row = await _t(ua.get_user_agent, o.history.db, res["agent_id"])
        assert row["status"] == "validated" and row["constitution_version"]
    finally:
        for t in ("user_agent", "agent_ownership"):
            await _t(o.history.db.execute, f"DELETE FROM {t} WHERE agent_id = ?", (res["agent_id"],))


async def test_generation_failure_reported_no_delivery():
    o = _fake_orch()
    o.lifecycle_manager.generate_code = AsyncMock(
        return_value={"status": "error", "error_message": "codegen boom"})
    res = await aa.author_and_deliver(
        o, user_id="u-gen", agent_name="Greeter2",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    try:
        assert res["status"] == "generation_failed" and "boom" in (res["error"] or "")
        o.deliver_agent_bundle.assert_not_awaited()
    finally:
        for t in ("user_agent", "agent_ownership"):
            await _t(o.history.db.execute, f"DELETE FROM {t} WHERE agent_id = ?", (res["agent_id"],))


def test_slug_is_owner_namespaced_and_non_reserved():
    a = aa.slug_agent_id("My Cool Agent!", "owner-abc-123")
    b = aa.slug_agent_id("My Cool Agent!", "different-owner")
    assert a != b and not a.startswith("__") and a.startswith("ua-")


# ── The REAL generate_code → _bundle_files path (the empty-bundle defect) ──────

async def _gen_byo(lm, agent_id, name="Byo Greeter"):
    draft = await lm.create_draft(
        user_id="u-byo", agent_name=name,
        description="greets the owner by their name",
        tools_spec=[{"name": "greet", "description": "greet"}])
    await _t(lm.db.update_draft_agent, draft["id"], origin=BYO_ORIGIN)
    return await lm.generate_code(draft["id"], target="byo", agent_id=agent_id)


def _write_bundle(tmp_path, files):
    for fname, content in files.items():
        (tmp_path / fname).write_text(content, encoding="utf-8")
    return tmp_path


def _run_bundle(bundle_dir, requests):
    """Run the bundle exactly as the desktop host does: a child process speaking
    JSON lines over stdio. Returns the frames it emitted."""
    stdin = "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, "agent_main.py"], cwd=str(bundle_dir), input=stdin,
        capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, f"worker exited {proc.returncode}: {proc.stderr[-800:]}"
    return [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]


async def test_real_generate_code_returns_finalized_v2_bundle(real_lifecycle):
    # The delivered bundle was ALWAYS EMPTY: generate_code returned a draft_agents
    # row, which has no files/agent_code column, so _bundle_files fell through to {}.
    gen = await _gen_byo(real_lifecycle, "ua-byo-greeter-uown")
    assert gen["status"] == "generated", gen.get("error_message")
    files = aa._bundle_files(gen)
    # The v2 artifact digest covers the three executable files; manifest.json is
    # the metadata envelope retained by the legacy flat-file delivery seam.
    assert set(files) == {
        "agent_main.py",
        "astralprims_ui.py",
        "mcp_tools.py",
        "manifest.json",
    }
    assert all(files.values())                      # no empty file
    assert "TOOL_REGISTRY" in files["mcp_tools.py"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["agent_id"] == "ua-byo-greeter-uown"
    assert manifest["revision_id"] == gen["revision_id"]
    assert manifest["bundle_sha256"] == gen["bundle_sha256"]
    assert manifest["runtime_contract_version"] == 2
    assert gen["artifact_relative_path"] == (
        f"revisions/ua-byo-greeter-uown/{gen['revision_id']}"
    )
    loaded = real_lifecycle.artifact_store.load(
        gen["artifact_relative_path"],
        expected_digest=gen["bundle_sha256"],
        expected_manifest_digest=gen["manifest_sha256"],
    )
    assert loaded.files["mcp_tools.py"] == files["mcp_tools.py"]


async def test_v2_authoring_delivery_forwards_immutable_artifact_path(monkeypatch):
    revision_id = str(uuid.uuid4())
    artifact_path = f"revisions/ua-path-owner/{revision_id}"
    executable = {
        "agent_main.py": "pass\n",
        "astralprims_ui.py": "pass\n",
        "mcp_tools.py": "TOOL_REGISTRY = {}\n",
    }
    manifest = {
        "manifest_version": 2,
        "runtime_contract_version": 2,
        "revision_id": revision_id,
        "agent_id": "ua-path-owner",
        "required_runtime_lock_sha256": "a" * 64,
        "bundle_sha256": "b" * 64,
        "digest_algorithm": "sha256",
        "files": [],
    }
    orch = MagicMock()
    orch.lifecycle_manager.generate_code = AsyncMock(
        return_value={
            "status": "generated",
            "files": {**executable, "manifest.json": json.dumps(manifest)},
            "runtime_manifest": manifest,
            "bundle_sha256": "b" * 64,
            "revision_id": revision_id,
            "runtime_contract_version": 2,
            "required_runtime_lock_sha256": "a" * 64,
            "artifact_relative_path": artifact_path,
        }
    )
    orch.deliver_agent_bundle = AsyncMock(return_value=1)
    monkeypatch.setattr(ua, "mark_validated", lambda *args, **kwargs: None)

    result = await aa._generate_and_deliver(
        orch,
        user_id="owner",
        agent_id="ua-path-owner",
        draft_id=str(uuid.uuid4()),
        tool_names=[],
        declared_scopes=[],
    )

    assert result["status"] == "delivered"
    assert orch.deliver_agent_bundle.await_args.kwargs[
        "artifact_relative_path"
    ] == artifact_path


async def test_bundle_is_self_contained(real_lifecycle):
    # contracts/host-bundle.md §2: the desktop host ships no backend package.
    gen = await _gen_byo(real_lifecycle, "ua-selfcontained-uown", name="Byo Selfcontained")
    files = aa._bundle_files(gen)
    assert files
    for fname, src in files.items():
        for forbidden in ("from shared", "import shared", "from agents.", "sys.path.insert"):
            assert forbidden not in src, f"{fname} reaches for the backend package"


async def test_generated_card_agent_id_matches_the_user_agent_row(real_lifecycle, tmp_path):
    # The registry looks up user_agent[card.agent_id]; a slug-derived '<slug>-1'
    # finds no row and registration is refused fail-closed (and silently).
    agent_id = aa.slug_agent_id("Byo Carded", "owner-sub-9")
    await _t(ua.create_user_agent, real_lifecycle.db, agent_id=agent_id,
             owner_user_id="owner-sub-9", display_name="Byo Carded")
    try:
        gen = await _gen_byo(real_lifecycle, agent_id, name="Byo Carded")
        files = aa._bundle_files(gen)
        frames = _run_bundle(_write_bundle(tmp_path, files), [])
        assert frames[0]["type"] == "register_agent"
        card = frames[0]["agent_card"]
        row = await _t(ua.get_user_agent, real_lifecycle.db, agent_id)
        assert card["agent_id"] == row["agent_id"]
        assert [s["name"] for s in card["skills"]] == ["greet"]
        assert "api_key" not in frames[0]      # authority is the owner's session
    finally:
        await _t(real_lifecycle.db.execute, "DELETE FROM user_agent WHERE agent_id = ?", (agent_id,))


def test_backend_target_agent_id_is_unchanged(real_lifecycle):
    # 027 must stay byte-identical: the slug-derived id is still the default.
    files = real_lifecycle.generator.generate_template_files(
        agent_name="Legacy", description="d", slug="legacy_thing")
    assert 'agent_id = "legacy-thing-1"' in files["legacy_thing_agent.py"]


async def test_bundle_runner_dispatch_semantics(real_lifecycle, tmp_path):
    gen = await _gen_byo(real_lifecycle, "ua-dispatch-uown", name="Byo Dispatch")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [
        {"type": "mcp_request", "request_id": "r1", "method": "tools/list"},
        {"type": "mcp_request", "request_id": "r2", "method": "tools/call",
         "params": {"name": "greet", "arguments": {"name": "Sam"}}},
        {"type": "mcp_request", "request_id": "r3", "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
        {"type": "mcp_request", "request_id": "r4", "method": "bogus/method"},
        "not json at all",
    ])
    by_id = {f.get("request_id"): f for f in frames if f["type"] == "mcp_response"}
    assert [t["name"] for t in by_id["r1"]["result"]["tools"]] == ["greet"]
    assert by_id["r2"]["result"] == {"greeted": "Sam"}
    assert by_id["r2"]["ui_components"][0]["type"] == "card"
    assert by_id["r3"]["error"]["code"] == -32601          # unknown tool
    assert by_id["r4"]["error"]["code"] == -32601          # unknown method
    assert len(by_id) == 4                                  # the junk line was discarded


async def test_bundle_runner_maps_a_raised_exception_to_32603(real_lifecycle, tmp_path):
    # The tool must survive spec validation (which calls it with sample args), so
    # it only explodes on an explicit mode — otherwise auto-fix rewrites it.
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value='''
from astralprims import Text


def boom(mode="ok", **kwargs):
    if mode == "explode":
        raise ValueError("kaboom")
    return {"_ui_components": [Text(content="fine").to_dict()], "_data": {}}


TOOL_REGISTRY = {"boom": {"function": boom, "description": "boom",
                          "input_schema": {"type": "object",
                                           "properties": {"mode": {"type": "string"}}},
                          "scope": "tools:read"}}
''')
    gen = await _gen_byo(real_lifecycle, "ua-boom-uown", name="Byo Boom")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [{"type": "mcp_request", "request_id": "r1",
                                   "method": "tools/call",
                                   "params": {"name": "boom",
                                              "arguments": {"mode": "explode"}}}])
    resp = [f for f in frames if f.get("request_id") == "r1"][0]
    assert resp["error"]["code"] == -32603 and "kaboom" in resp["error"]["message"]


async def test_bundle_runner_maps_an_error_alert_to_an_error_response(real_lifecycle,
                                                                      tmp_path):
    """The tool-error convention the backend MCPServer implements: a tool that
    handled its own failure returns create_ui_response([Alert(variant='error')]).
    The BYO runner dropped that check, so a FAILED tool call came back as a
    SUCCESS mcp_response."""
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value='''
from astralprims import Alert, Text, create_ui_response


def risky(mode="ok", **kwargs):
    if mode == "fail":
        return create_ui_response([Alert(message="upstream said no", variant="error")])
    return {"_ui_components": [Text(content="fine").to_dict()], "_data": {}}


TOOL_REGISTRY = {"risky": {"function": risky, "description": "r",
                           "input_schema": {"type": "object",
                                            "properties": {"mode": {"type": "string"}}},
                           "scope": "tools:read"}}
''')
    gen = await _gen_byo(real_lifecycle, "ua-alert-uown", name="Byo Alert")
    bundle = _write_bundle(tmp_path, aa._bundle_files(gen))
    frames = _run_bundle(bundle, [
        {"type": "mcp_request", "request_id": "ok", "method": "tools/call",
         "params": {"name": "risky", "arguments": {"mode": "ok"}}},
        {"type": "mcp_request", "request_id": "bad", "method": "tools/call",
         "params": {"name": "risky", "arguments": {"mode": "fail"}}},
    ])
    by_id = {f.get("request_id"): f for f in frames if f["type"] == "mcp_response"}
    assert not by_id["ok"].get("error")
    assert by_id["bad"]["error"]["code"] == -32603
    assert "upstream said no" in by_id["bad"]["error"]["message"]
    # 064: an error and renderable UI are mutually exclusive on the wire.
    assert "ui_components" not in by_id["bad"]


async def test_generate_code_refuses_the_backend_target_for_a_byo_draft(real_lifecycle):
    """The exec decision is a property of the ROW, not the caller's argument: the
    REST generate endpoint passes no target, and used to run a BYO draft's code
    in-process."""
    draft = await real_lifecycle.create_draft(
        user_id="u-byo", agent_name="Byo Rowkeyed",
        description="greets the owner by their name",
        tools_spec=[{"name": "greet", "description": "greet"}])
    await _t(real_lifecycle.db.update_draft_agent, draft["id"], origin=BYO_ORIGIN)
    with pytest.raises(ValueError, match="BYO"):
        await real_lifecycle.generate_code(draft["id"])       # default target=backend


async def test_backend_coupled_tools_file_is_refused_and_not_delivered(real_lifecycle):
    real_lifecycle.generator.generate_tools_file = AsyncMock(return_value=(
        "from shared.base_agent import BaseA2AAgent\n"
        "TOOL_REGISTRY = {}\n"))
    gen = await _gen_byo(real_lifecycle, "ua-coupled-uown", name="Byo Coupled")
    assert gen["status"] == "error"
    assert "self-contained" in (gen["error_message"] or "")
    assert aa._bundle_files(gen) == {}      # nothing to ship


async def test_authoring_refuses_to_deliver_an_empty_bundle():
    o = _fake_orch()
    o.lifecycle_manager.generate_code = AsyncMock(return_value={"status": "generated"})
    res = await aa.author_and_deliver(
        o, user_id="u-empty", agent_name="Greeter3",
        description="greets the owner by their name",
        declared_tools=["greet"], declared_scopes=["tools:read"],
        plan={"tools_used": ["greet"], "tool_scopes": {"greet": "tools:read"}})
    try:
        assert res["status"] == "generation_failed" and "empty" in res["error"]
        o.deliver_agent_bundle.assert_not_awaited()
    finally:
        for t in ("user_agent", "agent_ownership"):
            await _t(o.history.db.execute, f"DELETE FROM {t} WHERE agent_id = ?", (res["agent_id"],))


# ── G1/SC-002: BYO code is NEVER executed on the server, in ANY process ───────

async def test_byo_validation_never_execs_user_code(real_lifecycle):
    def _boom(*a, **kw):
        raise AssertionError("in-process exec of user code (G1 violation)")

    real_lifecycle.validator.validate = _boom      # the ONLY exec path
    gen = await _gen_byo(real_lifecycle, "ua-static-uown", name="Byo Static")
    assert gen["status"] == "generated", gen.get("error_message")
    report = json.loads(gen["validation_report"])
    assert report["tools_tested"] == 1 and report["passed"]   # it really did validate


def test_static_validation_does_not_import_run_or_touch_the_filesystem(real_lifecycle,
                                                                       tmp_path):
    """The reviewer's proof-of-exploit, inverted: a module whose import writes a
    file into the orchestrator's agent tree must not write ANYTHING, because the
    module is never imported. It is read as text."""
    probe = (tmp_path / "SBX_PROBE.txt").as_posix()
    hostile = (
        "import os, socket\n"
        f"open({probe!r}, 'w').write('pwned')\n"
        "socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "from astralprims import Text\n\n"
        "def probe(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'probe': {'function': probe, 'description': 'p',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n")
    report = real_lifecycle.validator.validate_static(hostile, "byo_probe")
    assert not os.path.exists(probe), "BYO code EXECUTED during validation (G1 violation)"
    assert report.passed and report.tools_tested == 1   # shape is fine; behavior is the host's


def test_static_validation_refuses_a_non_stdlib_import(real_lifecycle):
    """The desktop host ships stdlib + astralprims ONLY. An `import requests`
    bundle dies at import on the user's machine with no register_agent — so it is
    refused HERE, at generation, not surfaced as a silence timeout."""
    report = real_lifecycle.validator.validate_static(
        "import requests\nfrom astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n",
        "byo_reqs")
    assert not report.passed
    assert any("requests" in f.message and f.severity == "error" for f in report.findings)


def test_static_validation_accepts_stdlib_and_astralprims(real_lifecycle):
    report = real_lifecycle.validator.validate_static(CANNED_TOOLS, "byo_ok")
    assert report.passed and report.tools_tested == 1 and report.tools_passed == 1


def test_static_validation_catches_registry_and_return_shape(real_lifecycle):
    missing_registry = real_lifecycle.validator.validate_static(
        "from astralprims import Text\n\ndef t(**kwargs):\n    return {}\n", "byo_x")
    assert not missing_registry.passed

    bad_return = real_lifecycle.validator.validate_static(
        "from astralprims import Text\n\n"
        "def t(**kwargs):\n    return {'nope': 1}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n",
        "byo_y")
    assert not bad_return.passed
    assert any(f.category == "RETURN_FORMAT" for f in bad_return.findings)


async def test_non_stdlib_bundle_is_never_delivered(real_lifecycle):
    """End-to-end: the import allowlist is a GATE on the generated bundle."""
    from unittest.mock import AsyncMock as _AM
    real_lifecycle.generator.generate_tools_file = _AM(return_value=(
        "import httpx\nfrom astralprims import Text\n\n"
        "def t(**kwargs):\n"
        "    return {'_ui_components': [Text(content='x').to_dict()], '_data': {}}\n\n"
        "TOOL_REGISTRY = {'t': {'function': t, 'description': 'd',\n"
        "  'input_schema': {'type': 'object', 'properties': {}}, 'scope': 'tools:read'}}\n"))
    real_lifecycle.generator.refine_tools_file = _AM(side_effect=RuntimeError("no llm"))
    gen = await _gen_byo(real_lifecycle, "ua-httpx-uown", name="Byo Httpx")
    report = json.loads(gen["validation_report"])
    assert not report["passed"]
    assert any("httpx" in f["message"] for f in report["findings"])


# ── The codegen prompt and the BYO gate must never disagree ───────────────────

def test_byo_prompt_required_imports_pass_the_byo_gate():
    """An obedient LLM emits the prompt's own required-imports block. If the gate
    rejects that block, BYO codegen can NEVER succeed (it did: the block mandated
    a sys.path.insert the self-containment gate hard-fails)."""
    from orchestrator.agent_spec import generate_llm_prompt_section, BYO_REQUIRED_IMPORTS_BLOCK
    from orchestrator.agent_generator import byo_import_violations
    from orchestrator.agent_validator import disallowed_imports

    assert byo_import_violations(BYO_REQUIRED_IMPORTS_BLOCK) == []
    assert disallowed_imports(BYO_REQUIRED_IMPORTS_BLOCK) == []

    byo_prompt = generate_llm_prompt_section(self_contained=True)
    assert "sys.path.insert" not in byo_prompt
    # The 027 prompt is unchanged (it DOES need the backend shim).
    assert "sys.path.insert" in generate_llm_prompt_section()


def test_byo_security_rules_do_not_recommend_unavailable_http_libraries():
    from orchestrator.agent_generator import security_rules_block
    byo = security_rules_block(self_contained=True)
    assert "urllib.request" in byo
    assert "use `requests`/`httpx` for HTTP only" not in byo
    assert "use `requests`/`httpx` for HTTP only" in security_rules_block()


def test_generated_runner_bakes_the_id_it_is_handed(real_lifecycle):
    files = real_lifecycle.generator.generate_byo_files(
        agent_name="X", description="d", agent_id="ua-x-abc", skill_tags=["t"],
        constitution_version="9.9.9")
    tree = ast.parse(files["agent_main.py"])
    consts = {t.id: ast.literal_eval(n.value)       # module-level constants only
              for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}
    assert consts["AGENT_ID"] == "ua-x-abc"
    assert json.loads(files["manifest.json"])["constitution_version"] == "9.9.9"


async def test_byo_codegen_uses_the_per_call_owner_resolver_over_the_system_one():
    """Found live 2026-07-14: BYO code generation resolved the admin-managed
    SYSTEM LLM (feature 054), which is unset on deployments that never configured
    one — so generation failed 'LLM not configured' even though the owner (who was
    actively authoring) had a working model. A user authoring their own private
    agent must generate its code with THEIR LLM. This pins the resolver override
    precedence _aresolve_client threads through, without a real LLM call."""
    from orchestrator.agent_generator import AgentCodeGenerator

    class _Cfg:
        base_url = "http://owner.example/v1"
        model = "owner-model"
        api_key = "owner-key"

    # System resolver is UNSET (the failing deployment state).
    gen = AgentCodeGenerator(config_resolver=lambda: None)
    assert await gen._aresolve_client() == (None, None)

    # The owner's per-call resolver is used instead, yielding a client + model.
    client, model = await gen._aresolve_client(config_resolver=lambda: _Cfg())
    assert client is not None and model == "owner-model"
    assert str(client.base_url).startswith("http://owner.example")
