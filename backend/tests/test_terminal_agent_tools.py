"""Tests for the terminal-shell agent tools (agents/terminal_shell/): owner binding,
malformed input, session lifecycle through the tool layer, MCP channel framing, the
policy approval replay boundary, and the permission/safe-agent denial order.
"""

from __future__ import annotations

import base64
import inspect
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from agents.terminal_shell import mcp_tools  # noqa: E402
from agents.terminal_shell.mcp_server import MCPServer, bind_owner_context  # noqa: E402
from agents.terminal_shell.mcp_tools import TOOL_REGISTRY  # noqa: E402
from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent  # noqa: E402
from orchestrator import terminal_sessions as ts  # noqa: E402
from orchestrator.terminal_sessions import TerminalSessionManager  # noqa: E402
from shared.protocol import MCPRequest  # noqa: E402
from orchestrator.orchestrator import PreparedDispatch  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

OWNER = "user-1"
OTHER = "user-2"

MUTATING_TOOLS = ("start_terminal", "terminal_write", "terminal_resize",
                  "terminal_interrupt", "close_terminal")
READING_TOOLS = ("list_terminals", "terminal_read")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _text(payload: dict) -> str:
    return base64.b64decode(payload.get("data_b64") or "").decode("utf-8", "replace")


def _read_until(manager, owner, session_id, needle, *, offset=0, timeout=20.0):
    accumulated: list[str] = []
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = manager.read(owner, session_id, offset=offset, wait_seconds=1.0)
        accumulated.append(_text(last))
        offset = last["next_offset"]
        if needle in "".join(accumulated) or last["eof"]:
            break
    return last, "".join(accumulated)


def _read_to_eof(manager, owner, session_id, *, offset=0, timeout=20.0):
    accumulated: list[str] = []
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = manager.read(owner, session_id, offset=offset, wait_seconds=1.0)
        accumulated.append(_text(last))
        offset = last["next_offset"]
        if last["eof"]:
            break
    return last, "".join(accumulated)


@pytest.fixture
def env(monkeypatch):
    manager = TerminalSessionManager()
    orch = SimpleNamespace(terminal_sessions=manager)
    mcp_tools.register_deps(orch)
    yield SimpleNamespace(
        manager=manager, orch=orch, registry=mcp_tools.TOOL_REGISTRY,
        tools={name: info["function"] for name, info in mcp_tools.TOOL_REGISTRY.items()})
    manager.shutdown()
    mcp_tools.register_deps(None)


@pytest.fixture
def stub_env(monkeypatch):
    calls: list = []

    class StubManager:
        def __init__(self, **behaviors):
            self.behaviors = behaviors

        def _call(self, name, *args, **kwargs):
            calls.append((name, args, kwargs))
            behavior = self.behaviors.get(name)
            if isinstance(behavior, Exception):
                raise behavior
            if callable(behavior):
                return behavior(*args, **kwargs)
            if behavior is None:
                raise AssertionError(f"unexpected {name} call")
            return behavior

        def list_sessions(self, owner_id):
            return self._call("list_sessions", owner_id)

        def create(self, owner_id, **kwargs):
            return self._call("create", owner_id, **kwargs)

        def read(self, owner_id, session_id, **kwargs):
            return self._call("read", owner_id, session_id, **kwargs)

        def write(self, owner_id, session_id, data_b64):
            return self._call("write", owner_id, session_id, data_b64)

        def resize(self, owner_id, session_id, **kwargs):
            return self._call("resize", owner_id, session_id, **kwargs)

        def interrupt(self, owner_id, session_id):
            return self._call("interrupt", owner_id, session_id)

        def close(self, owner_id, session_id):
            return self._call("close", owner_id, session_id)

    orch = SimpleNamespace()
    mcp_tools.register_deps(orch)
    yield SimpleNamespace(orch=orch, calls=calls, stub=StubManager)
    mcp_tools.register_deps(None)


def _alert_text(result: dict) -> str:
    comps = result.get("_ui_components") or []
    for comp in comps:
        if comp.get("type") == "alert":
            return str(comp.get("message", ""))
    return ""


def _data_of(result: dict) -> dict:
    return result.get("_data") or {}


