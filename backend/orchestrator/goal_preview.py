"""Dispatches a completed local project's preview through its granted computer-use session."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

from orchestrator.goal_auto_verify import _canonical_root, _root_granted


async def start_local_preview(
    orch,
    websocket,
    goal_progress,
    goal_project,
    tool_to_agent,
    tool_to_unqualified,
    chat_id,
    user_id,
    *,
    task=None,
) -> dict[str, Any] | None:
    root = _canonical_root(goal_project.get("primary_root") if isinstance(goal_project, dict) else None)
    if (root is None or not _root_granted(root, goal_project)
            or goal_project.get("folder_status") != "granted"):
        return None
    names = {
        (tool_to_unqualified or {}).get(tool, tool): tool
        for tool, agent in (tool_to_agent or {}).items() if agent == "computer-use-1"
    }
    if not all(verb in names for verb in ("list_dir", "read_file", "project_preview")):
        return None

    async def call(verb: str, arguments: dict[str, Any], *, include_components: bool = False):
        if getattr(orch, "cancelled_sessions", {}).get(id(websocket)):
            return None
        if task is not None:
            await orch.task_manager.assert_current_execution(task)
        token = uuid.uuid4().hex
        call_id = f"goal-preview-{token[:20]}"
        tool_call = SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=names[verb], arguments=json.dumps(arguments, ensure_ascii=False)),
        )
        from orchestrator.hitl_confirmation import goal_call_scope

        with goal_call_scope(token):
            result = await orch.execute_single_tool(
                websocket, tool_call, tool_to_agent, chat_id, user_id=user_id,
                tool_to_unqualified=tool_to_unqualified,
            )
        if task is not None:
            await orch.task_manager.assert_current_execution(task)
        if getattr(orch, "cancelled_sessions", {}).get(id(websocket)):
            return None
        goal_progress.record(
            call_id, verb, tool_call.function.arguments, result,
            agent_id="computer-use-1", project_context=goal_project,
            approval_token=token,
        )
        if goal_progress.requires_approval or not goal_progress.is_completed_success(verb, result):
            return None
        data, error = goal_progress._unwrap(result)
        if error or not isinstance(data, dict):
            return None
        value = data.get("_data", data)
        if not isinstance(value, dict):
            return None
        if not include_components:
            return value
        raw = getattr(result, "result", result)
        components = getattr(result, "ui_components", None)
        if not isinstance(components, list) and isinstance(raw, dict):
            components = raw.get("_ui_components")
        return {
            **value,
            "status": "running" if value.get("state") == "running" else value.get("status"),
            "ui_components": components if isinstance(components, list) else [],
            "_result": result,
            "_tool_name": names[verb],
            "_tool_arguments": arguments,
        }

    listing = await call("list_dir", {"path": root})
    if listing is None:
        return {"status": "blocked", "reason": "The authorized project listing was unavailable."}
    entries = listing.get("entries")
    if not isinstance(entries, list):
        return {"status": "blocked", "reason": "The project listing did not return entries."}
    files = {entry.get("name") for entry in entries if isinstance(entry, dict)
             and isinstance(entry.get("name"), str) and entry.get("is_dir") is not True}
    if "package.json" not in files:
        return {"status": "not_applicable", "reason": "The selected project has no JavaScript package manifest."}
    manifest = await call("read_file", {"path": str(PurePosixPath(root) / "package.json"), "max_bytes": 32768})
    if manifest is None or manifest.get("truncated") is not False or not isinstance(manifest.get("text"), str):
        return {"status": "blocked", "reason": "The JavaScript package manifest could not be read in full."}
    try:
        package = json.loads(manifest["text"])
    except (ValueError, TypeError):
        return {"status": "blocked", "reason": "The JavaScript package manifest is invalid JSON."}
    manager = _package_manager(package, files)
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if manager is None or not isinstance(scripts, dict):
        return {"status": "not_applicable", "reason": "No supported package manager or scripts were declared."}
    script = next((name for name in ("preview", "dev", "start")
                   if isinstance(scripts.get(name), str)
                   and re.search(r"(?:^|[\s;&|])vite(?:\s|$)", scripts[name])), None)
    if script is None:
        return {"status": "not_applicable", "reason": "The project has no supported Vite preview, dev, or start script."}
    started = await call("project_preview", {
        "action": "start", "cwd": root, "package_manager": manager, "script": script,
    }, include_components=True)
    if started is None:
        return {"status": "blocked", "reason": "The preview start was denied, canceled, or failed."}
    if (started.get("state") != "running" or started.get("url") != "http://localhost:4173/"
            or not isinstance(started.get("job_id"), str) or not started["job_id"]):
        return {"status": "unavailable", "reason": "The preview server did not pass its bounded HTTP check."}
    return started


def _package_manager(package: Any, files: set[str]) -> str | None:
    declared = package.get("packageManager") if isinstance(package, dict) else None
    lock_managers = {
        manager for filename, manager in (
            ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
            ("bun.lock", "bun"), ("bun.lockb", "bun"), ("package-lock.json", "npm"),
        ) if filename in files
    }
    if isinstance(declared, str):
        name = declared.split("@", 1)[0]
        if name in {"npm", "pnpm", "yarn", "bun"}:
            return name if not lock_managers or lock_managers == {name} else None
    return next(iter(lock_managers)) if len(lock_managers) == 1 else None
