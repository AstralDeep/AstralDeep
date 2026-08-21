"""Machineless users must not carry the remote-compute tool catalog (067).

The 18 remote-compute verbs cost ~2,700 prompt tokens on every LLM call, yet
17 of them dead-end at "this machine is not in your inventory" for a user with
no registered machine. `eligible_tool_pairs` is the single visibility predicate
for chat and the MCP projection, so the machine-ownership subtraction lives
there: with zero owned machines only `list_machines` (the discovery verb whose
empty-state reply points at Settings → Remote machines) stays visible.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from agents.remote_compute.mcp_tools import TOOL_REGISTRY
from orchestrator.mcp_projection import project_tools
from orchestrator.plane_repository_context import ApplicationPlaneSource
from orchestrator.tool_visibility import eligible_tool_pairs
from shared.protocol import AgentCard, AgentSkill


REMOTE_AGENT_ID = "remote-compute-1"
DISCOVERY_VERBS = {"list_machines"}


class _MachineRepository:
    """Typed Plane remote-repository stub for the ownership projection."""

    def __init__(self, owns_machine: bool) -> None:
        self.owns_machine = owns_machine
        self.list_calls: list[tuple[str, int]] = []

    def list_machines(self, _transaction, *, owner_id: str, limit: int):
        self.list_calls.append((owner_id, limit))
        return (object(),) if self.owns_machine else ()


class _PlaneRuntime:
    """Minimal application-scoped transaction seam for the typed repository."""

    def __init__(self, remote: _MachineRepository) -> None:
        self.repositories = SimpleNamespace(remote=remote)

    @contextmanager
    def transaction(self):
        yield object()


def _orchestrator(owns_machine: bool) -> SimpleNamespace:
    remote_skills = [
        AgentSkill(id=verb, name=verb, description="", scope="tools:read")
        for verb in sorted(TOOL_REGISTRY)
    ]
    other_skill = AgentSkill(
        id="roll_dice", name="Roll", description="", scope="tools:read"
    )
    cards = {
        REMOTE_AGENT_ID: AgentCard(
            name="Remote Compute",
            description="",
            agent_id=REMOTE_AGENT_ID,
            skills=remote_skills,
        ),
        "dice-roller-1": AgentCard(
            name="Dice",
            description="",
            agent_id="dice-roller-1",
            skills=[other_skill],
        ),
    }
    remote = _MachineRepository(owns_machine)
    runtime = _PlaneRuntime(remote)
    machine_source = ApplicationPlaneSource(runtime, runtime.repositories)
    return SimpleNamespace(
        agent_cards=cards,
        agents={},
        local_agents={agent_id: object() for agent_id in cards},
        security_flags={},
        _is_draft_agent=lambda agent_id: False,
        tool_permissions=SimpleNamespace(
            is_tool_allowed=lambda *args: True,
            list_disabled_agents=lambda _user_id: (),
        ),
        plane_repository_source=machine_source,
    )


def test_registry_shape_matches_the_18_verb_contract():
    assert len(TOOL_REGISTRY) == 18
    assert DISCOVERY_VERBS < set(TOOL_REGISTRY)


def test_machineless_user_sees_only_the_discovery_verb():
    reasons = []
    orch = _orchestrator(owns_machine=False)
    pairs = eligible_tool_pairs(
        orch,
        "u1",
        log_exclusion=lambda agent, skill_id, reason: reasons.append(
            (agent, skill_id, reason)
        ),
    )
    remote_offered = {
        skill.id for agent_id, skill in pairs if agent_id == REMOTE_AGENT_ID
    }
    assert remote_offered == DISCOVERY_VERBS
    hidden = {
        skill_id
        for agent, skill_id, reason in reasons
        if reason == "no_registered_machine"
    }
    assert hidden == set(TOOL_REGISTRY) - DISCOVERY_VERBS
    # The subtraction never leaks onto other agents.
    assert ("dice-roller-1", "roll_dice") in [
        (agent_id, skill.id) for agent_id, skill in pairs
    ]


def test_machine_owner_keeps_the_full_catalog():
    reasons = []
    orch = _orchestrator(owns_machine=True)
    pairs = eligible_tool_pairs(
        orch,
        "u1",
        log_exclusion=lambda agent, skill_id, reason: reasons.append(reason),
    )
    remote_offered = {
        skill.id for agent_id, skill in pairs if agent_id == REMOTE_AGENT_ID
    }
    assert remote_offered == set(TOOL_REGISTRY)
    assert "no_registered_machine" not in reasons


def test_ownership_is_checked_at_most_once_per_projection():
    orch = _orchestrator(owns_machine=False)
    eligible_tool_pairs(orch, "u1")
    assert orch.plane_repository_source.plane_repositories.remote.list_calls == [
        ("u1", 1)
    ]


def test_machine_owner_pays_one_existence_probe_not_eighteen():
    orch = _orchestrator(owns_machine=True)
    eligible_tool_pairs(orch, "u1")
    assert orch.plane_repository_source.plane_repositories.remote.list_calls == [
        ("u1", 1)
    ]


def test_probe_failure_fails_open_to_the_full_catalog():
    # The subtraction is a prompt-cost optimization; the dispatch permission
    # gate still runs. A transient DB error must not blank the tool list.
    orch = _orchestrator(owns_machine=True)

    def _boom(_transaction, **_kwargs):
        raise RuntimeError("db down")

    orch.plane_repository_source.plane_repositories.remote.list_machines = _boom
    pairs = eligible_tool_pairs(orch, "u1")
    remote_offered = {
        skill.id for agent_id, skill in pairs if agent_id == REMOTE_AGENT_ID
    }
    assert remote_offered == set(TOOL_REGISTRY)


def test_mcp_projection_shares_the_machineless_subtraction():
    machineless = {
        tool.skill_id
        for tool in project_tools(_orchestrator(owns_machine=False), "u1")
        if tool.agent_id == REMOTE_AGENT_ID
    }
    owner = {
        tool.skill_id
        for tool in project_tools(_orchestrator(owns_machine=True), "u1")
        if tool.agent_id == REMOTE_AGENT_ID
    }
    assert machineless == DISCOVERY_VERBS
    assert owner == set(TOOL_REGISTRY)
