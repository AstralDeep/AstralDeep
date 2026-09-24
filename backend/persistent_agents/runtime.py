"""Composes the qualified research handler, chat handler and remote-approval bridge into
one AssignmentRunner/AssignmentService pair; called once by orchestrator.py after
Plane startup, adding no independent scheduler.
"""

from persistent_agents.approvals import AssignmentApprovalBridge
from persistent_agents.chat_episode import run_chat_episode
from persistent_agents.research_episode import run_research_episode
from persistent_agents.runner import AssignmentRunner, OneShotLifecycle
from persistent_agents.service import AssignmentService


def start_assignment_runtime(orchestrator):
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
    # Service is fully built before this is visible to requests
    orchestrator.persistent_assignments = service
    orchestrator.persistent_assignment_runner = runner
    return runner
