"""Connection admission captures only the currently registered host owner and ordinary work identity.
The durable coordinator checks this private origin again before committing the normal admission.
"""

from dataclasses import dataclass
from uuid import UUID

from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService


@dataclass(frozen=True, slots=True, repr=False)
class ConnectionOrigin:
    orchestrator: object
    context: object
    frame: object
    claims: object
    owner_id: str
    generation: UUID

    def assert_current(self):
        orch, context = self.orchestrator, self.context
        original = self.frame.ingress_registration
        if (type(original) is not tuple or len(original) != 5
                or original[0] is not orch or original[1] is not context
                or original[2] is not context.websocket or original[3] != self.generation
                or original[4] is not self.claims):
            raise HarnessSelectionError("harness_owner_invalid", 403)
        if (context.closing or not context.registered
                or getattr(orch, "_connection_contexts", {}).get(id(context.websocket)) is not context
                or getattr(orch, "ui_sessions", {}).get(context.websocket) is not self.claims
                or self.claims.get("sub") != self.owner_id
                or self.frame.authenticated_user_id != self.owner_id
                or context.connection_generation != self.generation
                or type(self.generation) is not UUID):
            raise HarnessSelectionError("harness_owner_invalid", 403)


def submit_connection(orchestrator, context, frame, request):
    coordinator = orchestrator.work_admission
    if frame.read_only or frame.operation_kind == "llm_credential_save" or frame.foreground:
        return coordinator.submit(request)
    service = getattr(orchestrator, "harness_selection", None)
    if type(service) is not HarnessSelectionService:
        raise HarnessSelectionError("harness_component_unavailable")
    claims = getattr(orchestrator, "ui_sessions", {}).get(context.websocket)
    owner = frame.authenticated_user_id
    if type(claims) is not dict or not owner:
        raise HarnessSelectionError("harness_owner_invalid", 403)
    origin = ConnectionOrigin(orchestrator, context, frame, claims, owner,
                              context.connection_generation)
    origin.assert_current()
    result = coordinator.submit_with_harness(request, service=service,
                                            origin=origin, owner_id=owner)
    origin.assert_current()
    return result
