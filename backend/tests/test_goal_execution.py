"""Tests host-bound routing and ordered tool batches for verified local-project goals."""

from types import SimpleNamespace
import asyncio

import pytest

from orchestrator.goal_execution import execute_goal_batch, route_correction
from orchestrator.goal_requirements import build_goal_contract


PROJECT = {
    "project_id": "project-a",
    "computer_id": "computer-a",
    "computer_name": "Sam's Mac",
    "folder_status": "granted",
    "workspace_roots": [{"path": "/Users/sam/Desktop/Minecraft"}],
    "primary_root": "/Users/sam/Desktop/Minecraft",
}
CONTRACT = build_goal_contract("local_project", PROJECT)


def _call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def test_local_project_rejects_entire_remote_batch_targeting_granted_root():
    calls = [
        _call("remote__make_directory", '{"machine_id":"cluster-a","path":"/Users/sam/Desktop/Minecraft/minecraft_clone"}'),
        _call("remote__upload_file", '{"machine_id":"cluster-a","remote_path":"/Users/sam/Desktop/Minecraft/minecraft_clone/main.py"}'),
    ]

    correction = route_correction(
        calls,
        {"remote__make_directory": "remote-compute-1", "remote__upload_file": "remote-compute-1",
         "local__run_command": "computer-use-1", "local__write_file": "computer-use-1"},
        {"remote__make_directory": "make_directory", "remote__upload_file": "upload_file",
         "local__run_command": "run_command", "local__write_file": "write_file"},
        PROJECT,
        CONTRACT,
    )

    assert correction is not None
    assert "remote__make_directory" in correction
    assert "remote__upload_file" in correction
    assert "local__run_command" in correction
    assert "local__write_file" in correction
    assert "Sam's Mac" in correction


@pytest.mark.parametrize("computer_argument", [
    {"computer_id": "other-computer"},
    {"computer_name": "Other host"},
    {"computer": "Other host"},
    {"host_id": "other-computer"},
])
def test_local_project_rejects_explicit_computer_use_host_mismatch(computer_argument):
    call = _call("local__run_command", computer_argument)

    correction = route_correction(
        [call], {"local__run_command": "computer-use-1"},
        {"local__run_command": "run_command"}, PROJECT, CONTRACT,
    )

    assert correction is not None
    assert "local__run_command" in correction
    assert "Sam's Mac" in correction


def test_local_project_accepts_computer_use_call_bound_to_selected_host():
    calls = [_call("local__run_command", '{"computer_id":"computer-a","cwd":"/Users/sam/Desktop/Minecraft"}')]

    correction = route_correction(
        calls, {"local__run_command": "computer-use-1"},
        {"local__run_command": "run_command"}, PROJECT, CONTRACT,
    )

    assert correction is None


@pytest.mark.parametrize("project,contract", [
    (None, CONTRACT),
    ({**PROJECT, "folder_status": "authority_unavailable"}, CONTRACT),
    (PROJECT, build_goal_contract("action")),
    (PROJECT, SimpleNamespace(kind="local_project", required_categories=("wrong",))),
    ({**PROJECT, "computer_id": ""}, CONTRACT),
    ({**PROJECT, "computer_name": "x" * 129}, CONTRACT),
    ({**PROJECT, "workspace_roots": []}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "/safe/../outside"}]}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "relative"}]}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "/safe/"}]}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "/safe\x00bad"}]}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "/" + "x" * 1024}]}, CONTRACT),
    ({**PROJECT, "workspace_roots": [{"path": "/root"}] * 17}, CONTRACT),
])
def test_routing_policy_does_not_expand_outside_valid_local_project_contract(project, contract):
    call = _call("remote__make_directory", '{"path":"/Users/sam/Desktop/Minecraft/subdir"}')

    correction = route_correction(
        [call], {"remote__make_directory": "remote-compute-1"},
        {"remote__make_directory": "make_directory"}, project, contract,
    )

    assert correction is None


def test_remote_work_outside_project_root_is_not_misrouted():
    call = _call("remote__make_directory", '{"machine_id":"cluster-a","path":"/scratch/job"}')

    correction = route_correction(
        [call], {"remote__make_directory": "remote-compute-1"},
        {"remote__make_directory": "make_directory"}, PROJECT, CONTRACT,
    )

    assert correction is None


@pytest.mark.parametrize("arguments,blocked", [
    ({"argv": ["mkdir", "/Users/sam/Desktop/Minecraft/new"]}, True),
    ({"command": "cd /Users/sam/Desktop/Minecraft/new && touch file"}, True),
    ({"script": "cd /Users/sam/Desktop/Minecraft/new"}, True),
    ({"path": "/Users/sam/Desktop/Minecraftish/new"}, False),
    ({"path": "/Users/sam/Desktop/Minecraft/../outside"}, False),
    ({"path": "/Users/sam/Desktop/Minecraftish", "argv": ["/Users/sam/Desktop/Minecraft/new"]}, True),
    ({"argv": [{"path": "/Users/sam/Desktop/Minecraft/new"}]}, True),
    ({"argv": [1]}, False),
    ({"command": "run /Users/sam/Desktop/Minecraftish /Users/sam/Desktop/Minecraft/ok"}, True),
    ({"path": "//Users/sam/Desktop/Minecraft/new"}, False),
    ({"path": "/Users/sam/Desktop/Minecraft/./new"}, False),
])
def test_remote_path_detection_handles_commands_and_rejects_nonmatching_paths(arguments, blocked):
    call = _call("remote__make_directory", arguments)

    correction = route_correction(
        [call], {"remote__make_directory": "remote-compute-1"},
        {"remote__make_directory": "make_directory"}, PROJECT, CONTRACT,
    )

    assert (correction is not None) is blocked


