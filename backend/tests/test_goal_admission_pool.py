"""Real PostgreSQL admission tests with one available Plane connection.
They exercise owner-locked selection and credential capture inside the admission transaction.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from audit.pii import private_binding_key
from llm_config import research_profile
from llm_config.user_store import UserLLMConfigStore
from orchestrator.harness import production_profiles, selection_service
from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.selection_service import HarnessSelectionService
from orchestrator.work_admission import OperationOwner, OwnerScope, WorkAdmissionCoordinator
from tests.test_harness_admission_postgres import request
from tests.test_harness_packaged_artifacts import manifest_fact
from tests.helpers.voice_plane_runtime import isolated_plane_runtime


@pytest.fixture
def one_connection_plane():
    with isolated_plane_runtime("goal_admission_pool") as plane:
        plane._driver_pool.maxconn = 1
        yield plane


def _user_request():
    return replace(request(), owner=OperationOwner(OwnerScope.USER, "owner-a", None),
                   idempotency_namespace="goal_admission_pool",
                   idempotency_key=str(uuid4()), normalized_input_digest="c" * 64)


def test_unselected_admission_uses_the_locked_transaction(one_connection_plane):
    service = HarnessSelectionService(plane_runtime=one_connection_plane)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=one_connection_plane)

    admitted = coordinator.submit_with_harness(_user_request(), service=service, owner_id="owner-a")

    assert admitted.accepted


@pytest.fixture
def selected_setup(one_connection_plane, monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("AUDIT_HMAC_KEY_ID", "synthetic_capture")
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-capture-private-binding-" + "x" * 40)
    store = UserLLMConfigStore(plane_runtime=one_connection_plane, data_dir=str(tmp_path))
    store.set_sync("owner-a", provider="ollama", base_url="http://127.0.0.1:11434/v1",
                   model="synthetic-model", api_key="")
    key = private_binding_key()
    config = research_profile.select_config(store.capture_user_sync("owner-a"), store=store,
        binding_key=key, profile=research_profile.LOCAL_PROFILE)
    fact = replace(manifest_fact(), config_binding_key_id=key.key_id,
                   config_revision=config.revision)
    assert production_profiles._verify_fact(fact) == fact

    def resolve(identifier):
        assert identifier == "standard_local"
        return fact

    monkeypatch.setattr(production_profiles, "deployment_for", resolve)
    monkeypatch.setattr(selection_service, "deployment_for", resolve)
    service = HarnessSelectionService(config_store=store)
    monkeypatch.setattr(service, "enabled", lambda: True)
    with one_connection_plane.transaction() as tx:
        selected = one_connection_plane.repositories.preferences.harness.compare_and_set(
            tx, owner_id="owner-a", expected_revision=0, identity=fact.identity,
            qualification_digest=fact.qualification_digest)
    coordinator = WorkAdmissionCoordinator.from_plane(plane_runtime=one_connection_plane)
    return service, coordinator, store, selected


def test_selected_capture_and_replay_avoid_nested_checkout(selected_setup, one_connection_plane,
                                                            monkeypatch):
    service, coordinator, store, selected = selected_setup
    command = _user_request()
    original = coordinator.submit_with_harness(command, service=service, owner_id="owner-a")
    assert original.accepted
    from astralplane.repositories.work_admission import OperationOwner as PlaneOwner, OwnerScope as PlaneScope

    with one_connection_plane.transaction() as tx:
        captured = one_connection_plane.repositories.work_admission.get_harness_capture(
            tx, PlaneOwner(PlaneScope.USER, "owner-a", None), original.operation_id)
        assert captured is not None and captured.selection_revision == selected.revision
        service.assert_captured_current(tx, captured)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("replay must not capture current selection or credentials")

    monkeypatch.setattr(store, "capture_user_sync", forbidden)
    with one_connection_plane.transaction() as tx:
        one_connection_plane.repositories.preferences.harness.compare_and_set(
            tx, owner_id="owner-a", expected_revision=selected.revision,
            identity=None, qualification_digest=None)
    replay = coordinator.submit_with_harness(command, service=service, owner_id="owner-a")
    assert replay.operation_id == original.operation_id
    with one_connection_plane.transaction() as tx:
        assert one_connection_plane.repositories.work_admission.get_harness_capture(
            tx, PlaneOwner(PlaneScope.USER, "owner-a", None), original.operation_id) == captured


def test_selection_change_denies_new_admission_without_side_effects(selected_setup,
                                                                    one_connection_plane):
    service, coordinator, _, selected = selected_setup
    prepared = service.prepare_sync("owner-a")
    with one_connection_plane.transaction() as tx:
        one_connection_plane.repositories.preferences.harness.compare_and_set(
            tx, owner_id="owner-a", expected_revision=selected.revision,
            identity=None, qualification_digest=None)
    command = _user_request()

    with pytest.raises(HarnessSelectionError, match="harness_selection_conflict"):
        coordinator.submit_with_harness(command, service=service, prepared=prepared,
                                        owner_id="owner-a")

    with one_connection_plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0


def test_origin_change_during_capture_rolls_back_admission(selected_setup, one_connection_plane,
                                                            monkeypatch):
    from types import SimpleNamespace
    from orchestrator.harness.host_admission import ConnectionOrigin

    service, coordinator, _, _ = selected_setup
    websocket = object()
    context = SimpleNamespace(websocket=websocket, connection_generation=uuid4(),
                              closing=False, registered=True)
    claims = {"sub": "owner-a"}
    orchestrator = SimpleNamespace(_connection_contexts={id(websocket): context},
                                   ui_sessions={websocket: claims})
    frame = SimpleNamespace(ingress_registration=(orchestrator, context, websocket,
        context.connection_generation, claims), authenticated_user_id="owner-a")
    origin = ConnectionOrigin(orchestrator, context, frame, claims, "owner-a",
                              context.connection_generation)
    original_prepare = service.prepare_sync

    def change_owner(owner_id, *, transaction=None):
        prepared = original_prepare(owner_id, transaction=transaction)
        claims["sub"] = "other-owner"
        return prepared

    monkeypatch.setattr(service, "prepare_sync", change_owner)
    with pytest.raises(HarnessSelectionError, match="harness_owner_invalid"):
        coordinator.submit_with_harness(_user_request(), service=service,
            owner_id="owner-a", origin=origin)

    with one_connection_plane.transaction() as tx:
        assert tx.fetch_one("SELECT count(*) AS n FROM operation_record")["n"] == 0
