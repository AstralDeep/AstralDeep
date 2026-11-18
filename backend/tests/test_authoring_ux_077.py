"""Tests for the personal-agent authoring UX across agent_authoring.py,
agent_quick_create.py, user_skills.py, and slash_commands.py: the express-creation
lane, desktop presence, skill-store bounds, and scoped progress pushes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator import agent_authoring as aa  # noqa: E402
from orchestrator import agent_quick_create as qc  # noqa: E402
from orchestrator import slash_commands  # noqa: E402
from orchestrator import user_skills as us  # noqa: E402
from tests.test_byo_authoring_flow import OWNER  # noqa: E402


class _LLM:
    def __init__(self, questions=None):
        self.questions = list(questions or [])
        self.calls = []

    def __call__(self, websocket, messages, schema=None, schema_name=None, feature=None):
        text = messages[-1]["content"]
        self.calls.append(text)
        if "Key: specification" in text:
            return {"specification": "Sorts the owner's own inbox into folders every morning."}
        if "Key: questions" in text:
            return {"questions": list(self.questions)}
        if "Keys: tools, notes" in text:
            return {"tools": [{"name": "sort_inbox", "scope": "tools:read",
                               "description": "reads my inbox and files messages"}],
                    "notes": ""}
        if "Key: tasks" in text:
            return {"tasks": ["read the inbox", "file the messages"]}
        return None


async def _settle(run: qc.QuickRun, timeout=5.0):
    if run.task is not None:
        await asyncio.wait_for(asyncio.shield(run.task), timeout)


def test_host_presence_counts_a_signed_in_desktop_before_any_tunnel():
    orch = SimpleNamespace(_tunnel_sockets={}, owner_host_sockets=lambda o: [],
                           computer_hosts=None, _personal_agent_host_sessions={})
    assert aa.host_presence(orch, OWNER) == {"online": False, "label": "your desktop client",
                                             "hosts": 0, "tunnels": False}
    sock = object()
    orch.owner_host_sockets = lambda o: [sock]
    orch._personal_agent_host_sessions = {id(sock): SimpleNamespace(platform="windows",
                                                                   client_version="0.5.0")}
    presence = aa.host_presence(orch, OWNER)
    assert presence["online"] is True and presence["label"] == "windows desktop client v0.5.0"
    orch.computer_hosts = SimpleNamespace(online_for_owner=lambda o: [SimpleNamespace(name="RyzenRoll")])
    assert aa.host_presence(orch, OWNER)["label"] == "RyzenRoll"
    orch.owner_host_sockets = lambda o: []
    orch._tunnel_sockets = {(OWNER, "ua-1"): object()}
    assert aa.host_online(orch, OWNER) is True


def test_skill_store_saves_lists_toggles_and_deletes(tmp_path):
    store = us.UserSkillStore(str(tmp_path))
    skill = store.save(OWNER, name="Weekly status", instructions="Three bullets, then risks.",
                       applies_to="", command="/status", reserved_commands=["help"])
    assert skill.slug == "weekly-status" and skill.command == "status" and skill.always
    assert store.list(OWNER)[0] == skill
    assert (tmp_path / "user_skills").is_dir()
    path = next((tmp_path / "user_skills").rglob("weekly-status.md"))
    text = path.read_text(encoding="utf-8")
    assert "type: user_skill" in text and "applies_to: [always]" in text
    assert store.list("someone-else") == [] and OWNER not in str(path)
    scoped = store.save(OWNER, name="Summarizer voice", instructions="Be terse, cite sources.",
                        applies_to="summarizer-1, web-research-1")
    assert scoped.applies_to == ("summarizer-1", "web-research-1") and not scoped.always
    off = store.set_enabled(OWNER, "weekly-status", False)
    assert off is not None and off.enabled is False and store.command_map(OWNER) == {}
    assert store.set_enabled(OWNER, "weekly-status", True).command == "status"
    assert set(store.command_map(OWNER)) == {"status"}
    edited = store.save(OWNER, name="Weekly status", instructions="Four bullets.",
                        applies_to="", command="stat", slug="weekly-status")
    assert edited.instructions == "Four bullets." and set(store.command_map(OWNER)) == {"stat"}
    assert store.delete(OWNER, "weekly-status") is True and store.delete(OWNER, "weekly-status") is False
    assert [s.slug for s in store.list(OWNER)] == ["summarizer-voice"]


def test_skill_store_validation(tmp_path):
    store = us.UserSkillStore(str(tmp_path))
    with pytest.raises(us.SkillValidationError, match="name"):
        store.save(OWNER, name="x", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="instructions"):
        store.save(OWNER, name="Fine", instructions="short", applies_to="")
    with pytest.raises(us.SkillValidationError, match="built-in"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
                   command="help", reserved_commands=slash_commands.reserved_names())
    with pytest.raises(us.SkillValidationError, match="command is"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
                   command="Bad Name!")
    with pytest.raises(us.SkillValidationError, match="not an agent id"):
        store.save(OWNER, name="Fine", instructions="Long enough instructions.",
                   applies_to="../etc")
    store.save(OWNER, name="Fine", instructions="Long enough instructions.", applies_to="",
               command="go")
    with pytest.raises(us.SkillValidationError, match="already have"):
        store.save(OWNER, name="fine", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="already used"):
        store.save(OWNER, name="Other", instructions="Long enough instructions.", applies_to="",
                   command="go")
    for i in range(us.MAX_SKILLS - 1):
        store.save(OWNER, name=f"Skill {i}", instructions="Long enough instructions.", applies_to="")
    with pytest.raises(us.SkillValidationError, match="up to"):
        store.save(OWNER, name="One too many", instructions="Long enough instructions.", applies_to="")


def test_code_gate_flags_builtins_but_not_library_methods_of_the_same_name():
    from orchestrator.code_security import CodeSecurityAnalyzer, Severity, blocks_execution
    analyzer = CodeSecurityAnalyzer()
    ok = analyzer.analyze(
        "import re\n"
        "PATTERN = re.compile(r'(\\\\d+)d(\\\\d+)')\n"
        "def roll(expr):\n"
        "    m = PATTERN.match(expr)\n"
        "    return m.groups() if m else None\n",
        filename="ok/mcp_tools.py")
    assert not blocks_execution(ok), [f.message for f in ok.findings]
    bad = analyzer.analyze("def run(src):\n    return compile(src, 'x', 'exec')\n", filename="bad.py")
    assert blocks_execution(bad) and bad.max_severity == Severity.CRITICAL
    sneaky = analyzer.analyze("import builtins\n"
                              "def run(src):\n    return builtins.eval(src)\n", filename="sneaky.py")
    assert blocks_execution(sneaky)
    dunder = analyzer.analyze("def run(src):\n    return __builtins__.exec(src)\n", filename="dunder.py")
    assert blocks_execution(dunder)
    system = analyzer.analyze("import os\ndef run(c):\n    return os.system(c)\n", filename="os.py")
    assert blocks_execution(system)


def test_agent_status_sees_a_fenced_v3_runtime_as_running():
    from shared.local_transport import FencedTunnelSocket
    fence = SimpleNamespace(agent_id="ua-1", host_session_id="hs-1",
                            runtime_instance_id="ri-1")
    route = FencedTunnelSocket(object(), OWNER, fence, AsyncMock())
    orch = SimpleNamespace(agents={"ua-1": route},
                           _tunnel_sockets={},
                           _personal_agent_runtime_sockets={"ri-1": route})
    assert aa.agent_status(orch, OWNER, "ua-1") == "running"
    assert aa.agent_status(orch, "someone-else", "ua-1") == "offline"
    assert aa.agent_status(orch, OWNER, "ua-2") == "offline"
    orch._personal_agent_runtime_sockets["ri-1"] = object()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
    orch._personal_agent_runtime_sockets.clear()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
    v2 = object()
    orch = SimpleNamespace(agents={"ua-1": v2}, _tunnel_sockets={(OWNER, "ua-1"): v2},
                           _personal_agent_runtime_sockets={})
    assert aa.agent_status(orch, OWNER, "ua-1") == "running"
    orch.agents["ua-1"] = object()
    assert aa.agent_status(orch, OWNER, "ua-1") == "offline"
