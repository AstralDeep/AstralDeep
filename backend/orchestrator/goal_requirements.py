"""Defines immutable goal intent contracts and validates bounded references to successful tool observations."""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

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
_SOURCE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".html", ".java", ".js",
    ".jsx", ".kt", ".m", ".mm", ".py", ".rb", ".rs", ".swift", ".ts",
    ".tsx", ".vue", ".css",
})
_PROBE_STEMS = frozenset({"probe", "scratch", "temp", "tmp"})
_PROJECT_MANIFESTS = frozenset({
    "cargo.toml", "go.mod", "package.json", "package-lock.json", "pnpm-lock.yaml",
    "pyproject.toml", "requirements.txt", "setup.py", "swift.package", "package.swift",
    "yarn.lock",
})


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
    referenced_categories: dict[str, tuple[str, ...]] = {}
    for category in contract.required_categories:
        ids = parsed.get(category, ())
        if not ids:
            missing.append(category)
            continue
        referenced_categories[category] = ids
        if any(not _valid_local_observation(category, calls.get(call_id), calls, roots, project_context)
               for call_id in ids):
            invalid.append(category)
    owners = {}
    for category, ids in referenced_categories.items():
        for call_id in ids:
            if call_id in owners:
                invalid.extend((owners[call_id], category))
            else:
                owners[call_id] = category
    if not missing:
        if not _valid_source_artifact(referenced_categories.get("files", ()), calls, roots, project_context):
            invalid.append("files")
        for category in ("environment", "dependencies", "tests", "startup"):
            ids = referenced_categories.get(category, ())
            if ids and not any(
                _valid_project_stage(category, calls.get(call_id), calls, roots, project_context)
                for call_id in ids
            ):
                invalid.append(category)
    invalid = list(dict.fromkeys(invalid))
    return ProofAssessment(not missing and not invalid, tuple(missing), tuple(invalid))


