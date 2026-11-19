"""Starts the owner assignment service and its Plane recovery worker when enabled."""

from persistent_agents.runner import AssignmentRunner
from persistent_agents.service import AssignmentService


def start_assignment_runtime(orchestrator):
    if (getattr(orchestrator, "persistent_assignments", None) is not None
            or getattr(orchestrator, "persistent_assignment_runner", None) is not None):
        raise RuntimeError("assignment runtime already initialized")
    service = AssignmentService(orchestrator)
    try:
        runner = AssignmentRunner(orchestrator, service)
        runner.start()
    except BaseException:
        service.store.close()
        raise
    # Service is fully built before this is visible to requests
    orchestrator.persistent_assignments = service
    orchestrator.persistent_assignment_runner = runner
    return runner
