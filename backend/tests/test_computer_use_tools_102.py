"""Tests for backend/agents/computer_use/mcp_tools.py: the extension registration hook, the
macOS tool descriptions, post-effect screenshots, the host wait verb and the three command tools.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_use_policy as policy
from orchestrator.computer_hosts import ComputerHostError
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared.protocol import ComputerHostDescriptor, MCPRequest
from tests.test_computer_use_076 import OWNER, _AgentOrch, _descriptor, _FakeHostSocket, _WS

_EXTENSION = "agents.computer_use.browser_tools"


def _extension(register, **attrs):
    module = types.ModuleType(_EXTENSION)
    module.register = register
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def _browser_entry(name):
    return {"function": lambda **_: {}, "description": name, "input_schema": {"type": "object"},
            "scope": policy.SCOPES[name], "tier": policy.TIERS[name], "retryable": False,
            "timeout": policy.TIMEOUTS[name]}


def test_hook_is_a_no_op_without_the_extension(monkeypatch):
    monkeypatch.setitem(sys.modules, _EXTENSION, None)
    registry = {"screenshot": {"description": "kept"}}
    mcp_tools.register_extension_tools(registry)
    assert registry == {"screenshot": {"description": "kept"}}


def test_hook_adds_new_browser_verbs_from_the_extension(monkeypatch):
    def register(registry):
        registry["browser_snapshot"] = _browser_entry("browser_snapshot")

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {"screenshot": {"description": "kept"}}
    mcp_tools.register_extension_tools(registry)
    assert set(registry) == {"screenshot", "browser_snapshot"}
    assert registry["screenshot"] == {"description": "kept"}


@pytest.mark.parametrize("name", ["run_shell", "screenshot", "run_command"])
def test_hook_refuses_unknown_or_overriding_entries_and_keeps_the_registry(monkeypatch, name):
    def register(registry):
        registry["browser_snapshot"] = _browser_entry("browser_snapshot")
        registry[name] = {"description": "forged"}

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {"screenshot": {"description": "kept"}}
    with pytest.raises(RuntimeError):
        mcp_tools.register_extension_tools(registry)
    assert registry == {"screenshot": {"description": "kept"}}


def test_hook_refuses_an_entry_whose_policy_declaration_drifted(monkeypatch):
    def register(registry):
        registry["browser_click"] = {**_browser_entry("browser_click"), "scope": "tools:read"}

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {}
    with pytest.raises(RuntimeError):
        mcp_tools.register_extension_tools(registry)
    assert registry == {}


def test_hook_propagates_a_broken_extension_import(monkeypatch):
    broken = types.ModuleType(_EXTENSION)
    monkeypatch.setitem(sys.modules, _EXTENSION, broken)
    with pytest.raises(AttributeError):
        mcp_tools.register_extension_tools({})


CHAT = "chat-1"
CTX = {"user_id": OWNER, "session_id": CHAT}
ROOT = "/Users/sam/Projects/x"
JOB = str(uuid.uuid4())
SHOT = {"screen_index": 0, "width": 1280, "height": 720, "scale": 0.5, "media_type": "image/jpeg",
        "base64": "/9j/AAAA"}


class _Gate:
    def __init__(self, fail=False):
        self.fail = fail
        self.seen = []

    def redact_for_storage(self, text):
        self.seen.append(text)
        if self.fail:
            raise ValueError("phi_redaction_unavailable")
        cleaned = text.replace("123-45-6789", "[REDACTED:phi]")
        return cleaned, cleaned != text


class _World:
    def __init__(self, orch, host, host_ws, session, replies):
        self.orch, self.host, self.host_ws, self.session, self.replies = orch, host, host_ws, session, replies

    async def call(self, tool, **kwargs):
        return await asyncio.to_thread(getattr(mcp_tools, tool), **CTX, **kwargs)

    def requests(self, verb=None):
        return [f for f in self.host_ws.frames("computer_request") if verb is None or f["verb"] == verb]


@pytest.fixture
def gate(monkeypatch):
    from personalization import phi_gate

    fake = _Gate()
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: fake)
    return fake


@pytest.fixture
async def world(monkeypatch):
    replies = {}

    def answers(verb, args):
        reply = replies.get(verb)
        if callable(reply):
            return reply(args)
        return reply if reply is not None else {"ok": True, "result": {}}

    from orchestrator import terminal_host_review

    orch = _AgentOrch()
    mcp_tools.register_deps(orch)
    monkeypatch.setattr(terminal_host_review, "computer_use_review_current", lambda current: current is orch)
    host_ws = orch.add(_FakeHostSocket(orch, answers))
    host, _ = orch.computer_hosts.register(OWNER, host_ws, ComputerHostDescriptor.from_dict(_descriptor(
        platform="macos", name="Mac", workspace_roots=[{"name": "x", "path": ROOT}])))
    orch.add(_WS(chat=CHAT))
    task = asyncio.create_task(asyncio.to_thread(mcp_tools.start_session, **CTX))
    for _ in range(400):
        await asyncio.sleep(0.005)
        live = orch.computer_sessions.live_for_host(OWNER, host.host_id)
        if live is not None:
            await orch.computer_sessions.on_host_event(OWNER, host.host_id, "heartbeat", live.session_id, None)
            break
    await task
    session = orch.computer_sessions.live_for_host(OWNER, host.host_id)
    yield _World(orch, host, host_ws, session, replies)
    await orch.computer_sessions.end_all("user_stop")


def test_descriptions_are_written_for_the_mac_and_steer_commands_and_the_browser_pane():
    reg = mcp_tools.TOOL_REGISTRY
    text = json.dumps({name: entry["description"] for name, entry in reg.items()})
    for retired in ("notepad", "powershell", "win+r", "alt+f4", "Windows Terminal"):
        assert retired not in text
    assert "cmd+l" in reg["press_keys"]["description"]
    for name in ("click", "double_click", "right_click", "move", "drag", "scroll"):
        assert "list_windows" in reg[name]["description"] and "hwnd" in reg[name]["description"]
        assert "Astral's own windows" in reg[name]["description"]
        assert "browser_" in reg[name]["description"]
    assert "run_command" in reg["type_text"]["description"] and "Terminal" in reg["type_text"]["description"]
    run = reg["run_command"]["description"]
    assert "argv" in run and "workspace_roots" in run and "screenshot" in run and "Terminal" in run
    assert reg["run_command"]["input_schema"]["required"] == ["cwd"]


def test_only_run_command_matches_a_scanner_pattern():
    analyzer = ToolSecurityAnalyzer()
    flagged = {name for name, entry in mcp_tools.TOOL_REGISTRY.items()
               if analyzer.analyze_tool(name, entry["description"], entry["input_schema"]) is not None}
    assert flagged == {"run_command"}


def test_command_tools_are_registered_with_policy_scopes():
    reg = mcp_tools.TOOL_REGISTRY
    assert (reg["run_command"]["scope"], reg["read_output"]["scope"], reg["interrupt_command"]["scope"]) == (
        "tools:execute", "tools:read", "tools:write")
    assert reg["run_command"]["destructive"] == "always"
    assert "destructive" not in reg["read_output"] and "destructive" not in reg["interrupt_command"]
    assert (reg["project_preview"]["scope"], reg["project_preview"]["tier"],
            reg["project_preview"]["destructive"]) == ("tools:execute", "consequential", "always")


def test_project_preview_server_argv_is_fixed_to_a_loopback_port():
    assert mcp_tools._preview_argv("npm", "dev") == [
        "npm", "run", "dev", "--", "--host", "localhost", "--port", "4173", "--strictPort",
    ]
    assert mcp_tools._preview_argv("pnpm", "preview")[0:3] == ["pnpm", "run", "preview"]
    assert "localhost:4173" in mcp_tools._preview_probe_script()


def test_project_preview_http_probe_fits_review_and_survives_text_privacy_filter(monkeypatch):
    from types import SimpleNamespace
    from orchestrator.hitl_confirmation import _review_arguments

    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _text: False))
    preview, reviewable = _review_arguments({
        "argv": ["node", "-e", mcp_tools._preview_probe_script()],
        "cwd": ROOT, "timeout_s": 20, "background": False,
    }, project_targets=frozenset({("cwd", ROOT)}))
    assert reviewable and "[REDACTED" not in preview


def test_project_preview_start_returns_url_and_job_bound_stop_button(monkeypatch):
    from types import SimpleNamespace

    host = SimpleNamespace(name="Mac", project_mode=True)
    session = SimpleNamespace(session_id="session-1", command_jobs={})
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, CHAT, host, session))
    monkeypatch.setattr(mcp_tools, "_command_args", lambda args, _host: args)
    monkeypatch.setattr(mcp_tools, "read_file", lambda **_kwargs: {
        "_data": {"text": '{"scripts":{"dev":"vite --host localhost"}}', "truncated": False},
    })
    monkeypatch.setattr(mcp_tools, "read_output", lambda **_kwargs: {"_data": {"job_id": JOB, "state": "running"}})
    replies = iter([
        {"_data": {"job_id": JOB, "started_at": "2026-10-29", "background": True}},
        {"_data": {"exit_code": 0}},
    ])
    seen = []

    def run(**args):
        seen.append(args)
        if args.get("background"):
            mcp_tools._track(session, JOB, "2026-10-29")
        return next(replies)

    monkeypatch.setattr(mcp_tools, "run_command", run)
    result = mcp_tools.project_preview(action="start", cwd=ROOT, package_manager="npm", script="dev")
    assert result["_data"]["url"] == "http://localhost:4173/"
    assert result["_data"]["job_id"] == JOB
    assert session.command_jobs == {JOB: {"started_at": "2026-10-29", "state": "running"}}
    buttons = result["_ui_components"][0]["content"][1:]
    assert [button["label"] for button in buttons] == ["Check status", "Stop preview"]
    assert buttons[1]["action"] == "component_action"
    assert buttons[1]["payload"] == {"kind": "invoke", "params_patch": {"action": "stop", "job_id": JOB}}
    assert seen[0]["background"] is True and seen[0]["timeout_s"] == 3600
    assert seen[1]["argv"][0:2] == ["node", "-e"]


def test_project_preview_refuses_non_project_sessions_without_running_commands(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, CHAT, SimpleNamespace(project_mode=False),
                                                               SimpleNamespace(session_id="session-1")))
    monkeypatch.setattr(mcp_tools, "_run", lambda *_args, **_kwargs: pytest.fail("command must not run"))
    result = mcp_tools.project_preview(action="start", cwd=ROOT, package_manager="npm", script="dev")
    assert result["_data"]["code"] == "permission_denied"


def test_project_preview_stop_delegates_only_the_session_job_id(monkeypatch):
    from types import SimpleNamespace

    host = SimpleNamespace(name="Mac", project_mode=True)
    session = SimpleNamespace(session_id="session-1")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, CHAT, host, session))
    calls = []

    def stop(**kwargs):
        calls.append(kwargs)
        return {"_data": {"job_id": JOB, "state": "interrupted"}}

    monkeypatch.setattr(mcp_tools, "interrupt_command", stop)
    result = mcp_tools.project_preview(action="stop", cwd=ROOT, job_id=JOB)
    assert result["_data"] == {"computer": "Mac", "job_id": JOB, "state": "interrupted"}
    assert len(calls) == 1 and calls[0]["job_id"] == JOB and calls[0]["signal"] == "INT"


def test_project_preview_refuses_scripts_that_do_not_run_vite(monkeypatch):
    from types import SimpleNamespace

    host = SimpleNamespace(name="Mac", project_mode=True)
    session = SimpleNamespace(session_id="session-1")
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _kwargs: (OWNER, CHAT, host, session))
    monkeypatch.setattr(mcp_tools, "_command_args", lambda args, _host: args)
    monkeypatch.setattr(mcp_tools, "read_file", lambda **_kwargs: {
        "_data": {"text": '{"scripts":{"dev":"next dev"}}', "truncated": False},
    })
    monkeypatch.setattr(mcp_tools, "run_command", lambda **_kwargs: pytest.fail("server must not start"))
    result = mcp_tools.project_preview(action="start", cwd=ROOT, package_manager="npm", script="dev")
    assert result["_data"]["code"] == "preview_unsupported"


async def test_input_verbs_attach_the_post_effect_screenshot_as_an_untrusted_image(world):
    world.session.remember_windows([{"hwnd": 42, "title": "Untitled", "bundle_id": "com.apple.TextEdit"}])
    world.replies["click"] = {"ok": True, "result": {"x": 10, "y": 20, "screenshot": SHOT}}
    result = await world.call("click", x=10, y=20, hwnd=42)
    assert result["_images"] == [{"media_type": "image/jpeg", "base64": "/9j/AAAA",
                                  "caption": "Screen of Mac after click"}]
    assert result["_data"]["screenshot"] == {"width": 1280, "height": 720, "scale": 0.5, "screen_index": 0}
    assert "base64" not in json.dumps(result["_data"])
    assert result["_ui_components"][-1]["id"] == f"au_cuview_{world.session.session_id}"
    assert world.session.screenshot_cache["data"] == b"\xff\xd8\xff\x00\x00\x00"
    assert world.session.last_screenshot["width"] == 1280
    from agents.computer_use.mcp_server import MCPServer

    world.replies["type_text"] = {"ok": True, "result": {"screenshot": SHOT}}
    served = await asyncio.to_thread(MCPServer().process_request, MCPRequest(method="tools/call", params={
        "name": "type_text", "arguments": {**CTX, "text": "hi", "hwnd": 42}}))
    assert served.result["_images"][0]["caption"] == "Screen of Mac after type_text"
    assert "_model_digest" not in served.result


@pytest.mark.parametrize("shot", [None, {"base64": "", "media_type": "image/jpeg"},
                                  {**SHOT, "media_type": "text/html"}, "junk"])
async def test_input_results_without_a_valid_screenshot_attach_nothing(world, shot):
    world.session.remember_windows([{"hwnd": 42, "title": "Untitled", "bundle_id": "com.apple.TextEdit"}])
    world.replies["press_keys"] = {"ok": True, "result": {"screenshot": shot} if shot is not None else {}}
    result = await world.call("press_keys", keys="enter", hwnd=42)
    assert "_images" not in result and "screenshot" not in result["_data"]


async def test_non_visual_sessions_keep_the_screenshot_out_of_the_model(world):
    world.session.images_supported = False
    world.replies["open_app"] = {"ok": True, "result": {"launched": True, "screenshot": SHOT}}
    result = await world.call("open_app", app="com.apple.TextEdit")
    assert result["_images"] == [] and result["_data"]["screenshot"]["width"] == 1280


@pytest.mark.parametrize("tool,args", [("click", {"x": 1, "y": 1}), ("drag", {"x1": 1, "y1": 1, "x2": 2, "y2": 2}),
                                       ("scroll", {"x": 1, "y": 1}), ("move", {"x": 1, "y": 1})])
async def test_mac_pointer_tools_refuse_without_a_window_handle(world, tool, args):
    result = await world.call(tool, **args)
    assert result["_data"]["code"] == "out_of_range"
    assert result["_data"]["message"] == "Call list_windows and pass hwnd for the target window."
    assert not world.requests(tool)


async def test_wait_uses_the_host_verb_while_active(world):
    world.replies["wait"] = {"ok": True, "result": {"waited": 0.2}}
    result = await world.call("wait", seconds=0.2)
    assert result["_data"]["state"] == "active" and result["_data"]["waited"] == 0.2
    assert world.requests("wait")[-1]["args"] == {"seconds": 0.2}
    world.replies["wait"] = {"ok": False, "error": {"code": "paused", "message": "paused"}}
    refused = await world.call("wait", seconds=0.2)
    assert refused["_data"]["code"] == "paused"


async def test_foreground_command_returns_shaped_privacy_gated_untrusted_output(world, gate):
    world.replies["run_command"] = {"ok": True, "result": {
        "exit_code": 0, "signal": None, "timed_out": False, "duration_ms": 42,
        "stdout": "3 notes.md\nSSN 123-45-6789\n", "stderr": "warn\x07ing", "stdout_bytes": 30, "stderr_bytes": 8,
        "truncated": False, "cwd": ROOT, "started_at": "2026-10-26T00:00:00Z"}}
    result = await world.call("run_command", argv=["wc", "-l", "notes.md"], cwd=ROOT, timeout_s=5)
    data = result["_data"]
    assert data["exit_code"] == 0 and data["duration_ms"] == 42 and data["timed_out"] is False
    assert data["stdout"] == "3 notes.md\nSSN [REDACTED:phi]\n" and data["stderr"] == "warning"
    assert data["privacy"] == "redacted" and data["untrusted_output"] is True
    assert (data["stdout_bytes"], data["stderr_bytes"], data["cwd"]) == (30, 8, ROOT)
    assert "_model_digest" not in result
    request = world.requests("run_command")[-1]
    assert request["args"] == {"argv": ["wc", "-l", "notes.md"], "cwd": ROOT, "timeout_s": 5, "background": False}
    assert 10000 < request["deadline_ms"] <= 15000


async def test_command_output_is_withheld_when_the_privacy_gate_is_unavailable(world, gate):
    gate.fail = True
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 1, "stdout": "x", "stderr": "",
                                                            "duration_ms": 1}}
    data = (await world.call("run_command", command="ls", cwd=ROOT))["_data"]
    assert data["stdout"] == "" and data["privacy"] == "withheld" and data["exit_code"] == 1


async def test_timed_out_command_reports_it_without_retrying(world, gate):
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": None, "signal": "TERM", "timed_out": True,
                                                            "duration_ms": 5000, "stdout": "", "stderr": ""}}
    data = (await world.call("run_command", argv=["sleep", "30"], cwd=ROOT, timeout_s=5))["_data"]
    assert data["timed_out"] is True and data["signal"] == "TERM" and len(world.requests("run_command")) == 1
    assert "not retried" in data["note"]


@pytest.mark.parametrize("args,code", [
    ({"cwd": ROOT}, "out_of_range"), ({"command": "ls", "argv": ["ls"], "cwd": ROOT}, "out_of_range"),
    ({"command": "", "cwd": ROOT}, "out_of_range"), ({"command": "x" * 4097, "cwd": ROOT}, "out_of_range"),
    ({"argv": [], "cwd": ROOT}, "out_of_range"), ({"argv": ["ls", 3], "cwd": ROOT}, "out_of_range"),
    ({"argv": ["ls"] * 257, "cwd": ROOT}, "out_of_range"), ({"argv": [""], "cwd": ROOT}, "out_of_range"),
    ({"command": "ls"}, "out_of_range"), ({"command": "ls", "cwd": "relative"}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT + "/../y"}, "out_of_range"), ({"command": "ls\x00", "cwd": ROOT}, "out_of_range"),
    ({"command": "ls", "cwd": "/Users/sam/Other"}, "cwd_outside_workspace"),
    ({"command": "ls", "cwd": ROOT, "env": {"BAD-NAME": "x"}}, "env_forbidden"),
    ({"command": "ls", "cwd": ROOT, "env": {"app_lower": "x"}}, "env_forbidden"),
    ({"command": "ls", "cwd": ROOT, "env": {"APP_" + "X" * 61: "x"}}, "env_forbidden"),
    ({"command": "ls", "cwd": ROOT, "env": {"A": 1}}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "env": {"LANG": "C\nPATH=/tmp"}}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "env": {"LANG": "C\rX"}}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "env": {"LANG": "x" * 4097}}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "env": {f"V{i}": "x" for i in range(33)}}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "env": "A=1"}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "timeout_s": 0}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "timeout_s": 601}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "timeout_s": 3601, "background": True}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "timeout_s": "5"}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "background": "yes"}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "stdin": "x" * 16385}, "out_of_range"),
    ({"command": "ls", "cwd": ROOT, "stdin": 5}, "out_of_range"),
])
async def test_invalid_commands_are_refused_before_launch(world, args, code):
    result = await world.call("run_command", **args)
    assert result["_data"]["code"] == code and not world.requests("run_command")


SHARED = [{"name": "x", "path": ROOT}, {"name": "AstralTest", "path": "/Users/sam/AstralTest"}]


@pytest.mark.parametrize("cwd,resolved", [
    (ROOT + "/notes", ROOT + "/notes"), ("//Users/sam/Projects/x/./a/", ROOT + "/a"), ("x", ROOT),
    ("x/notes/", ROOT + "/notes"), ("AstralTest/sub", "/Users/sam/AstralTest/sub"),
    ("~/AstralTest", "/Users/sam/AstralTest"), ("~/Projects/x/./a", ROOT + "/a"),
])
def test_cwd_resolves_absolute_root_named_and_home_relative_folders(cwd, resolved):
    assert policy.resolve_cwd(cwd, SHARED, "Mac") == resolved


@pytest.mark.parametrize("cwd,roots,code,fragment", [
    ("/Users/riley/AstralTest", SHARED, "cwd_outside_workspace", "AstralTest = /Users/sam/AstralTest"),
    ("~/Documents", SHARED, "cwd_outside_workspace", "x = " + ROOT),
    ("/Users/sam/Projects/xy", SHARED, "cwd_outside_workspace", "outside every folder"),
    ("~riley/x", SHARED, "out_of_range", "name of one shared folder"),
    ("notes", SHARED, "out_of_range", "name of one shared folder"),
    ("x/../y", SHARED, "out_of_range", "shared folder"), ("", SHARED, "out_of_range", "shared folder"),
    (None, SHARED, "out_of_range", "shared folder"), (5, SHARED, "out_of_range", "shared folder"),
    ("x\x00", SHARED, "out_of_range", "shared folder"), ("/" + "a" * 1024, SHARED, "out_of_range", "shared folder"),
    (ROOT, [], "cwd_outside_workspace", "Mac shares no workspace folder"),
    ("~/x", [{"name": "a", "path": "/Users/a/x"}, {"name": "b", "path": "/Users/b/y"}], "out_of_range", "~"),
    ("~/x", [{"name": "data", "path": "/Volumes/Data/x"}], "out_of_range", "~"),
    ("src/a", [{"name": "src", "path": "/p1"}, {"name": "src", "path": "/p2"}], "out_of_range", "/p1"),
])
def test_cwd_outside_or_unresolvable_names_the_shared_folders(cwd, roots, code, fragment):
    with pytest.raises(policy.CwdRefusal) as refused:
        policy.resolve_cwd(cwd, roots, "Mac")
    assert refused.value.code == code and fragment in str(refused.value)


def test_cwd_prefers_the_longest_matching_folder_name():
    roots = [{"name": "proj", "path": "/p1"}, {"name": "proj/sub", "path": "/p2"}]
    assert policy.resolve_cwd("proj/sub/x", roots, "Mac") == "/p2/x"
    assert policy.resolve_cwd("proj/other", roots, "Mac") == "/p1/other"


@pytest.mark.parametrize("cwd,sent", [("x/notes", ROOT + "/notes"), ("~/Projects/x", ROOT)])
async def test_run_command_sends_the_resolved_absolute_folder(world, gate, cwd, sent):
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "", "stderr": ""}}
    result = await world.call("run_command", argv=["ls"], cwd=cwd)
    assert result["_data"]["exit_code"] == 0 and world.requests("run_command")[-1]["args"]["cwd"] == sent


@pytest.mark.parametrize("verb,args,deadline", [
    ("run_command", {"timeout_s": 60}, 70.0), ("run_command", {}, 70.0), ("run_command", {"timeout_s": 600}, 610.0),
    ("run_command", {"timeout_s": 5000}, 610.0), ("run_command", {"timeout_s": "5"}, 70.0),
    ("run_command", {"timeout_s": True}, 70.0), ("run_command", {"timeout_s": float("nan")}, 70.0),
    ("run_command", {"timeout_s": 60, "background": True}, 30.0), ("click", {}, 10.0), ("unknown_verb", {}, 30.0),
])
def test_dispatch_deadline_is_the_host_deadline_plus_grace(verb, args, deadline):
    assert policy.host_deadline(verb, args) == deadline
    assert policy.dispatch_timeout(verb, args) == deadline + policy.HOST_DEADLINE_GRACE_S


async def test_the_dispatcher_bounds_computer_tools_by_their_own_deadline():
    from types import SimpleNamespace

    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    seen = []

    async def execute_tool_and_wait(agent, tool, args, timeout=None, **kwargs):
        seen.append((agent, tool, timeout))
        return MCPResponse(result={"_data": {"ok": True}})
    dispatcher = SimpleNamespace(MAX_RETRIES=1, RETRY_BACKOFF=[], execute_tool_and_wait=execute_tool_and_wait,
                                 _protected_dispatch_channel=lambda websocket, explicit=None: "web")
    for agent, tool, args in (("computer-use-1", "run_command", {"timeout_s": 120}),
                              ("computer-use-1", "screenshot", {}), ("general-1", "run_command", {"timeout_s": 120})):
        await Orchestrator._execute_with_retry_loop(dispatcher, None, agent, tool, args, user_id=OWNER,
                                                    audit_conversation_id=CHAT)
    assert seen == [("computer-use-1", "run_command", 130.0 + policy.HOST_DEADLINE_GRACE_S),
                    ("computer-use-1", "screenshot", 15.0 + policy.HOST_DEADLINE_GRACE_S),
                    ("general-1", "run_command", 30.0)]


async def test_a_host_that_never_answers_yields_a_clear_command_error(world, gate, monkeypatch):
    monkeypatch.setitem(policy.TIMEOUTS, "run_command", 0.2)
    world.host_ws.push = world.host_ws.sent.append
    result = await world.call("run_command", argv=["sleep", "30"], cwd=ROOT, timeout_s=1)
    assert result["_data"]["code"] == "outcome_uncertain"
    assert "Mac did not answer run_command" in result["_data"]["message"]
    assert not world.orch.computer_hosts._pending


@pytest.mark.parametrize("verb,code", [("click", "outcome_uncertain"), ("screenshot", "timeout")])
async def test_a_host_send_that_never_completes_is_bounded_by_the_request_deadline(world, verb, code):
    async def stuck(_ws, _data):
        await asyncio.Event().wait()

    world.orch._safe_send = stuck
    try:
        with pytest.raises(ComputerHostError) as refused:
            await world.orch.computer_hosts.request(world.host, world.session.session_id, verb, {}, 0.05)
    finally:
        del world.orch._safe_send
    assert refused.value.code == code and "did not answer" in refused.value.message
    assert not world.orch.computer_hosts._pending


async def test_background_jobs_are_tracked_read_and_interrupted(world, gate):
    world.replies["run_command"] = {"ok": True, "result": {"job_id": JOB.upper(), "started_at": "2026-10-26T00:00:00Z"}}
    started = await world.call("run_command", argv=["tail", "-f", "log.txt"], cwd=ROOT, background=True,
                               timeout_s=3600, env={"LANG": "C"}, stdin="in")
    assert started["_data"]["job_id"] == JOB and started["_data"]["background"] is True
    assert world.session.command_jobs[JOB]["state"] == "running"
    assert world.requests("run_command")[-1]["args"]["env"] == {"LANG": "C"}
    assert 25000 < world.requests("run_command")[-1]["deadline_ms"] <= 30000
    world.replies["read_output"] = {"ok": True, "result": {
        "job_id": JOB, "state": "running", "exit_code": None, "next_offset": 12, "truncated": False,
        "chunks": [{"stream": "stdout", "offset": 0, "data": "line 123-45-6789\n"}, "junk",
                   {"stream": "evil", "offset": 0, "data": "x"}]}}
    read = (await world.call("read_output", job_id=JOB, wait_s=2))["_data"]
    assert read["chunks"] == [{"stream": "stdout", "offset": 0, "data": "line [REDACTED:phi]\n"}]
    assert read["next_offset"] == 12 and read["state"] == "running" and read["untrusted_output"] is True
    assert world.requests("read_output")[-1]["args"] == {"job_id": JOB, "stream": "both", "offset": 0,
                                                         "max_bytes": 65536, "wait_s": 2}
    world.replies["interrupt_command"] = {"ok": True, "result": {"job_id": JOB, "state": "interrupted",
                                                                  "exit_code": 130}}
    stopped = (await world.call("interrupt_command", job_id=JOB, signal="INT"))["_data"]
    assert stopped == {"computer": "Mac", "job_id": JOB, "state": "interrupted", "exit_code": 130}
    assert world.session.command_jobs[JOB]["state"] == "interrupted"
    world.replies["read_output"] = {"ok": True, "result": {"job_id": JOB, "state": "interrupted",
                                                            "exit_code": 130, "chunks": [], "next_offset": 12}}
    assert (await world.call("read_output", job_id=JOB, offset=12))["_data"]["state"] == "interrupted"


@pytest.mark.parametrize("tool,args", [("read_output", {"job_id": str(uuid.uuid4())}),
                                       ("interrupt_command", {"job_id": str(uuid.uuid4()), "signal": "KILL"}),
                                       ("read_output", {"job_id": "not-a-uuid"}),
                                       ("interrupt_command", {"job_id": JOB, "signal": "HUP"}),
                                       ("read_output", {"job_id": JOB, "stream": "logs"}),
                                       ("read_output", {"job_id": JOB, "wait_s": 26}),
                                       ("read_output", {"job_id": JOB, "max_bytes": 0}),
                                       ("read_output", {"job_id": JOB, "offset": -1})])
async def test_unknown_jobs_and_bad_reads_are_refused_without_a_host_request(world, tool, args):
    world.session.command_jobs[JOB] = {"state": "running"}
    result = await world.call(tool, **args)
    assert result["_data"]["code"] in {"unknown_job", "out_of_range"} and not world.requests(tool)


async def test_host_job_ids_must_be_uuids_and_the_job_table_is_bounded(world, gate):
    world.replies["run_command"] = {"ok": True, "result": {"job_id": "../evil", "started_at": "x"}}
    bad = await world.call("run_command", command="ls", cwd=ROOT, background=True)
    assert bad["_data"]["code"] == "failed" and not world.session.command_jobs
    for index in range(64):
        world.session.command_jobs[str(uuid.UUID(int=index))] = {"state": "exited" if index else "running"}
    world.replies["run_command"] = {"ok": True, "result": {"job_id": JOB, "started_at": "x"}}
    await world.call("run_command", command="ls", cwd=ROOT, background=True)
    assert len(world.session.command_jobs) == 64 and JOB in world.session.command_jobs
    assert str(uuid.UUID(int=0)) in world.session.command_jobs


async def test_command_tools_do_not_wait_behind_the_physical_effect_lock(world, gate):
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "", "stderr": ""}}
    await world.session.lock.acquire()
    try:
        result = await asyncio.wait_for(world.call("run_command", command="ls", cwd=ROOT), 5)
    finally:
        world.session.lock.release()
    assert result["_data"]["exit_code"] == 0


async def test_host_refusals_surface_their_codes(world, gate):
    world.replies["run_command"] = {"ok": False, "error": {"code": "cwd_outside_workspace", "message": "outside"}}
    result = await world.call("run_command", command="ls", cwd=ROOT)
    assert result["_data"]["code"] == "cwd_outside_workspace"


def test_command_policy_bounds_match_the_host_protocol():
    assert policy.MAX_COMMAND_CHARS == 4096 and policy.MAX_ARGV_ITEMS == 256 and policy.MAX_ENV_VARS == 32
    assert ComputerHostError("x", "y").code == "x"


@pytest.mark.parametrize("name", [
    "ASTRAL_TOKEN", "GITHUB_TOKEN", "MY_SECRET", "API_KEY", "PASSWORD", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP", "NODE_OPTIONS", "NPM_CONFIG_PREFIX", "PERL5OPT", "RUBYOPT",
    "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "BASH_ENV", "ENV", "ZDOTDIR", "PATH", "IFS", "GCONV_PATH", "SHELL",
    "HOME", "TMPDIR", "EDITOR", "PAGER", "VISUAL",
])
async def test_env_names_outside_the_allowlist_are_forbidden(world, name):
    result = await world.call("run_command", command="ls", cwd=ROOT, env={name: "x"})
    assert result["_data"]["code"] == "env_forbidden" and not world.requests("run_command")


@pytest.mark.parametrize("name", ["LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "TERM", "NO_COLOR",
                                  "FORCE_COLOR", "CI", "DEBUG", "COLUMNS", "LINES", "APP_MODE"])
async def test_allowlisted_env_names_reach_the_host(world, gate, name):
    world.replies["run_command"] = {"ok": True, "result": {"exit_code": 0, "stdout": "", "stderr": ""}}
    await world.call("run_command", command="ls", cwd=ROOT, env={name: "1"})
    assert world.requests("run_command")[-1]["args"]["env"] == {name: "1"}


async def test_a_host_without_shared_folders_refuses_every_cwd(world):
    world.host.descriptor["workspace_roots"] = []
    result = await world.call("run_command", command="ls", cwd=ROOT)
    assert result["_data"]["code"] == "cwd_outside_workspace" and "shares no workspace folder" in \
        result["_data"]["message"]
    assert not world.requests("run_command")


async def test_run_command_refuses_when_the_host_review_is_not_current(world, monkeypatch):
    from orchestrator import terminal_host_review

    monkeypatch.setattr(terminal_host_review, "computer_use_review_current", lambda _orch: False)
    result = await world.call("run_command", command="ls", cwd=ROOT)
    assert result["_data"]["code"] == "security_blocked" and not world.requests("run_command")


@pytest.mark.parametrize("state", ["not_started", "uncertain", None])
async def test_host_typed_outcome_survives_the_mcp_error_boundary(world, state):
    from agents.computer_use.mcp_server import MCPServer

    error = {"code": "permission_denied", "message": "synthetic host denial"}
    if state is not None:
        error["effect_state"] = state
    world.replies["write_file"] = {"ok": False, "error": error}
    response = await asyncio.to_thread(MCPServer().process_request, MCPRequest(
        method="tools/call", params={"name": "write_file", "arguments": {
            **CTX, "path": ROOT + "/notes.md", "content": "safe"}}))
    assert response.error["code"] == "permission_denied"
    assert response.error.get("effect_state") == ("not_started" if state == "not_started" else None)


async def test_file_effect_rechecks_the_current_host_roots(world):
    world.host.descriptor["workspace_roots"] = [{"name": "other", "path": ROOT + "-other"}]
    response = await world.call("write_file", path=ROOT + "/notes.md", content="safe")
    assert response["_data"]["code"] == "path_outside_workspace"
    assert response["_data"]["effect_state"] == "not_started" and not world.requests("write_file")


async def test_file_effect_does_not_reinterpret_relative_reviewed_arguments(world):
    response = await world.call("write_file", path="notes.md", content="safe")
    assert response["_data"]["code"] == "path_outside_workspace"
    assert response["_data"]["effect_state"] == "not_started" and not world.requests("write_file")


@pytest.mark.parametrize("action,verb", [("status", "read_output"), ("stop", "interrupt_command")])
@pytest.mark.parametrize("code", ["permission_denied", "stale_project_grant", "confirmation_required"])
def test_preview_lifecycle_preserves_authority_failures(monkeypatch, action, verb, code):
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_tools, "_ctx", lambda _args: (
        OWNER, CHAT, SimpleNamespace(name="Mac", project_mode=True), SimpleNamespace(session_id="session")))
    denied = mcp_tools._fail(code, "Authority changed.")
    monkeypatch.setattr(mcp_tools, verb, lambda **_kwargs: denied)
    assert mcp_tools.project_preview(action=action, cwd=ROOT, job_id=JOB) == denied


@pytest.mark.parametrize("action,verb", [("status", "read_output"), ("stop", "interrupt_command")])
@pytest.mark.parametrize("data", [{"state": "uncertain", "job_id": JOB},
                                    {"state": "exited", "job_id": "another-job"}, None])
def test_preview_lifecycle_never_claims_stop_without_same_job_proof(monkeypatch, action, verb, data):
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_tools, "_ctx", lambda _args: (
        OWNER, CHAT, SimpleNamespace(name="Mac", project_mode=True), SimpleNamespace(session_id="session")))
    monkeypatch.setattr(mcp_tools, verb, lambda **_kwargs: {"_data": data})
    result = mcp_tools.project_preview(action=action, cwd=ROOT, job_id=JOB)
    assert result["_data"]["code"] == f"preview_{action}_uncertain"


@pytest.mark.parametrize("phase", ["manifest", "first_status", "probe", "last_status"])
@pytest.mark.parametrize("malformed", [False, True])
def test_preview_start_denied_or_malformed_checks_never_publish_running(monkeypatch, phase, malformed):
    from types import SimpleNamespace

    host = SimpleNamespace(name="Mac", project_mode=True)
    session = SimpleNamespace(session_id="session", command_jobs={})
    monkeypatch.setattr(mcp_tools, "_ctx", lambda _args: (OWNER, CHAT, host, session))
    monkeypatch.setattr(mcp_tools, "_command_args", lambda args, _host: args)
    denied = {"_data": None} if malformed else mcp_tools._fail("permission_denied", "Grant changed.")
    manifest = {"_data": {"text": '{"scripts":{"dev":"vite"}}', "truncated": False}}
    monkeypatch.setattr(mcp_tools, "read_file", lambda **_kwargs: denied if phase == "manifest" else manifest)
    status_count = 0

    def status(**kwargs):
        nonlocal status_count
        status_count += 1
        if phase == ("first_status" if status_count == 1 else "last_status"):
            return denied
        return {"_data": {"job_id": JOB, "state": "running"}}

    def run(**kwargs):
        if kwargs.get("background"):
            return {"_data": {"job_id": JOB}}
        return denied if phase == "probe" else {"_data": {"exit_code": 0}}

    monkeypatch.setattr(mcp_tools, "read_output", status)
    monkeypatch.setattr(mcp_tools, "run_command", run)
    cleanup = []
    monkeypatch.setattr(mcp_tools, "interrupt_command", lambda **args: cleanup.append(args))
    result = mcp_tools.project_preview(action="start", cwd=ROOT, package_manager="npm", script="dev")
    if malformed:
        assert result["_data"]["code"] == ("preview_unsupported" if phase == "manifest" else "preview_unavailable")
    else:
        assert result == denied
    assert len(cleanup) == (0 if phase == "manifest" else 1)


@pytest.mark.parametrize("action,verb", [("status", "read_output"), ("stop", "interrupt_command")])
@pytest.mark.parametrize("manager,script", [("npm", "dev"), ("pnpm", "preview"), ("yarn", "start"), ("bun", "dev")])
def test_confirmed_stopped_preview_can_restart_with_source_bound_launch_arguments(monkeypatch, action, verb, manager, script):
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_tools, "_ctx", lambda _args: (
        OWNER, CHAT, SimpleNamespace(name="Mac", project_mode=True), SimpleNamespace(session_id="session")))
    monkeypatch.setattr(mcp_tools, verb, lambda **_kwargs: {"_data": {"job_id": JOB, "state": "interrupted"}})
    original = {"action": action, "cwd": ROOT, "package_manager": manager, "script": script, "job_id": JOB}
    result = mcp_tools.project_preview(**original)
    button = result["_ui_components"][0]["content"][-1]
    assert button["label"] == "Start preview" and button["action"] == "component_action"
    assert button["payload"] == {"kind": "invoke", "params_patch": {"action": "start"}}
    assert {**original, **button["payload"]["params_patch"]} == {
        "action": "start", "cwd": ROOT, "package_manager": manager, "script": script, "job_id": JOB}


@pytest.mark.parametrize("launch", [{}, {"package_manager": "npm"}, {"script": "dev"},
                                    {"package_manager": "other", "script": "dev"},
                                    {"package_manager": "npm", "script": "arbitrary"},
                                    {"package_manager": "npm", "script": "dev", "cwd": "relative"}])
def test_stopped_arbitrary_job_does_not_invent_preview_launch_arguments(launch):
    card = mcp_tools._stopped_preview_card("Stopped", {"cwd": ROOT, "job_id": JOB, **launch}).to_dict()
    assert len(card["content"]) == 1
