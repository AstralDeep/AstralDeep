"""Feature 077 — the express lane for personal agents ("describe it and it exists").

One description → the SAME guided session the step editor drives
(`agent_authoring`: Specify → Clarify → Plan → Tasks → Analyze → Generate),
run as a background pipeline that re-renders the *My agents & skills* surface
after every step for the socket that started it. Nothing here bypasses a gate:

* Clarify is still the hard gate — when the assistant has questions the run
  stops in ``needs_answers`` and resumes only through :func:`agent_authoring.advance`
  with the owner's answers (FR-002).
* Analyze is still the deterministic checker — a refusal ends the run as
  ``failed`` with the violations and hands the owner the step editor (FR-003).
* Generate goes through :func:`agent_authoring.generate_from_session`, which
  re-evaluates the generation gate on the server (FR-001).

Run state is in-process and bounded (FR-004); the session rows are the durable
truth, so a restart loses only the in-flight run — the step editor can resume
any session from where it stands.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from orchestrator import agent_authoring as aa

logger = logging.getLogger("Orchestrator.QuickCreate")

#: Pipeline steps in order. ``deliver`` is the delivery half of Generate,
#: shown separately because it is the step a missing desktop client fails.
STEPS: Tuple[str, ...] = ("specify", "clarify", "plan", "tasks", "analyze", "generate", "deliver")
STEP_LABELS = {
    "specify": "Write the specification",
    "clarify": "Check for open questions",
    "plan": "Plan the tools",
    "tasks": "Break down the build",
    "analyze": "Check the agent rules",
    "generate": "Generate the code",
    "deliver": "Send to your desktop",
}

#: Run states.
RUNNING, NEEDS_ANSWERS, DONE, WAITING_FOR_DESKTOP, FAILED = (
    "running", "needs_answers", "done", "waiting_for_desktop", "failed")

MAX_RUNS_PER_OWNER = 5
MAX_LOG_LINES = 20
RUN_TTL_S = 6 * 3600

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "that", "which", "with",
    "my", "me", "i", "it", "its", "in", "on", "at", "by", "from", "as", "is",
    "are", "be", "will", "should", "can", "could", "would", "please", "agent",
    "want", "need", "like", "make", "build", "create", "something", "this",
    "into", "about", "when", "then", "so", "every", "each", "all", "any",
})


@dataclass
class QuickRun:
    owner: str
    draft_id: str
    agent_name: str
    state: str = RUNNING
    current: str = "specify"
    steps: Dict[str, str] = field(default_factory=lambda: {s: "pending" for s in STEPS})
    message: str = ""
    outcome: Dict[str, Any] = field(default_factory=dict)
    questions: List[Dict[str, str]] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    agent_id: str = ""
    task: Optional[asyncio.Task] = None
    socket_id: int = 0

    def note(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-MAX_LOG_LINES]
        self.updated_at = time.time()

    def public(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id, "agent_name": self.agent_name, "state": self.state,
            "current": self.current, "steps": dict(self.steps), "message": self.message,
            "outcome": dict(self.outcome), "questions": list(self.questions),
            "log": list(self.log), "agent_id": self.agent_id,
        }


_RUNS: Dict[Tuple[str, str], QuickRun] = {}


def _sweep() -> None:
    now = time.time()
    for key, run in list(_RUNS.items()):
        if run.state in (DONE, FAILED, WAITING_FOR_DESKTOP) and now - run.updated_at > RUN_TTL_S:
            _RUNS.pop(key, None)


def get_run(owner: str, draft_id: str) -> Optional[QuickRun]:
    return _RUNS.get((owner, draft_id))


def runs_for(owner: str) -> List[QuickRun]:
    _sweep()
    return sorted((r for (o, _), r in _RUNS.items() if o == owner),
                  key=lambda r: r.started_at, reverse=True)


def forget(owner: str, draft_id: str) -> None:
    run = _RUNS.pop((owner, draft_id), None)
    if run is not None and run.task is not None and not run.task.done():
        run.task.cancel()


def derive_agent_name(description: str) -> str:
    """A readable default name from the description — the owner can rename it
    later through Revise. Never empty."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", description or "")
             if w.lower() not in _STOPWORDS]
    picked = words[:4]
    name = " ".join(w if w.isupper() else w.capitalize() for w in picked).strip()
    if len(name) < 2:
        name = "My agent"
    return name[:60]