class TestAgentWiring:
    def test_flag_gates_discovery_and_reserves_the_agent_id(self, monkeypatch):
        from orchestrator import local_agents
        from shared.feature_flags import flags
        monkeypatch.setattr(flags, "is_enabled", lambda name: False)
        assert "terminal_shell" not in local_agents.discover_built_in_agent_dirs()
        assert "terminal_shell" in local_agents._TERMINAL_SHELL_AGENT_DIRS
        assert "terminal-shell-1" in local_agents.FIRST_PARTY_PUBLIC_AGENT_IDS

    def _registration(self, monkeypatch):
        from orchestrator import local_agents
        from shared import attachment_materializer, attachment_resolver
        attempted: list = []
        registered: list = []

        def _bind(runtime, repositories, blobs):
            return True

        def _bind_materializer(service):
            return True

        async def _register(ws, msg):
            registered.append(msg.agent_card.agent_id)

        monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs",
                            lambda *a, **k: [])
        monkeypatch.setattr(local_agents, "_load_agent_class",
                            lambda d: attempted.append(d) or TerminalShellAgent)
        monkeypatch.setattr(attachment_resolver, "register_plane_runtime", _bind)
        monkeypatch.setattr(attachment_materializer,
                            "register_materialization_service", _bind_materializer)
        plane = SimpleNamespace(
            runtime=object(), repositories=object(), blobs=object(),
            attachment_materializer=object())
        orch = SimpleNamespace(
            local_agents={}, register_agent=_register,
            runtime_composition=SimpleNamespace(plane=plane))
        return orch, attempted, registered

    async def _assert_registered(self, monkeypatch):
        monkeypatch.setenv("AGENT_KEY_PATH", str(Path(self._tmp) / "agent.pem"))
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == ["terminal-shell-1"]
        assert attempted == ["terminal_shell"] and registered == ["terminal-shell-1"]
        agent = orch.local_agents["terminal-shell-1"]
        assert {s.id for s in agent.card.skills} == set(TOOL_REGISTRY)
        response = agent.mcp_server.process_request(MCPRequest(
            request_id="req_agent", method="tools/call",
            params={"name": "list_terminals", "arguments": {"user_id": OWNER}}))
        assert response.error and "terminal_unavailable" in response.error["message"]

    async def test_flag_on_registration_binds_the_orchestrator(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)
        monkeypatch.setattr(flags, "is_enabled", lambda name: name == "terminal_shell")
        await self._assert_registered(monkeypatch)

    async def test_flag_off_registration_never_loads_the_agent(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)
        monkeypatch.setattr(flags, "is_enabled", lambda name: False)
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == []
        assert attempted == [] and registered == [] and orch.local_agents == {}

    async def test_flag_check_failure_leaves_the_agent_unregistered(self, monkeypatch, tmp_path):
        from shared.feature_flags import flags
        self._tmp = str(tmp_path)

        def _boom(name):
            if name == "terminal_shell":
                raise RuntimeError("flag store unavailable")
            return False

        monkeypatch.setattr(flags, "is_enabled", _boom)
        orch, attempted, registered = self._registration(monkeypatch)
        from orchestrator import local_agents
        assert await local_agents.register_built_ins(orch) == []
        assert attempted == [] and registered == [] and orch.local_agents == {}

    def test_agent_requires_the_orchestrator(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
        with pytest.raises(RuntimeError):
            TerminalShellAgent()


class TestGateStackBinding:
    def _orch(self):
        from orchestrator.concurrency_cap import ConcurrencyCap
        from orchestrator.hooks import HookManager
        from orchestrator.orchestrator import Orchestrator

        o = Orchestrator.__new__(Orchestrator)
        o.agents = {}
        o.a2a_clients = {}
        o.local_agents = {"terminal-shell-1": MagicMock()}
        o.agent_cards = {}
        o.ui_sessions = {}
        o.security_flags = {}
        o._pending_cap_entries = {}
        o._hop_cap_entries = {}
        o._job_context = {}
        o.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
        o.hooks = HookManager()
        o.stream_manager = None
        o.audit_recorder = MagicMock()
        o.audit_recorder.record = AsyncMock()
        o.send_ui_render = AsyncMock()
        o.tool_permissions = MagicMock()
        o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
        o._map_file_paths = lambda cid, a, **k: a
        o.credential_manager = MagicMock()
        o.credential_manager.get_agent_credentials_encrypted = MagicMock(
            return_value=None)
        o._llm_store = SimpleNamespace(
            get=AsyncMock(return_value=None),
            get_system=AsyncMock(return_value=None))
        return o

    async def test_gate_stack_binds_the_dispatching_owner_over_model_args(self, monkeypatch):
        monkeypatch.setenv("ASTRAL_ENV", "development")
        monkeypatch.delenv("DELEGATION_REQUIRED", raising=False)
        orch = self._orch()
        out = await orch._authorize_and_prepare(
            MagicMock(), "terminal-shell-1", "terminal_write",
            {"user_id": "attacker", "session_id": "s", "data": "x\n"},
            None, OWNER)
        assert isinstance(out, PreparedDispatch)
        assert out.args["user_id"] == OWNER

    async def test_gate_stack_leaves_no_owner_when_unauthenticated(self, monkeypatch):
        monkeypatch.setenv("ASTRAL_ENV", "development")
        monkeypatch.delenv("DELEGATION_REQUIRED", raising=False)
        orch = self._orch()
        out = await orch._authorize_and_prepare(
            MagicMock(), "terminal-shell-1", "terminal_write",
            {"user_id": "attacker", "session_id": "s", "data": "x\n"},
            None, None)
        assert isinstance(out, PreparedDispatch)
        assert out.args["user_id"] is None


class TestRegistryContract:
    def test_registry_is_the_complete_session_surface(self):
        assert set(TOOL_REGISTRY) == {
            "list_terminals", "start_terminal", "terminal_read", "terminal_write",
            "terminal_resize", "terminal_interrupt", "close_terminal",
        }

    def test_every_entry_is_complete_and_schema_valid(self):
        for name, info in TOOL_REGISTRY.items():
            assert callable(info["function"]), name
            assert info["description"] and info["scope"], name
            assert info["destructive"] == "never", name
            assert info["retryable"] is False and info["timeout"] > 0, name
            schema = info["input_schema"]
            assert schema["type"] == "object" and isinstance(schema.get("properties"), dict), name
            for required in schema.get("required", []):
                assert required in schema["properties"], name

    def test_scopes_split_read_from_mutating_verbs(self):
        for name in READING_TOOLS:
            assert TOOL_REGISTRY[name]["scope"] == "tools:read", name
        for name in MUTATING_TOOLS:
            assert TOOL_REGISTRY[name]["scope"] == "tools:write", name

    def test_every_tool_accepts_the_injected_owner_context(self):
        for name, info in TOOL_REGISTRY.items():
            params = inspect.signature(info["function"]).parameters
            assert any(p.kind == inspect.Parameter.VAR_KEYWORD
                       for p in params.values()), name

    def test_schemas_do_not_let_the_model_supply_the_owner(self):
        for name, info in TOOL_REGISTRY.items():
            assert "user_id" not in info["input_schema"]["properties"], name

    def test_server_serves_the_registry_and_refuses_unknowns(self):
        server = MCPServer()
        listed = {tool["name"] for tool in server.get_tool_list()}
        assert listed == set(TOOL_REGISTRY)
        response = server.process_request(MCPRequest(
            request_id="r1", method="tools/call",
            params={"name": "no_such_tool", "arguments": {}}))
        assert response.error and "Unknown tool" in response.error["message"]
        response = server.process_request(MCPRequest(
            request_id="r2", method="bogus/method", params={}))
        assert response.error and "Unknown method" in response.error["message"]


class TestOwnerBinding:
    def test_missing_owner_is_refused_without_creating_sessions(self, env):
        result = env.tools["list_terminals"]()
        assert _data_of(result)["code"] == "terminal_owner_required"
        result = env.tools["start_terminal"](user_id="")
        assert _data_of(result)["code"] == "terminal_owner_required"
        result = env.tools["terminal_write"](session_id="abc", data="ls\n")
        assert _data_of(result)["code"] == "terminal_owner_required"
        assert env.manager.list_sessions(OWNER) == []

    def test_server_binding_overwrites_a_model_supplied_owner(self):
        args = {"user_id": "attacker", "session_id": "s"}
        bind_owner_context(args, OWNER)
        assert args["user_id"] == OWNER

    def test_unauthenticated_binding_leaves_no_owner_to_forgesh(self, env):
        args = {"user_id": "attacker"}
        bind_owner_context(args, None)
        assert args["user_id"] is None
        result = mcp_tools.list_terminals(**args)
        assert _data_of(result)["code"] == "terminal_owner_required"
        assert env.manager.list_sessions(OWNER) == []

    def test_sessions_never_leak_across_owners(self, env):
        info = env.tools["start_terminal"](user_id=OWNER, command="echo owner_marker")
        session_id = _data_of(info)["session"]["session_id"]
        assert _data_of(env.tools["list_terminals"](user_id=OTHER))["sessions"] == []
        for tool, kwargs in (("terminal_read", {"offset": 0}),
                             ("terminal_write", {"data": "x\n"}),
                             ("terminal_resize", {"cols": 80, "rows": 24}),
                             ("terminal_interrupt", {}),
                             ("close_terminal", {})):
            result = env.tools[tool](user_id=OTHER, session_id=session_id, **kwargs)
            assert _data_of(result)["code"] == "terminal_session_not_found", tool
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)


