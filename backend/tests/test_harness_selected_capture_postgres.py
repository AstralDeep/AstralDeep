"""Synthetic resolver facts exercise selected capture mechanics through real Plane and encrypted USER config.
They never publish production qualification, contact a provider or change the empty deployment registry.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from astralplane.repositories.work_admission import OperationOwner as PlaneOwner, OwnerScope as PlaneScope

from audit.pii import private_binding_key
from llm_config import research_profile
from llm_config.user_store import UserLLMConfigStore
from orchestrator.harness import production_profiles, selection_service
from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService, select_captured_config
from orchestrator.work_admission import OperationOwner, OwnerScope, WorkAdmissionCoordinator
from persistent_agents.tests.test_engine_postgres import plane as plane
from tests.test_harness_admission_postgres import request
from tests.test_harness_packaged_artifacts import manifest_fact

pytestmark = pytest.mark.asyncio


@pytest.fixture
def captured(plane, monkeypatch):
    monkeypatch.setenv('CREDENTIAL_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('AUDIT_HMAC_KEY_ID', 'synthetic_capture')
    monkeypatch.setenv('AUDIT_HMAC_SECRET', 'synthetic-capture-private-binding-' + 'x' * 40)
    store = UserLLMConfigStore(plane_runtime=plane)
    store.set_sync('owner-a', provider='ollama', base_url='http://127.0.0.1:11434/v1',
                   model='synthetic-model', api_key='')
    key = private_binding_key()
    config = research_profile.select_config(store.capture_user_sync('owner-a'), store=store,
        binding_key=key, profile=research_profile.LOCAL_PROFILE)
    fact = replace(manifest_fact(), config_binding_key_id=key.key_id, config_revision=config.revision)
    assert production_profiles._verify_fact(fact) == fact
    assert production_profiles.QUALIFIED_DEPLOYMENTS == ()
    def resolve(identifier):
        assert identifier == 'standard_local'
        return fact
    monkeypatch.setattr(production_profiles, 'deployment_for', resolve)
    monkeypatch.setattr(selection_service, 'deployment_for', resolve)
    service = HarnessSelectionService(config_store=store)
    monkeypatch.setattr(service, 'enabled', lambda: True)
    with plane.transaction() as tx:
        selected = plane.repositories.preferences.harness.compare_and_set(tx, owner_id='owner-a',
            expected_revision=0, identity=fact.identity, qualification_digest=fact.qualification_digest)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=plane)
    return service, coordinator, store, selected, fact


def owned_request():
    return replace(request(), owner=OperationOwner(OwnerScope.USER, 'owner-a', None),
        idempotency_namespace='selected_capture', idempotency_key=str(uuid4()), normalized_input_digest='c' * 64)


async def test_selected_capture_binds_actual_config_and_execution_then_survives_new_work_rollback(captured, plane, monkeypatch):
    service, coordinator, store, selected, fact = captured
    choice = await service.choice('owner-a')
    prepared = await service.prepare_choice(choice)
    assert (await service.prepare('owner-a')).capture == prepared.capture
    command = owned_request()
    accepted = coordinator.submit_with_harness(command, service=service, owner_id='owner-a')
    claimed = coordinator.claim_operation(command.admission_class, accepted.operation_id)
    config, key = await select_captured_config(prepared.capture, store=store, owner_id='owner-a')
    original = coordinator.harness_capture(owner=command.owner, fence=claimed.fence, service=service,
        owner_id='owner-a', expected=prepared.capture, config=config, key=key)
    assert original.identity == fact.identity and original.selection_revision == selected.revision
    monkeypatch.setattr(service, 'enabled', lambda: False)
    with pytest.raises(HarnessSelectionError, match='harness_selection_disabled'):
        coordinator.submit_with_harness(owned_request(), service=service, owner_id='owner-a')
    with plane.transaction() as tx:
        plane.repositories.preferences.harness.compare_and_set(tx, owner_id='owner-a',
            expected_revision=selected.revision, identity=None, qualification_digest=None)
        service.assert_captured_current(tx, original)
    replay = coordinator.submit_with_harness(command, service=service, owner_id='owner-a')
    assert replay.operation_id == accepted.operation_id
    assert coordinator.harness_capture(owner=command.owner, fence=claimed.fence, service=service,
        owner_id='owner-a', expected=original, config=config, key=key) == original
    assert production_profiles.QUALIFIED_DEPLOYMENTS == ()


@pytest.mark.parametrize('change', ['config', 'key', 'owner'])
async def test_selected_capture_never_rebinds_changed_private_execution_prerequisite(captured, plane, monkeypatch, change):
    service, coordinator, store, _, _ = captured
    prepared = await service.prepare('owner-a')
    command = owned_request()
    accepted = coordinator.submit_with_harness(command, service=service, owner_id='owner-a')
    claim = coordinator.claim_operation(command.admission_class, accepted.operation_id)
    owner_id = 'owner-a'
    if change == 'config':
        store.set_sync('owner-a', provider='ollama', base_url='http://127.0.0.1:11435/v1',
                       model='synthetic-model', api_key='')
    elif change == 'key':
        monkeypatch.setenv('AUDIT_HMAC_SECRET', 'synthetic-replaced-binding-' + 'y' * 40)
    else:
        owner_id = 'other-owner'
    with pytest.raises(HarnessSelectionError):
        coordinator.harness_capture(owner=command.owner, fence=claim.fence, service=service,
            owner_id=owner_id, expected=prepared.capture, config=prepared.config, key=prepared.key)
    with pytest.raises(HarnessSelectionError):
        await select_captured_config(prepared.capture, store=store, owner_id=owner_id)
    with plane.transaction() as tx:
        assert plane.repositories.work_admission.get_harness_capture(tx,
            PlaneOwner(PlaneScope.USER, 'owner-a', None),
            accepted.operation_id) == prepared.capture
    assert production_profiles.QUALIFIED_DEPLOYMENTS == ()
