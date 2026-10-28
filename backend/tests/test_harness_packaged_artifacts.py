"""Packaged manifest validation binds exact source bytes while leaving production qualification empty.
Synthetic records exercise schema and tampering denials without enabling any deployment or provider.
"""

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from orchestrator.harness import production_profiles as module


def manifest_fact():
    entries = tuple((name, hashlib.sha256(module._implementation_path(name).read_bytes()).hexdigest())
                    for name in sorted(module._REQUIRED_IMPLEMENTATION))
    digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
    return module._PackagedFact(HarnessProfileIdentity("standard_local", "1.0.0", digest),
        "a" * 64, "b" * 64, "synthetic_binding", "c" * 64, "ollama", "synthetic-model", entries,
        module.StandardLLMHarness().identity)


def record(fact):
    return {"identity": {"profile": fact.identity.profile, "version": fact.identity.version,
                        "artifact_digest": fact.identity.artifact_digest},
        "qualification_digest": fact.qualification_digest, "deployment_digest": fact.deployment_digest,
        "config_binding_key_id": fact.config_binding_key_id, "config_revision": fact.config_revision,
        "provider": fact.provider, "model": fact.model, "implementation": fact.implementation,
        "adapter_identity": asdict(fact.adapter_identity)}


def test_manifest_helper_verifies_exact_bytes_without_qualifying_synthetic_evidence():
    fact = manifest_fact()
    assert module._verify_fact(fact) == fact
    assert module._fact_from_record(record(fact)) == fact
    assert module.public_profiles()[0]["available"] is False


@pytest.mark.parametrize("changes", [
    {"qualification_digest": "invalid"}, {"deployment_digest": "invalid"},
    {"config_revision": "invalid"}, {"config_binding_key_id": "raw\n"},
    {"provider": "openai"}, {"model": ""}, {"model": "other\n"},
    {"implementation": ()}, {"implementation": (("../outside", "a" * 64),)},
])
def test_manifest_refuses_unbounded_identity_or_untrusted_paths(changes):
    with pytest.raises(module.HarnessSelectionError, match="harness_artifact_unavailable"):
        module._verify_fact(replace(manifest_fact(), **changes))


def test_manifest_refuses_missing_duplicate_wrong_digest_and_replaced_package_identity():
    fact = manifest_fact()
    entries = fact.implementation
    variants = [replace(fact, implementation=entries[:-1]),
                replace(fact, implementation=entries + (entries[0],)),
                replace(fact, implementation=((entries[0][0], "d" * 64), *entries[1:])),
                replace(fact, identity=replace(fact.identity, artifact_digest="f" * 64))]
    for value in variants:
        with pytest.raises(module.HarnessSelectionError, match="harness_artifact_unavailable"):
            module._verify_fact(value)


@pytest.mark.parametrize("change", ["extra", "missing", "identity", "implementation"])
def test_packaged_record_decoder_rejects_unknown_missing_and_malformed_fields(change):
    value = record(manifest_fact())
    if change == "extra":
        value["qualified"] = True
    elif change == "missing":
        value.pop("deployment_digest")
    elif change == "identity":
        value["identity"] = {"profile": "standard_local", "artifact_digest": "a" * 64}
    else:
        value["implementation"] = None
    with pytest.raises(module.HarnessSelectionError):
        module._fact_from_record(value)


def test_deployment_profile_maps_to_exact_distinct_standard_adapter_identity():
    from dataclasses import asdict
    from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
    value = record(manifest_fact())
    value["adapter_identity"] = asdict(StandardLLMHarness().identity)
    captured = module._fact_from_record(value)
    assert module._verify_fact(captured) == captured
    assert captured.identity.profile == "standard_local"
    assert captured.adapter_identity.harness_id == "standard_openai_compatible"
    assert captured.adapter_identity.profile == "standard"
    assert module.public_profiles()[0]["available"] is False


@pytest.mark.parametrize("change", ["missing", "extra", "wrong", "malformed", "relabel"])
def test_packaged_record_cannot_relabel_or_omit_adapter_execution_identity(change):
    value = record(manifest_fact())
    if change == "missing":
        value.pop("adapter_identity")
    elif change == "extra":
        value["adapter_identity"]["qualified"] = True
    elif change == "wrong":
        value["adapter_identity"]["adapter_version"] = "9.0.0"
    elif change == "malformed":
        value["adapter_identity"]["contract_version"] = True
    else:
        value["adapter_identity"]["profile"] = "standard_local"
    with pytest.raises(module.HarnessSelectionError, match="harness_artifact_unavailable"):
        module._verify_fact(module._fact_from_record(value))