class TestLifecycle:
    def test_start_list_and_close_round_trip(self, env):
        result = env.tools["start_terminal"](user_id=OWNER)
        data = _data_of(result)
        session_id = data["session"]["session_id"]
        assert session_id and result["_ui_components"][0]["type"] == "card"
        listed = _data_of(env.tools["list_terminals"](user_id=OWNER))["sessions"]
        assert [s["session_id"] for s in listed] == [session_id]
        closed = _data_of(env.tools["close_terminal"](user_id=OWNER,
                                                      session_id=session_id))
        assert closed["closed"] is True and closed["reason"] == "owner_close"
        assert _data_of(env.tools["list_terminals"](user_id=OWNER))["sessions"] == []
        again = env.tools["close_terminal"](user_id=OWNER, session_id=session_id)
        assert _data_of(again)["closed"] is True
        assert _data_of(again)["reason"] == "owner_close"

    def test_command_mode_runs_to_completion_and_reports_eof(self, env):
        result = env.tools["start_terminal"](user_id=OWNER,
                                             command="echo tool_eof_marker_42")
        session_id = _data_of(result)["session"]["session_id"]
        out, text = _read_to_eof(env.manager, OWNER, session_id)
        assert "tool_eof_marker_42" in text and out["eof"] is True
        read = env.tools["terminal_read"](user_id=OWNER, session_id=session_id, offset=0)
        assert _data_of(read)["eof"] is True

    def test_write_then_read_round_trips(self, env):
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        sent = env.tools["terminal_write"](user_id=OWNER, session_id=session_id,
                                           data="echo round_$((19+4))_trip\n")
        assert _data_of(sent)["sent_bytes"] == len(b"echo round_$((19+4))_trip\n")
        _, text = _read_until(env.manager, OWNER, session_id, "round_23_trip")
        assert "round_23_trip" in text
        read = env.tools["terminal_read"](user_id=OWNER, session_id=session_id, offset=0,
                                          wait_seconds=2.0)
        data = _data_of(read)
        assert "round_23_trip" in data["text"] and data["next_offset"] > 0

    def test_read_with_no_new_output_is_truthful(self, env):
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        end = env.manager.list_sessions(OWNER)[0]["ring_end"]
        read = env.tools["terminal_read"](user_id=OWNER, session_id=session_id,
                                          offset=end, wait_seconds=0)
        data = _data_of(read)
        assert data["bytes"] == 0 and data["text"] == ""
        assert "(no new output yet)" in json.dumps(result_texts(read))
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)

    def test_stale_offset_is_reported_not_silent(self, env, monkeypatch):
        monkeypatch.setattr(ts, "OUTPUT_RING_BYTES", 16)
        info = env.tools["start_terminal"](user_id=OWNER, command="sleep 30")
        session_id = _data_of(info)["session"]["session_id"]
        env.tools["terminal_write"](user_id=OWNER, session_id=session_id,
                                    data="x" * 40)
        stale = env.tools["terminal_read"](user_id=OWNER, session_id=session_id,
                                           offset=0)
        data = _data_of(stale)
        assert data["truncated"] is True and data["bytes"] > 0
        assert "retained" in json.dumps(stale["_ui_components"])
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)

    def test_interrupt_stops_the_foreground_command(self, env):
        info = env.tools["start_terminal"](user_id=OWNER, command="sleep 45")
        session_id = _data_of(info)["session"]["session_id"]
        interrupted = env.tools["terminal_interrupt"](user_id=OWNER,
                                                      session_id=session_id)
        assert interrupted["_ui_components"][0]["type"] == "card"
        out, _ = _read_to_eof(env.manager, OWNER, session_id, timeout=25.0)
        assert out["eof"] is True

    def test_spawned_shells_start_interruptible_despite_parent_pollution(self, env):
        import signal as _signal
        previous = {
            sig: _signal.getsignal(sig)
            for sig in (_signal.SIGHUP, _signal.SIGINT, _signal.SIGQUIT)
        }
        try:
            for sig in previous:
                _signal.signal(sig, _signal.SIG_IGN)
            info = env.tools["start_terminal"](user_id=OWNER, command="sleep 60")
            session_id = _data_of(info)["session"]["session_id"]
            session = env.manager._sessions[session_id]
            child = session.process.pid
            ign = next(
                int(line.split()[1], 16)
                for line in open(f"/proc/{child}/status")
                if line.startswith("SigIgn:")
            )
            assert ign & 0b111 == 0
            env.tools["terminal_interrupt"](user_id=OWNER, session_id=session_id)
            deadline = time.monotonic() + 8
            while not session.exited() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert session.exited()
        finally:
            for sig, handler in previous.items():
                _signal.signal(sig, handler)

    def test_resize_updates_the_session(self, env):
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        resized = env.tools["terminal_resize"](user_id=OWNER, session_id=session_id,
                                               cols=120, rows=40)
        assert (_data_of(resized)["session"]["cols"],
                _data_of(resized)["session"]["rows"]) == (120, 40)
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)

    def test_closed_session_refuses_further_use(self, env):
        info = env.tools["start_terminal"](user_id=OWNER, command="sleep 45")
        session_id = _data_of(info)["session"]["session_id"]
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)
        for tool, kwargs in (("terminal_write", {"data": "x\n"}),
                             ("terminal_resize", {"cols": 80, "rows": 24}),
                             ("terminal_interrupt", {})):
            result = env.tools[tool](user_id=OWNER, session_id=session_id, **kwargs)
            assert _data_of(result)["code"] == "terminal_session_closed", tool
        read = env.tools["terminal_read"](user_id=OWNER, session_id=session_id, offset=0)
        assert _data_of(read)["eof"] is True

    def test_owner_quota_is_enforced(self, env, monkeypatch):
        monkeypatch.setattr(ts, "SESSIONS_PER_OWNER", 1)
        first = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(first)["session"]["session_id"]
        second = env.tools["start_terminal"](user_id=OWNER)
        assert _data_of(second)["code"] == "terminal_session_limit_reached"
        assert "close_terminal" in _data_of(second)["next_action"]
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)
        retry = env.tools["start_terminal"](user_id=OWNER)
        assert _data_of(retry)["session"]["session_id"]
        env.tools["close_terminal"](user_id=OWNER,
                                    session_id=_data_of(retry)["session"]["session_id"])
    def test_read_output_is_sanitized_and_bounded(self, stub_env):
        payload = {"offset": 0, "next_offset": 5, "bytes": 5, "truncated": False,
                   "eof": False, "session": {"session_id": "s"},
                   "data_b64": _b64("ok\x1b[31mred\n".encode())}
        stub_env.orch.terminal_sessions = stub_env.stub(read=payload)
        result = mcp_tools.terminal_read(user_id=OWNER, session_id="s", offset=0)
        assert _data_of(result)["text"] == "ok[31mred\n"

    def test_unreadable_output_still_reports_the_offset(self, stub_env):
        payload = {"offset": 3, "next_offset": 3, "bytes": 0, "truncated": False,
                   "eof": False, "session": None}
        stub_env.orch.terminal_sessions = stub_env.stub(read=payload)
        result = mcp_tools.terminal_read(user_id=OWNER, session_id="s", offset=3)
        assert _data_of(result)["text"] == ""
        assert _data_of(result)["next_offset"] == 3

    def test_listing_sanitizes_hostile_manager_fields(self, stub_env):
        sessions = [{"session_id": "s" * 400, "cols": None, "rows": 24,
                     "idle_seconds": None, "ring_start": None, "ring_end": None}]
        stub_env.orch.terminal_sessions = stub_env.stub(list_sessions=sessions)
        result = mcp_tools.list_terminals(user_id=OWNER)
        assert _data_of(result)["sessions"] == sessions
        rendered = json.dumps(result["_ui_components"], ensure_ascii=False)
        assert "…" in rendered

    def test_tools_list_serves_the_registry(self):
        server = MCPServer()
        response = server.process_request(MCPRequest(
            request_id="req_list", method="tools/list", params={}))
        names = {tool["name"] for tool in response.result["tools"]}
        assert names == set(TOOL_REGISTRY)
        assert all("input_schema" in tool for tool in response.result["tools"])


