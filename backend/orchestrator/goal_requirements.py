"""Defines immutable goal intent contracts and validates bounded references to successful tool observations."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable


GOAL_CLASSIFIER_GUIDANCE = """Classify only the user's original request.
Use informational for answers, explanations, and advice that do not ask to perform an action.
When uncertain whether a requested effect is required, choose action rather than informational.
Use action for a requested effect that is not implementation or operation of a local software project.
Use local_project when the user wants a runnable software project delivered, or explicitly asks to set up, install, build, test, run, or verify a project.
Use action for a one-off file edit, documentation change, or data-only operation; require an outcome appropriate to that request.
Do not classify a question about how software works as local_project unless the user asks you to perform the work.
If runnable project delivery or execution lacks a selected granted project folder, still return local_project.
Return exactly {"kind":"informational|action|local_project"} as one JSON object."""

PROOF_CATEGORIES = ("files", "environment", "dependencies", "tests", "startup")
_KINDS = frozenset({"informational", "action", "local_project"})
_MAX_RESPONSE = 1024
_MAX_OBSERVATIONS = 256
_MAX_PROOF_REFS = 8
_MAX_TOTAL_REFS = 32
_MAX_CALL_ID = 128
_NOOP_EXECUTABLES = frozenset({"echo", "printf", "true", ":"})


@dataclass(frozen=True)
class GoalContract:
    kind: str
    required_categories: tuple[str, ...]
    blocked_reason: str | None = None


@dataclass(frozen=True)
class ProofAssessment:
    accepted: bool
    missing: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()


def parse_goal_contract(text: str) -> GoalContract:
    if not isinstance(text, str) or len(text) > _MAX_RESPONSE:
        raise ValueError("goal classification must be bounded JSON text")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("goal classification must be a JSON object") from exc
    if type(value) is not dict or set(value) != {"kind"}:
        raise ValueError("goal classification has an invalid shape")
    kind = value["kind"]
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("goal classification kind is invalid")
    required = () if kind == "informational" else ("outcome",) if kind == "action" else PROOF_CATEGORIES
    return GoalContract(kind, required)


def build_goal_contract(kind: str, project_context: dict[str, Any] | None = None) -> GoalContract:
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("goal classification kind is invalid")
    if kind == "informational":
        return GoalContract(kind, ())
    if kind == "action":
        return GoalContract(kind, ("outcome",))
    if not _project_context_valid(project_context):
        return GoalContract(kind, PROOF_CATEGORIES,
                            "A current project folder grant is required for local project work.")
    return GoalContract(kind, PROOF_CATEGORIES)


def validate_goal_proofs(
    contract: GoalContract,
    references: Any,
    observations: Iterable[dict[str, Any]],
    project_context: dict[str, Any] | None,
) -> ProofAssessment:
    if not isinstance(contract, GoalContract):
        raise TypeError("contract must be a GoalContract")
    if not _contract_valid(contract):
        return ProofAssessment(False, (), ("goal contract is malformed",))
    if contract.blocked_reason:
        return ProofAssessment(False, contract.required_categories, (contract.blocked_reason,))
    if contract.kind == "informational":
        if references in ({}, None):
            return ProofAssessment(True)
        calls, call_errors = _index_observations(observations)
        if call_errors:
            return ProofAssessment(False, (), tuple(call_errors))
        parsed, reference_errors = _parse_references(GoalContract("action", ("outcome",)), references)
        if reference_errors:
            return ProofAssessment(False, (), tuple(reference_errors))
        if any(not _completed_success(calls.get(call_id)) for call_id in parsed.get("outcome", ())):
            return ProofAssessment(False, (), ("informational proof references must name completed outcomes",))
        return ProofAssessment(True)

    calls, call_errors = _index_observations(observations)
    if call_errors:
        return ProofAssessment(False, contract.required_categories, tuple(call_errors))
    parsed, reference_errors = _parse_references(contract, references)
    if reference_errors:
        return ProofAssessment(False, contract.required_categories, tuple(reference_errors))

    missing: list[str] = []
    invalid: list[str] = []
    if contract.kind == "action":
        ids = parsed.get("outcome", ())
        if not ids:
            missing.append("outcome")
        elif any(not _completed_success(calls.get(call_id)) for call_id in ids):
            invalid.append("outcome")
        return ProofAssessment(not missing and not invalid, tuple(missing), tuple(invalid))

    if contract.kind != "local_project" or not _project_context_valid(project_context):
        return ProofAssessment(False, contract.required_categories, ("current project authority is unavailable",))
    roots = _project_roots(project_context)
    for category in contract.required_categories:
        ids = parsed.get(category, ())
        if not ids:
            missing.append(category)
            continue
        if any(not _valid_local_observation(category, calls.get(call_id), calls, roots, project_context)
               for call_id in ids):
            invalid.append(category)
    return ProofAssessment(not missing and not invalid, tuple(missing), tuple(invalid))


def _parse_references(contract: GoalContract, references: Any) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    if type(references) is not dict:
        return {}, ["proof references must be an object"]
    allowed = set(contract.required_categories)
    if set(references) - allowed:
        return {}, ["proof references contain an unrequested category"]
    result: dict[str, tuple[str, ...]] = {}
    total = 0
    for category, value in references.items():
        if type(category) is not str or type(value) is not list or len(value) > _MAX_PROOF_REFS:
            return {}, ["proof reference list is malformed or oversized"]
        if any(type(call_id) is not str or not call_id or len(call_id) > _MAX_CALL_ID for call_id in value):
            return {}, ["proof reference call ID is malformed"]
        if len(set(value)) != len(value):
            return {}, ["proof reference list contains duplicate IDs"]
        total += len(value)
        result[category] = tuple(value)
    if total > _MAX_TOTAL_REFS:
        return {}, ["proof references exceed the total bound"]
    return result, []


def _index_observations(observations: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if isinstance(observations, (str, bytes, dict)):
        return {}, ["tool observations must be a list of records"]
    try:
        rows = []
        for row in observations:
            rows.append(row)
            if len(rows) > _MAX_OBSERVATIONS:
                return {}, ["tool observations exceed the request bound"]
    except TypeError:
        return {}, ["tool observations must be iterable"]
    calls: dict[str, dict[str, Any]] = {}
    for row in rows:
        if type(row) is not dict:
            return {}, ["tool observation is malformed"]
        call_id = row.get("call_id")
        if type(call_id) is not str or not call_id or len(call_id) > _MAX_CALL_ID or call_id in calls:
            return {}, ["tool observation call ID is missing, malformed, or duplicated"]
        calls[call_id] = row
    return calls, []


def _contract_valid(contract: GoalContract) -> bool:
    if type(contract.kind) is not str or contract.kind not in _KINDS:
        return False
    if type(contract.required_categories) is not tuple or any(type(item) is not str for item in contract.required_categories):
        return False
    expected = {
        "informational": (),
        "action": ("outcome",),
        "local_project": PROOF_CATEGORIES,
    }[contract.kind]
    return (contract.required_categories == expected
            and (contract.blocked_reason is None or type(contract.blocked_reason) is str))


def _completed_success(call: dict[str, Any] | None) -> bool:
    return bool(call and call.get("outcome") == "success" and call.get("completed") is True
                and isinstance(call.get("agent_id"), str) and call.get("agent_id"))


def _project_context_valid(context: dict[str, Any] | None) -> bool:
    if type(context) is not dict or context.get("folder_status") != "granted":
        return False
    if not isinstance(context.get("computer_id"), str) or not context["computer_id"]:
        return False
    if len(context["computer_id"]) > 128:
        return False
    if not isinstance(context.get("computer_name"), str) or not context["computer_name"]:
        return False
    if len(context["computer_name"]) > 128 or not isinstance(context.get("project_id"), str) or not context["project_id"]:
        return False
    roots = context.get("workspace_roots")
    return isinstance(roots, list) and len(roots) <= 16 and bool(_project_roots(context))


def _project_roots(context: dict[str, Any]) -> tuple[str, ...]:
    roots = context.get("workspace_roots")
    if not isinstance(roots, list):
        return ()
    found = []
    for row in roots:
        if not isinstance(row, dict):
            continue
        path = _canonical_path(row.get("path"))
        if path is not None:
            found.append(path)
    return tuple(found)


def _canonical_path(value: Any) -> str | None:
    if type(value) is not str or len(value) > 1024 or not value.startswith("/") or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    normalized = str(path)
    if normalized != value.rstrip("/") and not (value == "/" and normalized == "/"):
        return None
    return normalized


def _inside_roots(path: Any, roots: tuple[str, ...]) -> str | None:
    normalized = _canonical_path(path)
    if normalized is None:
        return None
    if any(normalized == root or root == "/" or normalized.startswith(root.rstrip("/") + "/")
           for root in roots):
        return normalized
    return None


def _valid_local_observation(category: str, call: dict[str, Any] | None,
                             observations: dict[str, dict[str, Any]], roots: tuple[str, ...],
                             context: dict[str, Any]) -> bool:
    if not _completed_success(call) or call.get("agent_id") != "computer-use-1":
        return False
    if call.get("computer_id") != context["computer_id"] or call.get("computer_name") != context["computer_name"]:
        return False
    if call.get("project_id") != context["project_id"]:
        return False
    tool = call.get("tool")
    args = call.get("arguments")
    data = _result_data(call.get("result", call.get("data")))
    if not isinstance(args, dict) or not isinstance(data, dict):
        return False
    if data.get("computer") != context["computer_name"]:
        return False
    if tool in {"read_file", "list_dir", "write_file"}:
        if category != "files":
            return False
        requested = _inside_roots(args.get("path"), roots)
        observed = _inside_roots(data.get("path"), roots)
        if requested is None or observed != requested:
            return False
        if tool == "read_file":
            return (data.get("truncated") is False
                    and (type(data.get("size")) is int and data.get("size", -1) >= 0
                         or type(data.get("has_meaningful_content")) is bool))
        if tool == "list_dir":
            return (type(data.get("count")) is int and data.get("count", -1) >= 0
                    or type(data.get("has_entries")) is bool)
        return type(data.get("bytes")) is int and data.get("bytes", -1) >= 0
    if tool == "run_command":
        return _successful_local_command(args, data, roots)
    if tool == "read_output":
        return _successful_background_observation(call, args, data, observations, roots, context)
    return False


def _result_data(result: Any) -> Any:
    if isinstance(result, dict) and "_data" in result:
        return result.get("_data")
    return result


def _successful_local_command(args: dict[str, Any], data: dict[str, Any], roots: tuple[str, ...]) -> bool:
    arg_cwd = _inside_roots(args.get("cwd"), roots)
    result_cwd = _inside_roots(data.get("cwd"), roots)
    if arg_cwd is None or result_cwd != arg_cwd:
        return False
    if (type(data.get("exit_code")) is not int or data.get("exit_code") != 0
            or data.get("timed_out") is not False or data.get("signal") not in (None, "")
            or data.get("background") is True or data.get("job_id") is not None):
        return False
    return _meaningful_command(args)


def _meaningful_command(args: dict[str, Any], depth: int = 0) -> bool:
    argv = args.get("argv")
    if isinstance(argv, list):
        if not argv or any(type(item) is not str for item in argv):
            return False
        return _meaningful_words(argv, depth)
    command = args.get("command")
    if type(command) is not str or not command.strip():
        return False
    try:
        words = list(shlex.shlex(command, posix=True, punctuation_chars=";&|"))
    except ValueError:
        return False
    if words and os.path.basename(words[0]) in {"sh", "bash", "zsh"} and "-c" in words:
        if depth >= 4:
            return False
        index = words.index("-c") + 1
        if index >= len(words):
            return False
        return _meaningful_command({"command": words[index]}, depth + 1)
    if not words:
        return False
    command_words: list[str] = []
    meaningful = False
    for word in words:
        if word in {";", "&&", "||", "|", "&"}:
            meaningful = _meaningful_words(command_words, depth) or meaningful
            command_words = []
        else:
            command_words.append(word)
    return _meaningful_words(command_words, depth) or meaningful


def _meaningful_words(words: list[str], depth: int) -> bool:
    if not words:
        return False
    executable = os.path.basename(words[0])
    if executable in {"sh", "bash", "zsh"} and "-c" in words:
        if depth >= 4:
            return False
        index = words.index("-c") + 1
        return index < len(words) and _meaningful_command({"command": words[index]}, depth + 1)
    if executable == "env":
        index = 1
        while index < len(words) and (words[index].startswith("-") or "=" in words[index]):
            index += 1
        return index < len(words) and _meaningful_words(words[index:], depth + 1)
    if executable == "command":
        index = 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
        return index < len(words) and _meaningful_words(words[index:], depth + 1)
    return executable not in _NOOP_EXECUTABLES


def _successful_background_observation(call: dict[str, Any], args: dict[str, Any], data: dict[str, Any],
                                       observations: dict[str, dict[str, Any]], roots: tuple[str, ...],
                                       context: dict[str, Any]) -> bool:
    job_id = args.get("job_id")
    if (not isinstance(job_id, str) or data.get("job_id") != job_id or data.get("state") != "exited"
            or type(data.get("exit_code")) is not int or data.get("exit_code") != 0):
        return False
    if data.get("computer") != context["computer_name"]:
        return False
    for origin in observations.values():
        if not isinstance(origin, dict) or origin.get("tool") != "run_command":
            continue
        origin_data = _result_data(origin.get("result", origin.get("data")))
        origin_args = origin.get("arguments")
        if (not isinstance(origin_data, dict) or not isinstance(origin_args, dict)
                or origin.get("outcome") != "success" or origin.get("completed") is not False
                or origin_data.get("job_id") != job_id or origin_data.get("background") is not True
                or ("cwd" in origin_data
                    and _inside_roots(origin_data.get("cwd"), roots) != _inside_roots(origin_args.get("cwd"), roots))
                or origin_data.get("computer") != context["computer_name"]
                or origin.get("agent_id") != "computer-use-1"
                or origin.get("computer_id") != context["computer_id"]
                or origin.get("computer_name") != context["computer_name"]
                or origin.get("project_id") != context["project_id"]
                or type(origin.get("sequence")) is not int
                or type(call.get("sequence")) is not int
                or origin["sequence"] >= call["sequence"]
                or _inside_roots(origin_args.get("cwd"), roots) is None
                or not _meaningful_command(origin_args)):
            continue
        return True
    return False
