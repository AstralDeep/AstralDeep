"""Binds preview host effects to an exact, ordered child plan for the active project session."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

_ACTIVE = contextvars.ContextVar("preview_host_effect", default=None)


@dataclass
class PreviewAuthority:
    session: object
    generation: int | None
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

    def matches(self, session, verb, arguments):
        if (not self.active or session is None or self.session is not session
                or session.authority_generation != self.generation
                or type(arguments) is not dict):
            return False
        if (self.request.get("action") == "start" and self.job and 2 <= self.step <= 5
                and verb == "interrupt_command" and arguments == {"job_id": self.job, "signal": "INT"}):
            return True
        return self.expected() == (verb, arguments)


    def record(self, verb, result):
        if self.step == 1 and verb == "run_command" and isinstance(result, dict):
            job = result.get("job_id")
            self.job = job.lower() if isinstance(job, str) else None
        self.step = 6 if verb == "interrupt_command" else self.step + 1


@contextmanager
def preview_authority_scope(request, session):
    plan = PreviewAuthority(session, getattr(session, "authority_generation", None),
                            {key: request.get(key) for key in ("action", "cwd", "package_manager", "script", "job_id")})
    token = _ACTIVE.set(plan)
    try:
        yield
    finally:
        plan.active = False
        _ACTIVE.reset(token)


def preview_request_current(session, request):
    plan = _ACTIVE.get()
    return (isinstance(plan, PreviewAuthority) and plan.active and session is not None and plan.session is session
            and session.authority_generation == plan.generation
            and all(request.get(key) == value for key, value in plan.request.items()))


def current_preview():
    return _ACTIVE.get()
