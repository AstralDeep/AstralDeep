"""Checks that provider tool-call IDs cannot alias distinct goal observations."""

from types import SimpleNamespace

import pytest

from orchestrator.goal_progress import CompletionDecision, GoalProgress
from orchestrator.goal_requirements import GoalContract, validate_goal_proofs


def test_reused_provider_id_keeps_distinct_goal_observation_identity():
    progress = GoalProgress("Create a project file")
    result = SimpleNamespace(error=None, result={"_data": {"path": "src/app.py"}})
    progress.record("provider-call-1", "write_file", {"path": "src/app.py", "content": "one"}, result,
                    agent_id="file-agent")
    progress.record("provider-call-1", "write_file", {"path": "src/app.py", "content": "two"}, result,
                    agent_id="file-agent")

    observations = progress.private_observations()

    assert len({row["call_id"] for row in observations}) == 2
    assert [row.get("provider_call_id", row["call_id"]) for row in observations] == [
        "provider-call-1", "provider-call-1"]
    assert [row["sequence"] for row in observations] == [1, 2]
    contract = GoalContract("action", ("outcome",))
    proof = validate_goal_proofs(
        contract,
        {"outcome": [row["call_id"] for row in observations]},
        observations,
        None,
    )
    assert proof.accepted


def test_reused_provider_id_does_not_alias_failure_or_unknown_tracking():
    progress = GoalProgress("Repair a file")
    progress.record("reused", "write_file", {"path": "src/a.py", "content": "x"},
                    SimpleNamespace(error="write failed", result=None))
    progress.record("reused", "search", {}, SimpleNamespace(error=None, result=None))
    progress.record("reused", "get_time", {},
                    SimpleNamespace(error=None, result={"_data": {"time": "noon"}}))

    evidence = progress.evidence()
    observations = progress.private_observations()

    assert evidence["unresolved_failures"] == [observations[0]["call_id"]]
    assert evidence["unresolved_unknown"] == [observations[1]["call_id"]]
    assert progress.requires_inspection
    assert len({row["call_id"] for row in observations}) == 3


def test_minted_and_truncated_ids_are_checked_against_all_prior_ids():
    progress = GoalProgress("Inspect a project")
    result = SimpleNamespace(error=None, result={"_data": {"items": ["ok"]}})
    provider_ids = [
        "goal-call-5",
        "reused",
        "reused",
        "goal-call-4",
        "reused",
        "x" * 129,
        "x" * 128 + "z",
        "",
    ]
    for provider_id in provider_ids:
        progress.record(provider_id, "search", {}, result)

    observations = progress.private_observations()

    assert [row["call_id"] for row in observations] == [
        "goal-call-5",
        "reused",
        "goal-call-3",
        "goal-call-4",
        "goal-call-5-2",
        "x" * 128,
        "goal-call-7",
        "goal-call-8",
    ]
    assert [row.get("provider_call_id", row["call_id"]) for row in observations] == [
        "goal-call-5",
        "reused",
        "reused",
        "goal-call-4",
        "reused",
        "x" * 128,
        "x" * 128,
        "",
    ]
    assert len({row["call_id"] for row in observations}) == len(provider_ids)


def test_explicit_resolution_accepts_known_write_retried_as_overwrite():
    progress = GoalProgress("Create a source file")
    context = {
        "project_id": "project-a",
        "computer_id": "computer-a",
        "computer_name": "workspace",
    }
    progress.record(
        "failed-write",
        "write_file",
        {"path": "src/app.py", "cwd": "/workspace", "content": "print('ready')", "if_exists": "refuse"},
        SimpleNamespace(error="parent directory missing", result=None),
        agent_id="computer-use-1",
        project_context=context,
    )
    progress.record(
        "retried-write",
        "write_file",
        {"path": "/workspace/src/app.py", "content": "print('ready')", "if_exists": "overwrite"},
        SimpleNamespace(error=None, result={"_data": {"ok": True}}),
        agent_id="computer-use-1",
        project_context=context,
    )

    decision = CompletionDecision("complete", resolutions=(("failed-write", "retried-write"),))

    assert progress.assess(decision).status == "complete"
    assert progress.evidence()["unresolved_failures"] == []


def test_explicit_resolution_does_not_clear_unknown_write_with_retry_alone():
    progress = GoalProgress("Create and verify a source file")
    context = {
        "project_id": "project-a",
        "computer_id": "computer-a",
        "computer_name": "workspace",
    }
    progress.record(
        "unknown-write",
        "write_file",
        {"path": "/workspace/src/app.py", "content": "print('ready')", "if_exists": "refuse"},
        None,
        agent_id="computer-use-1",
        project_context=context,
    )
    progress.record(
        "retried-write",
        "write_file",
        {"path": "/workspace/src/app.py", "content": "print('ready')", "if_exists": "overwrite"},
        SimpleNamespace(error=None, result={"_data": {"ok": True}}),
        agent_id="computer-use-1",
        project_context=context,
    )

    decision = CompletionDecision("complete", resolutions=(("unknown-write", "retried-write"),))

    assert progress.assess(decision).status == "continue"
    assert progress.evidence()["unresolved_unknown"] == ["unknown-write"]


@pytest.mark.parametrize(("path", "content", "agent_id", "computer_id", "project_id"), [
    ("/workspace/src/other.py", "print('ready')", "computer-use-1", "computer-a", "project-a"),
    ("/workspace/src/app.py", "print('different')", "computer-use-1", "computer-a", "project-a"),
    ("/workspace/src/app.py", "print('ready')", "other-agent", "computer-a", "project-a"),
    ("/workspace/src/app.py", "print('ready')", "computer-use-1", "other-computer", "project-a"),
    ("/workspace/src/app.py", "print('ready')", "computer-use-1", "computer-a", "other-project"),
])
def test_write_retry_resolution_keeps_effect_and_identity_boundaries(
    path, content, agent_id, computer_id, project_id,
):
    progress = GoalProgress("Create a source file")
    progress.record(
        "failed-write",
        "write_file",
        {"path": "/workspace/src/app.py", "content": "print('ready')", "if_exists": "refuse"},
        SimpleNamespace(error="write failed", result=None),
        agent_id="computer-use-1",
        computer_id="computer-a",
        computer_name="workspace",
        project_context={"project_id": "project-a"},
    )
    progress.record(
        "retry",
        "write_file",
        {"path": path, "content": content, "if_exists": "overwrite"},
        SimpleNamespace(error=None, result={"_data": {"ok": True}}),
        agent_id=agent_id,
        computer_id=computer_id,
        computer_name="workspace",
        project_context={"project_id": project_id},
    )

    decision = CompletionDecision("complete", resolutions=(("failed-write", "retry"),))

    assert progress.assess(decision).status == "continue"
    assert progress.evidence()["unresolved_failures"] == ["failed-write"]
