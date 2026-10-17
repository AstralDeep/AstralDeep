"""Composes framework credential and Work facades from the application's existing Plane services.
Orchestrator startup mounts them after assignment composition and shutdown detaches their borrowed references.
"""

from orchestrator.framework_credentials import FrameworkCredentialService
from orchestrator.plane_repository_context import plane_source_from_orchestrator
from orchestrator.work_operations import FrameworkWorkOperations
from persistent_agents.service import AssignmentService
from shared.feature_flags import flags


def start_framework_runtime(orchestrator):
    if (getattr(orchestrator, "framework_credentials", None) is not None
            or getattr(orchestrator, "framework_work_operations", None) is not None):
        raise RuntimeError("framework runtime already initialized")
    credentials = operations = None
    if flags.is_enabled("framework_credentials"):
        source = plane_source_from_orchestrator(orchestrator)
        credentials = FrameworkCredentialService(
            plane_runtime=source.plane_runtime,
            plane_repositories=source.plane_repositories,
            sessions=source.plane_repositories.history.sessions,
            audit=orchestrator.audit_repo,
        )
        assignments = getattr(orchestrator, "persistent_assignments", None)
        if assignments is not None:
            if (not isinstance(assignments, AssignmentService)
                    or assignments.orch is not orchestrator
                    or assignments.store.plane_runtime is not source.plane_runtime
                    or assignments.store.repository is not source.plane_repositories.assignments):
                raise RuntimeError("framework assignment composition unavailable")
            operations = FrameworkWorkOperations(assignments=assignments, credentials=credentials)
    orchestrator.framework_credentials = credentials
    orchestrator.framework_work_operations = operations
    return credentials


def stop_framework_runtime(orchestrator):
    orchestrator.framework_work_operations = None
    orchestrator.framework_credentials = None
