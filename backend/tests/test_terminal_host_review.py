"""Exercises host-owned review of the exact bundled isolated terminal implementation.
Normal agent registration and real owner profile repositories retain all action authorization gates.
"""

from types import SimpleNamespace
from copy import deepcopy

import pytest

from orchestrator import local_agents
from orchestrator.orchestrator import Orchestrator
from orchestrator.terminal_security import TerminalError, bind_terminal_authority
from orchestrator.terminal_sessions import TerminalSessionManager
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared import attachment_materializer, attachment_resolver
from shared.feature_flags import flags
from tests.test_agent_key_enforcement import _make_fake
from tests.test_terminal_ssh_postgres import runtime as runtime
from shared.protocol import RegisterAgent


@pytest.fixture
async def registered(runtime, monkeypatch, tmp_path):
    env = runtime
    fake = _make_fake()
    orch = Orchestrator.__new__(Orchestrator)
    orch.__dict__.update(vars(fake))
    orch.__dict__.update(vars(env.orch))
    orch.register_agent = Orchestrator.register_agent.__get__(orch)
    orch.local_agents = {}
    orch.security_analyzer = ToolSecurityAnalyzer()
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=env.plane, repositories=env.plane.repositories, blobs=object(), attachment_materializer=object()))
    env.manager._orchestrator = orch
    monkeypatch.setenv("AGENT_API_KEY", "synthetic-registration-key")
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "agent.pem"))
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: True)
    for name in ("computer_use", "remote_compute", "cresco", "gaiakeep", "fhir", "evidence_context"):
        monkeypatch.setitem(flags._flags, name, False)
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    assert await local_agents.register_built_ins(orch) == ["terminal-shell-1"]
    return SimpleNamespace(env=env, orch=orch, socket=env.socket)


@pytest.mark.asyncio
async def test_actual_registration_keeps_code_execution_visible_and_reviews_only_host_implementation(registered):
    env, orch = registered.env, registered.orch
    findings = orch.security_flags["terminal-shell-1"]
    assert set(findings) == {"start_terminal", "terminal_write"}
    assert all(value["category"] == "CODE_EXECUTION" and value["blocked"] is False for value in findings.values())
    authority = bind_terminal_authority(orch, registered.socket, "owner", "start_terminal")
    result = env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    assert result["session_id"] and env.channels[0].effects[-1] == ("shell",)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["marker", "flags", "unknown_category", "extra_finding", "scalar_flags",
    "remote", "a2a", "replaced_card", "forged_card", "metadata", "agent_subclass", "manager_subclass",
    "foreign_manager", "foreign_host", "inprocess", "terminal", "agent_method", "server_method"])
async def test_changed_host_binding_or_untrusted_review_cannot_authorize_execution(registered, monkeypatch, loss):
    from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent

    orch = registered.orch
    agent = orch.local_agents["terminal-shell-1"]
    if loss == "marker":
        del orch._terminal_host_review
    elif loss == "flags":
        del orch.security_flags["terminal-shell-1"]
    elif loss == "unknown_category":
        orch.security_flags["terminal-shell-1"]["start_terminal"]["category"] = "CREDENTIAL_ACCESS"
    elif loss == "extra_finding":
        orch.security_flags["terminal-shell-1"]["unknown"] = {"category": "DESTRUCTIVE", "blocked": True}
    elif loss == "scalar_flags":
        orch.security_flags["terminal-shell-1"] = "self-approved"
    elif loss == "remote":
        orch.agents["terminal-shell-1"] = object()
    elif loss == "a2a":
        orch.a2a_clients = {"terminal-shell-1": "https://foreign.invalid"}
    elif loss == "replaced_card":
        orch.agent_cards["terminal-shell-1"] = deepcopy(agent.card)
    elif loss == "forged_card":
        agent.card.skills[0].scope = "tools:system"
    elif loss == "metadata":
        agent.card.metadata["host_security_review"] = True
    elif loss == "agent_subclass":
        class Derived(TerminalShellAgent):
            pass

        orch.local_agents["terminal-shell-1"] = Derived(orchestrator=orch)
        orch.agent_cards["terminal-shell-1"] = orch.local_agents["terminal-shell-1"].card
    elif loss == "manager_subclass":
        class DerivedManager(TerminalSessionManager):
            pass

        orch.terminal_sessions = DerivedManager(orchestrator=orch)
    elif loss == "foreign_manager":
        orch.terminal_sessions = TerminalSessionManager(orchestrator=object())
    elif loss == "foreign_host":
        other = Orchestrator.__new__(Orchestrator)
        other.__dict__.update(vars(orch))
        orch = other
    elif loss == "inprocess":
        monkeypatch.setitem(flags._flags, "inprocess_agents", False)
    elif loss == "terminal":
        monkeypatch.setitem(flags._flags, "terminal_shell", False)
    elif loss == "agent_method":
        agent.handle_mcp_request = lambda *a: None
    else:
        agent.mcp_server.process_request = lambda *a: None
    with pytest.raises(TerminalError):
        bind_terminal_authority(orch, registered.socket, "owner", "start_terminal")
    assert registered.env.channels == []


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["absent", "malformed", "foreign_owner", "credential"])
async def test_review_never_grants_a_profile_or_current_credentials(registered, monkeypatch, loss):
    env, orch = registered.env, registered.orch
    if loss == "absent":
        monkeypatch.delenv("TERMINAL_SSH_PROFILE_FILE")
    elif loss == "malformed":
        env.profile.chmod(0o600)
        env.profile.write_text("{bad")
        env.profile.chmod(0o400)
    elif loss == "credential":
        env.credentials.delete_machine_credential(env.machine, "owner")
    owner = "foreign" if loss == "foreign_owner" else "owner"
    assert orch.security_flags["terminal-shell-1"]["start_terminal"]["blocked"] is False
    with pytest.raises(TerminalError) as error:
        env.manager.require_execution(owner)
    assert error.value.status_code == 503
    assert env.channels == []


