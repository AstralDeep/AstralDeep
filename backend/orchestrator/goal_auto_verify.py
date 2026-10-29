"""Bounded deterministic verification for local Python and JavaScript projects."""

from __future__ import annotations

import json
import uuid
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

from orchestrator.goal_requirements import (
    select_goal_proof_references,
)

_MAX_DIRECTORIES = 24
_MAX_FILES = 128
_MAX_DEPTH = 3
_IGNORED_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules", ".tox"})
_SOURCE_SUFFIXES = frozenset({".py"})
_JS_SOURCE_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})


async def verify_local_project(
    orch,
    websocket,
    goal_progress,
    goal_project,
    goal_contract,
    tool_to_agent,
    tool_to_unqualified,
    chat_id,
    user_id,
    task=None,
) -> dict[str, list[str]]:
    """Run project-scoped setup, tests and a bounded startup check.

    Returns references only when the ordinary private-observation validator accepts
    all required categories. Tool dispatch always uses ``execute_single_tool``.
    """
    root = _canonical_root(goal_project.get("primary_root") if isinstance(goal_project, dict) else None)
    if (root is None or not _root_granted(root, goal_project)
            or goal_project.get("folder_status") != "granted"
            or getattr(goal_contract, "kind", None) != "local_project"):
        return {}
    names = {
        (tool_to_unqualified or {}).get(name, name): name
        for name, agent in (tool_to_agent or {}).items()
        if agent == "computer-use-1"
    }
    if not all(verb in names for verb in ("list_dir", "read_file", "run_command")):
        return {}

    async def call(verb: str, arguments: dict[str, Any]):
        if task is not None:
            await orch.task_manager.assert_current_execution(task)
        token = uuid.uuid4().hex
        call_id = f"goal-auto-{token[:20]}"
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
        return data.get("_data", data)

    def stopped():
        return bool(getattr(orch, "cancelled_sessions", {}).get(id(websocket)))

    listing = await call("list_dir", {"path": root})
    if listing is None or stopped():
        return {}
    root_entries = listing.get("entries")
    if isinstance(root_entries, list) and any(
        isinstance(entry, dict) and entry.get("name") == "package.json" and entry.get("is_dir") is not True
        for entry in root_entries
    ) and not any(
        isinstance(entry, dict) and entry.get("name") in {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"}
        and entry.get("is_dir") is not True for entry in root_entries
    ):
        return await _verify_javascript_project(
            root, listing, call, stopped, goal_contract, goal_progress, goal_project,
        )
    root_files: set[str] = set()
    queue: list[tuple[str, int]] = [(root, 0)]
    files: list[str] = []
    seen_dirs: set[str] = set()
    while queue and len(seen_dirs) < _MAX_DIRECTORIES and len(files) < _MAX_FILES:
        directory, depth = queue.pop(0)
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        data = listing if directory == root else await call("list_dir", {"path": directory})
        if data is None or stopped():
            return {}
        entries = data.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries[:500]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                continue
            path = str(PurePosixPath(directory) / name)
            if entry.get("is_dir") is True:
                if depth < _MAX_DEPTH and name not in _IGNORED_DIRS and not name.startswith("."):
                    queue.append((path, depth + 1))
            else:
                files.append(path)
                if directory == root:
                    root_files.add(name.lower())
                if len(files) >= _MAX_FILES:
                    break

    py_files = [path for path in files if PurePosixPath(path).suffix.lower() in _SOURCE_SUFFIXES]
    if not py_files:
        return {}
    py_files.sort(key=lambda path: (
        0 if PurePosixPath(path).name.lower() in {"main.py", "app.py", "__main__.py"} else 1,
        path,
    ))

    # A source readback is required even if the worker reported success. Pick the
    # first bounded source file with meaningful content from the actual host.
    source_texts: dict[str, str] = {}
    read_source = False
    for path in py_files[:16]:
        data = await call("read_file", {"path": path, "max_bytes": 32768})
        if data is None or stopped():
            return {}
        text = data.get("text")
        if (type(data.get("size")) is int and data["size"] >= 256
                and data.get("truncated") is False and isinstance(text, str) and text.strip()):
            source_texts[path] = text[:32768]
            read_source = True
    if not read_source:
        return {}

    venv_python = "./.venv/bin/python"
    env = await call("run_command", {
        "argv": ["python3", "-m", "venv", ".venv"], "cwd": root,
        "timeout_s": 60, "background": False,
    })
    if env is None or stopped():
        return {}

    if "requirements.txt" in root_files:
        dependency_argv = [venv_python, "-m", "pip", "install", "-r", "requirements.txt", "pytest"]
    elif root_files.intersection({"pyproject.toml", "setup.py", "setup.cfg"}):
        dependency_argv = [venv_python, "-m", "pip", "install", "-e", ".", "pytest"]
    else:
        dependency_argv = [venv_python, "-m", "pip", "install", "pytest"]
    dependencies = await call("run_command", {
        "argv": dependency_argv, "cwd": root, "timeout_s": 300, "background": False,
    })
    if dependencies is None or stopped():
        return {}

    has_tests = any(
        PurePosixPath(path).name.startswith("test_") or PurePosixPath(path).parent.name in {"test", "tests"}
        for path in py_files
    )
    if not has_tests:
        return {}
    tests = await call("run_command", {
        "argv": [venv_python, "-m", "pytest", "-q"], "cwd": root,
        "timeout_s": 180, "background": False,
    })
    if tests is None or stopped():
        return {}

    startup = _startup_argv(root, py_files, source_texts)
    if startup is None:
        return {}
    result = await call("run_command", {
        "argv": startup, "cwd": root, "timeout_s": 20, "background": False,
    })
    if result is None or stopped():
        return {}

    return select_goal_proof_references(
        goal_contract, goal_progress.private_observations(), goal_project,
    )


def _root_granted(root: str, project: dict[str, Any]) -> bool:
    if (type(project.get("project_id")) is not str or not project["project_id"]
            or type(project.get("computer_id")) is not str or not project["computer_id"]
            or type(project.get("computer_name")) is not str or not project["computer_name"]):
        return False
    rows = project.get("workspace_roots")
    if type(rows) is not list:
        return False
    return any(
        type(row) is dict and isinstance(row.get("path"), str)
        and _canonical_root(row["path"]) == row["path"]
        and (root == row["path"] or row["path"] == "/" or root.startswith(row["path"].rstrip("/") + "/"))
        for row in rows
    )


async def _verify_javascript_project(root, listing, call, stopped, goal_contract, goal_progress, goal_project):
    """Install, test, build and serve a root JavaScript project through Mac tools."""
    entries = listing.get("entries", [])
    root_files = {entry["name"].lower() for entry in entries
                  if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                  and entry.get("is_dir") is not True}
    package_path = str(PurePosixPath(root) / "package.json")
    package_data = await call("read_file", {"path": package_path, "max_bytes": 32768})
    if package_data is None or stopped():
        return {}
    package_text = package_data.get("text")
    if not isinstance(package_text, str) or package_data.get("truncated") is True:
        return {}
    try:
        package = json.loads(package_text)
    except (ValueError, TypeError):
        return {}
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict):
        return {}

    sources = []
    queue = [(root, 0)]
    seen = set()
    while queue and len(seen) < _MAX_DIRECTORIES and len(sources) < _MAX_FILES:
        directory, depth = queue.pop(0)
        if directory in seen:
            continue
        seen.add(directory)
        data = listing if directory == root else await call("list_dir", {"path": directory})
        if data is None or stopped():
            return {}
        for entry in data.get("entries", [])[:500]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                continue
            path = str(PurePosixPath(directory) / name)
            if entry.get("is_dir") is True:
                if depth < _MAX_DEPTH and name not in _IGNORED_DIRS and not name.startswith("."):
                    queue.append((path, depth + 1))
            elif PurePosixPath(name).suffix.lower() in _JS_SOURCE_SUFFIXES:
                sources.append(path)
                if len(sources) >= _MAX_FILES:
                    break
    if not sources:
        return {}
    readable = False
    for path in sorted(sources)[:16]:
        data = await call("read_file", {"path": path, "max_bytes": 32768})
        if data is None or stopped():
            return {}
        if (type(data.get("size")) is int and data["size"] >= 128
                and data.get("truncated") is False and isinstance(data.get("text"), str)
                and data["text"].strip()):
            readable = True
    if not readable:
        return {}

    package_manager = _javascript_package_manager(root_files)
    if package_manager is None or not isinstance(scripts.get("test"), str) or not scripts["test"].strip():
        return {}
    manager, install = package_manager
    if "node_modules" not in root_files:
        result = await call("run_command", {
            "argv": install, "cwd": root, "timeout_s": 300, "background": False,
        })
        if result is None or stopped():
            return {}
    for script in ("test", "build"):
        if script == "build" and (not isinstance(scripts.get(script), str) or not scripts[script].strip()):
            return {}
        argv = _javascript_script_argv(manager, script)
        result = await call("run_command", {
            "argv": argv, "cwd": root, "timeout_s": 240, "background": False,
            "env": {"CI": "true"} if script == "test" else {},
        })
        if result is None or stopped():
            return {}

    startup = _javascript_http_startup_argv(manager, scripts)
    if startup is None:
        return {}
    result = await call("run_command", {
        "argv": startup, "cwd": root, "timeout_s": 30, "background": False,
    })
    if result is None or stopped():
        return {}
    return select_goal_proof_references(
        goal_contract, goal_progress.private_observations(), goal_project,
    )


