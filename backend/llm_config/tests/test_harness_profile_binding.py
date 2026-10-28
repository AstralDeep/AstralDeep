"""Private config selection binds a deployment observation to the exact owner row and HMAC key.
These helper tests never register deployment facts or enable production selection.
"""

from types import SimpleNamespace

import pytest

from audit.pii import PrivateBindingKey
from llm_config import research_profile as profile
from llm_config.tests.test_local_profile_088 import binding as binding, seed, LOCAL_MODEL
from orchestrator.harness.production_profiles import HarnessSelectionError, public_profiles
from orchestrator.harness.selection_service import _selected_config


def fact_for(store, binding, capture):
    selected = profile.select_config(capture, store=store, binding_key=binding,
                                     profile=profile.LOCAL_PROFILE)
    return SimpleNamespace(profile=profile.LOCAL_PROFILE, provider="ollama", model=LOCAL_MODEL,
        deployment_digest="d" * 64, config_binding_key_id=binding.key_id,
        config_revision=selected.revision)


def test_exact_owner_configuration_helper_does_not_qualify_a_deployment(store, binding):
    captured = seed(store)
    fact = fact_for(store, binding, captured)
    selected = _selected_config(fact, captured, store, binding)
    assert selected.revision == fact.config_revision
    assert selected.profile.reservation() == {"model_calls": 1, "tokens": 33792, "elapsed_ms": 120000}
    assert public_profiles()[0]["available"] is False


def test_same_model_label_on_another_local_endpoint_is_not_the_observed_deployment(store, binding):
    captured = seed(store)
    fact = fact_for(store, binding, captured)
    different = seed(store, base_url="http://127.0.0.1:12345/v1")
    with pytest.raises(HarnessSelectionError, match="harness_config_changed"):
        _selected_config(fact, different, store, binding)


def test_changed_private_binding_key_or_row_never_inherits_observation(store, binding):
    captured = seed(store)
    fact = fact_for(store, binding, captured)
    changed_key = PrivateBindingKey("different", b"different-synthetic-binding-key")
    with pytest.raises(HarnessSelectionError, match="harness_config_changed"):
        _selected_config(fact, captured, store, changed_key)
    replaced = seed(store)
    with pytest.raises(HarnessSelectionError, match="harness_config_changed"):
        _selected_config(fact, replaced, store, binding)