def result_texts(result: dict) -> list:
    texts = []
    for comp in result.get("_ui_components") or []:
        content = comp.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(str(item.get("content", "")) for item in content
                         if isinstance(item, dict))
    return texts


class TestMalformedInput:
    def test_command_validation(self, env):
        for command in ("", "   ", 123, "echo " + "x" * 600):
            result = env.tools["start_terminal"](user_id=OWNER, command=command)
            assert _data_of(result)["code"] == "terminal_command_invalid", command
        assert env.manager.list_sessions(OWNER) == []

    def test_write_validation(self, env, monkeypatch):
        monkeypatch.setattr(mcp_tools, "INPUT_CHUNK_MAX", 16)
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        for data in (None, "", 42):
            result = env.tools["terminal_write"](user_id=OWNER, session_id=session_id,
                                                 data=data)
            assert _data_of(result)["code"] == "terminal_input_invalid", data
        oversize = env.tools["terminal_write"](user_id=OWNER, session_id=session_id,
                                               data="x" * 17)
        assert _data_of(oversize)["code"] == "terminal_input_invalid"
        huge = env.tools["terminal_write"](user_id=OWNER, session_id=session_id,
                                           data="x" * (mcp_tools._MAX_WRITE_CHARS + 1))
        assert _data_of(huge)["code"] == "terminal_input_invalid"
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)

    def test_session_id_validation(self, env):
        for session_id in ("", None, 42, "s" * 100):
            result = env.tools["terminal_read"](user_id=OWNER, session_id=session_id)
            assert _data_of(result)["code"] == "terminal_session_invalid", session_id
        for tool, kwargs in (("terminal_write", {"data": "x\n"}),
                             ("terminal_resize", {"cols": 80, "rows": 24}),
                             ("terminal_interrupt", {}),
                             ("close_terminal", {})):
            result = env.tools[tool](user_id=OWNER, session_id="", **kwargs)
            assert _data_of(result)["code"] == "terminal_session_invalid", tool
        result = env.tools["terminal_write"](user_id=OWNER, session_id="ok", data="x")
        assert _data_of(result)["code"] == "terminal_session_not_found"

    def test_read_argument_validation(self, env):
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        for offset in (-1, "5", 1.5, True, [3]):
            result = env.tools["terminal_read"](user_id=OWNER, session_id=session_id,
                                                offset=offset)
            assert _data_of(result)["code"] == "terminal_offset_invalid", offset
        for wait in (-0.5, 26, "2", True):
            result = env.tools["terminal_read"](user_id=OWNER, session_id=session_id,
                                                wait_seconds=wait)
            assert _data_of(result)["code"] == "terminal_wait_invalid", wait
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)

    def test_resize_bounds_match_the_manager(self, env):
        info = env.tools["start_terminal"](user_id=OWNER)
        session_id = _data_of(info)["session"]["session_id"]
        for kwargs, code in (({"cols": 1, "rows": 24}, "terminal_cols_invalid"),
                             ({"cols": 1001, "rows": 24}, "terminal_cols_invalid"),
                             ({"cols": 80, "rows": 1}, "terminal_rows_invalid"),
                             ({"cols": 80, "rows": 301}, "terminal_rows_invalid")):
            result = env.tools["terminal_resize"](user_id=OWNER, session_id=session_id,
                                                  **kwargs)
            assert _data_of(result)["code"] == code, kwargs
        env.tools["close_terminal"](user_id=OWNER, session_id=session_id)


