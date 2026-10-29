"""Selected client construction uses the captured encrypted USER row and approved guarded transport.
These isolated helper checks send no provider request and leave production facts unavailable.
"""

from types import SimpleNamespace

from llm_config import research_profile as profile
from llm_config.tests.test_local_profile_088 import binding as binding, seed
from orchestrator.harness.execution_binding import ModelHarnessBinding
from orchestrator.harness.production_profiles import public_profiles


def test_bound_client_uses_exact_local_row_and_optional_context_capture(store, binding, monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    captured = seed(store)
    selected = profile.select_config(captured, store=store, binding_key=binding, profile=profile.LOCAL_PROFILE)
    value = ModelHarnessBinding(SimpleNamespace(config_store=store), None, None, None,
                                 "alice", None, selected, binding, None, None)
    for evidence_bound in [False, True]:
        client, source, resolved = value.client(evidence_bound=evidence_bound)
        try:
            assert resolved.base_url == selected.profile.base_url and resolved.model == selected.profile.model
            assert source.value == "user" and client.timeout == 120
            assert getattr(client, "_evidence_provider_capture", None) == (captured if evidence_bound else None)
        finally:
            client.close()
    assert public_profiles()[0]["available"] is False
