"""Routes local-project goal calls to their granted computer and orders dependent execution."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from orchestrator.goal_progress import GoalProgress
from orchestrator.goal_requirements import GoalContract, PROOF_CATEGORIES

_REMOTE_COMPUTE_AGENT = "remote-compute-1"
_COMPUTER_USE_AGENT = "computer-use-1"
_PATH_KEYS = frozenset({
    "path", "cwd", "directory", "dir", "script_path", "remote_path", "destination_path",
    "source_path", "file_path", "target_path", "working_directory", "script", "command", "argv",
})
_HOST_ID_KEYS = frozenset({"computer_id", "host_id"})
_HOST_NAME_KEYS = frozenset({"computer_name"})


def route_correction(
    tool_calls: Iterable[Any],
    tool_to_agent: Mapping[str, str],
    tool_to_unqualified: Mapping[str, str] | None,
    project: dict[str, Any] | None,
    contract: GoalContract | None,
) -> str | None:
    roots = _granted_roots(project, contract)
    if not roots:
        return None
    local_tools = sorted(name for name, agent in tool_to_agent.items() if agent == _COMPUTER_USE_AGENT)
    rejected = []
    for call in tool_calls:
        name = getattr(getattr(call, "function", None), "name", None)
        if not isinstance(name, str):
            continue
        agent = tool_to_agent.get(name)
        canonical = (tool_to_unqualified or {}).get(name, name)
        arguments = _call_arguments(getattr(getattr(call, "function", None), "arguments", None))
        if agent == _REMOTE_COMPUTE_AGENT and _targets_granted_root(arguments, roots):
            rejected.append(name)
        elif agent == _COMPUTER_USE_AGENT and _host_mismatch(arguments, project):
            rejected.append(name)
        elif agent == _REMOTE_COMPUTE_AGENT and canonical in {"make_directory", "upload_file", "remove_path"}:
            if arguments is None:
                rejected.append(name)
    return _host_correction(project, local_tools, rejected) if rejected else None


async def execute_goal_batch(
    calls: Iterable[Any],
    dispatch: Callable[[Any], Any],
    should_stop: Callable[[], Any],
    canonical_tool_name: Callable[[Any], str] | None = None,
) -> list[Any]:
    results = []
    for call in calls:
        if await _stop_requested(should_stop):
            break
        result = dispatch(call)
        if inspect.isawaitable(result):
            result = await result
        results.append(result)
        tool_name = canonical_tool_name(call) if canonical_tool_name is not None else _call_tool_name(call)
        if (GoalProgress.is_approval_pending(result)
                or not GoalProgress.is_completed_success(tool_name, result)
                or await _stop_requested(should_stop)):
            break
    return results


def _call_tool_name(call: Any) -> str:
    value = getattr(getattr(call, "function", None), "name", None)
    return value if isinstance(value, str) else ""


def _granted_roots(project: dict[str, Any] | None, contract: GoalContract | None) -> tuple[str, ...]:
    if (not isinstance(contract, GoalContract)
            or contract.kind != "local_project"
            or contract.required_categories != PROOF_CATEGORIES
            or contract.blocked_reason is not None
            or type(project) is not dict
            or project.get("folder_status") != "granted"):
        return ()
    for key, maximum in (("project_id", 128), ("computer_id", 128), ("computer_name", 128)):
        value = project.get(key)
        if type(value) is not str or not value or len(value) > maximum:
            return ()
    rows = project.get("workspace_roots")
    if type(rows) is not list or not rows or len(rows) > 16:
        return ()
    roots = tuple(path for row in rows if type(row) is dict
                  if (path := _canonical_root(row.get("path"))) is not None)
    return roots


def _canonical_root(value: Any) -> str | None:
    if type(value) is not str or not value.startswith("/") or "\x00" in value or len(value) > 1024:
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    normalized = str(path)
    return normalized if normalized == value.rstrip("/") or value == "/" else None


def _call_arguments(value: Any) -> dict[str, Any] | None:
    if type(value) is dict:
        return value
    if not isinstance(value, str) or len(value) > 32768:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return None
    return parsed if type(parsed) is dict else None


def _targets_granted_root(arguments: dict[str, Any] | None, roots: tuple[str, ...]) -> bool:
    if arguments is None:
        return False
    return any(_value_targets_root(value, roots, key in {"command", "argv", "script"})
               for key, value in arguments.items() if key in _PATH_KEYS)


def _value_targets_root(value: Any, roots: tuple[str, ...], command_text: bool) -> bool:
    if isinstance(value, str):
        if any(_inside_root(value, root) for root in roots):
            return True
        return command_text and any(_contains_root_reference(value, root) for root in roots)
    if isinstance(value, (list, tuple)):
        return any(_value_targets_root(item, roots, command_text) for item in value[:128])
    if isinstance(value, dict):
        return any(_value_targets_root(item, roots, command_text)
                   for key, item in list(value.items())[:128] if key in _PATH_KEYS)
    return False


def _inside_root(value: str, root: str) -> bool:
    if not value.startswith("/") or "\x00" in value:
        return False
    path = PurePosixPath(value)
    if ".." in path.parts:
        return False
    normalized = str(path)
    if normalized != value.rstrip("/") and not (value == "/" and normalized == "/"):
        return False
    return normalized == root or root == "/" or normalized.startswith(root.rstrip("/") + "/")


def _contains_root_reference(value: str, root: str) -> bool:
    start = 0
    while True:
        index = value.find(root, start)
        if index < 0:
            return False
        end = index + len(root)
        if end == len(value) or value[end] == "/" or not value[end].isalnum() and value[end] not in "._-":
            return True
        start = index + 1


def _host_mismatch(arguments: dict[str, Any] | None, project: dict[str, Any]) -> bool:
    if arguments is None:
        return False
    selected_id = project["computer_id"]
    selected_name = project["computer_name"]
    for key in _HOST_ID_KEYS:
        if key in arguments and arguments[key] != selected_id:
            return True
    for key in _HOST_NAME_KEYS:
        if key in arguments and arguments[key] != selected_name:
            return True
    for key in ("computer", "host"):
        if key in arguments:
            value = arguments[key]
            if value != selected_id and value != selected_name:
                return True
    return False


def _host_correction(project: dict[str, Any], local_tools: list[str], rejected_tools: list[str]) -> str:
    rejected = ", ".join(f"`{name}`" for name in sorted(set(rejected_tools))[:16])
    if local_tools:
        choices = ", ".join(f"`{name}`" for name in local_tools[:16])
        choice_text = f"Use only the registered local computer-use tools for {project['computer_name']}: {choices}."
    else:
        choice_text = (f"No local computer-use tools are registered for {project['computer_name']}; "
                       "stop and report the missing capability instead of substituting remote-compute.")
    return (f"Do not dispatch this batch; it routes {rejected} away from the selected project grant on "
            f"{project['computer_name']}. {choice_text} Keep every path within the granted project roots, "
            "create each required parent before writing, and wait for each dependent tool result before the next call.")


async def _stop_requested(should_stop: Callable[[], Any]) -> bool:
    value = should_stop()
    if inspect.isawaitable(value):
        value = await value
    return bool(value)