class TestUnavailableAndRuntimeFailures:
    def test_missing_manager_reports_unavailable(self, stub_env):
        stub_env.orch.terminal_sessions = None
        for name, kwargs in (("list_terminals", {}),
                             ("start_terminal", {}),
                             ("terminal_read", {"session_id": "s"}),
                             ("terminal_write", {"session_id": "s", "data": "x"}),
                             ("terminal_resize", {"session_id": "s", "cols": 80, "rows": 24}),
                             ("terminal_interrupt", {"session_id": "s"}),
                             ("close_terminal", {"session_id": "s"})):
            result = mcp_tools.TOOL_REGISTRY[name]["function"](user_id=OWNER, **kwargs)
            assert _data_of(result)["code"] == "terminal_unavailable", name

    def test_spawn_failure_is_reported(self, stub_env):
        stub_env.orch.terminal_sessions = stub_env.stub(create=OSError("no ptys"))
        result = mcp_tools.start_terminal(user_id=OWNER)
        assert _data_of(result)["code"] == "terminal_spawn_failed"

    def test_runtime_write_failure_maps_to_its_code(self, stub_env):
        from orchestrator.terminal_sessions import TerminalError
        stub_env.orch.terminal_sessions = stub_env.stub(
            write=TerminalError("terminal_write_failed", 500))
        result = mcp_tools.terminal_write(user_id=OWNER, session_id="s", data="x")
        assert _data_of(result)["code"] == "terminal_write_failed"

    def test_unknown_error_codes_still_refuse_cleanly(self, stub_env):
        from orchestrator.terminal_sessions import TerminalError
        stub_env.orch.terminal_sessions = stub_env.stub(
            read=TerminalError("terminal_future_code", 418))
        result = mcp_tools.terminal_read(user_id=OWNER, session_id="s", offset=0)
        assert _data_of(result)["code"] == "terminal_future_code"
        assert _data_of(result)["message"]