def select_goal_proof_references(
    contract: GoalContract,
    observations: Iterable[dict[str, Any]],
    project_context: dict[str, Any] | None,
) -> dict[str, list[str]]:
    """Select proof IDs from private observations; never infer IDs from model prose."""
    if (not isinstance(contract, GoalContract) or contract.kind != "local_project"
            or not _contract_valid(contract) or contract.blocked_reason
            or not _project_context_valid(project_context)):
        return {}
    if isinstance(observations, (str, bytes, dict)):
        return {}
    rows: list[dict[str, Any]] = []
    try:
        for row in observations:
            rows.append(row)
            if len(rows) > _MAX_OBSERVATIONS:
                return {}
    except TypeError:
        return {}
    calls, errors = _index_observations(rows)
    if errors:
        return {}
    roots = _project_roots(project_context)
    ordered = sorted(calls, key=lambda call_id: (
        calls[call_id].get("sequence") if type(calls[call_id].get("sequence")) is int else 2**31,
        call_id,
    ))

    # A file artifact needs a source readback paired either with a matching write
    # or with an attested worker whose cwd contains the readback.
    reads: list[str] = []
    writes: list[str] = []
    workers: list[str] = []
    for call_id in ordered:
        call = calls[call_id]
        if not _valid_local_observation("files", call, calls, roots, project_context):
            continue
        args = call.get("arguments", {})
        data = _result_data(call.get("result", call.get("data")))
        if call.get("tool") == "run_command" and call.get("attested_worker") is True:
            if _inside_roots(args.get("cwd"), roots) is not None:
                workers.append(call_id)
        elif call.get("tool") == "read_file":
            path = _inside_roots(args.get("path"), roots)
            if (path and _source_path(path) and type(data.get("size")) is int
                    and data["size"] >= 256 and data.get("has_meaningful_content") is True
                    and data.get("truncated") is False and isinstance(data.get("content_sha256"), str)):
                reads.append(call_id)
        elif call.get("tool") == "write_file":
            path = _inside_roots(args.get("path"), roots)
            size = args.get("content_size", data.get("bytes", data.get("bytes_written")))
            if (path and _source_path(path) and type(size) is int and size >= 256
                    and isinstance(args.get("content_sha256"), str)):
                writes.append(call_id)

    file_options: list[tuple[str, ...]] = []
    for read_id in reads:
        read = calls[read_id]
        read_args = read.get("arguments", {})
        read_data = _result_data(read.get("result", read.get("data")))
        read_path = _inside_roots(read_args.get("path"), roots)
        for write_id in writes:
            write = calls[write_id]
            write_data = _result_data(write.get("result", write.get("data")))
            if (_inside_roots(write.get("arguments", {}).get("path"), roots) == read_path
                    and write.get("arguments", {}).get("content_sha256") == read_data.get("content_sha256")):
                file_options.append((write_id, read_id))
        for worker_id in workers:
            cwd = _inside_roots(calls[worker_id].get("arguments", {}).get("cwd"), roots)
            if cwd and read_path.startswith(cwd.rstrip("/") + "/"):
                file_options.append((worker_id, read_id))
    file_options = sorted(set(file_options), key=lambda refs: tuple(ordered.index(item) for item in refs))
    if not file_options:
        return {}

    stage_options = {
        category: [call_id for call_id in ordered
                   if _valid_project_stage(category, calls[call_id], calls, roots, project_context)]
        for category in ("environment", "dependencies", "tests", "startup")
    }
    if any(not stage_options[category] for category in contract.required_categories[1:]):
        return {}

    # Try file witnesses in deterministic order. Backtrack over stage choices so
    # one observation cannot be assigned to multiple categories.
    for file_refs in file_options:
        selected = {"files": list(file_refs)}
        used = set(file_refs)
        stages = sorted(contract.required_categories[1:], key=lambda c: (len(stage_options[c]), c))

        def assign(index: int) -> bool:
            if index == len(stages):
                return validate_goal_proofs(contract, selected, rows, project_context).accepted
            category = stages[index]
            for call_id in stage_options[category]:
                if call_id in used:
                    continue
                used.add(call_id)
                selected[category] = [call_id]
                if assign(index + 1):
                    return True
                selected.pop(category, None)
                used.remove(call_id)
            return False

        if assign(0):
            return {category: selected[category] for category in contract.required_categories}
    return {}


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
        if category != "files" and not (
            category == "dependencies" and tool == "read_file"
        ):
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


def _valid_source_artifact(ids: tuple[str, ...], calls: dict[str, dict[str, Any]],
                           roots: tuple[str, ...], context: dict[str, Any]) -> bool:
    writes = []
    workers = []
    reads = []
    for call_id in ids:
        call = calls.get(call_id)
        if not _valid_local_observation("files", call, calls, roots, context):
            continue
        args = call.get("arguments", {})
        data = _result_data(call.get("result", call.get("data")))
        if call.get("tool") == "run_command":
            if call.get("attested_worker") is True:
                cwd = _inside_roots(args.get("cwd"), roots)
                if cwd is not None:
                    workers.append(cwd)
            continue
        path = _inside_roots(args.get("path"), roots)
        if path is None or not _source_path(path):
            continue
        if call.get("tool") == "write_file":
            size = args.get("content_size", data.get("bytes", data.get("bytes_written")))
            if type(size) is int and size >= 256 and isinstance(args.get("content_sha256"), str):
                writes.append((path, args.get("content_sha256")))
        elif call.get("tool") == "read_file":
            size = data.get("size")
            if (type(size) is int and size >= 256 and data.get("has_meaningful_content") is True
                    and data.get("truncated") is False and isinstance(data.get("content_sha256"), str)):
                reads.append((path, data.get("content_sha256")))
    matched_write = any(write_path == read_path and write_hash == read_hash
                        for write_path, write_hash in writes for read_path, read_hash in reads)
    matched_worker = any(
        read_path.startswith(cwd.rstrip("/") + "/")
        for cwd in workers for read_path, _read_hash in reads
    )
    return matched_write or matched_worker


def _source_path(path: str) -> bool:
    basename = PurePosixPath(path).name.lower()
    stem = PurePosixPath(basename).stem
    return (PurePosixPath(basename).suffix in _SOURCE_EXTENSIONS
            and "probe" not in stem
            and stem not in _PROBE_STEMS
            and not any(stem.startswith(prefix + "_") or stem.endswith("_" + prefix)
                        for prefix in _PROBE_STEMS))


