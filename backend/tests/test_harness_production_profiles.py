"""Production profile resolution accepts only packaged facts and preserves the legacy Work contract.
In-memory conformance reports and provider URLs cannot authorize selected execution.
"""

from types import SimpleNamespace

import pytest

from llm_config import research_profile as profile
from orchestrator.harness.production_profiles import (
    HarnessSelectionError, deployment_for, profile_for_record, public_profiles,
)


def test_unselected_work_retains_exact_declared_default():
    assert profile_for_record(SimpleNamespace()) is profile.OPENAI_PROFILE
    assert profile_for_record(SimpleNamespace(harness_capture=None)) is profile.OPENAI_PROFILE


def test_local_profile_is_unavailable_without_canonical_packaged_evidence():
    with pytest.raises(HarnessSelectionError, match="harness_qualification_missing"):
        deployment_for("standard_local")
    values = public_profiles()
    assert values == [{"harness_id": "standard_local", "available": False,
        "reason": "harness_qualification_missing", "context_tokens": 32768,
        "output_tokens": 1024, "reserved_tokens": 33792, "reserved_milliseconds": 120000}]


@pytest.mark.parametrize("identifier", [None, True, "", "../local", "other", "local\n"])
def test_caller_profile_names_never_create_deployment_facts(identifier):
    with pytest.raises(HarnessSelectionError):
        deployment_for(identifier)


def test_arbitrary_capture_does_not_select_from_endpoint_or_profile_name():
    record = SimpleNamespace(harness_capture={"profile": "standard_local",
        "qualified": True, "base_url": "http://127.0.0.1:1234/v1"})
    with pytest.raises(HarnessSelectionError):
        profile_for_record(record)


def test_work_route_accepts_record_without_changing_legacy_wire_shape():
    from persistent_agents.research_input import route
    expected = {"kind": "model", "provider": "openai", "model": profile.MODEL,
        "max_output_tokens": 1024, "response_format": {"type": "json_object"}}
    assert route(SimpleNamespace(harness_capture=None)) == expected
    assert route() == expected


def test_saved_private_capture_profile_decoding_never_infers_local_authority():
    from orchestrator.harness import production_profiles as module
    assert module.profile_for_capture_value(None) is profile.OPENAI_PROFILE
    with pytest.raises(HarnessSelectionError):
        module.profile_for_capture_value({"profile": "standard_local", "qualified": True})
