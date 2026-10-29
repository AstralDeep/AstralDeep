"""Private preparation binds typed selection, exact config and key before normal admission.
Resolver doubles exercise lifecycle invariants only; canonical production qualification remains empty.
"""

from types import SimpleNamespace

import pytest

from astralplane.repositories import RepositoryConflictError
from astralplane.repositories.harness_capture import HarnessSelectionRecord
from llm_config.tests.test_harness_profile_binding import fact_for
from llm_config.tests.test_local_profile_088 import binding as binding, seed
from orchestrator.harness import selection_service as module
from orchestrator.harness.production_profiles import HarnessSelectionError, public_profiles
from tests.test_harness_packaged_artifacts import manifest_fact


@pytest.fixture
def preparation(store, binding, monkeypatch):
    captured = seed(store)
    details = fact_for(store, binding, captured)
    manifest = manifest_fact()
    fact = SimpleNamespace(**details.__dict__, identity=manifest.identity,
                            qualification_digest="a" * 64)
    selected = HarnessSelectionRecord("alice", 1, fact.identity, fact.qualification_digest)
    class Preference:
        selection = selected
        def get(self, query, *, owner_id):
            return self.selection
        def assert_current(self, tx, *, owner_id, expected):
            if self.selection != expected:
                raise RepositoryConflictError("changed")
    preference = Preference()
    runtime = store._repository.plane_runtime
    runtime.repositories.preferences = SimpleNamespace(harness=preference)
    configs = runtime.repositories.encrypted_llm_config
    configs.get_user_for_update = lambda tx, owner_id: configs.get_user(tx, owner_id=owner_id)
    service = module.HarnessSelectionService(config_store=store)
    monkeypatch.setattr(service, "enabled", lambda: True)
    monkeypatch.setattr(module, "deployment_for", lambda _: fact)
    monkeypatch.setattr(module, "deployment_for_capture", lambda _: fact)
    return service, preference, fact


def test_exact_private_preparation_and_recheck_do_not_qualify_a_deployment(preparation, store):
    service, preference, fact = preparation
    choice = service.choice_sync("alice")
    admitted = service.prepare_choice_sync(choice)
    assert admitted.capture.identity == fact.identity and admitted.capture.owner_id == "alice"
    assert admitted.capture.config_revision == admitted.config.revision
    with service.runtime.transaction() as tx:
        service.assert_current(tx, admitted)
        service.assert_captured_current(tx, admitted.capture)
    assert public_profiles()[0]["available"] is False
    preference.selection = HarnessSelectionRecord("alice", 2, None, None)
    with service.runtime.transaction() as tx:
        service.assert_captured_current(tx, admitted.capture)
        with pytest.raises(HarnessSelectionError, match="harness_selection_conflict"):
            service.assert_current(tx, admitted)


@pytest.mark.parametrize("change", ["flag", "config", "key", "choice", "repository", "owner"])
def test_changed_private_prerequisite_denies_before_normal_admission(preparation, store, monkeypatch, change):
    service, preference, fact = preparation
    choice = service.choice_sync("alice")
    admitted = service.prepare_choice_sync(choice)
    if change == "flag":
        monkeypatch.setattr(service, "enabled", lambda: False)
    elif change == "config":
        seed(store, base_url="http://127.0.0.1:12345/v1")
    elif change == "key":
        monkeypatch.setenv("AUDIT_HMAC_SECRET", "synthetic-replaced-binding-" + "x" * 40)
    elif change == "choice":
        admitted = object()
    elif change == "repository":
        service.runtime.repositories.preferences.harness = object()
    else:
        admitted = module.HarnessAdmission(HarnessSelectionRecord("other", 1, None, None))
        preference.selection = HarnessSelectionRecord("alice", 1, None, None)
    with pytest.raises(HarnessSelectionError) as failure:
        with service.runtime.transaction() as tx:
            service.assert_current(tx, admitted)
    if change == "flag":
        assert failure.value.code == "harness_selection_disabled" and failure.value.status_code == 409
    assert public_profiles()[0]["available"] is False