class TestMCPChannel:
    def _request(self, name, arguments):
        return MCPRequest(request_id=f"req_{name}", method="tools/call",
                          params={"name": name, "arguments": arguments})

    def test_result_payloads_strip_ui_from_data(self, env):
        server = MCPServer()
        response = server.process_request(self._request("list_terminals",
                                                        {"user_id": OWNER}))
        assert response.result == {"sessions": []}
        assert [c["type"] for c in response.ui_components] == ["card"]

    def test_error_alert_becomes_an_mcp_error(self, stub_env):
        from orchestrator.terminal_sessions import TerminalError
        stub_env.orch.terminal_sessions = stub_env.stub(
            create=TerminalError("terminal_session_limit_reached", 409))
        server = MCPServer()
        response = server.process_request(self._request("start_terminal",
                                                        {"user_id": OWNER}))
        assert response.error and "terminal_session_limit_reached" in response.error["message"]
        assert response.error["retryable"] is False

    def test_unexpected_exception_is_reported_not_swallowed(self, stub_env):
        stub_env.orch.terminal_sessions = stub_env.stub(
            list_sessions=ValueError("boom"))
        server = MCPServer()
        response = server.process_request(self._request("list_terminals",
                                                        {"user_id": OWNER}))
        assert response.error and "boom" in response.error["message"]

    def test_read_payload_reaches_the_channel_with_components(self, env):
        server = MCPServer()
        info = env.tools["start_terminal"](user_id=OWNER,
                                           command="echo channel_probe_77")
        session_id = _data_of(info)["session"]["session_id"]
        _, full = _read_to_eof(env.manager, OWNER, session_id)
        response = server.process_request(self._request(
            "terminal_read", {"user_id": OWNER, "session_id": session_id, "offset": 0}))
        assert "channel_probe_77" in json.dumps(response.result)
        assert response.ui_components and response.ui_components[0]["type"] == "card"


