"""Public assignment projections omit private admission capture for selected and default work.
The owner preference API separately exposes only its bounded public selection state.
"""

import json

from persistent_agents.service import public_record


def record(capture=None):
    return {"owner_id": "private-owner", "definition": {"offline_grant_id": "grant", "limits": {}},
            "checkpoint": {}, "harness_capture": capture}


def test_public_assignment_omits_private_capture_and_preserves_default_shape():
    baseline = record()
    baseline.pop("harness_capture")
    assert public_record(record()) == public_record(baseline)
    private = {"version": 1, "owner_id": "private-owner", "selection_revision": 2,
        "identity": {"profile": "standard_local", "version": "1.0.0", "artifact_digest": "a" * 64},
        "qualification_digest": "b" * 64, "config_binding_key_id": "private_binding",
        "config_revision": "c" * 64, "research_profile": "local-page-selection/v1"}
    projected = public_record(record(private))
    assert "harness_capture" not in projected
    text = json.dumps(projected)
    assert "config_revision" not in text and "private_binding" not in text
    assert "private-owner" not in text