# ---------------------------------------------------------------------------
# Starting / resuming
# ---------------------------------------------------------------------------

async def start(orch, websocket, user_id: str, roles: Any, *, description: str,
                agent_name: str = "", refresh=None) -> Tuple[Optional[QuickRun], str]:
    """Open a session for ``description`` and launch the pipeline. Returns
    ``(run, message)``; ``run`` is None when the request was refused."""
    if not aa.byo_enabled():
        return None, "Personal agents are not enabled on this deployment."
    description = (description or "").strip()
    if len(description) < 10:
        return None, "Describe what the agent should do in at least a sentence (10+ characters)."
    active = [r for r in runs_for(user_id) if r.state in (RUNNING, NEEDS_ANSWERS)]
    if len(active) >= MAX_RUNS_PER_OWNER:
        return None, (f"{len(active)} agents are already being created — finish or "
                      "cancel one first.")
    name = (agent_name or "").strip() or derive_agent_name(description)
    session = await aa.start_session(orch, user_id=user_id, agent_name=name,
                                     description=description)
    run = QuickRun(owner=user_id, draft_id=str(session["id"]), agent_name=name,
                   socket_id=id(websocket))
    run.note(f"Started for “{name}”.")
    _RUNS[(user_id, run.draft_id)] = run
    _launch(orch, websocket, user_id, roles, run, refresh)
    return run, f"Creating “{name}” — you can watch each step below."


async def resume_with_answers(orch, websocket, user_id: str, roles: Any, draft_id: str,
                              fields: Dict[str, str], refresh=None) -> Tuple[bool, str]:
    """Save the owner's Clarify answers through the hard gate and resume."""
    run = get_run(user_id, draft_id)
    if run is None or run.state != NEEDS_ANSWERS:
        return False, "That agent is not waiting for answers."
    advanced, _phase, message = await asyncio.to_thread(
        aa.advance, orch, user_id, draft_id, dict(fields or {}))
    if not advanced:
        run.message = message
        run.note(message)
        return False, message
    run.steps["clarify"] = "done"
    run.questions = []
    run.state = RUNNING
    run.message = "Answers saved — continuing."
    run.note("Clarify answered.")
    run.socket_id = id(websocket)
    _launch(orch, websocket, user_id, roles, run, refresh, resume_from="plan")
    return True, run.message


def _launch(orch, websocket, user_id, roles, run: QuickRun, refresh, resume_from: str = "specify") -> None:
    from orchestrator.detached_context import detached_context
    run.task = asyncio.create_task(
        _pipeline(orch, websocket, user_id, roles, run, refresh, resume_from),
        context=detached_context(orch),
        name=f"quick-create-{run.draft_id}")


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