def test_non_tool_call_names_are_ignored_and_invalid_host_arguments_do_not_crash():
    calls = [SimpleNamespace(function=SimpleNamespace(name=None, arguments="{}")),
             _call("local__run_command", "not-json")]

    correction = route_correction(
        calls, {"local__run_command": "computer-use-1"},
        {"local__run_command": "run_command"}, PROJECT, CONTRACT,
    )

    assert correction is None


@pytest.mark.parametrize("arguments", [
    {"computer_id": {"unexpected": "object"}},
    {"computer": ["computer-a"]},
])
def test_unusable_explicit_host_values_are_rejected_without_type_errors(arguments):
    correction = route_correction(
        [_call("local__run_command", arguments)], {"local__run_command": "computer-use-1"},
        {"local__run_command": "run_command"}, PROJECT, CONTRACT,
    )

    assert correction is not None


@pytest.mark.asyncio
async def test_goal_batch_stops_after_none_result_and_retains_the_observation():
    calls = ["mkdir", "write", "tests"]
    events = []

    async def dispatch(call):
        events.append(("start", call))
        await asyncio.sleep(0)
        events.append(("end", call))
        return {"mkdir": {"ok": True}, "write": None, "tests": {"exit_code": 0}}[call]

    async def should_stop():
        return False

    results = await execute_goal_batch(calls, dispatch, should_stop)

    assert results == [{"ok": True}, None]
    assert events == [
        ("start", "mkdir"), ("end", "mkdir"),
        ("start", "write"), ("end", "write"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_result", [
    {"status": "uncertain"},
    {"code": "outcome_uncertain", "message": "effect unknown", "next_action": "inspect"},
    {"ok": False, "error": "write failed"},
    {},
    False,
])
async def test_goal_batch_stops_after_any_noncompleted_goal_progress_outcome(uncertain_result):
    calls = ["first", "dependent-write"]
    executed = []

    async def dispatch(call):
        executed.append(call)
        return uncertain_result

    results = await execute_goal_batch(calls, dispatch, lambda: False)

    assert executed == ["first"]
    assert results == [uncertain_result]


@pytest.mark.asyncio
async def test_goal_batch_uses_canonical_name_for_background_command_completion():
    calls = [
        SimpleNamespace(function=SimpleNamespace(name="computer-use-1__run_command")),
        SimpleNamespace(function=SimpleNamespace(name="computer-use-1__write_file")),
    ]
    executed = []
    canonical_names = []

    async def dispatch(call):
        executed.append(call.function.name)
        return {"job_id": "job-a", "background": True, "exit_code": None}

    def canonical_tool_name(call):
        name = call.function.name
        canonical = name.split("__", 1)[1]
        canonical_names.append(canonical)
        return canonical

    results = await execute_goal_batch(calls, dispatch, lambda: False, canonical_tool_name)

    assert executed == ["computer-use-1__run_command"]
    assert canonical_names == ["run_command"]
    assert results == [{"job_id": "job-a", "background": True, "exit_code": None}]


@pytest.mark.asyncio
async def test_goal_batch_checks_stop_before_dispatching_first_call():
    executed = []

    async def dispatch(call):
        executed.append(call)
        return {"ok": True}

    results = await execute_goal_batch(["write"], dispatch, lambda: True)

    assert executed == []
    assert results == []


@pytest.mark.asyncio
async def test_goal_batch_stops_after_confirmation_required_result():
    calls = ["write", "delete"]
    executed = []

    async def dispatch(call):
        executed.append(call)
        return SimpleNamespace(result={"_data": {"status": "confirmation_required"}})

    results = await execute_goal_batch(calls, dispatch, lambda: False)

    assert executed == ["write"]
    assert len(results) == 1
    assert results[0].result["_data"]["status"] == "confirmation_required"


@pytest.mark.asyncio
async def test_goal_batch_stops_when_cancellation_or_uncertainty_callback_says_stop():
    calls = ["first", "second"]
    executed = []
    stop_checks = iter([False, True])

    async def dispatch(call):
        executed.append(call)
        return {"result": call}

    results = await execute_goal_batch(calls, dispatch, lambda: next(stop_checks))

    assert executed == ["first"]
    assert results == [{"result": "first"}]


@pytest.mark.asyncio
async def test_goal_batch_supports_synchronous_dispatch_results():
    results = await execute_goal_batch(["one"], lambda call: call, lambda: False)

    assert results == ["one"]


def test_invalid_remote_mutator_arguments_fail_closed_for_granted_local_project():
    call = _call("remote__make_directory", "not-json")

    correction = route_correction(
        [call], {"remote__make_directory": "remote-compute-1"},
        {"remote__make_directory": "make_directory"}, PROJECT, CONTRACT,
    )

    assert correction is not None
    assert "No local computer-use tools are registered" in correction


def test_oversized_remote_arguments_fail_closed_for_mutating_tool():
    correction = route_correction(
        [_call("remote__make_directory", "x" * 32769)],
        {"remote__make_directory": "remote-compute-1"},
        {"remote__make_directory": "make_directory"}, PROJECT, CONTRACT,
    )

    assert correction is not None
