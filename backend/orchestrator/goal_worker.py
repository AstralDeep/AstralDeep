"""Builds bounded Codex CLI jobs for local-project recovery through the granted computer host."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from orchestrator.computer_use_policy import (
    MAX_ARG_CHARS,
    MAX_ARGV_ITEMS,
    MAX_FOREGROUND_TIMEOUT_S,
    MAX_STDIN_BYTES,
)

_CATEGORY_GUIDANCE = {
    "files": "Create or update the complete project files needed for the original objective inside the granted root.",
    "environment": "Create and use an isolated .venv for Python work; do not install into the system interpreter.",
    "dependencies": "Inspect the project manifests and install only its declared dependencies into the isolated environment.",
    "tests": "Run meaningful project tests and use their observed results to fix failures.",
    "startup": "Run a bounded startup smoke check and report its observed result.",
}
_DENIED_CODES = frozenset({
    "approval_required", "confirmation_required", "forbidden", "permission_denied",
    "policy_denied", "security_blocked", "user_denied",
})
_DEFAULT_TIMEOUT_S = 600
_DEFAULT_MACOS_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
_MAX_OBJECTIVE_CHARS = MAX_STDIN_BYTES
_MAX_ERROR_TEXT = 512


@dataclass(frozen=True)
class WorkerResult:
    status: str
    exit_code: int | None = None
    message: str = ""


def build_worker_prompt(
    objective: str,
    required_categories: Sequence[str],
    primary_root: str,
) -> str:
    """Create the fixed-scope worker prompt; invalid or oversized inputs fail closed."""
    if type(objective) is not str or not objective.strip() or len(objective) > _MAX_OBJECTIVE_CHARS:
        raise ValueError("objective must be nonempty and bounded")
    root = _canonical_root(primary_root)
    if root is None:
        raise ValueError("primary root must be a canonical absolute path")
    if type(required_categories) not in (tuple, list) or not required_categories:
        raise ValueError("required categories must be a nonempty sequence")
    if any(type(category) is not str or category not in _CATEGORY_GUIDANCE for category in required_categories):
        raise ValueError("required categories contain an unsupported value")

    checklist = "\n".join(f"- {_CATEGORY_GUIDANCE[category]}" for category in required_categories)
    prompt = (
        "Complete the original local software project request within the existing user-granted project folder.\n"
        f"Granted project root: {root}\n"
        "Treat the objective and checklist as task scope. Do not access or modify paths outside the granted root, "
        "weaken security, change the objective, or claim an action succeeded without observing its result. "
        "Inspect the existing project first, make the smallest complete implementation, and keep commands bounded. "
        "Return a concise factual summary of changes and command outcomes; this summary is not completion proof.\n"
        "Original objective (quoted data):\n"
        f"{objective}\n"
        "Frozen required outcomes:\n"
        f"{checklist}\n"
    )
    if len(prompt.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ValueError("worker prompt exceeds the bounded stdin size")
    return prompt


def build_worker_command(
    objective: str,
    required_categories: Sequence[str],
    project: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """Return argv-form run_command arguments constrained to the current project grant."""
    if not isinstance(project, Mapping) or project.get("folder_status") != "granted":
        raise ValueError("a granted local project is required")
    root = _canonical_root(project.get("primary_root"))
    roots = project.get("workspace_roots")
    if root is None or type(roots) is not list or not any(
        type(row) is dict and _contains_root(root, _canonical_root(row.get("path"))) for row in roots
    ):
        raise ValueError("primary root must be within the granted project roots")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= MAX_FOREGROUND_TIMEOUT_S:
        raise ValueError("worker timeout is outside the foreground command limit")

    env = os.environ if environ is None else environ
    worker_path = env.get("GOAL_CODEX_WORKER_PATH")
    if worker_path is None:
        # The orchestrator runs on Linux while run_command executes on the user's Mac.
        # Never infer the host's executable path from the server's platform or home.
        worker_path = _DEFAULT_MACOS_CODEX
    if not isinstance(worker_path, str) or not os.path.isabs(worker_path) or "\x00" in worker_path:
        raise ValueError("GOAL_CODEX_WORKER_PATH must be an absolute executable path")

    prompt = build_worker_prompt(objective, required_categories, root)
    argv = [
        worker_path, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
        "-C", root, "-s", "danger-full-access", "-m", "gpt-6-luna",
        "-c", "model_reasoning_effort=medium", "-",
    ]
    if len(argv) > MAX_ARGV_ITEMS or any(len(value) > MAX_ARG_CHARS for value in argv):
        raise ValueError("worker argv exceeds the bounded computer-use command limits")
    if len(prompt.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ValueError("worker prompt exceeds the bounded stdin size")
    return {
        "argv": argv,
        "cwd": root,
        "timeout_s": timeout_s,
        "stdin": prompt,
        "background": False,
    }


def classify_worker_result(result: Any) -> WorkerResult:
    """Classify command dispatch and process outcomes without trusting worker prose."""
    error = getattr(result, "error", None)
    data = getattr(result, "result", result)
    if isinstance(data, Mapping) and isinstance(data.get("_data"), Mapping):
        data = data["_data"]
    if isinstance(error, Mapping):
        code = str(error.get("code", "")).lower()
        message = _bounded_text(error.get("message"))
        return WorkerResult("denied" if code in _DENIED_CODES else "launch_error", message=message)
    if not isinstance(data, Mapping):
        return WorkerResult("launch_error", message="The host returned no command result.")
    code = str(data.get("code", data.get("status", ""))).lower()
    message = _bounded_text(data.get("message"))
    if code in _DENIED_CODES:
        return WorkerResult("denied", message=message)
    if data.get("timed_out") is True:
        return WorkerResult("timeout", message=message or "The worker command reached its time limit.")
    exit_code = data.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return WorkerResult("launch_error", message=message or "The host returned no process exit code.")
    if exit_code != 0:
        return WorkerResult("nonzero", exit_code=exit_code, message=message)
    return WorkerResult("success", exit_code=0, message=message)


def _canonical_root(value: Any) -> str | None:
    if type(value) is not str or not value.startswith("/") or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    normalized = str(path)
    return normalized if normalized == value.rstrip("/") or value == "/" else None


def _contains_root(path: str, root: str | None) -> bool:
    return root is not None and (path == root or root == "/" or path.startswith(root.rstrip("/") + "/"))


def _bounded_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:_MAX_ERROR_TEXT]