async def _pipeline(orch, websocket, user_id, roles, run: QuickRun, refresh, resume_from: str) -> None:
    async def push() -> None:
        if refresh is None:
            return
        try:
            await refresh(orch, websocket, user_id, roles, run)
        except Exception:  # noqa: BLE001 — progress is best-effort
            logger.debug("quick-create: progress push failed", exc_info=True)

    async def fail(step: str, message: str, **outcome: Any) -> None:
        run.steps[step] = "failed"
        run.state = FAILED
        run.current = step
        run.message = message
        run.outcome = dict(outcome, step=step)
        run.note(f"{STEP_LABELS[step]}: {message}")
        await push()

    try:
        steps = list(STEPS[STEPS.index(resume_from):])
        for step in steps:
            run.current = step
            run.steps[step] = "running"
            await push()

            if step in ("specify", "clarify", "plan", "tasks"):
                row = await asyncio.to_thread(aa.get_session, orch, user_id, run.draft_id)
                if row is None:
                    await fail(step, "The authoring session disappeared.")
                    return
                if aa.phase_of(row) != step:
                    # The step editor moved this session meanwhile: stop quietly,
                    # the session view is the truth.
                    await fail(step, "This agent is being edited step by step — continue there.")
                    return
                drafted, message = await aa.draft_phase(orch, websocket, user_id, run.draft_id)
                if not drafted:
                    await fail(step, message + " Open the step editor to write it yourself.")
                    return
                run.note(message)
                if step == "clarify":
                    row = await asyncio.to_thread(aa.get_session, orch, user_id, run.draft_id)
                    questions = aa.clarify_items(row or {})
                    if questions:
                        run.questions = questions
                        run.steps[step] = "waiting"
                        run.state = NEEDS_ANSWERS
                        run.message = (f"{len(questions)} question(s) to answer before this "
                                       "can be built.")
                        run.note(run.message)
                        await push()
                        return
                advanced, _phase, message = await asyncio.to_thread(
                    aa.advance, orch, user_id, run.draft_id, None)
                if not advanced:
                    await fail(step, message)
                    return
                run.steps[step] = "done"
                continue

            if step == "analyze":
                result = await asyncio.to_thread(aa.run_analyze, orch, user_id, run.draft_id)
                status = result.get("status")
                if status == "passed":
                    run.steps[step] = "done"
                    run.note("The agent rules passed.")
                    continue
                if status == "analyze_failed":
                    await fail(step, "The agent rules refused this design — nothing was generated.",
                               violations=list(result.get("violations") or []))
                    return
                await fail(step, f"Analyze could not run ({status}).")
                return

            if step == "generate":
                result = await aa.generate_from_session(orch, user_id, run.draft_id,
                                                        websocket=websocket)
                status = str(result.get("status") or "")
                run.agent_id = str(result.get("agent_id") or run.agent_id)
                if status == "delivered":
                    run.steps["generate"] = "done"
                    run.steps["deliver"] = "done"
                    run.current = "deliver"
                    run.state = DONE
                    run.message = "Delivered — your desktop client is starting it."
                    run.outcome = dict(result)
                    run.note(run.message)
                    await push()
                    return
                if status in ("no_host", "delivery_pending"):
                    run.steps["generate"] = "done"
                    run.steps["deliver"] = "waiting"
                    run.current = "deliver"
                    run.state = WAITING_FOR_DESKTOP
                    run.message = ("The agent is built and verified. It will be sent as soon as "
                                   "your desktop client is connected.")
                    run.outcome = dict(result)
                    run.note(run.message)
                    await push()
                    return
                reasons = {
                    "gate_blocked": result.get("reason") or "Analyze has not passed for this agent.",
                    "analyze_failed": "The agent rules refused this design — nothing was generated.",
                    "generation_failed": f"Code generation failed: {result.get('error') or 'unknown error'}",
                    "delivery_failed": ("The verified bundle could not be activated on your desktop. "
                                        "Start a new revision from the agent list."),
                    "conflict": "This agent changed elsewhere — open it in the step editor.",
                    "disabled": "Personal agents are not enabled on this deployment.",
                }
                await fail("deliver" if status == "delivery_failed" else "generate",
                           reasons.get(status, f"Generate stopped ({status or 'unknown'})."),
                           **{k: v for k, v in result.items() if k != "status"})
                return
    except asyncio.CancelledError:
        run.state = FAILED
        run.message = "Cancelled."
        raise
    except Exception as exc:  # noqa: BLE001 — never a silent hang
        logger.exception("quick-create: pipeline crashed for %s", run.draft_id)
        await fail(run.current, f"Something went wrong: {exc}")
