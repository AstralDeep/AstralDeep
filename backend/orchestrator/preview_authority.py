"""Binds preview host effects to an exact, ordered child plan under the original approval."""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass

_ACTIVE = contextvars.ContextVar("preview_host_effect", default=None)


@dataclass
class PreviewAuthority:
    approval: object
    session: object
    request: dict
    step: int = 0
    job: str | None = None
    active: bool = True

    def expected(self):
        from agents.computer_use.mcp_tools import _preview_argv, _preview_probe_script

        action = self.request.get("action")
        cwd = self.request.get("cwd")
        if action == "status" and self.step == 0:
            return "read_output", self.output(self.request.get("job_id"), 0)
        if action == "stop" and self.step == 0:
            return "interrupt_command", {"job_id": self.request.get("job_id"), "signal": "INT"}
        if action != "start":
            return None
        manager, script = self.request.get("package_manager"), self.request.get("script")
        if manager not in {"npm", "pnpm", "yarn", "bun"} or script not in {"dev", "preview", "start"}:
            return None
        if self.step == 0:
            return "read_file", {"path": cwd.rstrip("/") + "/package.json", "max_bytes": 32768}
        if self.step == 1:
            return "run_command", {"argv": _preview_argv(manager, script), "cwd": cwd,
                                   "timeout_s": 3600, "background": True}
        if self.step == 2 and self.job:
            return "read_output", self.output(self.job, 2)
        if self.step == 3 and self.job:
            return "run_command", {"argv": ["node", "-e", _preview_probe_script()], "cwd": cwd,
                                   "timeout_s": 20, "background": False}
        if self.step == 4 and self.job:
            return "read_output", self.output(self.job, 0)
        return None

    @staticmethod
    def output(job, wait):
        return {"job_id": job, "stream": "both", "offset": 0, "max_bytes": 4096, "wait_s": wait}

    def matches(self, approval, session, verb, arguments):
        if (not self.active or self.approval is not approval or self.session is not session
                or type(arguments) is not dict):
            return False
        if (self.request.get("action") == "start" and self.job and 2 <= self.step <= 5
                and verb == "interrupt_command" and arguments == {"job_id": self.job, "signal": "INT"}):
            return True
        return self.expected() == (verb, arguments)


@contextmanager
def preview_authority_scope(approval, session):
    request = json.loads(approval.call.arguments) if approval is not None else None
    plan = None
    if (isinstance(request, dict) and approval.call.tool == "project_preview"
            and isinstance(request.get("cwd"), str) and request["cwd"].startswith("/")):
        plan = PreviewAuthority(approval, session, request)
    token = _ACTIVE.set(plan)
    try:
        yield
    finally:
        if plan is not None:
            plan.active = False
        _ACTIVE.reset(token)


def preview_effect_matches(approval, session, verb, arguments):
    plan = _ACTIVE.get()
    return isinstance(plan, PreviewAuthority) and plan.matches(approval, session, verb, arguments)


def record_preview_effect(approval, session, verb, arguments, result):
    plan = _ACTIVE.get()
    if not isinstance(plan, PreviewAuthority) or not plan.matches(approval, session, verb, arguments):
        return
    if plan.step == 1 and verb == "run_command" and isinstance(result, dict):
        job = result.get("job_id")
        plan.job = job.lower() if isinstance(job, str) else None
    plan.step = 6 if verb == "interrupt_command" else plan.step + 1