@pytest.mark.asyncio
async def test_review_does_not_override_current_permission_or_credential_revocation(registered):
    env, orch = registered.env, registered.orch
    authority = bind_terminal_authority(orch, registered.socket, "owner", "start_terminal")
    result = env.manager.create("owner", authority=authority, runtime_binding=env.manager.runtime_binding("owner"))
    authority = bind_terminal_authority(orch, registered.socket, "owner", "terminal_write")
    binding = env.manager.runtime_binding("owner")
    env.permissions.set_tool_permission("owner", "terminal-shell-1", "terminal_write", "tools:system", False)
    with pytest.raises(TerminalError, match="terminal_permission_denied"):
        env.manager.write("owner", result["session_id"], "aWQK", authority=authority, runtime_binding=binding)
    assert env.channels[0].closed and env.channels[0].effects == [
        ("pty", {"term": "xterm", "width": 80, "height": 24}), ("shell",)]


@pytest.mark.asyncio
async def test_external_same_id_registration_and_self_attestation_remain_hard_blocked(registered):
    orch = registered.orch
    card = deepcopy(orch.agent_cards["terminal-shell-1"])
    card.metadata["host_security_review"] = True
    await orch.register_agent(object(), RegisterAgent(agent_card=card, api_key="synthetic-registration-key"))
    assert orch.security_flags["terminal-shell-1"]["start_terminal"]["blocked"] is True
    with pytest.raises(TerminalError, match="terminal_security_blocked"):
        bind_terminal_authority(orch, registered.socket, "owner", "start_terminal")
    assert registered.env.channels == []


@pytest.mark.asyncio
async def test_host_review_does_not_reclassify_an_additional_or_different_finding(registered):
    from orchestrator.terminal_host_review import review_registered_terminal

    orch = registered.orch
    canonical = {name: value.to_dict() for name, value in ToolSecurityAnalyzer().analyze_agent(
        orch.agent_cards["terminal-shell-1"]).items()}
    canonical["start_terminal"]["category"] = "CREDENTIAL_ACCESS"
    orch.security_flags["terminal-shell-1"] = canonical
    assert review_registered_terminal(orch) is False
    assert all(value["blocked"] is True for value in canonical.values())
    with pytest.raises(TerminalError, match="terminal_security_blocked"):
        bind_terminal_authority(orch, registered.socket, "owner", "start_terminal")


@pytest.mark.asyncio
async def test_subclass_host_cannot_skip_missing_review_at_execution_boundary(registered):
    class DerivedHost(Orchestrator):
        pass

    host = DerivedHost.__new__(DerivedHost)
    host.__dict__.update(vars(registered.orch))
    del host._terminal_host_review
    host.security_flags = {}
    registered.env.manager._orchestrator = host
    with pytest.raises(TerminalError, match="terminal_security_blocked"):
        bind_terminal_authority(host, registered.socket, "owner", "start_terminal")
    assert registered.env.channels == []