def _valid_project_stage(category: str, call: dict[str, Any] | None,
                         calls: dict[str, dict[str, Any]], roots: tuple[str, ...],
                         context: dict[str, Any]) -> bool:
    if not _valid_local_observation(category, call, calls, roots, context):
        return False
    if call.get("tool") in {"read_file", "list_dir"}:
        if category != "dependencies" or call.get("tool") != "read_file":
            return False
        path = PurePosixPath(call.get("arguments", {}).get("path", "")).name.lower()
        data = _result_data(call.get("result", call.get("data")))
        return path in _PROJECT_MANIFESTS and data.get("truncated") is False and data.get("has_meaningful_content") is True
    args = _command_arguments(call, calls)
    if args is None:
        return False
    commands = _command_segments(args)
    predicates = {
        "environment": _is_environment_command,
        "dependencies": _is_dependency_command,
        "tests": _is_test_command,
        "startup": _is_startup_command,
    }
    return any(predicates[category](segment) for segment in commands)


def _command_arguments(call: dict[str, Any], calls: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if call.get("tool") == "run_command":
        return call.get("arguments") if isinstance(call.get("arguments"), dict) else None
    if call.get("tool") != "read_output":
        return None
    job_id = call.get("arguments", {}).get("job_id")
    for origin in calls.values():
        data = _result_data(origin.get("result", origin.get("data")))
        if (origin.get("tool") == "run_command" and isinstance(data, dict)
                and data.get("job_id") == job_id and data.get("background") is True):
            return origin.get("arguments") if isinstance(origin.get("arguments"), dict) else None
    return None


def _command_segments(args: dict[str, Any]) -> list[list[str]]:
    argv = args.get("argv")
    if isinstance(argv, list) and all(type(item) is str for item in argv):
        words = argv
    elif type(args.get("command")) is str:
        try:
            words = list(shlex.shlex(args["command"], posix=True, punctuation_chars=";&|"))
        except ValueError:
            return []
    else:
        return []
    segments = []
    current = []
    for word in words:
        if word in {";", "&&", "||", "|", "&"}:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(word)
    if current:
        segments.append(current)
    normalized = []
    for segment in segments:
        if (len(segment) >= 3 and os.path.basename(segment[0]) in {"sh", "bash", "zsh"}
                and "-c" in segment):
            index = segment.index("-c") + 1
            if index < len(segment):
                normalized.extend(_command_segments({"command": segment[index]}))
                continue
        while segment and os.path.basename(segment[0]) in {"env", "command"}:
            segment.pop(0)
        normalized.append(segment)
    return normalized


def _tokens(segment: list[str]) -> list[str]:
    return [os.path.basename(word).lower() for word in segment]


def _has_sequence(tokens: list[str], sequence: tuple[str, ...]) -> bool:
    return any(tuple(tokens[index:index + len(sequence)]) == sequence
               for index in range(len(tokens) - len(sequence) + 1))


def _is_environment_command(segment: list[str]) -> bool:
    tokens = _tokens(segment)
    return (len(tokens) == 2 and tokens[0] == "node" and segment[1] in {"--version", "-v"}
            or _has_sequence(tokens, ("-m", "venv")) or _has_sequence(tokens, ("-m", "virtualenv"))
            or _has_sequence(tokens, ("uv", "venv"))
            or _has_sequence(tokens, ("uv", "run"))
            or _has_sequence(tokens, ("virtualenv",))
            or _has_sequence(tokens, ("poetry", "run"))
            or _has_sequence(tokens, ("pipenv", "run"))
            or any("sys.prefix" in word and "sys.base_prefix" in word for word in segment))


def _is_dependency_command(segment: list[str]) -> bool:
    tokens = _tokens(segment)
    return (
        _has_sequence(tokens, ("pip", "install")) or _has_sequence(tokens, ("pip3", "install"))
        or _has_sequence(tokens, ("-m", "pip", "install"))
        or _has_sequence(tokens, ("uv", "sync")) or _has_sequence(tokens, ("uv", "install"))
        or _has_sequence(tokens, ("npm", "install")) or _has_sequence(tokens, ("npm", "ci"))
        or _has_sequence(tokens, ("pnpm", "install")) or _has_sequence(tokens, ("yarn", "install"))
        or _has_sequence(tokens, ("cargo", "fetch")) or _has_sequence(tokens, ("cargo", "build"))
        or _has_sequence(tokens, ("go", "mod", "download"))
        or _has_sequence(tokens, ("go", "mod", "tidy"))
        or _has_sequence(tokens, ("swift", "package", "resolve"))
    )


def _is_test_command(segment: list[str]) -> bool:
    tokens = _tokens(segment)
    return (
        "pytest" in tokens or "py.test" in tokens or _has_sequence(tokens, ("-m", "unittest"))
        or _has_sequence(tokens, ("npm", "test")) or _has_sequence(tokens, ("npm", "run", "test"))
        or _has_sequence(tokens, ("pnpm", "test")) or _has_sequence(tokens, ("yarn", "test"))
        or _has_sequence(tokens, ("cargo", "test")) or _has_sequence(tokens, ("go", "test"))
        or _has_sequence(tokens, ("swift", "test"))
    )


def _is_startup_command(segment: list[str]) -> bool:
    tokens = _tokens(segment)
    if _is_test_command(segment):
        return False
    if segment in [
        _javascript_http_probe_argv(manager, script)
        for manager in ("npm", "pnpm", "yarn", "bun")
        for script in ("preview", "start")
    ]:
        return True
    return (
        _has_sequence(tokens, ("npm", "start")) or _has_sequence(tokens, ("npm", "run", "start"))
        or _has_sequence(tokens, ("pnpm", "start")) or _has_sequence(tokens, ("yarn", "start"))
        or _has_sequence(tokens, ("cargo", "run")) or _has_sequence(tokens, ("go", "run"))
        or _has_sequence(tokens, ("swift", "run"))
        or (len(tokens) >= 2 and tokens[0] in {"python", "python3", "node"}
            and (tokens[1].endswith((".py", ".js", ".mjs", ".cjs"))
                 or tokens[1] == "-m" and len(tokens) >= 3
                 and tokens[2] not in {"pip", "venv", "virtualenv", "pytest", "unittest"}))
    )


def _javascript_http_probe_argv(manager: str, script: str) -> list[str]:
    """Return the single canonical bounded HTTP probe accepted as JS startup proof."""
    launch = {
        "npm": [manager, "run", script, "--", "--host", "127.0.0.1", "--port", "4173"],
        "pnpm": [manager, "run", script, "--host", "127.0.0.1", "--port", "4173"],
        "yarn": [manager, script, "--host", "127.0.0.1", "--port", "4173"],
        "bun": [manager, "run", script, "--host", "127.0.0.1", "--port", "4173"],
    }.get(manager)
    if launch is None or script not in {"preview", "start"}:
        raise ValueError("unsupported JavaScript HTTP probe command")
    node = ("const {spawn}=require('node:child_process');const p=spawn(" + json.dumps(launch[0]) + "," +
            json.dumps(launch[1:]) + ",{stdio:'ignore',detached:true});let done=false;" +
            "const stop=()=>{if(!p.pid)return;try{process.kill(-p.pid,'SIGTERM')}catch{p.kill('SIGTERM')}};" +
            "const end=(c)=>{if(done)return;done=true;stop();process.exit(c)};" +
            "p.on('error',()=>end(1));p.on('exit',c=>{if(!done)end(c||1)});" +
            "process.on('SIGTERM',()=>end(1));process.on('SIGINT',()=>end(1));" +
            "const t=Date.now();(async()=>{while(Date.now()-t<20000){try{const r=await fetch('http://127.0.0.1:4173/');" +
            "if(r.status>=200&&r.status<400)return end(0)}catch{}await new Promise(r=>setTimeout(r,250))}end(1)})();")
    return ["node", "-e", node]


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
