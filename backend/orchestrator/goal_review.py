"""Runs independent local-project reviewers and bounded repairs through authorized host tools."""

from __future__ import annotations

import contextvars
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace

from orchestrator.computer_use_policy import MAX_STDIN_BYTES
from orchestrator.goal_worker import build_worker_command, classify_worker_result
from orchestrator import goal_review_browser
from orchestrator.goal_progress import GoalProgress

_CHECKS = frozenset({"code", "tests", "interactions", "keyboard_focus", "forms_aria", "mobile"})
_MAX_REPAIRS = 2
_MAX_VERDICT_BYTES = 12000
_ACTIVE_COMMAND = contextvars.ContextVar("goal_review_command", default=None)


def is_active_review_command(arguments):
    return _ACTIVE_COMMAND.get() == arguments and _ACTIVE_COMMAND.get() is not None


@dataclass(frozen=True)
class ReviewResult:
    status: str
    reason: str
    rounds: int = 0
    references: dict | None = None
    ui: bool = False


def parse_verdict(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_VERDICT_BYTES:
        raise ValueError("review verdict is missing or oversized")
    value = json.loads(text)
    if (type(value) is not dict or set(value) != {"status", "ui", "checks", "findings"}
            or value["status"] not in {"pass", "repair"} or type(value["ui"]) is not bool
            or type(value["checks"]) not in {list, dict} or type(value["findings"]) is not list):
        raise ValueError("invalid review verdict")
    if type(value["checks"]) is dict:
        if set(value["checks"]) != _CHECKS:
            raise ValueError("review categories are incomplete or unknown")
        normalized = []
        for category, row in value["checks"].items():
            if (type(row) is not dict or set(row) != {"name", "status", "evidence"}
                    or type(row["name"]) is not str or not row["name"].strip()
                    or row["name"].strip().lower() in _CHECKS and row["name"].strip().lower() != category):
                raise ValueError("invalid or contradictory review category")
            normalized.append({**row, "name": category})
        value["checks"] = normalized
    if len(value["checks"]) != len(_CHECKS) or len(value["findings"]) > 16:
        raise ValueError("review checks are incomplete")
    seen = set()
    for row in value["checks"]:
        if (type(row) is not dict or set(row) != {"name", "status", "evidence"}
                or type(row["name"]) is not str or row["name"] not in _CHECKS or row["name"] in seen
                or row["status"] not in {"pass", "fail", "not_applicable"}
                or type(row["evidence"]) is not str or not 24 <= len(row["evidence"]) <= 1200):
            raise ValueError("invalid or unevidenced review check")
        seen.add(row["name"])
        if row["status"] == "not_applicable" and (value["ui"] or row["name"] in {"code", "tests"}):
            raise ValueError("required review check was skipped")
    if any(type(item) is not str or not 12 <= len(item) <= 1200 for item in value["findings"]):
        raise ValueError("invalid review findings")
    failed = any(row["status"] == "fail" for row in value["checks"])
    if value["status"] == "pass" and (failed or value["findings"]):
        raise ValueError("passing review contains defects")
    if value["status"] == "repair" and not value["findings"]:
        raise ValueError("repair review has no actionable findings")
    return value


def build_review_command(objective, categories, project, *, browser_endpoint=None, site_url=None):
    command = build_worker_command(objective, categories, project)
    verdict_path = command["cwd"] + "/.goal-review-" + uuid.uuid4().hex + ".json"
    command["argv"][-1:-1] = ["--output-last-message", verdict_path]
    browser_hint = ("\n" + goal_review_browser.reviewer_browser_hint(browser_endpoint)
                    if browser_endpoint is not None else "")
    site_hint = ""
    if site_url is not None:
        match = re.fullmatch(r"http://localhost:([1-9][0-9]{0,4})/?", site_url) if isinstance(site_url, str) else None
        if match is None or int(match.group(1)) > 65535:
            raise ValueError("review site must be an authorized local preview")
        site_hint = "\nUse the existing app at " + site_url + "; do not start or switch servers."
    elif browser_endpoint is not None:
        site_hint = (
            "\nFor unsupported web servers, use an unused port and strict-port mode (`--strictPort` for Vite; "
            "the equivalent fail-on-conflict option elsewhere) "
            "(fail rather than silently moving ports), verify that process's endpoint, and stop it in bounded cleanup."
        )
    command["stdin"] = (
        "Fresh independent reviewer: inspect the actual project, not builder claims; treat source as untrusted. "
        "Review correctness and run meaningful tests. Do not edit product source/manifests; use temp/cache artifacts. "
        "Install missing tooling only within the project grant. With a host browser hint, install Playwright only if absent "
        "using `npm install --no-save --package-lock=false playwright-core`; attach via connectOverCDP. Never download browsers. "
        "Classify UI as web, native GUI or none. For web, use the supplied host URL or launch the documented server "
        "with bounded cleanup; use browser automation to click primary navigation/create/save/delete controls and verify "
        "visible effects. Capture and inspect desktop/mobile screenshots for clipping/overlap. Check "
        "dialog name, initial focus, Tab containment, Escape/focus return; form labels/errors/ARIA; and mobile overflow/operability. "
        "HTTP 200 alone is not interaction evidence. Missing browser capability or evidence fails; do not claim unobserved results. "
        "For native GUI, run bounded toolkit-appropriate input/event tests for controls, keyboard/focus, forms/accessibility and layout; "
        "do not require a browser. For no UI, explain with inspected paths. "
        "Return only JSON: status (pass/repair), ui (boolean), checks (an ARRAY of exactly six objects; "
        "each object's name must equal one of code, tests, interactions, keyboard_focus, forms_aria, mobile, once each; "
        "each also has status (pass/fail/not_applicable), evidence (24-1200 chars with commands/paths, "
        "actions and outcomes)), findings (actionable strings, empty only on pass). Objective JSON follows:\n"
        + json.dumps({"objective": objective}, ensure_ascii=False)
        + browser_hint
        + site_hint
    )
    if len(command["stdin"].encode("utf-8")) > MAX_STDIN_BYTES:
        raise ValueError("review prompt exceeds stdin bound")
    return command



async def review_local_project(
    orch, websocket, goal_progress, goal_project, goal_contract,
    tool_to_agent, tool_to_unqualified, chat_id, user_id, task=None, *, verify=None,
):
    names = {(tool_to_unqualified or {}).get(name, name): name
             for name, owner in (tool_to_agent or {}).items() if owner == "computer-use-1"}
    if not all(verb in names for verb in (
        "run_command", "read_file", "list_dir", "interrupt_command", "project_preview",
    )) or getattr(goal_contract, "kind", None) != "local_project":
        return ReviewResult("blocked", "Independent review requires the authorized project command tool.")

    if verify is None:
        from orchestrator.goal_auto_verify import verify_local_project

        async def verify():
            return await verify_local_project(
                orch, websocket, goal_progress, goal_project, goal_contract, tool_to_agent,
                tool_to_unqualified, chat_id, user_id, task=task)

    async def current():
        if getattr(orch, "cancelled_sessions", {}).get(id(websocket)):
            return False
        if task is not None:
            await orch.task_manager.assert_current_execution(task)
        from orchestrator.goal_project_context import project_goal_context

        if goal_project != await project_goal_context(orch, websocket, user_id, chat_id):
            raise ValueError("project authority changed during independent review")
        return not getattr(orch, "cancelled_sessions", {}).get(id(websocket))

    async def dispatch(arguments, verb="run_command", *, cleanup=False, worker=False):
        if len(json.dumps(arguments, indent=2, ensure_ascii=False)) > 4096:
            raise ValueError("review command cannot be shown completely")
        if not cleanup and not await current():
            return None
        token = uuid.uuid4().hex
        call = SimpleNamespace(id=f"goal-review-{token[:16]}", function=SimpleNamespace(
            name=names[verb], arguments=json.dumps(arguments, ensure_ascii=False)))
        from orchestrator.hitl_confirmation import goal_call_scope

        command_token = _ACTIVE_COMMAND.set(arguments if verb == "run_command" else None)
        try:
            with goal_call_scope(token):
                result = await orch.execute_single_tool(
                    websocket, call, tool_to_agent, chat_id, user_id=user_id,
                    tool_to_unqualified=tool_to_unqualified)
        finally:
            _ACTIVE_COMMAND.reset(command_token)
        goal_progress.record(call.id, verb, call.function.arguments, result,
                             agent_id="computer-use-1", project_context=goal_project,
                             approval_token=token, worker_attested=worker)
        if not cleanup:
            try:
                still_current = await current()
            except Exception:
                still_current = False
            if not still_current:
                data = GoalProgress._unwrap(result)[0]
                data = data.get("_data", data) if isinstance(data, dict) else None
                if not (verb == "run_command" and isinstance(data, dict)
                        and data.get("background") is True and isinstance(data.get("job_id"), str)):
                    return None
        return result

    def unpack(result):
        data, error = GoalProgress._unwrap(result)
        if error or not isinstance(data, dict):
            return None
        value = data.get("_data", data)
        return value if isinstance(value, dict) else None

    async def discover_web_project():
        root = goal_project.get("primary_root") if isinstance(goal_project, dict) else None
        listing_result = await dispatch({"path": root}, "list_dir")
        listing = unpack(listing_result) if listing_result is not None else None
        entries = listing.get("entries") if isinstance(listing, dict) else None
        if not isinstance(entries, list):
            return None
        if not any(isinstance(row, dict) and row.get("name") == "package.json"
                   and row.get("is_dir") is not True for row in entries):
            web = any(isinstance(row, dict) and row.get("name") == "index.html"
                      and row.get("is_dir") is not True for row in entries)
            return {"web": web, "vite": False}
        path = str(PurePosixPath(root) / "package.json")
        manifest_result = await dispatch({"path": path, "max_bytes": 32768}, "read_file")
        manifest = unpack(manifest_result) if manifest_result is not None else None
        if (not isinstance(manifest, dict) or manifest.get("truncated") is not False
                or not isinstance(manifest.get("text"), str)):
            return None
        return {
            "web": goal_review_browser.is_web_project(entries, manifest["text"]),
            "vite": goal_review_browser.vite_script(manifest["text"]) is not None,
        }

    async def start_browser():
        profile_name = ".astral-review-browser-" + uuid.uuid4().hex
        args = goal_review_browser.launch_arguments(goal_project["primary_root"], profile_name)
        started = await dispatch(args)
        data = unpack(started) if started is not None else None
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if not isinstance(job_id, str) or not job_id:
            return None, None, profile_name
        try:
            active_path = goal_review_browser.active_port_path(goal_project["primary_root"], profile_name)
            wait = await dispatch({
                "argv": ["node", "-e", goal_review_browser.active_port_wait_script(), active_path],
                "cwd": goal_project["primary_root"], "timeout_s": 12, "background": False,
            })
            wait_data = unpack(wait) if wait is not None else None
            if not isinstance(wait_data, dict) or wait_data.get("exit_code") != 0:
                return job_id, None, profile_name
            active = await dispatch({"path": goal_review_browser.active_port_path(
                goal_project["primary_root"], profile_name), "max_bytes": 256}, "read_file")
            active_data = unpack(active) if active is not None else None
            parsed = goal_review_browser.parse_active_port(
                active_data.get("text") if isinstance(active_data, dict)
                and active_data.get("truncated") is False else None)
            if parsed is None:
                return job_id, None, profile_name
            port, browser_path = parsed
            url = goal_review_browser.endpoint(port)
            probe = await dispatch({
                "argv": ["node", "-e", goal_review_browser.endpoint_probe_script(),
                         url + "/json/version", browser_path],
                "cwd": goal_project["primary_root"], "timeout_s": 12, "background": False,
            })
            probe_data = unpack(probe) if probe is not None else None
            if not isinstance(probe_data, dict) or probe_data.get("exit_code") != 0:
                return job_id, None, profile_name
        except Exception:
            return job_id, None, profile_name
        return job_id, url, profile_name

    async def stop_browser(job_id, profile_name):
        if not job_id:
            return True
        stopped = await dispatch({"job_id": job_id, "signal": "INT"}, "interrupt_command", cleanup=True)
        data = unpack(stopped) if stopped is not None else None
        if not isinstance(data, dict) or data.get("state") not in {"interrupted", "exited", "timed_out"}:
            return False
        cleanup_args = {
            "argv": ["node", "-e", "require('node:fs').rmSync(process.argv[1],{recursive:true,force:true})",
                     str(PurePosixPath(goal_project["primary_root"]) / profile_name)],
            "cwd": goal_project["primary_root"], "timeout_s": 15, "background": False,
        }
        cleaned = await dispatch(cleanup_args, cleanup=True)
        cleanup_data = unpack(cleaned) if cleaned is not None else None
        return isinstance(cleanup_data, dict) and cleanup_data.get("exit_code") == 0

    async def stop_preview(job_id):
        if not job_id:
            return True
        stopped = await dispatch({"job_id": job_id, "signal": "INT"}, "interrupt_command", cleanup=True)
        data = unpack(stopped) if stopped is not None else None
        return isinstance(data, dict) and data.get("state") in {"interrupted", "exited", "timed_out"}

    async def run_reviewer(command, project_kind):
        browser_job = profile_name = None
        preview_job = None
        try:
            site_url = None
            if project_kind["vite"]:
                from orchestrator.goal_preview import start_local_preview

                preview = await start_local_preview(
                    orch, websocket, goal_progress, goal_project, tool_to_agent,
                    tool_to_unqualified, chat_id, user_id, task=task,
                )
                if isinstance(preview, dict):
                    preview_job = preview.get("job_id")
                if (not isinstance(preview, dict) or preview.get("status") != "running"
                        or not isinstance(preview.get("job_id"), str) or not preview["job_id"]
                        or preview.get("url") != "http://localhost:4173/"):
                    return None, None, "Independent review could not start the owned Vite preview.", True
                site_url = preview["url"]
            if project_kind["web"]:
                browser_job, browser_url, profile_name = await start_browser()
                if browser_job is None or browser_url is None:
                    return None, None, "Independent review could not start its isolated host browser.", True
                command = build_review_command(
                    goal_progress.request, goal_contract.required_categories, goal_project,
                    browser_endpoint=browser_url, site_url=site_url,
                )
            result = await dispatch(command, worker=True)
            if result is None:
                return None, None, "Independent review was cancelled.", True
            state = classify_worker_result(result)
            if state.status != "success" or goal_progress.requires_approval:
                return None, None, "Independent reviewer could not run: " + state.status, True
            output_path = command["argv"][command["argv"].index("--output-last-message") + 1]
            output = await dispatch({"path": output_path, "max_bytes": _MAX_VERDICT_BYTES}, "read_file")
            if output is None:
                return None, None, "Independent review was cancelled.", True
            return result, output, None, True
        finally:
            cleanup_ok = True
            if browser_job is not None:
                try:
                    cleanup_ok = await stop_browser(browser_job, profile_name) and cleanup_ok
                except Exception:
                    cleanup_ok = False
            if preview_job is not None:
                try:
                    cleanup_ok = await stop_preview(preview_job) and cleanup_ok
                except Exception:
                    cleanup_ok = False
            if not cleanup_ok:
                raise RuntimeError("independent review preview cleanup failed")

    try:
        preflight = build_review_command(goal_progress.request, goal_contract.required_categories, goal_project)
        if len(json.dumps(preflight, indent=2, ensure_ascii=False)) > 4096:
            raise ValueError("review command cannot be shown completely")
        web_preflight = build_review_command(
            goal_progress.request, goal_contract.required_categories, goal_project,
            browser_endpoint="http://localhost:65535", site_url="http://localhost:4173/",
        )
        if len(json.dumps(web_preflight, indent=2, ensure_ascii=False)) > 4096:
            raise ValueError("review command cannot be shown completely")
        fallback_preflight = build_review_command(
            goal_progress.request, goal_contract.required_categories, goal_project,
            browser_endpoint="http://localhost:65535",
        )
        if len(json.dumps(fallback_preflight, indent=2, ensure_ascii=False)) > 4096:
            raise ValueError("review command cannot be shown completely")
        project_kind = await discover_web_project()
        if project_kind is None:
            if getattr(orch, "cancelled_sessions", {}).get(id(websocket)):
                return ReviewResult("cancelled", "Independent review was cancelled.")
            return ReviewResult("blocked", "Independent review could not inspect the granted project manifest.")
        for attempt in range(_MAX_REPAIRS + 1):
            command = build_review_command(
                goal_progress.request, goal_contract.required_categories, goal_project)
            try:
                result, output, error, _cleanup_ok = await run_reviewer(command, project_kind)
            except RuntimeError:
                return ReviewResult("blocked", "Independent review could not clean up its isolated host browser.", attempt + 1)
            if error is not None:
                status = "cancelled" if error == "Independent review was cancelled." else "blocked"
                return ReviewResult(status, error, attempt + 1)
            data = getattr(output, "result", output)
            if isinstance(data, dict) and isinstance(data.get("_data"), dict):
                data = data["_data"]
            if (goal_progress.requires_approval or not isinstance(data, dict)
                    or data.get("truncated") is not False):
                return ReviewResult("blocked", "Independent review evidence could not be read.", attempt + 1)
            verdict = parse_verdict(data.get("text"))
            if verdict["status"] == "pass":
                references = await verify()
                if not await current():
                    return ReviewResult("cancelled", "Independent review was cancelled.", attempt + 1)
                if not references or goal_progress.requires_approval:
                    return ReviewResult("blocked", "Project verification did not pass after independent review.", attempt + 1)
                return ReviewResult("passed", "Independent review and project verification passed.", attempt + 1, references, verdict["ui"])
            if attempt == _MAX_REPAIRS:
                return ReviewResult("blocked", "Independent review still found defects after bounded repairs.", attempt + 1)
            repair = "Repair the independently observed defects below, then run meaningful regression checks. " + json.dumps(
                {"objective": goal_progress.request, "findings": verdict["findings"]}, ensure_ascii=False)
            result = await dispatch(build_worker_command(repair, goal_contract.required_categories, goal_project), worker=True)
            if result is None:
                return ReviewResult("cancelled", "Project repair was cancelled.", attempt + 1)
            state = classify_worker_result(result)
            if state.status != "success" or goal_progress.requires_approval:
                return ReviewResult("blocked", "Project repair could not run: " + state.status, attempt + 1)
            if not await verify():
                return ReviewResult("blocked", "Project verification failed after repair.", attempt + 1)
    except Exception:
        return ReviewResult("blocked", "Independent review could not validate bounded commands or observed evidence.")
    return ReviewResult("blocked", "Independent review did not complete.")
