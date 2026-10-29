"""Covers bounded Codex worker command construction and result classification."""

from types import SimpleNamespace

import pytest

from orchestrator import goal_worker


PROJECT = {
    "folder_status": "granted",
    "primary_root": "/work/demo",
    "workspace_roots": [{"path": "/work"}],
}
CATEGORIES = ("files", "environment", "dependencies", "tests", "startup")


def test_prompt_preserves_objective_and_required_execution_criteria():
    prompt = goal_worker.build_worker_prompt("Make a small game", CATEGORIES, "/work/demo")

    assert "Make a small game" in prompt
    assert ".venv" in prompt
    assert "install project dependencies" in prompt
    assert "meaningful tests" in prompt
    assert "startup with a bounded smoke check" in prompt
    assert "Report observed results" in prompt


def test_command_uses_configured_absolute_cli_and_bounded_argv():
    result = goal_worker.build_worker_command(
        "Make a small game", CATEGORIES, PROJECT,
        environ={"GOAL_CODEX_WORKER_PATH": "/opt/codex/bin/codex"},
    )

    assert result["argv"] == [
        "/opt/codex/bin/codex", "exec", "--ignore-user-config", "--ephemeral",
        "--skip-git-repo-check", "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=true", "-m", "gpt-6-luna",
        "-c", "model_reasoning_effort=medium", "-",
    ]
    assert result["cwd"] == "/work/demo"
    assert result["timeout_s"] == 600
    assert result["background"] is False
    assert "Make a small game" in result["stdin"]


def test_default_uses_bundled_chatgpt_codex_path_on_any_server_platform():
    result = goal_worker.build_worker_command("Make a small game", CATEGORIES, PROJECT, environ={})

    assert result["argv"][0] == "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


@pytest.mark.parametrize("project", [
    {**PROJECT, "folder_status": "ungranted"},
    {**PROJECT, "primary_root": "/elsewhere"},
    {**PROJECT, "primary_root": "/work/../elsewhere"},
])
def test_command_rejects_missing_or_out_of_scope_grant(project):
    with pytest.raises(ValueError):
        goal_worker.build_worker_command(
            "Make a small game", CATEGORIES, project,
            environ={"GOAL_CODEX_WORKER_PATH": "/opt/codex"},
        )


@pytest.mark.parametrize("environ", [
    {"GOAL_CODEX_WORKER_PATH": "codex"},
    {"GOAL_CODEX_WORKER_PATH": "/opt/codex\x00/bin"},
])
def test_command_requires_absolute_configured_path(environ):
    with pytest.raises(ValueError):
        goal_worker.build_worker_command("Make a small game", CATEGORIES, PROJECT,
                                         environ=environ)


@pytest.mark.parametrize("result,expected,exit_code", [
    (SimpleNamespace(error={"code": "permission_denied"}, result=None), "denied", None),
    ({"_data": {"status": "confirmation_required"}}, "denied", None),
    (SimpleNamespace(error={"code": "command_not_found"}, result=None), "launch_error", None),
    ({"_data": {"timed_out": True, "exit_code": None}}, "timeout", None),
    ({"_data": {"timed_out": False, "exit_code": 7}}, "nonzero", 7),
    ({"_data": {"timed_out": False, "exit_code": 0}}, "success", 0),
    ({"_data": {"timed_out": False}}, "launch_error", None),
])
def test_result_classifier(result, expected, exit_code):
    classified = goal_worker.classify_worker_result(result)

    assert classified.status == expected
    assert classified.exit_code == exit_code


def test_prompt_and_timeout_limits_fail_closed():
    with pytest.raises(ValueError):
        goal_worker.build_worker_prompt("x" * 16_385, CATEGORIES, "/work/demo")
    with pytest.raises(ValueError):
        goal_worker.build_worker_prompt("Make a small game", ("unknown",), "/work/demo")
    with pytest.raises(ValueError):
        goal_worker.build_worker_command(
            "Make a small game", CATEGORIES, PROJECT,
            environ={"GOAL_CODEX_WORKER_PATH": "/opt/codex"}, timeout_s=601,
        )