class TestApprovalBinding:
    def test_require_token_policy_binds_the_exact_call(self, monkeypatch):
        from orchestrator import policy, transaction_token as txn
        monkeypatch.setenv("TXN_TOKEN_KEY", "test-token-key")
        rules = [{"id": "confirm-terminal-write", "effect": "require_token",
                  "when": {"tool": "terminal_write"}}]
        args = {"session_id": "s", "data": "echo hi\n"}
        decision = policy.evaluate_policy(rules, {
            "tool": "terminal_write", "agent": "terminal-shell-1",
            "user_id": OWNER, "args": args})
        assert decision.effect == policy.REQUIRE_TOKEN
        store = txn.ConsumedStore()
        token = txn.mint("terminal-shell-1", OWNER, "terminal_write", args)
        assert token
        ok, why = txn.verify_and_consume(store, token, "terminal-shell-1", OWNER,
                                         "terminal_write", args)
        assert ok and why == "ok"
        replayed, replay_why = txn.verify_and_consume(store, token, "terminal-shell-1",
                                                      OWNER, "terminal_write", args)
        assert replayed is False and replay_why == "already used"

    def test_token_does_not_serve_a_different_tool_or_args(self, monkeypatch):
        from orchestrator import transaction_token as txn
        monkeypatch.setenv("TXN_TOKEN_KEY", "test-token-key")
        store = txn.ConsumedStore()
        token = txn.mint("terminal-shell-1", OWNER, "terminal_write",
                         {"session_id": "s", "data": "echo hi\n"})
        wrong_args, _ = txn.verify_and_consume(store, token, "terminal-shell-1", OWNER,
                                               "terminal_write",
                                               {"session_id": "s", "data": "rm -rf /\n"})
        assert wrong_args is False
        wrong_tool, _ = txn.verify_and_consume(store, token, "terminal-shell-1", OWNER,
                                               "terminal_read", {"session_id": "s"})
        assert wrong_tool is False
        stale, _ = txn.verify_and_consume(store, token, "terminal-shell-1", OWNER,
                                          "terminal_write",
                                          {"session_id": "s", "data": "echo hi\n"},
                                          now_ms=int(time.time() * 1000) + 400_000)
        assert stale is False

    def test_policy_deny_and_confirm_cover_terminal_tools(self):
        from orchestrator import policy
        rules = [{"id": "no-terminal-writes", "effect": "deny",
                  "when": {"agent": "terminal-shell-1", "tool": "terminal_write"}},
                 {"id": "confirm-reads", "effect": "confirm",
                  "when": {"tool": "terminal_read"}}]
        denied = policy.evaluate_policy(rules, {
            "tool": "terminal_write", "agent": "terminal-shell-1",
            "user_id": OWNER, "args": {}})
        assert denied.effect == policy.DENY
        confirmed = policy.evaluate_policy(rules, {
            "tool": "terminal_read", "agent": "terminal-shell-1",
            "user_id": OWNER, "args": {}})
        assert confirmed.effect == policy.CONFIRM


