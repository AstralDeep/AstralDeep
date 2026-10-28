"""Unit hosts explicitly model only unavailable selection and normal default admission.
PostgreSQL hosts bind the real selection service to their own Plane runtime.
"""

from types import SimpleNamespace

from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.work_admission import InMemoryWorkAdmissionRepository


def default_harness_service():
    runtime = SimpleNamespace(repositories=SimpleNamespace(preferences=SimpleNamespace(harness=None)))
    return HarnessSelectionService(plane_runtime=runtime)


class DefaultHarnessRepository(InMemoryWorkAdmissionRepository):
    @staticmethod
    def _default(service, owner_id, origin=None):
        assert type(service) is HarnessSelectionService
        assert service.repository is None and service.enabled() is False
        assert service.prepare_sync(owner_id) is None
        service.assert_current(None, None)
        service.assert_connection_origin(origin)

    def submit_with_harness(self, request, *, service, owner_id, prepared=None, origin=None, **policy):
        self._default(service, owner_id, origin)
        assert request.harness_capture is request.harness_selection is request.harness_assignment_id is None
        return self.submit(request, **policy)

    def observe_harness_retry(self, *, owner, operation_id, service, owner_id):
        self._default(service, owner_id)
        return None

    def submit_harness_retry(self, request, *, observation, service, owner_id, **policy):
        self._default(service, owner_id)
        assert observation is None
        return self.submit(request, **policy)


def bind_default_model_harness(host):
    service = default_harness_service()
    def captured(*, owner, fence, service, owner_id, expected=None, config=None, key=None):
        DefaultHarnessRepository._default(service, owner_id)
        assert owner.owner_user_id == owner_id and fence is not None
        assert expected is config is key is None
        return None
    host.harness_selection = service
    host.work_admission = SimpleNamespace(harness_capture=captured)
