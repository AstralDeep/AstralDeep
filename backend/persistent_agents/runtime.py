"""Compose the qualified research handler into the existing assignment runner.

The persistent-agent feature flag remains owned by orchestrator startup. This
helper adds no worker, authority store, provider client or independent scheduler.
"""

from persistent_agents.approvals import AssignmentApprovalBridge
from persistent_agents.chat_episode import run_chat_episode
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runner import AssignmentRunner, OneShotLifecycle
from persistent_agents.service import AssignmentService


def start_assignment_runtime(orchestrator):
    """Publish one coherent service/runner pair after successful construction."""
    if (getattr(orchestrator, "persistent_assignments", None) is not None
            or getattr(orchestrator, "persistent_assignment_runner", None) is not None):
        raise RuntimeError("assignment runtime already initialized")
    service = AssignmentService(orchestrator)
    try:
        sessions = orchestrator.web_sessions
        runner = AssignmentRunner(orchestrator, service,
            one_shot=OneShotLifecycle(sessions, run_research_episode, chat=run_chat_episode))
        if not runner.fixed_research_ready(service=service, sessions=sessions):
            raise RuntimeError("assignment research composition unavailable")
        service.approval_executor = AssignmentApprovalBridge(runner)
        runner.start()
    except BaseException:
        service.store.close()
        raise
    # start() schedules, but cannot run a task before this synchronous function
    # returns. A request cannot observe half of the constructed pair.
    orchestrator.persistent_assignments = service
    orchestrator.persistent_assignment_runner = runner
    return runner