class TestPermissionDenials:
    def _resolver(self, safe_default=False):
        from orchestrator.tool_permissions import resolve_effective_tool_permissions
        scope_map = {name: info["scope"] for name, info in TOOL_REGISTRY.items()}
        return scope_map, (
            lambda rows, overrides, safe: resolve_effective_tool_permissions(
                scope_map, owner_id=OWNER, agent_id="terminal-shell-1",
                scope_rows=rows, override_rows=overrides, safe_default=safe))

    def test_deny_by_default_without_any_grant(self):
        scope_map, resolve = self._resolver()
        decisions = resolve([], [], safe=False)
        assert all(decisions[name]["tools:read" if name in READING_TOOLS else "tools:write"]
                   is False for name in TOOL_REGISTRY)

    def test_safe_flip_cannot_overcome_an_explicit_user_denial(self):
        scope_map, resolve = self._resolver()
        override = SimpleNamespace(owner_id=OWNER, agent_id="terminal-shell-1",
                                   tool_name="terminal_write", permission_kind=None,
                                   enabled=False)
        decisions = resolve([], [override], safe=True)
        assert decisions["terminal_write"]["tools:write"] is False
        assert decisions["list_terminals"]["tools:read"] is True

    def test_safe_flip_cannot_overcome_a_disabled_scope(self):
        scope_map, resolve = self._resolver()
        scope_row = SimpleNamespace(owner_id=OWNER, agent_id="terminal-shell-1",
                                    scope="tools:write", enabled=False)
        decisions = resolve([scope_row], [], safe=True)
        for name in MUTATING_TOOLS:
            assert decisions[name]["tools:write"] is False, name
        assert decisions["list_terminals"]["tools:read"] is True

    def test_granting_the_write_scope_enables_mutating_verbs_only(self):
        scope_map, resolve = self._resolver()
        scope_row = SimpleNamespace(owner_id=OWNER, agent_id="terminal-shell-1",
                                    scope="tools:write", enabled=True)
        decisions = resolve([scope_row], [], safe=False)
        for name in MUTATING_TOOLS:
            assert decisions[name]["tools:write"] is True, name
        for name in READING_TOOLS:
            assert decisions[name]["tools:read"] is False, name

    def test_read_only_delegation_cannot_reach_mutating_verbs(self):
        from orchestrator.delegation import DelegationService
        scopes = ["tools:read"]
        assert DelegationService.is_tool_in_scope("terminal_read", scopes, "tools:read")
        for name in MUTATING_TOOLS:
            required = TOOL_REGISTRY[name]["scope"]
            assert DelegationService.is_tool_in_scope(
                name, scopes, required) is False, name

    def test_write_scoped_delegation_cannot_reach_undeclared_scopes(self):
        from orchestrator.delegation import DelegationService
        scopes = ["tools:write"]
        for name in TOOL_REGISTRY:
            required = TOOL_REGISTRY[name]["scope"]
            expected = required == "tools:write"
            assert DelegationService.is_tool_in_scope(
                name, scopes, required) is expected, name