def _javascript_package_manager(root_files: set[str]):
    if "package-lock.json" in root_files:
        return "npm", ["npm", "ci", "--no-audit", "--no-fund"]
    if "npm-shrinkwrap.json" in root_files:
        return "npm", ["npm", "ci", "--no-audit", "--no-fund"]
    if "pnpm-lock.yaml" in root_files:
        return "pnpm", ["pnpm", "install", "--frozen-lockfile"]
    if "yarn.lock" in root_files:
        return "yarn", ["yarn", "install", "--immutable"]
    if "bun.lock" in root_files or "bun.lockb" in root_files:
        return "bun", ["bun", "install", "--frozen-lockfile"]
    return "npm", ["npm", "install", "--no-audit", "--no-fund"]


def _javascript_script_argv(manager: str, script: str) -> list[str]:
    return {"npm": ["npm", "run", script],
            "pnpm": ["pnpm", "run", script], "yarn": ["yarn", script],
            "bun": ["bun", "run", script]}[manager]


def _javascript_http_startup_argv(manager: str, scripts: dict[str, Any]) -> list[str] | None:
    script = "preview" if isinstance(scripts.get("preview"), str) and scripts["preview"].strip() else (
        "start" if isinstance(scripts.get("start"), str) and scripts["start"].strip() else None
    )
    if script is None:
        return None
    launch = {"npm": ["npm", "run", script, "--", "--host", "127.0.0.1", "--port", "4173"],
              "pnpm": ["pnpm", "run", script, "--host", "127.0.0.1", "--port", "4173"],
              "yarn": ["yarn", script, "--host", "127.0.0.1", "--port", "4173"],
              "bun": ["bun", "run", script, "--host", "127.0.0.1", "--port", "4173"]}[manager]
    # Keep the child lifetime bounded and require an HTTP response before success.
    node = ("const {spawn}=require('node:child_process');const p=spawn(" + json.dumps(launch[0]) + "," +
            json.dumps(launch[1:]) + ",{stdio:'ignore',detached:true});let done=false;" +
            "const stop=()=>{if(!p.pid)return;try{process.kill(-p.pid,'SIGTERM')}catch{p.kill('SIGTERM')}};" +
            "const end=(c)=>{if(done)return;done=true;stop();process.exit(c)};" +
            "p.on('error',()=>end(1));p.on('exit',c=>{if(!done)end(c||1)});" +
            "process.on('SIGTERM',()=>end(1));process.on('SIGINT',()=>end(1));" +
            "const t=Date.now();(async()=>{while(Date.now()-t<20000){try{const r=await fetch('http://127.0.0.1:4173/');" +
            "if(r.status>=200&&r.status<400)return end(0)}catch{}await new Promise(r=>setTimeout(r,250))}end(1)})();")
    return ["node", "-e", node]


