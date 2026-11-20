"""Builds bounded Codex CLI jobs for local-project recovery through the granted computer host."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from orchestrator.computer_use_policy import (
    MAX_ARG_CHARS,
    MAX_ARGV_ITEMS,
    MAX_FOREGROUND_TIMEOUT_S,
    MAX_STDIN_BYTES,
)

_CATEGORY_GUIDANCE = {
    "files": "create project files",
    "environment": "set up and use the language-appropriate project environment (Python uses .venv)",
    "dependencies": "install project dependencies",
    "tests": "run meaningful tests and fix failures",
    "startup": "verify startup with a bounded smoke check",
}


_DEFAULT_TIMEOUT_S = 600
_DEFAULT_MACOS_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
_MAX_OBJECTIVE_CHARS = MAX_STDIN_BYTES
_MAX_ERROR_TEXT = 512


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

    checklist = "; ".join(_CATEGORY_GUIDANCE[category] for category in required_categories)
    prompt = (
        f"{objective}\n"
        f"In this folder, {checklist} before finishing. Report observed results."
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
        "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=true",
        "-m", "gpt-6-luna", "-c", "model_reasoning_effort=medium", "-",
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