def _canonical_root(value: Any) -> str | None:
    if type(value) is not str or not value.startswith("/") or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if ".." in path.parts:
        return None
    normalized = str(path)
    return normalized if normalized == value.rstrip("/") or value == "/" else None


def _startup_argv(root: str, py_files: list[str], source_texts: dict[str, str]) -> list[str] | None:
    # A package entrypoint can delegate its CLI to another source module.
    source_bodies = [
        text for path, text in source_texts.items()
        if PurePosixPath(path).name.lower() not in {"test.py"}
        and not PurePosixPath(path).name.lower().startswith("test_")
    ]
    smoke_flag = next(
        ([flag] for flag in ("--smoke-test", "--smoke")
         if any(flag in text for text in source_bodies)), []
    )
    candidates = [path for path in py_files if PurePosixPath(path).name.lower() in {"main.py", "app.py"}]
    for path in candidates:
        if smoke_flag:
            return ["./.venv/bin/python", str(PurePosixPath(path).relative_to(root)), *smoke_flag]
    if candidates:
        return ["./.venv/bin/python", str(PurePosixPath(candidates[0]).relative_to(root))]
    main_modules = [path for path in py_files if PurePosixPath(path).name == "__main__.py"]
    if main_modules:
        module = ".".join(PurePosixPath(main_modules[0]).parent.relative_to(root).parts)
        if module and all(part.isidentifier() for part in module.split(".")):
            return ["./.venv/bin/python", "-m", module, *smoke_flag]
    return None
